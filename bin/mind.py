#!/usr/bin/env python3
"""mind — the CLI that drives this personal LLM wiki.

wiki/ is canonical. cache/ is derived. raw/ is immutable inputs.
Agent-driven commands (ingest, query, lint) shell out to config.agent.command.
See AGENTS.md for the contract, CLAUDE.md for Claude-Code notes, llm-wiki.md for the philosophy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from collections import defaultdict, deque
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError:
    sys.stderr.write("mind: pyyaml is required. Run: pip install -r requirements.txt\n")
    sys.exit(1)


# ---------- paths ----------

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
RAW = ROOT / "raw"
CACHE = ROOT / "cache"
CONFIG_PATH = ROOT / "config.yaml"
CONFIG_WIKI_PATH = ROOT / "config.wiki.yaml"
CONFIG_LOCAL_PATH = ROOT / "config.local.yaml"
SEED_WIKI = ROOT / "seed" / "wiki"


def _rel(p: Path) -> str:
    """ROOT-relative path as a forward-slash string.

    All persisted/displayed relative paths go through here so data written on
    Windows stays portable (never backslashed)."""
    return p.relative_to(ROOT).as_posix()


# ---------- config ----------

DEFAULT_CONFIG: dict[str, Any] = {
    "agent": {"command": ["claude", "-p"], "model": "claude-opus-4-7"},
    "preprocessing": {
        "audio": {
            "enabled": True,
            "tool": "whisper-cpp",
            "model": "base.en",
            # Optional: absolute or ROOT-relative path to the ggml model file.
            # If unset, we look for whisper.cpp/models/ggml-<model>.bin under ROOT.
            "model_path": None,
            # Optional: explicit binary path. If unset, we search PATH then
            # common local clone locations (whisper.cpp/build/bin/whisper-cli, etc.).
            "binary": None,
        },
        "pdf": {"enabled": True, "tool": "pypdf"},
        "image": {"enabled": False, "tool": "claude-vision"},
        "video": {"enabled": False, "tool": "ffmpeg+whisper"},
    },
    "ingest": {"default_mode": "interactive", "auto_commit": True},
    "digest": {
        "default_window_days": 7,
        "auto_commit": True,
        "min_cluster_size": 2,
        "open_threads_slug": "open-threads",
    },
    "verify": {
        "default": True,
        "modalities": ["audio", "image", "pdf"],
        "max_inline_lines": 200,
    },
    "query": {"file_answer_default": "prompt"},
    "raw": {"filename_convention": "{date}_{slug}{ext}"},
    "pull": {
        # Per-kind source dirs; set in config.local.yaml (see config.local.example.yaml).
        # e.g. {"docs": "~/.../docs_out", "audio": "~/.../audio_out"}
        "sources": {},
        "staging_dir": "inbox",
        "move": True,
    },
}


def load_config() -> dict[str, Any]:
    # Merge order (later wins): shipped defaults < config.yaml (tracked, generic)
    # < config.wiki.yaml (data-repo-tracked, syncs with the wiki: taxonomy etc.)
    # < config.local.yaml (gitignored, per-machine paths).
    cfg = DEFAULT_CONFIG
    for path in (CONFIG_PATH, CONFIG_WIKI_PATH, CONFIG_LOCAL_PATH):
        if path.exists():
            try:
                with path.open() as f:
                    overlay = yaml.safe_load(f) or {}
            except yaml.YAMLError as e:
                sys.stderr.write(f"mind: invalid YAML in {path.name}: {e}\n")
                sys.exit(1)
            if not isinstance(overlay, dict):
                print(f"mind: ignoring {path.name}: top level must be a YAML mapping",
                      file=sys.stderr)
                continue
            cfg = _deep_merge(cfg, overlay)
    return cfg


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# ---------- markdown page parsing ----------

FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n(.*)$", re.DOTALL)
# [text](target.md), [text](target.md#anchor), but not external [text](https://...)
MD_LINK_RE = re.compile(r"(?<!!)\[([^\]]+)\]\((?!https?://|mailto:|#)([^)\s]+?)(?:#[^)]*)?\)")
# [[target]] or [[target|alias]] — Obsidian-style, tolerated
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|[^\]]*)?(?:#[^\]]*)?\]\]")
# Canonical link text for references to daily pages: "YYYY-MM-DD — <subject>".
# Em dash (U+2014); a plain hyphen doesn't count.
DAILY_LINK_TEXT_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \u2014 .+$")
# Strip fenced code blocks and inline code before link extraction — example link
# syntax in docs shouldn't create phantom graph edges.
FENCED_CODE_RE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


def _strip_code(body: str) -> str:
    body = FENCED_CODE_RE.sub("", body)
    body = INLINE_CODE_RE.sub("", body)
    return body


class Page:
    __slots__ = ("slug", "path", "fm", "body")

    def __init__(self, slug: str, path: Path, fm: dict, body: str):
        self.slug = slug
        self.path = path
        self.fm = fm
        self.body = body

    @property
    def title(self) -> str:
        return self.fm.get("title") or self.slug

    @property
    def tags(self) -> list[str]:
        t = self.fm.get("tags") or []
        return list(t) if isinstance(t, list) else [str(t)]

    @property
    def page_type(self) -> str:
        tags = self.tags
        return tags[0] if tags else "untyped"

    @property
    def sources(self) -> list[str]:
        s = self.fm.get("sources") or []
        return list(s) if isinstance(s, list) else [str(s)]

    @property
    def related(self) -> list[str]:
        r = self.fm.get("related") or []
        return list(r) if isinstance(r, list) else [str(r)]

    @property
    def summary(self) -> str:
        """Frontmatter `summary` if present; else first non-empty non-heading body line.

        Daily pages are required to carry `summary` (see A10). Other page types
        may set it optionally; if absent, fall back to the first real body line.
        """
        fm_sum = self.fm.get("summary")
        if fm_sum:
            return str(fm_sum).strip()[:200]
        for line in self.body.splitlines():
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("<!--"):
                continue
            return s[:200]
        return ""

    def outbound_links(self) -> list[str]:
        """Return slugs this page links to (via body links, both standard md and wikilinks)."""
        body = _strip_code(self.body)
        out: list[str] = []
        seen: set[str] = set()
        for m in MD_LINK_RE.finditer(body):
            target = m.group(2)
            slug = _target_to_slug(target)
            if slug and slug != self.slug and slug not in seen:
                out.append(slug)
                seen.add(slug)
        for m in WIKILINK_RE.finditer(body):
            slug = _target_to_slug(m.group(1))
            if slug and slug != self.slug and slug not in seen:
                out.append(slug)
                seen.add(slug)
        return out

    def wikilink_targets(self) -> list[str]:
        """Return slugs referenced via [[wikilink]] syntax (style violation when writing)."""
        body = _strip_code(self.body)
        out: list[str] = []
        for m in WIKILINK_RE.finditer(body):
            slug = _target_to_slug(m.group(1))
            if slug:
                out.append(slug)
        return out

    def h1_titles(self) -> list[str]:
        """Return all H1 lines in the body (after stripping code blocks)."""
        body = _strip_code(self.body)
        return [line[2:].strip() for line in body.splitlines() if line.startswith("# ")]


def _target_to_slug(target: str) -> str | None:
    """Resolve a link target ('foo.md', 'foo', 'subdir/foo.md') to a slug."""
    t = target.strip()
    if not t:
        return None
    t = t.split("#", 1)[0]
    t = Path(t).name
    if t.endswith(".md"):
        t = t[:-3]
    return t or None


def load_page(path: Path) -> Page | None:
    if not path.is_file() or path.suffix != ".md":
        return None
    raw = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(raw)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = m.group(2)
    else:
        fm = {}
        body = raw
    slug = fm.get("slug") or path.stem
    return Page(slug=slug, path=path, fm=fm, body=body)


def iter_pages() -> Iterable[Page]:
    if not WIKI.is_dir():
        return
    for p in sorted(WIKI.glob("*.md")):
        page = load_page(p)
        if page is not None:
            yield page


# ---------- cache ----------

def rebuild_cache() -> None:
    CACHE.mkdir(exist_ok=True)

    pages = list(iter_pages())

    index = []
    links: dict[str, dict] = {}
    raw_manifest: dict[str, list[str]] = defaultdict(list)

    for p in pages:
        index.append({
            "slug": p.slug,
            "title": p.title,
            "type": p.page_type,
            "tags": p.tags,
            "summary": p.summary,
            "created": _date_str(p.fm.get("created")),
            "updated": _date_str(p.fm.get("updated")),
            "sources": p.sources,
            "related": p.related,
            "path": _rel(p.path),
        })
        links[p.slug] = {"out": p.outbound_links(), "in": []}
        for src in p.sources:
            raw_manifest[src].append(p.slug)

    for slug, payload in links.items():
        for target in payload["out"]:
            if target in links:
                links[target]["in"].append(slug)

    (CACHE / "index.json").write_text(json.dumps(index, indent=2, default=str), encoding="utf-8")
    (CACHE / "links.json").write_text(json.dumps(links, indent=2, default=str), encoding="utf-8")
    (CACHE / "raw-manifest.json").write_text(
        json.dumps(dict(raw_manifest), indent=2, default=str), encoding="utf-8"
    )

    _rebuild_search_db(pages)

    print(f"mind: rebuilt cache for {len(pages)} pages → {CACHE}")


def _rebuild_search_db(pages: list[Page]) -> None:
    db_path = CACHE / "search.db"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    try:
        con.executescript(
            """
            CREATE VIRTUAL TABLE pages USING fts5(
                slug UNINDEXED, title, tags, body,
                tokenize = 'porter unicode61'
            );
            """
        )
        with con:
            con.executemany(
                "INSERT INTO pages(slug, title, tags, body) VALUES (?, ?, ?, ?)",
                [(p.slug, p.title, " ".join(p.tags), p.body) for p in pages],
            )
    finally:
        con.close()


def _date_str(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    return str(v)


def _cache_is_stale() -> bool:
    index_path = CACHE / "index.json"
    if not index_path.exists():
        return True
    cache_mtime = index_path.stat().st_mtime
    wiki_root = ROOT / "wiki"
    if not wiki_root.exists():
        return False
    for md in wiki_root.rglob("*.md"):
        if md.stat().st_mtime > cache_mtime:
            return True
    return False


def _ensure_cache_fresh() -> None:
    if _cache_is_stale():
        print("mind: wiki newer than cache; rebuilding…", file=sys.stderr)
        rebuild_cache()


def _load_cache_json(name: str) -> Any:
    _ensure_cache_fresh()
    return json.loads((CACHE / name).read_text(encoding="utf-8"))


# ---------- subcommands: deterministic ----------

def cmd_index(args: argparse.Namespace) -> int:
    data = _load_cache_json("index.json")

    if args.tag:
        data = [e for e in data if args.tag in (e.get("tags") or [])]
    if args.type:
        data = [e for e in data if e.get("type") == args.type]

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0
    if args.titles_only:
        for e in data:
            print(f"{e['slug']}: {e['title']}")
        return 0
    if args.full:
        for e in data:
            tags = ",".join(e.get("tags") or [])
            print(f"- [{e['title']}]({e['slug']}.md) [{tags}] updated={e.get('updated')} — {e.get('summary') or ''}")
        return 0
    # default: titles + one-line summary, matching wiki/index.md style
    for e in data:
        summary = e.get("summary") or ""
        print(f"- [{e['title']}]({e['slug']}.md) — {summary}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    _ensure_cache_fresh()
    db_path = CACHE / "search.db"
    if not db_path.exists():
        rebuild_cache()
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT slug, title, snippet(pages, 3, '«', '»', '…', 12) "
            "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT ?",
            (args.query, args.limit),
        ).fetchall()
    finally:
        con.close()
    if not rows:
        print(f"mind: no matches for {args.query!r}")
        return 0
    for slug, title, snippet in rows:
        print(f"{slug}: {title}")
        print(f"    {snippet}")
    return 0


def cmd_graph(args: argparse.Namespace) -> int:
    links: dict[str, dict] = _load_cache_json("links.json")

    if args.from_slug:
        if args.from_slug not in links:
            print(f"mind: unknown slug {args.from_slug!r}", file=sys.stderr)
            return 2
        nodes, edges = _bfs(links, args.from_slug, args.depth)
    else:
        nodes = set(links.keys())
        edges = [(src, dst) for src, payload in links.items() for dst in payload.get("out", [])]

    if args.format == "json":
        print(json.dumps(
            {
                "nodes": sorted(nodes),
                "edges": [{"src": s, "dst": d} for s, d in edges],
            },
            indent=2,
        ))
    elif args.format == "dot":
        print("digraph wiki {")
        for n in sorted(nodes):
            print(f'  "{n}";')
        for s, d in edges:
            print(f'  "{s}" -> "{d}";')
        print("}")
    elif args.format == "mermaid":
        print("graph LR")
        for s, d in edges:
            print(f"  {_mermaid_id(s)}[{s}] --> {_mermaid_id(d)}[{d}]")
    return 0


def _bfs(links: dict[str, dict], start: str, depth: int) -> tuple[set[str], list[tuple[str, str]]]:
    nodes: set[str] = {start}
    edges: list[tuple[str, str]] = []
    q: deque[tuple[str, int]] = deque([(start, 0)])
    seen_edge: set[tuple[str, str]] = set()
    while q:
        node, d = q.popleft()
        if d >= depth:
            continue
        payload = links.get(node, {})
        successors = list(payload.get("out", [])) + list(payload.get("in", []))
        for target in successors:
            e = (node, target)
            if e not in seen_edge:
                seen_edge.add(e)
                edges.append(e)
            if target not in nodes:
                nodes.add(target)
                q.append((target, d + 1))
    return nodes, edges


def _mermaid_id(slug: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", slug)


def cmd_rebuild_cache(_: argparse.Namespace) -> int:
    rebuild_cache()
    return 0


DATA_GIT_DIR = ROOT / ".git-data"


def _ensure_exclude_lines(git_dir: Path, lines: list[str]) -> None:
    """Idempotently append per-repo ignore rules to <git_dir>/info/exclude.

    The shipped .gitignore hides wiki/, raw/, and config.wiki.yaml from the
    code repo (so fresh clones are safe by default); the data repo bypasses it
    with `git add -f`. These info/exclude rules are a supplement: they keep
    code files from showing as untracked in the data repo, and act as
    belt-and-suspenders for the code repo."""
    info = git_dir / "info"
    info.mkdir(exist_ok=True)
    exclude = info / "exclude"
    existing = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
    missing = [ln for ln in lines if ln not in existing]
    if missing:
        exclude.write_text("\n".join(existing + missing) + "\n", encoding="utf-8")


def _ensure_data_repo() -> bool:
    """Create/maintain the local-only data repo (.git-data).

    wiki/ and raw/ are personal data. Their history lives in a second git repo
    whose git dir is .git-data and whose worktree is the project root, so the
    main repo (safe to publish) never tracks them. See README
    "Two repos, one directory"."""
    if not DATA_GIT_DIR.exists():
        print("mind: initializing data repo .git-data (local-only; tracks wiki/ + raw/)")
        r = subprocess.run(
            ["git", "--git-dir", str(DATA_GIT_DIR), "init", "--quiet"],
            cwd=str(ROOT), check=False,
        )
        if r.returncode != 0:
            return False
        # `git --git-dir=<dir> init` marks the repo bare when <dir> isn't
        # named .git; un-mark it or core.worktree is rejected as invalid.
        subprocess.run(
            ["git", "--git-dir", str(DATA_GIT_DIR), "config", "core.bare", "false"],
            cwd=str(ROOT), check=False,
        )
        subprocess.run(
            ["git", "--git-dir", str(DATA_GIT_DIR), "config", "core.worktree", ".."],
            cwd=str(ROOT), check=False,
        )
    # Data repo sees only wiki/ + raw/ + config.wiki.yaml; code repo never sees them.
    _ensure_exclude_lines(DATA_GIT_DIR, ["/*", "!/wiki", "!/raw", "!/config.wiki.yaml"])
    if (ROOT / ".git").is_dir():
        _ensure_exclude_lines(ROOT / ".git", ["/wiki/", "/raw/", "/config.wiki.yaml"])
    return True


def _data_add_paths() -> list[str]:
    """Pathspecs `mind commit`/`mind init` force-add into the data repo."""
    paths = ["wiki", "raw"]
    if (ROOT / "config.wiki.yaml").exists():
        paths.append("config.wiki.yaml")
    return paths


def _data_git_add(git: list[str]) -> bool:
    """Force-add the data paths into the data repo, surfacing failures.

    -f bypasses the shipped .gitignore (which hides wiki/, raw/, and
    config.wiki.yaml so the *code* repo can never stage them)."""
    r = subprocess.run(
        git + ["add", "-f", "--", *_data_add_paths(), ":(exclude)**/.DS_Store"],
        cwd=str(ROOT), check=False, capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"mind: git add into data repo failed:\n{r.stderr.strip()}", file=sys.stderr)
        return False
    return True


def cmd_commit(args: argparse.Namespace) -> int:
    cfg = load_config()
    no_lint = getattr(args, "no_lint", False)
    if cfg.get("lint", {}).get("block_commit", True) and not no_lint:
        violations = run_strict_lint(cfg)
        if violations:
            print_lint_report(violations)
            print(
                f"\nmind: refusing to commit — {len(violations)} strict-lint violation(s). "
                f"Fix them, or pass --no-lint to bypass.",
                file=sys.stderr,
            )
            return 1

    if not WIKI.is_dir():
        print("mind: no wiki/ directory — run ./bin/mind init first", file=sys.stderr)
        return 1
    msg = args.message or f"mind: auto-commit {datetime.now().isoformat(timespec='seconds')}"
    if not _ensure_data_repo():
        print("mind: could not initialize data repo .git-data", file=sys.stderr)
        return 1
    git = ["git", "--git-dir", str(DATA_GIT_DIR)]
    if not _data_git_add(git):
        return 1
    result = subprocess.run(git + ["diff", "--cached", "--quiet"], cwd=str(ROOT), check=False)
    if result.returncode == 0:
        print("mind: nothing to commit")
        return 0
    subprocess.run(git + ["commit", "-m", msg], cwd=str(ROOT), check=True)
    return 0


# ---------- subcommand: init ----------

CONFIG_WIKI_STUB = """\
# mind — wiki-scoped config overlay (tracked by the DATA repo, not the code repo)
#
# This file rides along with your wiki: `mind commit` versions it in .git-data
# together with wiki/ and raw/, so taxonomy decisions sync with your data if
# you ever back the data repo up to a *private* remote.
#
# Values here deep-merge over config.yaml (and under config.local.yaml).
# NOTE: lists REPLACE the shipped list wholesale — when extending one, repeat
# the generic entries from config.yaml, then add your own. Example:
#
# lint:
#   meta_exempt_slugs:
#     - index
#     - log
#     - contradictions
#     - open-threads
#     - journal
#     - my-hub-page
#   canonical_tags:
#     - device
#     - interest
#     - orphan-ok
#     - person
#     - place
#     - practice
#     - software
#     - voice
#     - my-new-tag
"""


def cmd_init(args: argparse.Namespace) -> int:
    """Bootstrap a fresh clone: dirs, seed wiki pages, data repo, cache."""
    for d in (WIKI, RAW, CACHE, ROOT / "inbox"):
        d.mkdir(exist_ok=True)
    gitkeep = RAW / ".gitkeep"
    if not gitkeep.exists():
        gitkeep.write_text("", encoding="utf-8")

    today = date.today().isoformat()
    seeded: list[str] = []
    if SEED_WIKI.is_dir():
        for tpl in sorted(SEED_WIKI.glob("*.md")):
            dest = WIKI / tpl.name
            if dest.exists():
                continue  # idempotent: never clobber a page the user already has
            dest.write_text(
                tpl.read_text(encoding="utf-8").replace("{date}", today),
                encoding="utf-8",
            )
            seeded.append(_rel(dest))
    if seeded:
        print(f"mind: seeded {len(seeded)} wiki page(s): {', '.join(seeded)}")
    else:
        print("mind: wiki/ already populated — no seed pages written")

    if not (ROOT / "config.wiki.yaml").exists():
        (ROOT / "config.wiki.yaml").write_text(CONFIG_WIKI_STUB, encoding="utf-8")
        print("mind: wrote config.wiki.yaml stub (data-repo-tracked taxonomy overlay)")

    if not _ensure_data_repo():
        print("mind: could not initialize data repo .git-data", file=sys.stderr)
        return 1
    git = ["git", "--git-dir", str(DATA_GIT_DIR)]
    has_head = subprocess.run(
        git + ["rev-parse", "-q", "--verify", "HEAD"],
        cwd=str(ROOT), check=False, capture_output=True,
    ).returncode == 0
    if not has_head:
        if not _data_git_add(git):
            return 1
        r = subprocess.run(git + ["commit", "-m", "mind init: seed wiki"],
                           cwd=str(ROOT), check=False)
        if r.returncode != 0:
            print("mind: initial data-repo commit failed (is git user.name/email configured?)",
                  file=sys.stderr)
            return 1
        print("mind: created data repo .git-data with initial commit")

    rebuild_cache()
    print(
        "\nmind: ready. Next steps:\n"
        "  ./bin/mind serve                  # browse at http://localhost:8787\n"
        "  ./bin/mind ingest <file>          # feed it something\n"
        "  cp config.local.example.yaml config.local.yaml   # per-machine paths (optional)"
    )
    return 0


# ---------- strict (deterministic) lint ----------

SLUG_RE = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
REQUIRED_FRONTMATTER = ("title", "slug", "created", "updated", "tags", "sources", "related")

# Agent-notes disclosure block — see AGENTS.md "Per-page guidance".
DETAILS_OPEN_RE = re.compile(r"<details\b", re.IGNORECASE)
DETAILS_CLOSE_RE = re.compile(r"</details\s*>", re.IGNORECASE)
SUMMARY_RE = re.compile(r"<summary\s*>([^<]*)</summary\s*>", re.IGNORECASE)
AGENT_NOTES_OPEN_LINE_RE = re.compile(r"^<details\b[^>]*><summary\s*>[^<]*</summary\s*>\s*$")
AGENT_NOTES_CLOSE_LINE_RE = re.compile(r"^</details\s*>\s*$")


def _scan_details_blocks(body: str) -> list[dict]:
    """Return <details>...</details> blocks in document order.

    Each entry: {open_idx, close_idx, open_line, close_line, summary}.
    Summary is the text inside the first <summary> tag within the block, or None.
    """
    lines = body.splitlines()
    blocks: list[dict] = []
    i = 0
    while i < len(lines):
        if DETAILS_OPEN_RE.search(lines[i]):
            open_idx = i
            # Find matching close (non-nested — treat first </details> as the close).
            close_idx = None
            for j in range(open_idx, len(lines)):
                if DETAILS_CLOSE_RE.search(lines[j]):
                    close_idx = j
                    break
            if close_idx is None:
                break  # unclosed — stop scanning
            # Summary text: search opening line through close (inclusive).
            summary: str | None = None
            for j in range(open_idx, close_idx + 1):
                sm = SUMMARY_RE.search(lines[j])
                if sm:
                    summary = sm.group(1)
                    break
            blocks.append({
                "open_idx": open_idx,
                "close_idx": close_idx,
                "open_line": lines[open_idx],
                "close_line": lines[close_idx],
                "summary": summary,
            })
            i = close_idx + 1
        else:
            i += 1
    return blocks


def _check_agent_notes_format(p: "Page") -> list[tuple[str, str, str]]:
    """Validate the Agent-notes <details> block format (presence is not required).

    Canonical form (placed directly after the H1, one blank line between):

        # <Title>

        <details><summary>Agent notes</summary>

        <body>

        </details>
    """
    viols: list[tuple[str, str, str]] = []
    body = _strip_code(p.body)
    lines = body.splitlines()
    blocks = _scan_details_blocks(body)
    if not blocks:
        return viols

    agent_blocks = [
        b for b in blocks
        if b["summary"] is not None and b["summary"].strip().lower() == "agent notes"
    ]
    if not agent_blocks:
        return viols

    # Summary text must match exactly "Agent notes".
    for b in agent_blocks:
        if b["summary"].strip() != "Agent notes":
            viols.append((p.slug, "A9-agent-notes-format",
                          f"<summary> text {b['summary']!r} must be exactly 'Agent notes' (case-sensitive)"))

    # At most one Agent-notes block.
    if len(agent_blocks) > 1:
        viols.append((p.slug, "A9-agent-notes-format",
                      f"{len(agent_blocks)} Agent-notes blocks found; expected at most one"))

    # Agent-notes block, when present, must be the first <details> in the file.
    if blocks[0]["open_idx"] != agent_blocks[0]["open_idx"]:
        viols.append((p.slug, "A9-agent-notes-format",
                      "Agent-notes block must be the first <details> in the file"))

    ab = agent_blocks[0]

    # Opening line must be exactly `<details><summary>Agent notes</summary>` on its own line.
    if not AGENT_NOTES_OPEN_LINE_RE.match(ab["open_line"]):
        viols.append((p.slug, "A9-agent-notes-format",
                      f"opening line must be '<details><summary>Agent notes</summary>' on its own line; got {ab['open_line']!r}"))

    # Closing line must be exactly `</details>` on its own line.
    if not AGENT_NOTES_CLOSE_LINE_RE.match(ab["close_line"]):
        viols.append((p.slug, "A9-agent-notes-format",
                      f"closing line must be '</details>' on its own line; got {ab['close_line']!r}"))

    # Position: directly after H1, with exactly one blank line between.
    h1_idx = next((k for k, ln in enumerate(lines) if ln.startswith("# ")), None)
    if h1_idx is not None:
        expected_open = h1_idx + 2
        blank_between = (h1_idx + 1 < len(lines) and lines[h1_idx + 1].strip() == "")
        if ab["open_idx"] != expected_open or not blank_between:
            viols.append((p.slug, "A9-agent-notes-format",
                          "Agent-notes block must be placed directly after the H1 with exactly one blank line between"))

    return viols


def run_strict_lint(cfg: dict) -> list[tuple[str, str, str]]:
    """Walk wiki/ and return a list of (slug, rule, message) violations.

    Rules are grouped:
      A. Structural — filename/slug, required fields, dates, H1, page type, dangling links,
         duplicate slugs, no-wikilinks, agent-notes format, daily-page summary field,
         daily-link text convention.
      B. Graph symmetry — related ⊆ body links, back-link symmetry. Pages whose slug is in
         lint.meta_exempt_slugs are exempt as link *sources* (their outbound links are not
         required to come back), but they are still valid link *targets*.
      C. Taxonomy — tags[0] in page_types, tags[1:] ⊆ canonical_tags.
    """
    lint_cfg = cfg.get("lint", {})
    page_types = set(lint_cfg.get("page_types") or [])
    canonical_tags = set(lint_cfg.get("canonical_tags") or [])
    meta_exempt = set(lint_cfg.get("meta_exempt_slugs") or [])

    pages = list(iter_pages())
    by_slug: dict[str, list[Page]] = defaultdict(list)
    for p in pages:
        by_slug[p.slug].append(p)
    valid_slugs = set(by_slug.keys())
    daily_slugs = {p.slug for p in pages if p.tags and p.tags[0] == "daily"}

    violations: list[tuple[str, str, str]] = []

    # A7. Duplicate slugs (one entry per duplicated slug)
    for slug, group in sorted(by_slug.items()):
        if len(group) > 1:
            paths = ", ".join(_rel(g.path) for g in group)
            violations.append((slug, "A7-duplicate-slug", f"slug appears in {len(group)} files: {paths}"))

    for p in pages:
        rel_path = _rel(p.path)

        # A1. Filename = <slug>.md
        if p.path.stem != p.slug:
            violations.append((p.slug, "A1-filename-mismatch",
                               f"file is {rel_path} but slug is {p.slug!r}"))
        if not SLUG_RE.match(p.slug):
            violations.append((p.slug, "A1-bad-slug",
                               f"slug {p.slug!r} is not lowercase kebab-case"))

        # A2. Required frontmatter fields
        for field in REQUIRED_FRONTMATTER:
            if field not in p.fm:
                violations.append((p.slug, "A2-missing-field", f"frontmatter missing {field!r}"))

        # A3. Date format + ordering
        created = _date_str(p.fm.get("created"))
        updated = _date_str(p.fm.get("updated"))
        for field, val in (("created", created), ("updated", updated)):
            if val is None:
                continue
            if not ISO_DATE_RE.match(val):
                violations.append((p.slug, "A3-bad-date",
                                   f"{field} {val!r} is not ISO YYYY-MM-DD"))
        if created and updated and ISO_DATE_RE.match(created) and ISO_DATE_RE.match(updated):
            if updated < created:
                violations.append((p.slug, "A3-date-order",
                                   f"updated {updated} is before created {created}"))

        # A4. Exactly one H1, matches title verbatim
        h1s = p.h1_titles()
        if len(h1s) == 0:
            violations.append((p.slug, "A4-missing-h1", "no H1 in body"))
        elif len(h1s) > 1:
            violations.append((p.slug, "A4-multiple-h1",
                               f"{len(h1s)} H1 headings; expected exactly 1"))
        elif p.fm.get("title") and h1s[0] != p.fm["title"]:
            violations.append((p.slug, "A4-h1-title-mismatch",
                               f"H1 {h1s[0]!r} != title {p.fm['title']!r}"))

        # A5/C. Page type + tag vocabulary
        tags = p.tags
        if not tags:
            violations.append((p.slug, "A5-empty-tags", "tags list is empty"))
        else:
            if page_types and tags[0] not in page_types:
                violations.append((p.slug, "C-bad-page-type",
                                   f"tags[0]={tags[0]!r} not in {sorted(page_types)}"))
            if canonical_tags:
                bad = [t for t in tags[1:] if t not in canonical_tags]
                for t in bad:
                    violations.append((p.slug, "C-unknown-tag",
                                       f"tag {t!r} not in canonical_tags (add to config.yaml or rename)"))

            # A10. Daily pages must carry a non-empty, bounded `summary` so referring
            # pages can surface the entry's subject in link text (see A11).
            if tags[0] == "daily":
                fm_sum = p.fm.get("summary")
                if not fm_sum or not str(fm_sum).strip():
                    violations.append((p.slug, "A10-daily-summary-required",
                                       "daily page missing non-empty frontmatter 'summary'"))
                elif len(str(fm_sum)) > 80:
                    violations.append((p.slug, "A10-daily-summary-required",
                                       f"summary is {len(str(fm_sum))} chars; must be \u2264 80"))

        # A6. Dangling body links
        for target in p.outbound_links():
            if target not in valid_slugs:
                violations.append((p.slug, "A6-dangling-link",
                                   f"body links to {target!r} which has no wiki page"))

        # A8. No wikilinks in body
        for target in p.wikilink_targets():
            violations.append((p.slug, "A8-wikilink-style",
                               f"body uses [[{target}]]; rewrite as [text]({target}.md)"))

        # A9. Agent-notes <details> block format (presence not required).
        violations.extend(_check_agent_notes_format(p))

        # A11. Link text to daily pages must be "YYYY-MM-DD \u2014 <subject>" so readers
        # know what's behind the link without opening it. Catalogs / logs in
        # meta_exempt hand-write their own summaries and are skipped.
        if p.slug not in meta_exempt:
            body_stripped = _strip_code(p.body)
            for m in MD_LINK_RE.finditer(body_stripped):
                text, target = m.group(1), m.group(2)
                tgt_slug = _target_to_slug(target)
                if tgt_slug in daily_slugs and tgt_slug != p.slug:
                    if not DAILY_LINK_TEXT_RE.match(text):
                        violations.append((p.slug, "A11-daily-link-text",
                                           f"link text {text!r} to daily {tgt_slug!r} must match "
                                           f"'YYYY-MM-DD \u2014 <subject>'"))

        # B9/B10. Graph symmetry — skip exempt pages as *source*; they may have many
        # outbound links by design (catalogs).
        if p.slug in meta_exempt:
            continue

        body_out = set(p.outbound_links())
        related_set = {_target_to_slug(r) for r in p.related}
        related_set.discard(None)

        # B9. related ⊆ body outbound (related entries that aren't actually linked in body)
        for r in sorted(related_set - body_out):
            violations.append((p.slug, "B9-related-not-in-body",
                               f"related lists {r!r} but body has no link to it"))

        # B10. Back-link symmetry: every body link A→B must have B's related include A.
        # Skip when target is itself exempt (catalogs don't track related back).
        for target in sorted(body_out):
            if target not in by_slug or target in meta_exempt:
                continue
            target_page = by_slug[target][0]
            target_related = {_target_to_slug(r) for r in target_page.related}
            if p.slug not in target_related:
                violations.append((p.slug, "B10-asymmetric-link",
                                   f"links to {target!r} but {target!r} does not list {p.slug!r} in related"))

    return violations


def print_lint_report(violations: list[tuple[str, str, str]]) -> None:
    """Print violations grouped by rule, then by slug."""
    by_rule: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for slug, rule, msg in violations:
        by_rule[rule].append((slug, msg))
    for rule in sorted(by_rule):
        items = by_rule[rule]
        print(f"\n[{rule}] ({len(items)})")
        for slug, msg in items:
            print(f"  {slug}: {msg}")


def cmd_strict_lint(args: argparse.Namespace) -> int:
    cfg = load_config()
    violations = run_strict_lint(cfg)
    if not violations:
        print("mind: strict lint clean.")
        return 0
    print_lint_report(violations)
    print(f"\nmind: {len(violations)} strict-lint violation(s).", file=sys.stderr)
    return 1


# ---------- subcommands: agent-driven ----------

def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = load_config()
    src = Path(args.path).resolve()
    if not src.exists():
        print(f"mind: no such file {src}", file=sys.stderr)
        return 2

    RAW.mkdir(exist_ok=True)
    dest = _move_into_raw(src, cfg, keep_original=args.keep)
    print(f"mind: ingested {src} → {_rel(dest)}")

    sibling = _preprocess(dest, cfg)
    if sibling:
        print(f"mind: produced sibling text {_rel(sibling)}")

    if sibling and _should_verify(sibling, cfg, args):
        if not _verify_sibling(sibling, dest, cfg):
            print("mind: ingest aborted by user", file=sys.stderr)
            return 1

    prompt = _build_ingest_prompt(dest, sibling, cfg, batch=args.batch)
    agent_ok = _invoke_agent(cfg, prompt)

    rebuild_cache()
    if cfg["ingest"]["auto_commit"]:
        cmd_commit(argparse.Namespace(message=f"ingest: {dest.name}"))

    return 0 if agent_ok else 1


def cmd_query(args: argparse.Namespace) -> int:
    cfg = load_config()
    prompt = _build_query_prompt(args.query, cfg, file_answer=args.file)
    ok = _invoke_agent(cfg, prompt)
    rebuild_cache()
    if args.file and cfg["ingest"]["auto_commit"]:
        cmd_commit(argparse.Namespace(message=f"query: {args.query[:60]}"))
    return 0 if ok else 1


def cmd_lint(args: argparse.Namespace) -> int:
    cfg = load_config()
    violations = run_strict_lint(cfg)
    if violations:
        print_lint_report(violations)
        print(
            f"\nmind: {len(violations)} strict-lint violation(s); refusing to invoke "
            "the agent until these are resolved. Fix manually or pass --skip-strict.",
            file=sys.stderr,
        )
        if not getattr(args, "skip_strict", False):
            return 1
        print("mind: --skip-strict set; proceeding to agent lint despite violations.", file=sys.stderr)
    else:
        print("mind: strict lint clean; invoking agent for fuzzy checks.")

    prompt = _build_lint_prompt(cfg)
    ok = _invoke_agent(cfg, prompt)
    rebuild_cache()
    if cfg["ingest"]["auto_commit"]:
        cmd_commit(argparse.Namespace(message="lint", no_lint=False))
    return 0 if ok else 1


def cmd_digest(args: argparse.Namespace) -> int:
    cfg = load_config()
    dcfg = cfg["digest"]

    today = date.today()
    window_start, window_source = _resolve_digest_window(
        since=args.since,
        since_last=args.since_last,
        window_days=args.window_days or dcfg["default_window_days"],
        today=today,
    )
    window_end = today

    if window_start > window_end:
        print(f"mind: window start {window_start} is in the future; nothing to do", file=sys.stderr)
        return 1

    pages = list(iter_pages())
    entries = _find_recent_entries(pages, window_start, window_end)

    if not entries:
        print(
            f"mind: no daily/source entries updated between {window_start} and {window_end} "
            f"(window via {window_source}); nothing to digest."
        )
        return 0

    by_slug = {p.slug: p for p in pages}
    clusters = _cluster_by_concept_overlap(entries, by_slug, min_size=dcfg["min_cluster_size"])

    open_threads_slug = dcfg["open_threads_slug"]
    open_threads_path = WIKI / f"{open_threads_slug}.md"
    prior_open_threads = (
        open_threads_path.read_text(encoding="utf-8") if open_threads_path.exists() else None
    )

    prompt = _build_digest_prompt(
        cfg=cfg,
        window_start=window_start,
        window_end=window_end,
        window_source=window_source,
        entries=entries,
        clusters=clusters,
        prior_open_threads=prior_open_threads,
    )

    print(
        f"mind: digest window {window_start} → {window_end} "
        f"({len(entries)} entries, {len(clusters)} clusters)"
    )
    ok = _invoke_agent(cfg, prompt)
    rebuild_cache()
    if not args.no_commit and dcfg["auto_commit"]:
        cmd_commit(argparse.Namespace(message=f"digest: {window_start}..{window_end}"))
    return 0 if ok else 1


def _resolve_digest_window(
    since: str | None, since_last: bool, window_days: int, today: date
) -> tuple[date, str]:
    """Return (window_start, explanation_string)."""
    if since:
        parsed = _parse_since(since, today)
        if parsed is None:
            raise SystemExit(
                f"mind: could not parse --since {since!r}; use e.g. '7d', '2w', or '2026-04-01'."
            )
        return parsed, f"--since {since}"

    # --since-last is implicit when neither flag is given
    last = _find_last_digest_date()
    if last is not None:
        label = "--since-last" if since_last else "default"
        return last, f"{label} (last digest {last.isoformat()})"

    delta_days = max(window_days, 1)
    start = date.fromordinal(today.toordinal() - delta_days)
    label_prefix = "no prior digest, " if since_last else ""
    return start, f"{label_prefix}default window ({delta_days}d)"


def _parse_since(s: str, today: date) -> date | None:
    s = s.strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)([dwm])", s.lower())
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n if unit == "d" else n * 7 if unit == "w" else n * 30
        return date.fromordinal(today.toordinal() - days)
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


_DIGEST_FILENAME_RE = re.compile(r"^digest-(\d{4}-\d{2}-\d{2})\.md$")


def _find_last_digest_date() -> date | None:
    if not WIKI.is_dir():
        return None
    candidates: list[date] = []
    for p in WIKI.glob("digest-*.md"):
        m = _DIGEST_FILENAME_RE.match(p.name)
        if not m:
            continue
        try:
            candidates.append(date.fromisoformat(m.group(1)))
        except ValueError:
            continue
    return max(candidates) if candidates else None


def _find_recent_entries(pages: list[Page], window_start: date, window_end: date) -> list[Page]:
    """Pages typed daily or source whose updated/created falls in [window_start, window_end]."""
    entries: list[Page] = []
    for p in pages:
        if p.page_type not in {"daily", "source"}:
            continue
        d = _page_date(p)
        if d is None:
            continue
        if window_start <= d <= window_end:
            entries.append(p)
    entries.sort(key=lambda pg: (_page_date(pg) or date.min, pg.slug))
    return entries


def _page_date(p: Page) -> date | None:
    for key in ("updated", "created"):
        v = p.fm.get(key)
        if isinstance(v, date):
            return v
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, str):
            try:
                return date.fromisoformat(v[:10])
            except ValueError:
                continue
    return None


def _cluster_by_concept_overlap(
    entries: list[Page],
    by_slug: dict[str, Page],
    min_size: int,
) -> list[dict]:
    """Group entries by the concept/entity pages they touch.

    Each cluster = {"target": <slug>, "title": <title>, "entries": [entry_slugs...]}.
    Only concept/entity targets are clustered; daily/source/digest/meta targets are ignored.
    Clusters with fewer than min_size entries are dropped.
    """
    buckets: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        touched: set[str] = set()
        touched.update(entry.outbound_links())
        for rel in entry.related:
            slug = _target_to_slug(rel)
            if slug:
                touched.add(slug)
        for target_slug in touched:
            target = by_slug.get(target_slug)
            if target is None:
                continue
            if target.page_type not in {"concept", "entity"}:
                continue
            buckets[target_slug].append(entry.slug)

    clusters: list[dict] = []
    for target_slug, entry_slugs in buckets.items():
        if len(entry_slugs) < min_size:
            continue
        target = by_slug.get(target_slug)
        clusters.append({
            "target": target_slug,
            "title": target.title if target else target_slug,
            "type": target.page_type if target else "unknown",
            "entries": sorted(set(entry_slugs)),
        })
    clusters.sort(key=lambda c: (-len(c["entries"]), c["target"]))
    return clusters


def _plan_pull(
    kind: str = "all",
    source: str | None = None,
) -> tuple[Path, list[tuple[str, Path, list[str]]], list[dict[str, str]]]:
    """Resolve configured sources and collect files to pull, without moving anything.

    Returns (staging_dir, plans, skipped). Raises RuntimeError with a message on config errors.
    """
    cfg = load_config()
    pull_cfg = cfg["pull"]

    staging = (ROOT / pull_cfg["staging_dir"]).resolve()
    staging.mkdir(parents=True, exist_ok=True)

    if source:
        pairs: list[tuple[str, Path]] = [("override", Path(source).expanduser())]
    else:
        sources = pull_cfg.get("sources") or {}
        if not sources:
            raise RuntimeError(
                "no pull sources configured. Copy config.local.example.yaml → "
                "config.local.yaml and set `pull.sources.*`."
            )
        if kind == "all":
            pairs = [(k, Path(v).expanduser()) for k, v in sources.items()]
        else:
            if kind not in sources:
                raise RuntimeError(
                    f"no source configured for kind '{kind}'. "
                    f"Configured kinds: {', '.join(sources) or '(none)'}"
                )
            pairs = [(kind, Path(sources[kind]).expanduser())]

    plans: list[tuple[str, Path, list[str]]] = []
    skipped: list[dict[str, str]] = []
    for k, src_dir in pairs:
        if not src_dir.is_dir():
            skipped.append({"kind": k, "reason": f"source folder does not exist: {src_dir}"})
            continue
        files = _collect_files(src_dir)
        if not files:
            skipped.append({"kind": k, "reason": f"nothing to pull from {src_dir}"})
            continue
        plans.append((k, src_dir, files))

    return staging, plans, skipped


def _execute_pull_plan(
    staging: Path,
    plans: list[tuple[str, Path, list[str]]],
    keep: bool,
) -> list[dict[str, str]]:
    """Copy files per the pre-computed plan, optionally unlinking sources."""
    pulled: list[dict[str, str]] = []
    for k, src_dir, files in plans:
        for rel in files:
            src = src_dir / rel
            dst = staging / _sanitize_inbox_name(Path(rel).name)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                dst = _dedupe_path(dst)
            shutil.copy2(str(src), str(dst))
            if not keep:
                src.unlink()
            pulled.append(
                {
                    "kind": k,
                    "filename": dst.name,
                    "path": _rel(dst),
                }
            )
        if not keep:
            _cleanup_empty_dirs(src_dir)
    return pulled


def _do_pull(
    kind: str = "all",
    keep: bool = False,
    source: str | None = None,
) -> dict[str, Any]:
    """Non-interactive pull: collect → copy into inbox/ → optionally remove source.

    Shared core for the UI's /api/pull endpoint. Does no prompting or printing;
    returns a structured result.
    """
    cfg = load_config()
    do_move = not (keep or not cfg["pull"]["move"])

    staging, plans, skipped = _plan_pull(kind=kind, source=source)
    pulled = _execute_pull_plan(staging, plans, keep=not do_move)

    return {
        "staging": _rel(staging),
        "pulled": pulled,
        "skipped": skipped,
    }


def cmd_pull(args: argparse.Namespace) -> int:
    """Pull files from per-kind source folders into the staging inbox."""
    try:
        staging, plans, skipped = _plan_pull(kind=args.kind, source=args.source)
    except RuntimeError as e:
        print(f"mind: {e}", file=sys.stderr)
        return 2

    staging_rel = _rel(staging)

    for entry in skipped:
        print(f"mind: skipping {entry['kind']}: {entry['reason']}", file=sys.stderr)

    if not plans:
        return 0

    total = sum(len(files) for _, _, files in plans)
    for kind, src_dir, files in plans:
        print(f"mind pull [{kind}]: {src_dir} → {staging_rel}")
        for rel in files:
            print(f"  + {rel}")

    if not args.yes:
        if not sys.stdin.isatty():
            print(
                f"\nmind: refusing to prompt on non-TTY stdin. "
                f"Pass -y to confirm pulling {total} item(s).",
                file=sys.stderr,
            )
            return 1
        try:
            answer = input(f"\nPull {total} item(s) across {len(plans)} source(s)? [y/N] ").strip().lower()
        except EOFError:
            print("\nAborted (EOF).", file=sys.stderr)
            return 1
        if answer != "y":
            print("Aborted.")
            return 1

    cfg = load_config()
    do_move = not (args.keep or not cfg["pull"]["move"])
    pulled_items = _execute_pull_plan(staging, plans, keep=not do_move)
    for item in pulled_items:
        print(f"  staged [{item['kind']}] {item['path']}")

    print(f"\nmind: pulled {len(pulled_items)} item(s) to {staging_rel}/")
    print(f"  next: run `mind ingest {staging_rel}/<file>` per item")
    return 0


_INBOX_AUDIO_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}


def cmd_pending(args: argparse.Namespace) -> int:
    """List captures staged in inbox/ along with parse state.

    An audio file with a sibling `.transcript.md` is `parsed` (whisper ran,
    awaiting human review + `mind ingest`). Without the sibling it's
    `captured` (preprocessing hasn't run yet).
    """
    cfg = load_config()
    staging = (ROOT / cfg["pull"]["staging_dir"]).resolve()
    items: list[dict[str, Any]] = []
    if staging.is_dir():
        for p in sorted(staging.rglob("*")):
            if not p.is_file():
                continue
            if p.name.startswith("."):
                continue
            if p.name.endswith(_SIBLING_SUFFIXES):
                continue
            rel = _rel(p)
            ext = p.suffix.lower()
            sibling = p.with_suffix(".transcript.md") if ext in _INBOX_AUDIO_EXTS else None
            state = "captured"
            transcript_preview: str | None = None
            if sibling and sibling.exists() and sibling.stat().st_size > 0:
                state = "parsed"
                transcript_preview = _transcript_preview(sibling)
            elif ext not in _INBOX_AUDIO_EXTS:
                state = "other"
            items.append(
                {
                    "path": rel,
                    "filename": p.name,
                    "state": state,
                    "bytes": p.stat().st_size,
                    "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
                    "transcript_preview": transcript_preview,
                }
            )
    if args.json:
        print(json.dumps(items, indent=2))
        return 0
    if not items:
        print("mind: nothing pending")
        return 0
    staging_rel = _rel(staging)
    print(f"mind: {len(items)} pending in {staging_rel}/")
    for it in items:
        tag = it["state"]
        size_kb = it["bytes"] / 1024
        print(f"  [{tag:>8}] {it['path']}  {size_kb:,.1f} kB  {it['mtime']}")
        if it["transcript_preview"]:
            preview = it["transcript_preview"].replace("\n", " ").strip()
            if len(preview) > 100:
                preview = preview[:97] + "..."
            print(f"             └─ {preview}")
    print(f"  next: `mind ingest {staging_rel}/<file>` to promote into the wiki")
    return 0


_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def _transcript_preview(sibling: Path) -> str:
    try:
        text = sibling.read_text(encoding="utf-8")
    except Exception:
        return ""
    m = _FRONTMATTER_RE.match(text)
    body = text[m.end():] if m else text
    body = body.lstrip()
    body = re.sub(r"^# [^\n]*\n+", "", body, count=1)
    return body.strip()


def _collect_files(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for f in filenames:
            if f.startswith("."):
                continue
            result.append(os.path.relpath(os.path.join(dirpath, f), root))
    return sorted(result)


def _dedupe_path(dst: Path) -> Path:
    stem, suffix = dst.stem, dst.suffix
    n = 1
    while True:
        candidate = dst.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# Must produce names matching ui/server.py:_SAFE_FILENAME_RE
# (r"^[A-Za-z0-9][A-Za-z0-9._-]*$"). Keep in sync if that regex changes.
_INBOX_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _sanitize_inbox_name(name: str) -> str:
    """Map a source filename to a safe, readable inbox name.

    Spaces become underscores; non-`[A-Za-z0-9._-]` chars are dropped; runs of
    underscores collapse; leading/trailing `_`, `-`, `.` are stripped. Empty
    stems fall back to `audio` — collisions then flow through `_dedupe_path`.
    """
    base = Path(name).name
    dot = base.rfind(".")
    if dot <= 0:
        stem, ext = base, ""
    else:
        stem, ext = base[:dot], base[dot:].lower()
    stem = stem.replace(" ", "_")
    stem = re.sub(r"[^A-Za-z0-9._-]", "", stem)
    stem = re.sub(r"_+", "_", stem)
    stem = stem.strip("_-.")
    if not stem:
        stem = "audio"
    result = f"{stem}{ext}"
    if not _INBOX_NAME_RE.match(result):
        raise ValueError(f"sanitizer produced invalid inbox name: {result!r}")
    return result


def _cleanup_empty_dirs(root: Path) -> None:
    if not root.is_dir():
        return
    for dirpath, _, _ in os.walk(root, topdown=False):
        if Path(dirpath) == root:
            continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass


def _slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "untitled"


_SIBLING_SUFFIXES = (".transcript.md", ".text.md", ".caption.md")


def _move_into_raw(src: Path, cfg: dict, keep_original: bool) -> Path:
    today = date.today().isoformat()
    stem = _slugify(src.stem)
    convention = cfg["raw"]["filename_convention"]
    filename = convention.format(date=today, slug=stem, ext=src.suffix)
    dest = RAW / filename
    n = 1
    while dest.exists():
        dest = RAW / convention.format(date=today, slug=f"{stem}-{n}", ext=src.suffix)
        n += 1
    siblings = [src.parent / (src.stem + suf) for suf in _SIBLING_SUFFIXES]
    siblings = [s for s in siblings if s.exists()]
    if keep_original:
        shutil.copy2(src, dest)
        for s in siblings:
            shutil.copy2(s, dest.parent / (dest.stem + _sibling_suffix(s)))
    else:
        shutil.move(str(src), dest)
        for s in siblings:
            shutil.move(str(s), dest.parent / (dest.stem + _sibling_suffix(s)))
    return dest


def _sibling_suffix(sibling: Path) -> str:
    name = sibling.name
    for suf in _SIBLING_SUFFIXES:
        if name.endswith(suf):
            return suf
    return sibling.suffix


def _preprocess(raw_file: Path, cfg: dict) -> Path | None:
    """Produce a sibling text artifact for non-text inputs, per config. Returns sibling path or None."""
    suffix = raw_file.suffix.lower()
    pp = cfg["preprocessing"]

    if suffix in {".md", ".txt", ".rst", ".org"}:
        return None

    if suffix in {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".aac"}:
        if not pp["audio"]["enabled"]:
            return None
        return _preprocess_audio(raw_file, pp["audio"])

    if suffix == ".pdf":
        if not pp["pdf"]["enabled"]:
            return None
        return _preprocess_pdf(raw_file, pp["pdf"])

    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic"}:
        if not pp["image"]["enabled"]:
            return None
        return _preprocess_image(raw_file, pp["image"])

    if suffix in {".mp4", ".mov", ".mkv", ".webm", ".m4v"}:
        if not pp["video"]["enabled"]:
            return None
        return _preprocess_video(raw_file, pp["video"])

    return None


def _resolve_whisper_cpp(cfg: dict) -> tuple[Path | None, Path | None]:
    """Return (binary_path, model_path) for whisper.cpp, or (None, None) if unresolvable.

    Lookup order:
      binary: cfg.binary -> PATH (whisper-cli, whisper-cpp, main; PATHEXT covers .exe)
              -> ROOT/whisper.cpp/{build/bin,build/bin/Release,.}/whisper-cli[.exe]
              -> ROOT/whisper.cpp/main[.exe]
      model:  cfg.model_path -> ROOT/whisper.cpp/models/ggml-<cfg.model>.bin
    Both resolutions are per-machine; the whisper.cpp dir is gitignored.
    """
    explicit_bin = cfg.get("binary")
    if explicit_bin:
        p = Path(explicit_bin).expanduser()
        if not p.is_absolute():
            p = ROOT / p
        binary = p if p.exists() else None
    else:
        binary = None
        for name in ("whisper-cli", "whisper-cpp", "main"):
            found = shutil.which(name)
            if found:
                binary = Path(found)
                break
        if binary is None:
            wc = ROOT / "whisper.cpp"
            for candidate in (
                wc / "build" / "bin" / "whisper-cli",                  # cmake, unix layout
                wc / "build" / "bin" / "whisper-cli.exe",              # cmake, Windows single-config
                wc / "build" / "bin" / "Release" / "whisper-cli.exe",  # cmake, MSVC multi-config
                wc / "whisper-cli",                                    # prebuilt / copied binary
                wc / "whisper-cli.exe",
                wc / "main",                                           # legacy binary name
                wc / "main.exe",
            ):
                if candidate.exists():
                    binary = candidate
                    break

    explicit_model = cfg.get("model_path")
    if explicit_model:
        mp = Path(explicit_model).expanduser()
        if not mp.is_absolute():
            mp = ROOT / mp
        model = mp if mp.exists() else None
    else:
        model_name = cfg.get("model", "base.en")
        mp = ROOT / "whisper.cpp" / "models" / f"ggml-{model_name}.bin"
        model = mp if mp.exists() else None

    return binary, model


def _preprocess_audio(f: Path, cfg: dict) -> Path | None:
    sibling = f.with_suffix(".transcript.md")
    tool = cfg.get("tool", "whisper-cpp")
    if tool == "off":
        return None
    # Idempotency: a non-empty sibling means either a previous preprocess run
    # or a human edit during review. Don't clobber either — `mind ingest`
    # relies on this to promote inbox/ transcripts into raw/ unchanged.
    if sibling.exists() and sibling.stat().st_size > 0:
        return sibling
    try:
        if tool == "whisper-cpp":
            binary, model = _resolve_whisper_cpp(cfg)
            if binary is None:
                _write_stub(sibling, f, "preprocess(audio): whisper.cpp binary not found. Build it (see README) or set preprocessing.audio.binary.")
                return sibling
            if model is None:
                _write_stub(sibling, f, f"preprocess(audio): ggml model for {cfg.get('model', 'base.en')!r} not found. Run whisper.cpp/models/download-ggml-model.sh or set preprocessing.audio.model_path.")
                return sibling
            result = subprocess.run(
                [str(binary), "-m", str(model), "-nt", "-otxt", "-of", "-", str(f)],
                capture_output=True, text=True, check=False,
            )
            text = result.stdout.strip() or result.stderr.strip()
            _write_preprocessed(sibling, f, tool, text or "(empty transcript)")
            return sibling
        if tool in {"mlx-whisper", "openai-whisper"}:
            binary_name = {"mlx-whisper": "mlx_whisper", "openai-whisper": "whisper"}[tool]
            if shutil.which(binary_name) is None:
                _write_stub(sibling, f, f"preprocess(audio): {binary_name!r} not on PATH; agent should read raw on demand")
                return sibling
            result = subprocess.run([binary_name, str(f)], capture_output=True, text=True, check=False)
            text = result.stdout.strip() or result.stderr.strip()
            _write_preprocessed(sibling, f, tool, text or "(empty transcript)")
            return sibling
    except Exception as e:
        _write_stub(sibling, f, f"preprocess(audio) failed: {e}")
        return sibling
    return None


def _preprocess_pdf(f: Path, cfg: dict) -> Path | None:
    sibling = f.with_suffix(".text.md")
    try:
        from pypdf import PdfReader
    except ImportError:
        _write_stub(sibling, f, "preprocess(pdf): pypdf not installed; install it or set preprocessing.pdf.enabled: false")
        return sibling
    try:
        reader = PdfReader(str(f))
        chunks = []
        for i, page in enumerate(reader.pages):
            chunks.append(f"\n\n### Page {i+1}\n\n{page.extract_text() or ''}")
        _write_preprocessed(sibling, f, cfg.get("tool", "pypdf"), "".join(chunks).strip() or "(no extractable text)")
        return sibling
    except Exception as e:
        _write_stub(sibling, f, f"preprocess(pdf) failed: {e}")
        return sibling


def _preprocess_image(f: Path, cfg: dict) -> Path | None:
    """Write a minimal `.caption.md` sibling for an image.

    Images are not OCR'd. The sibling is a carrier: it lets the pending list
    track the image and lets the user attach ingest instructions via the UI
    (stored in the sibling's frontmatter). The ingest agent reads the raw image
    directly via its vision model and overwrites the sibling with the clean
    extraction.
    """
    sibling = f.with_suffix(".caption.md")
    # Idempotency: non-empty sibling means either a previous preprocess run, a
    # UI-uploaded caption (with `instructions:` frontmatter), or a human edit.
    # Don't clobber any of them.
    if sibling.exists() and sibling.stat().st_size > 0:
        return sibling
    _write_preprocessed(
        sibling,
        f,
        "instructions-carrier",
        "_Awaiting ingest — agent will read the image directly and extract per instructions (if any)._",
    )
    return sibling


def _preprocess_video(f: Path, _cfg: dict) -> Path | None:
    sibling = f.with_suffix(".transcript.md")
    _write_stub(sibling, f, "preprocess(video): disabled by default. Enable in config.yaml to chain ffmpeg + whisper.")
    return sibling


def _write_preprocessed(sibling: Path, source: Path, tool: str, body: str) -> None:
    today = date.today().isoformat()
    content = (
        f"---\n"
        f"sibling_of: {source.name}\n"
        f"preprocessor: {tool}\n"
        f"created: {today}\n"
        f"---\n\n"
        f"# Preprocessed text: {source.name}\n\n"
        f"{body}\n"
    )
    sibling.write_text(content, encoding="utf-8")


def _write_stub(sibling: Path, source: Path, note: str) -> None:
    today = date.today().isoformat()
    content = (
        f"---\n"
        f"sibling_of: {source.name}\n"
        f"preprocessor: stub\n"
        f"created: {today}\n"
        f"---\n\n"
        f"# Preprocess stub for {source.name}\n\n"
        f"{note}\n"
    )
    sibling.write_text(content, encoding="utf-8")


# ---------- agent prompt construction ----------

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


def _build_ingest_prompt(raw_file: Path, sibling: Path | None, cfg: dict, batch: bool) -> str:
    mode = "batch (no user approval)" if batch else cfg["ingest"]["default_mode"]
    sibling_line = f"- Sibling text: `{_rel(sibling)}`" if sibling else "- Sibling text: none (read the raw file directly)"

    image_block = ""
    if raw_file.suffix.lower() in _IMAGE_EXTS and sibling is not None and sibling.exists():
        try:
            fm, _ = _split_sibling_frontmatter(sibling.read_text(encoding="utf-8"))
        except Exception:
            fm = {}
        instructions = (fm.get("instructions") or "").strip() if isinstance(fm.get("instructions"), str) else ""
        if instructions:
            image_block = f"""
## Image capture — specific extraction intent

The user captured this image with a specific extraction instruction:
> "{instructions}"

**View the image directly with your vision and produce a clean extraction that honors the user's intent EXACTLY — nothing more, nothing less.**

After producing the clean extraction:
1. Overwrite the sibling `.caption.md` with the final version — keep the YAML frontmatter but set `agent_pass: done`, and replace the body with the clean extraction.
2. Then proceed with the normal ingest: create/update the appropriate wiki page(s) using the clean extraction as the source of truth.
"""
        else:
            image_block = """
## Image capture

View the image directly with your vision and produce a full clean transcription of its text / content.

After producing the clean transcription:
1. Overwrite the sibling `.caption.md` with the final version — keep the YAML frontmatter but set `agent_pass: done`, and replace the body with the clean transcription.
2. Then proceed with the normal ingest.
"""

    return f"""You are invoked by `mind ingest`. Read `AGENTS.md` at the project root and follow the "Ingest" workflow exactly.

Project root: {ROOT}
Raw file just added: `{_rel(raw_file)}`
{sibling_line}
Ingest mode: {mode}
{image_block}
Do the work: orient via `./bin/mind index --titles-only`, read the source, create or update the appropriate page(s), weave inline cross-references to related existing pages, back-link from the other side, update `wiki/index.md`, append to `wiki/log.md`. Do not commit — `mind` will commit for you.

If anything is ambiguous (new page type, major restructure, unresolved contradiction), stop and ask the user before writing.
"""


def _build_query_prompt(question: str, cfg: dict, file_answer: bool) -> str:
    file_default = cfg["query"]["file_answer_default"]
    file_instr = (
        "After answering, ALSO save the answer as a wiki page `wiki/q-YYYY-MM-DD_<slug>.md` with `tags: [query, ...]`. Update `wiki/index.md` and `wiki/log.md`."
        if file_answer or file_default == "always"
        else "Do not save a page unless the user asks."
    )
    return f"""You are invoked by `mind query`. Read `AGENTS.md` at the project root and follow the "Query" workflow.

Project root: {ROOT}
Question: {question!r}

{file_instr}

Locate relevant pages with `./bin/mind index --titles-only` + `./bin/mind search "<terms>"`, read them, synthesize an answer with inline citations (links to pages, raw source filenames)."""


def _build_digest_prompt(
    cfg: dict,
    window_start: date,
    window_end: date,
    window_source: str,
    entries: list[Page],
    clusters: list[dict],
    prior_open_threads: str | None,
) -> str:
    today = date.today().isoformat()
    open_threads_slug = cfg["digest"]["open_threads_slug"]
    digest_slug = f"digest-{today}"

    entries_block_lines = []
    for e in entries:
        d = _page_date(e)
        d_str = d.isoformat() if d else "?"
        entries_block_lines.append(
            f"- `{_rel(e.path)}` — {e.title} ({e.page_type}, {d_str})"
        )
    entries_block = "\n".join(entries_block_lines) if entries_block_lines else "(none)"

    if clusters:
        cluster_block_lines = []
        for c in clusters:
            members = ", ".join(f"`{s}.md`" for s in c["entries"])
            cluster_block_lines.append(
                f"- **{c['title']}** (`{c['target']}.md`, {c['type']}) — touched by {len(c['entries'])} entries: {members}"
            )
        clusters_block = "\n".join(cluster_block_lines)
    else:
        clusters_block = (
            "(no clusters — every concept/entity was touched by fewer than "
            f"{cfg['digest']['min_cluster_size']} entries; still synthesize across the entries as a whole)"
        )

    if prior_open_threads is None:
        open_threads_block = f"(no `wiki/{open_threads_slug}.md` yet — create it with `tags: [meta]` if you have threads to record)"
    else:
        open_threads_block = f"Current contents of `wiki/{open_threads_slug}.md`:\n\n```\n{prior_open_threads}\n```"

    return f"""You are invoked by `mind digest`. Read `AGENTS.md` at the project root and follow the "Digest" workflow.

Project root: {ROOT}
Window: {window_start.isoformat()} → {window_end.isoformat()}  (source: {window_source})
Today: {today}
New digest page to write: `wiki/{digest_slug}.md` (tags: [digest])

## Entries in the window

{entries_block}

## Pre-computed clusters (concept/entity targets touched by ≥ {cfg['digest']['min_cluster_size']} entries)

{clusters_block}

## Prior open threads

{open_threads_block}

## What to do

1. Read every entry above. For each cluster, also read the target concept/entity page.
2. Write `wiki/{digest_slug}.md` with this shape:
   - Frontmatter: `tags: [digest]`, `slug: {digest_slug}`, `created: {today}`, `updated: {today}`, `sources: []`, `related: [<the concept/entity pages clustered>]`.
   - H1: `Digest {window_start.isoformat()} → {window_end.isoformat()}`
   - `## Clusters` — per cluster: a short prose synthesis of what the entries say about that concept, noting drift, new claims, and open questions. Use inline `[Title](slug.md)` links.
   - `## Proposed edits` — a markdown checklist. Each item: `- [ ] <slug>.md: <specific change>`. Be concrete; these are for a human (or later --apply agent) to execute. Do NOT edit the target pages in this run.
   - `## Stats` — entry count, cluster count, pages touched.
3. Maintain `wiki/{open_threads_slug}.md` (tags: [meta]): add any new open questions or TODOs surfaced by the entries, close/remove any that subsequent entries resolved. Each item: `- [ ] <question> — from [<entry title>](<entry-slug>.md), opened <date>`.
4. Update `wiki/index.md`: under `## Digests` (create the section if missing, directly before `## Meta`), add a line `- [Digest {window_start.isoformat()} → {window_end.isoformat()}]({digest_slug}.md) — <one-line summary>`. Under `## Meta`, ensure `[Open Threads]({open_threads_slug}.md)` is listed if it wasn't.
5. Append one line to `wiki/log.md`: `## [{today}] digest | {window_start.isoformat()}..{window_end.isoformat()}` followed by a one-paragraph note on clusters synthesized and open threads updated.

Hard constraints for this run:
- Do NOT edit the clustered concept/entity pages themselves (this is dry-run v1). Their updates live as checklist items in the digest.
- Do NOT commit — `mind` will commit for you.
- If the window has too few entries to be meaningful, still emit a short digest page noting that and skip to log.md.
"""


def _build_lint_prompt(_cfg: dict) -> str:
    return f"""You are invoked by `mind lint`. Read `AGENTS.md` and follow the "Lint" workflow.

Project root: {ROOT}

Scan the wiki for dangling links, duplicate slugs, orphans, back-link asymmetry, stale claims, and contradictions. Write findings to `wiki/contradictions.md` and inline `<!-- lint: ... -->` HTML comments on affected pages. Do not commit — `mind` will commit for you.
"""


_SIBLING_MODALITY_BY_SUFFIX = {
    ".transcript.md": "audio",
    ".text.md": "pdf",
    ".caption.md": "image",
}


def _sibling_modality(sibling: Path) -> str | None:
    name = sibling.name.lower()
    for suf, modality in _SIBLING_MODALITY_BY_SUFFIX.items():
        if name.endswith(suf):
            return modality
    return None


def _should_verify(sibling: Path, cfg: dict, args: argparse.Namespace) -> bool:
    if getattr(args, "batch", False):
        return False
    explicit = getattr(args, "verify", None)
    if explicit is True:
        return True
    if explicit is False:
        return False
    vcfg = cfg.get("verify", {})
    if not vcfg.get("default", True):
        return False
    modality = _sibling_modality(sibling)
    if modality is None or modality not in vcfg.get("modalities", []):
        return False
    if not sys.stdin.isatty():
        print("mind: skipping verification (stdin is not a TTY)", file=sys.stderr)
        return False
    return True


def _split_sibling_frontmatter(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, m.group(2)


def _render_sibling_for_review(sibling: Path, raw: Path, cfg: dict) -> None:
    max_lines = int(cfg.get("verify", {}).get("max_inline_lines", 200))
    text = sibling.read_text(encoding="utf-8")
    fm, body = _split_sibling_frontmatter(text)
    preprocessor = fm.get("preprocessor", "unknown")
    lines = body.splitlines()
    bar = "─" * 60
    print()
    print(bar)
    print(f"Verification: {raw.name}")
    print(f"preprocessor: {preprocessor}")
    print(f"sibling:      {_rel(sibling)}")
    print(bar)
    if len(lines) > max_lines:
        head = lines[: max_lines - 20]
        tail = lines[-20:]
        print("\n".join(head))
        print(f"\n[… {len(lines) - max_lines} more lines elided; full file: {_rel(sibling)} …]\n")
        print("\n".join(tail))
    else:
        print("\n".join(lines))
    print(bar)


def _build_verify_correction_prompt(sibling: Path, raw: Path, feedback: str) -> str:
    return f"""You are correcting a preprocessed artifact before it is ingested into a personal wiki. Do NOT touch wiki/ or raw/ originals — your only job is to edit the single sibling file below.

Project root: {ROOT}
Raw source:   {_rel(raw)}
Sibling file: {_rel(sibling)}

User feedback (verbatim):
\"\"\"
{feedback}
\"\"\"

Read the sibling file, apply exactly the correction(s) the user asked for, and write the file back to the same path. Preserve the YAML frontmatter unless the user explicitly asked to change it. Keep edits minimal and focused on the feedback; do not rephrase unrelated sentences. Do not commit."""


def _verify_sibling(sibling: Path, raw: Path, cfg: dict) -> bool:
    while True:
        _render_sibling_for_review(sibling, raw, cfg)
        try:
            answer = input("Approve? [y]es / [n]o / describe changes: ").strip()
        except EOFError:
            print("mind: no input received; aborting ingest", file=sys.stderr)
            return False
        if answer == "" or answer.lower() in {"y", "yes"}:
            return True
        if answer.lower() in {"n", "no"}:
            return False
        prompt = _build_verify_correction_prompt(sibling, raw, answer)
        ok = _invoke_agent(cfg, prompt)
        if not ok:
            print("mind: correction agent failed; you may re-describe the change or abort with 'n'", file=sys.stderr)
        # loop: re-display the (possibly updated) sibling and re-prompt


def _invoke_agent(cfg: dict, prompt: str) -> bool:
    cmd = list(cfg["agent"]["command"])
    resolved = shutil.which(cmd[0])
    if resolved is None:
        print(f"mind: agent binary {cmd[0]!r} not found on PATH.", file=sys.stderr)
        print("mind: printing the prompt so you can paste it into an agent manually:\n", file=sys.stderr)
        print("---- BEGIN PROMPT ----")
        print(prompt)
        print("---- END PROMPT ----")
        return False
    # Use the resolved path as argv[0]: on Windows, list-form subprocess.run
    # can't launch a `.cmd`/`.exe` shim by bare name.
    cmd[0] = resolved
    print(f"mind: invoking agent: {' '.join(cmd)}")
    try:
        if len(prompt) > 30000:
            # Windows caps the command line at ~32K chars; `claude -p` reads
            # the prompt from stdin when it isn't passed as an argument.
            result = subprocess.run(cmd, input=prompt, text=True, cwd=str(ROOT), check=False)
        else:
            result = subprocess.run(cmd + [prompt], cwd=str(ROOT), check=False)
        return result.returncode == 0
    except KeyboardInterrupt:
        print("mind: agent interrupted", file=sys.stderr)
        return False


# ---------- subcommand: serve ----------

def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("mind: uvicorn not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 1
    # Fresh-clone tolerance: the UI mounts raw/ at import time and lists wiki/.
    WIKI.mkdir(exist_ok=True)
    RAW.mkdir(exist_ok=True)
    # Ensure cache exists so the UI can read it on first request.
    if not (CACHE / "index.json").exists():
        rebuild_cache()
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    cmd = [
        sys.executable, "-m", "uvicorn", "ui.server:app",
        "--host", args.host, "--port", str(args.port),
    ]
    if args.reload:
        cmd.append("--reload")
    print(f"mind: http://{args.host}:{args.port}")
    return subprocess.call(cmd, cwd=str(ROOT), env=env)


# ---------- argparse ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mind", description="mind — personal LLM wiki CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    pinit = sub.add_parser("init", help="bootstrap a fresh clone: seed wiki/, data repo, cache")
    pinit.set_defaults(fn=cmd_init)

    pi = sub.add_parser("index", help="project the page inventory at various resolutions")
    pi.add_argument("--titles-only", action="store_true", help="just slug: title lines (cheapest)")
    pi.add_argument("--full", action="store_true", help="titles + summaries + tags + updated")
    pi.add_argument("--tag", help="filter by a tag")
    pi.add_argument("--type", help="filter by page type (tags[0])")
    pi.add_argument("--json", action="store_true", help="raw cache/index.json")
    pi.set_defaults(fn=cmd_index)

    ps = sub.add_parser("search", help="FTS5 search over wiki bodies")
    ps.add_argument("query")
    ps.add_argument("--limit", type=int, default=20)
    ps.set_defaults(fn=cmd_search)

    pg = sub.add_parser("graph", help="traverse the page graph")
    pg.add_argument("--from", dest="from_slug", default=None)
    pg.add_argument("--depth", type=int, default=2)
    pg.add_argument("--format", choices=["json", "dot", "mermaid"], default="json")
    pg.set_defaults(fn=cmd_graph)

    prc = sub.add_parser("rebuild-cache", help="rebuild cache/* from wiki/")
    prc.set_defaults(fn=cmd_rebuild_cache)

    pc = sub.add_parser("commit", help="stage and commit wiki/ + raw/")
    pc.add_argument("-m", "--message", default=None)
    pc.add_argument("--no-lint", dest="no_lint", action="store_true",
                    help="bypass the strict-lint gate (use sparingly)")
    pc.set_defaults(fn=cmd_commit)

    pin = sub.add_parser("ingest", help="ingest a source file into the wiki")
    pin.add_argument("path")
    pin.add_argument("--batch", action="store_true", help="non-interactive mode")
    pin.add_argument("--keep", action="store_true", help="copy instead of move the original")
    pin.add_argument("--verify", dest="verify", action="store_true", default=None,
                     help="force verification gate for preprocessed siblings (overrides config)")
    pin.add_argument("--no-verify", dest="verify", action="store_false",
                     help="skip verification gate even if enabled in config")
    pin.set_defaults(fn=cmd_ingest)

    pq = sub.add_parser("query", help="ask a question against the wiki")
    pq.add_argument("query")
    pq.add_argument("--file", action="store_true", help="save the answer as a wiki page")
    pq.set_defaults(fn=cmd_query)

    pl = sub.add_parser("lint", help="health-check the wiki (deterministic + agent)")
    pl.add_argument("--skip-strict", dest="skip_strict", action="store_true",
                    help="run the agent lint even if strict lint reports violations")
    pl.set_defaults(fn=cmd_lint)

    psl = sub.add_parser("strict-lint",
                         help="deterministic-only lint (no agent); used by `commit` as a gate")
    psl.set_defaults(fn=cmd_strict_lint)

    pd = sub.add_parser(
        "digest",
        help="batched cross-reference synthesis across recent daily/source entries",
    )
    pd.add_argument(
        "--since",
        default=None,
        help="window start: duration like '7d'/'2w' or ISO date '2026-04-01'",
    )
    pd.add_argument(
        "--since-last",
        action="store_true",
        help="start from the most recent digest's date (default when --since not given)",
    )
    pd.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="fallback window size in days when no prior digest exists (overrides digest.default_window_days)",
    )
    pd.add_argument(
        "--no-commit",
        action="store_true",
        help="skip auto-commit regardless of config",
    )
    pd.set_defaults(fn=cmd_digest)

    ppl = sub.add_parser("pull", help="pull files from configured source folders into the staging inbox")
    ppl.add_argument(
        "--kind",
        default="all",
        help="which configured source to pull from (e.g. docs, audio, all). See config.local.yaml.",
    )
    ppl.add_argument("--source", default=None, help="override: pull from this single folder, ignoring --kind")
    ppl.add_argument("--keep", action="store_true", help="copy instead of moving (leaves source files in place)")
    ppl.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    ppl.set_defaults(fn=cmd_pull)

    ppd = sub.add_parser(
        "pending",
        help="list captures staged in inbox/ (captured, parsed) awaiting ingest",
    )
    ppd.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    ppd.set_defaults(fn=cmd_pending)

    pv = sub.add_parser("serve", help="launch the read-only web UI")
    pv.add_argument("--host", default="127.0.0.1")
    pv.add_argument("--port", type=int, default=8787)
    pv.add_argument("--reload", action=argparse.BooleanOptionalAction, default=True)
    pv.set_defaults(fn=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
