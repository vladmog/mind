"""mind — human browsing UI.

Reads from wiki/ and cache/. Also owns the voice-capture staging flow:
/record lets a user record audio, review playback, then save into inbox/
and preprocess to a sibling .transcript.md. Nothing touches wiki/ or raw/
— `mind ingest` promotes the inbox artifacts the normal way.
If cache is missing, pages still render (but backlinks / tag filters /
search may be empty until `mind rebuild-cache` runs).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from markdown_it import MarkdownIt
from markupsafe import escape
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from bin.mind import (
    _INBOX_AUDIO_EXTS,
    _SIBLING_SUFFIXES,
    _do_pull,
    _preprocess_audio,
    _preprocess_image,
    _rel,
    _transcript_preview,
    load_config,
)

ROOT = Path(__file__).resolve().parent.parent
WIKI = ROOT / "wiki"
CACHE = ROOT / "cache"
RAW = ROOT / "raw"
INBOX = ROOT / "inbox"
# Fresh-clone tolerance: the StaticFiles mount below requires raw/ to exist
# at import time (check_dir), and raw/ ships empty (gitignored data dir).
RAW.mkdir(exist_ok=True)

templates = Jinja2Templates(directory=str(ROOT / "ui" / "templates"))

# Single-user, read-only, locally-authored content — raw HTML (<details>/<summary>)
# is safe to pass through. Revisit with an allowlist sanitizer (e.g. bleach) if the
# wiki ever renders untrusted content.
md = MarkdownIt("commonmark", {"html": True, "linkify": True, "typographer": True}).enable("table")


# Rewrite internal relative link hrefs (e.g. "log.md", "log.md#sec") at the token
# level, so fenced / inline code content (which isn't parsed as links) is untouched.
_default_link_open = md.renderer.rules.get("link_open")

def _rewrite_link_open(tokens, idx, options, env):
    token = tokens[idx]
    href = token.attrGet("href") or ""
    if href and not re.match(r"^(?:[a-z]+:|#|/)", href):
        target = href.split("#", 1)[0]
        anchor = href[len(target):]
        slug = target.split("/")[-1].removesuffix(".md")
        token.attrSet("href", f"/p/{slug}{anchor}")
    if _default_link_open:
        return _default_link_open(tokens, idx, options, env)
    return md.renderer.renderToken(tokens, idx, options, env)

md.renderer.rules["link_open"] = _rewrite_link_open


FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n(.*)$", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:\|([^\]#]*))?(?:#[^\]]*)?\]\]")
H1_CLOSE_RE = re.compile(r"</h1>", re.IGNORECASE)


def _split_fm(text: str) -> tuple[dict, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        import yaml
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return fm, m.group(2)


def _load_index() -> list[dict]:
    p = CACHE / "index.json"
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def _load_links() -> dict[str, dict]:
    p = CACHE / "links.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def _resolve_wikilinks(body: str) -> str:
    """Convert [[target]] / [[target|alias]] to standard markdown links so markdown-it parses them."""
    def repl(m: re.Match) -> str:
        target = m.group(1).strip()
        alias = (m.group(2) or "").strip() or target
        return f"[{alias}]({target})"
    return WIKILINK_RE.sub(repl, body)


def _render_page(page_path: Path) -> tuple[dict, str]:
    text = page_path.read_text(encoding="utf-8")
    fm, body = _split_fm(text)
    # Only wikilinks need pre-processing; standard [text](slug.md) links are
    # rewritten at render time by _rewrite_link_open so code blocks stay clean.
    body = _resolve_wikilinks(body)
    html = md.render(body)
    summary = fm.get("summary")
    if summary:
        lede = f'<p class="page-summary">{escape(summary)}</p>'
        html, n = H1_CLOSE_RE.subn(f"</h1>{lede}", html, count=1)
        if n == 0:
            html = lede + html
    return fm, html


# ---------- routes ----------

def landing(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "landing.html", {})


def all_entries(request: Request) -> HTMLResponse:
    tag = request.query_params.get("tag")
    type = request.query_params.get("type")
    sort = request.query_params.get("sort") or "connections"
    if sort not in ("connections", "title", "updated"):
        sort = "connections"
    entries = _load_index()
    hidden_slugs = {"index", "log", "contradictions"}
    entries = [e for e in entries if e["slug"] not in hidden_slugs]
    if tag:
        entries = [e for e in entries if tag in (e.get("tags") or [])]
    if type:
        entries = [e for e in entries if e.get("type") == type]
    links = _load_links()
    for e in entries:
        ln = links.get(e["slug"], {})
        e["connections"] = len(ln.get("in", [])) + len(ln.get("out", []))
    if sort == "title":
        entries.sort(key=lambda e: (e.get("title") or e["slug"]).lower())
    elif sort == "updated":
        entries.sort(key=lambda e: (e.get("updated") or "", e["slug"]), reverse=True)
    else:
        entries.sort(key=lambda e: (-e["connections"], (e.get("title") or e["slug"]).lower()))
    tag_counts: dict[str, int] = defaultdict(int)
    type_counts: dict[str, int] = defaultdict(int)
    for e in _load_index():
        for t in (e.get("tags") or []):
            tag_counts[t] += 1
        type_counts[e.get("type") or "untyped"] += 1
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "entries": entries,
            "active_tag": tag,
            "active_type": type,
            "active_sort": sort,
            "tag_counts": dict(sorted(tag_counts.items(), key=lambda kv: -kv[1])),
            "type_counts": dict(sorted(type_counts.items(), key=lambda kv: -kv[1])),
        },
    )


def page(request: Request) -> HTMLResponse:
    slug = request.path_params["slug"]
    path = WIKI / f"{slug}.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"no page {slug!r}")
    fm, html = _render_page(path)
    links = _load_links().get(slug, {})
    index = {e["slug"]: e for e in _load_index()}
    inbound = [{"slug": s, "title": index.get(s, {}).get("title", s)} for s in links.get("in", [])]
    outbound = [{"slug": s, "title": index.get(s, {}).get("title", s)} for s in links.get("out", [])]
    typed = {
        kind: [{"slug": s, "title": index.get(s, {}).get("title", s)} for s in targets]
        for kind, targets in links.get("typed", {}).items()
    }
    return templates.TemplateResponse(
        request,
        "page.html",
        {
            "slug": slug,
            "fm": fm,
            "html": html,
            "inbound": inbound,
            "outbound": outbound,
            "typed": typed,
        },
    )


def search(request: Request) -> HTMLResponse:
    q = request.query_params.get("q", "")
    results: list[dict] = []
    if q:
        db_path = CACHE / "search.db"
        if db_path.exists():
            con = sqlite3.connect(db_path)
            try:
                rows = con.execute(
                    "SELECT slug, title, snippet(pages, 3, '<mark>', '</mark>', '…', 16) "
                    "FROM pages WHERE pages MATCH ? ORDER BY bm25(pages) LIMIT 50",
                    (q,),
                ).fetchall()
            except sqlite3.OperationalError as e:
                raise HTTPException(status_code=400, detail=f"FTS error: {e}")
            finally:
                con.close()
            results = [{"slug": s, "title": t, "snippet": sn} for s, t, sn in rows]
    return templates.TemplateResponse(
        request,
        "search.html",
        {"q": q, "results": results},
    )


_SUGGEST_TOKEN_RE = re.compile(r"[^\w]+", re.UNICODE)


def suggest(request: Request) -> JSONResponse:
    q = request.query_params.get("q", "").strip()
    if not q:
        return JSONResponse([])
    tokens = [t for t in _SUGGEST_TOKEN_RE.split(q) if t]
    if not tokens:
        return JSONResponse([])
    expr_title = " ".join(f"title: {t}*" for t in tokens)
    expr_broad = " ".join(f"{{title tags body}}: {t}*" for t in tokens)
    db_path = CACHE / "search.db"
    if not db_path.exists():
        return JSONResponse([])
    limit = 8
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    con = sqlite3.connect(db_path)
    try:
        try:
            rows = con.execute(
                "SELECT slug, title FROM pages WHERE pages MATCH ? "
                "ORDER BY length(title) ASC, title ASC LIMIT ?",
                (expr_title, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for s, t in rows:
            if s not in seen:
                seen.add(s)
                out.append((s, t))

        if len(out) < limit:
            try:
                rows = con.execute(
                    "SELECT slug, title FROM pages WHERE pages MATCH ? "
                    "ORDER BY bm25(pages, 0.0, 10.0, 2.0, 1.0) LIMIT ?",
                    (expr_broad, limit * 2),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for s, t in rows:
                if s in seen:
                    continue
                seen.add(s)
                out.append((s, t))
                if len(out) >= limit:
                    break
    finally:
        con.close()
    return JSONResponse([{"slug": s, "title": t} for s, t in out])


def graph(request: Request) -> HTMLResponse:
    links = _load_links()
    index = {e["slug"]: e for e in _load_index()}
    hidden = {"log", "index"}
    hidden |= {s for s, e in index.items() if e.get("type") == "daily"}

    # contradictions is only shown if it's wired up to a non-hidden node
    c = "contradictions"
    c_outs = [d for d in links.get(c, {}).get("out", []) if d not in hidden and d != c]
    c_ins = any(
        c in links.get(other, {}).get("out", [])
        for other in links
        if other not in hidden and other != c
    )
    if not c_outs and not c_ins:
        hidden.add(c)

    nodes = [
        {
            "id": s,
            "label": index.get(s, {}).get("title", s),
            "type": index.get(s, {}).get("type", "untyped"),
            "tags": index.get(s, {}).get("tags", []),
        }
        for s in links.keys()
        if s not in hidden
    ]
    edges: list[dict] = []
    for src, payload in links.items():
        if src in hidden:
            continue
        for dst in payload.get("out", []):
            if dst in hidden:
                continue
            edges.append({"source": src, "target": dst})
    return templates.TemplateResponse(
        request,
        "graph.html",
        {"graph_data": json.dumps({"nodes": nodes, "edges": edges})},
    )


def raw_manifest(request: Request) -> PlainTextResponse:
    p = CACHE / "raw-manifest.json"
    return PlainTextResponse(p.read_text(encoding="utf-8") if p.exists() else "{}")


# ---------- voice entry: capture → inbox/, parse on demand ----------

_RECORD_EXT_MAP = {
    "audio/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
_RECORD_MAX_BYTES = 50 * 1024 * 1024  # 50 MB sanity cap
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def record_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "record.html", {})


async def record_upload(request: Request) -> JSONResponse:
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    ext = _RECORD_EXT_MAP.get(mime, ".webm")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    if len(data) > _RECORD_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"body exceeds {_RECORD_MAX_BYTES} bytes")
    # Inbox filename: no date prefix — `mind ingest` adds today's date when
    # promoting into raw/. Two captures in the same second get -1, -2 suffixes.
    stamp = datetime.now().strftime("%H%M%S")
    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / f"ui-voice_{stamp}{ext}"
    n = 1
    while dest.exists():
        dest = INBOX / f"ui-voice_{stamp}-{n}{ext}"
        n += 1
    dest.write_bytes(data)
    return JSONResponse(
        {
            "ok": True,
            "filename": dest.name,
            "path": _rel(dest),
            "bytes": len(data),
            "mime": mime or "unknown",
        }
    )


def _resolve_inbox_file(filename: str) -> Path:
    if not _SAFE_FILENAME_RE.match(filename or ""):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = (INBOX / filename).resolve()
    inbox_resolved = INBOX.resolve()
    if inbox_resolved not in target.parents and target.parent != inbox_resolved:
        raise HTTPException(status_code=400, detail="path escapes inbox/")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"no inbox file {filename!r}")
    return target


async def record_parse(request: Request) -> JSONResponse:
    body = await request.json()
    filename = (body or {}).get("filename", "")
    audio = _resolve_inbox_file(filename)
    if audio.suffix.lower() not in _INBOX_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"not an audio extension: {audio.suffix}")
    cfg = load_config()
    audio_cfg = cfg["preprocessing"]["audio"]
    if not audio_cfg.get("enabled"):
        raise HTTPException(status_code=400, detail="audio preprocessing disabled in config.yaml")
    sibling = await run_in_threadpool(_preprocess_audio, audio, audio_cfg)
    if sibling is None or not sibling.exists():
        raise HTTPException(status_code=500, detail="preprocessing produced no sibling")
    return JSONResponse(
        {
            "ok": True,
            "filename": audio.name,
            "sibling": sibling.name,
            "transcript": _transcript_preview(sibling),
        }
    )


_EXT_TO_MIME = {v: k for k, v in _RECORD_EXT_MAP.items()}


def record_audio(request: Request) -> FileResponse:
    filename = request.path_params["filename"]
    target = _resolve_inbox_file(filename)
    ext = target.suffix.lower()
    if ext not in _INBOX_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"not an audio extension: {ext}")
    return FileResponse(target, media_type=_EXT_TO_MIME.get(ext, "application/octet-stream"))


def _save_sibling_body(sibling: Path, new_body: str) -> None:
    """Rewrite a sibling's body, preserving its frontmatter and H1."""
    existing = sibling.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(existing)
    if not m:
        raise HTTPException(status_code=500, detail="sibling missing frontmatter")
    fm_raw = existing[: m.start(2)]
    after = m.group(2)
    h1_match = re.match(r"\n*(# [^\n]*)\n", after)
    parts = [fm_raw]
    if h1_match:
        parts.append("\n" + h1_match.group(1) + "\n")
    parts.append("\n" + new_body.rstrip() + "\n")
    sibling.write_text("".join(parts), encoding="utf-8")


async def record_save_transcript(request: Request) -> JSONResponse:
    filename = request.path_params["filename"]
    audio = _resolve_inbox_file(filename)
    if audio.suffix.lower() not in _INBOX_AUDIO_EXTS:
        raise HTTPException(status_code=400, detail=f"not an audio extension: {audio.suffix}")
    sibling = audio.with_suffix(".transcript.md")
    if not sibling.is_file():
        raise HTTPException(status_code=404, detail="no transcript sibling — parse first")
    payload = await request.json()
    new_body = (payload or {}).get("body")
    if not isinstance(new_body, str):
        raise HTTPException(status_code=400, detail="body must be a string")
    _save_sibling_body(sibling, new_body)
    return JSONResponse(
        {
            "ok": True,
            "filename": audio.name,
            "sibling": sibling.name,
            "transcript": _transcript_preview(sibling),
        }
    )


async def record_delete(request: Request) -> JSONResponse:
    filename = request.path_params["filename"]
    target = _resolve_inbox_file(filename)
    removed = [target.name]
    target.unlink()
    for suf in _SIBLING_SUFFIXES:
        candidate = INBOX / (target.stem + suf)
        if candidate.is_file():
            candidate.unlink()
            removed.append(candidate.name)
    return JSONResponse({"ok": True, "removed": removed})


# ---------- image entry: capture → inbox/ + instructions-carrier sibling ----------

_IMAGE_EXT_MAP = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/heic": ".heic",
    "image/heif": ".heif",
}
_INBOX_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".heif"}
_IMAGE_MAX_BYTES = 25 * 1024 * 1024
_INSTRUCTIONS_MAX_CHARS = 500


def _sanitize_stem(raw: str) -> str | None:
    s = (raw or "").strip().replace(" ", "_")
    s = re.sub(r"[^A-Za-z0-9._-]", "", s)
    s = re.sub(r"_+", "_", s)
    s = s.strip("_-.")
    if not s or not _SAFE_FILENAME_RE.match(s):
        return None
    return s


def image_file(request: Request) -> FileResponse:
    filename = request.path_params["filename"]
    target = _resolve_inbox_file(filename)
    ext = target.suffix.lower()
    if ext not in _INBOX_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"not an image extension: {ext}")
    mime = next((k for k, v in _IMAGE_EXT_MAP.items() if v == ext), "application/octet-stream")
    return FileResponse(target, media_type=mime)


async def image_save_instructions(request: Request) -> JSONResponse:
    filename = request.path_params["filename"]
    image = _resolve_inbox_file(filename)
    if image.suffix.lower() not in _INBOX_IMAGE_EXTS:
        raise HTTPException(status_code=400, detail=f"not an image extension: {image.suffix}")
    sibling = image.with_suffix(".caption.md")
    if not sibling.is_file():
        raise HTTPException(status_code=404, detail="no caption sibling — upload first")
    payload = await request.json()
    new_instr = (payload or {}).get("instructions")
    if not isinstance(new_instr, str):
        raise HTTPException(status_code=400, detail="instructions must be a string")
    new_instr = new_instr.strip()
    if len(new_instr) > _INSTRUCTIONS_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"instructions exceed {_INSTRUCTIONS_MAX_CHARS} chars")
    if new_instr:
        _inject_instructions(sibling, new_instr)
    else:
        _clear_instructions(sibling)
    return JSONResponse(
        {
            "ok": True,
            "filename": image.name,
            "sibling": sibling.name,
            "instructions": new_instr,
            "transcript": _transcript_preview(sibling),
        }
    )


async def image_upload(request: Request) -> JSONResponse:
    mime = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    ext = _IMAGE_EXT_MAP.get(mime)
    if ext is None:
        raise HTTPException(status_code=400, detail=f"unsupported image type: {mime!r}")
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty body")
    if len(data) > _IMAGE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"body exceeds {_IMAGE_MAX_BYTES} bytes")

    raw_stem = request.headers.get("x-filename-stem", "")
    stem = _sanitize_stem(raw_stem) if raw_stem else None
    if stem is None:
        stem = f"ui-image_{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    raw_instr = request.headers.get("x-instructions", "").strip()
    if len(raw_instr) > _INSTRUCTIONS_MAX_CHARS:
        raise HTTPException(status_code=400, detail=f"instructions exceed {_INSTRUCTIONS_MAX_CHARS} chars")

    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / f"{stem}{ext}"
    n = 1
    while dest.exists():
        dest = INBOX / f"{stem}-{n}{ext}"
        n += 1
    dest.write_bytes(data)

    # Write the caption sibling: a minimal "instructions-carrier" (no OCR).
    # The ingest agent reads the raw image directly via its vision model.
    cfg = load_config()
    image_cfg = cfg["preprocessing"]["image"]
    sibling = dest.with_suffix(".caption.md")
    try:
        if image_cfg.get("enabled", True):
            await run_in_threadpool(_preprocess_image, dest, image_cfg)
    except Exception as e:
        sibling.write_text(
            _fallback_image_sibling(dest, f"(preprocess error: {e})"),
            encoding="utf-8",
        )

    # If user attached instructions at capture time, splice them into the sibling.
    if raw_instr and sibling.exists():
        _inject_instructions(sibling, raw_instr)

    return JSONResponse(
        {
            "ok": True,
            "filename": dest.name,
            "path": _rel(dest),
            "bytes": len(data),
            "mime": mime,
            "stem": stem,
            "sibling": sibling.name if sibling.exists() else None,
            "has_instructions": bool(raw_instr),
            "sibling_bytes": sibling.stat().st_size if sibling.exists() else 0,
        }
    )


def _fallback_image_sibling(source: Path, note: str) -> str:
    """Error-path fallback when `_preprocess_image` raises.

    The canonical stub is written by `bin.mind._preprocess_image` (the
    "instructions-carrier" sibling). This is only used if that call failed.
    """
    today = datetime.now().date().isoformat()
    return (
        f"---\n"
        f"sibling_of: {source.name}\n"
        f"preprocessor: ui-upload\n"
        f"created: {today}\n"
        f"agent_pass: pending\n"
        f"---\n\n"
        f"# Preprocessed text: {source.name}\n\n"
        f"{note}\n"
    )


_INSTRUCTIONS_BODY_RE = re.compile(
    r"\*\*Instructions \(for ingest agent\):\*\*[^\n]*\n+", re.MULTILINE
)


def _inject_instructions(sibling: Path, instructions: str) -> None:
    """Rewrite the sibling so its frontmatter has `instructions: <json>` and
    its body has a single leading instructions block. Idempotent — calling it
    repeatedly replaces the previous instructions, it doesn't stack them."""
    text = sibling.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        today = datetime.now().date().isoformat()
        sibling.write_text(
            f"---\n"
            f"sibling_of: {sibling.with_suffix('').name}\n"
            f"preprocessor: ui-upload\n"
            f"created: {today}\n"
            f"agent_pass: pending\n"
            f"instructions: {json.dumps(instructions)}\n"
            f"---\n\n"
            f"**Instructions (for ingest agent):** {instructions}\n\n"
            f"{text}",
            encoding="utf-8",
        )
        return
    fm_raw = m.group(1)
    body = m.group(2)
    fm_lines = [
        ln for ln in fm_raw.splitlines()
        if not ln.startswith("instructions:") and not ln.startswith("agent_pass:")
    ]
    fm_lines.append("agent_pass: pending")
    fm_lines.append(f"instructions: {json.dumps(instructions)}")
    new_fm = "\n".join(fm_lines) + "\n"
    # Strip any existing instructions block, then insert a fresh one after the h1.
    body_stripped = _INSTRUCTIONS_BODY_RE.sub("", body.lstrip("\n"), count=1)
    h1 = ""
    rest = body_stripped
    h1_match = re.match(r"(# [^\n]*\n)", body_stripped)
    if h1_match:
        h1 = h1_match.group(1)
        rest = body_stripped[h1_match.end():]
    new_body = f"\n{h1}\n**Instructions (for ingest agent):** {instructions}\n\n{rest.lstrip()}"
    sibling.write_text(f"---\n{new_fm}---\n{new_body}", encoding="utf-8")


def _clear_instructions(sibling: Path) -> None:
    """Remove `instructions:` from frontmatter and the instructions block from
    body. Inverse of `_inject_instructions`."""
    text = sibling.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return
    fm_raw = m.group(1)
    body = m.group(2)
    fm_lines = [ln for ln in fm_raw.splitlines() if not ln.startswith("instructions:")]
    new_fm = "\n".join(fm_lines) + "\n"
    new_body = _INSTRUCTIONS_BODY_RE.sub("", body, count=1)
    sibling.write_text(f"---\n{new_fm}---\n{new_body}", encoding="utf-8")


def _read_instructions(sibling: Path) -> str:
    """Read the `instructions:` field from a sibling's frontmatter, or ""."""
    try:
        fm, _ = _split_fm(sibling.read_text(encoding="utf-8"))
    except Exception:
        return ""
    val = fm.get("instructions")
    return val if isinstance(val, str) else ""


# ---------- text entry: typed input → inbox/ (raw file IS the content, no sibling) ----------

_INBOX_TEXT_EXTS = {".txt"}
_TEXT_MAX_CHARS = 100_000


async def text_upload(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    body = (payload or {}).get("body")
    if not isinstance(body, str):
        raise HTTPException(status_code=400, detail="body must be a string")
    body = body.strip("\n")
    if not body.strip():
        raise HTTPException(status_code=400, detail="body must not be empty")
    if len(body) > _TEXT_MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"body exceeds {_TEXT_MAX_CHARS} chars")

    raw_stem = (payload or {}).get("stem", "")
    stem = _sanitize_stem(raw_stem) if isinstance(raw_stem, str) and raw_stem else None
    if stem is None:
        stem = f"ui-text_{datetime.now().strftime('%H%M%S')}"

    INBOX.mkdir(parents=True, exist_ok=True)
    dest = INBOX / f"{stem}.txt"
    n = 1
    while dest.exists():
        dest = INBOX / f"{stem}-{n}.txt"
        n += 1
    data = (body.rstrip() + "\n").encode("utf-8")
    dest.write_bytes(data)
    return JSONResponse(
        {
            "ok": True,
            "filename": dest.name,
            "path": _rel(dest),
            "bytes": len(data),
            "stem": stem,
        }
    )


async def text_save(request: Request) -> JSONResponse:
    filename = request.path_params["filename"]
    target = _resolve_inbox_file(filename)
    if target.suffix.lower() not in _INBOX_TEXT_EXTS:
        raise HTTPException(status_code=400, detail=f"not a text extension: {target.suffix}")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    new_body = (payload or {}).get("body")
    if not isinstance(new_body, str):
        raise HTTPException(status_code=400, detail="body must be a string")
    new_body = new_body.strip("\n")
    if not new_body.strip():
        raise HTTPException(status_code=400, detail="body must not be empty")
    if len(new_body) > _TEXT_MAX_CHARS:
        raise HTTPException(status_code=413, detail=f"body exceeds {_TEXT_MAX_CHARS} chars")
    target.write_text(new_body.rstrip() + "\n", encoding="utf-8")
    return JSONResponse(
        {
            "ok": True,
            "filename": target.name,
            "transcript": _transcript_preview(target),
        }
    )


async def pull_sources(request: Request) -> JSONResponse:
    body: dict = {}
    if await request.body():
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
    kind = (body or {}).get("kind", "audio")
    keep = bool((body or {}).get("keep", False))
    if not isinstance(kind, str) or not kind:
        raise HTTPException(status_code=400, detail="kind must be a non-empty string")
    try:
        result = await run_in_threadpool(_do_pull, kind=kind, keep=keep)
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return JSONResponse({"ok": True, **result})


def pending_list(request: Request) -> JSONResponse:
    items: list[dict] = []
    if INBOX.is_dir():
        for p in sorted(INBOX.iterdir(), key=lambda x: x.stat().st_mtime if x.is_file() else 0, reverse=True):
            if not p.is_file() or p.name.startswith("."):
                continue
            if p.name.endswith(_SIBLING_SUFFIXES):
                continue
            ext = p.suffix.lower()
            if ext in _INBOX_AUDIO_EXTS:
                sibling = p.with_suffix(".transcript.md")
                kind = "audio"
            elif ext in _INBOX_IMAGE_EXTS:
                sibling = p.with_suffix(".caption.md")
                kind = "image"
            elif ext in _INBOX_TEXT_EXTS:
                sibling = None
                kind = "text"
            else:
                sibling = None
                kind = "other"
            state = "captured"
            transcript = None
            instructions = None
            if kind == "text":
                state = "parsed"
                transcript = _transcript_preview(p)
            elif sibling and sibling.exists() and sibling.stat().st_size > 0:
                state = "parsed"
                transcript = _transcript_preview(sibling)
                if kind == "image":
                    instructions = _read_instructions(sibling)
            elif sibling is None:
                state = "other"
            st = p.stat()
            items.append(
                {
                    "filename": p.name,
                    "path": _rel(p),
                    "state": state,
                    "kind": kind,
                    "bytes": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "transcript": transcript,
                    "instructions": instructions,
                }
            )
    return JSONResponse({"items": items})


app = Starlette(
    routes=[
        Mount("/static", StaticFiles(directory=str(ROOT / "ui" / "static")), name="static"),
        Mount("/raw", StaticFiles(directory=str(RAW)), name="raw"),
        Route("/", landing),
        Route("/all", all_entries),
        Route("/p/{slug}", page),
        Route("/api/suggest", suggest),
        Route("/search", search),
        Route("/graph", graph),
        Route("/raw-manifest", raw_manifest),
        Route("/record", record_page),
        Route("/api/record", record_upload, methods=["POST"]),
        Route("/api/record/parse", record_parse, methods=["POST"]),
        Route("/api/record/{filename}/audio", record_audio, methods=["GET"]),
        Route("/api/record/{filename}/transcript", record_save_transcript, methods=["PUT"]),
        Route("/api/record/{filename}", record_delete, methods=["DELETE"]),
        Route("/api/image", image_upload, methods=["POST"]),
        Route("/api/image/{filename}", image_file, methods=["GET"]),
        Route("/api/image/{filename}/instructions", image_save_instructions, methods=["PUT"]),
        Route("/api/text", text_upload, methods=["POST"]),
        Route("/api/text/{filename}", text_save, methods=["PUT"]),
        Route("/api/pending", pending_list),
        Route("/api/pull", pull_sources, methods=["POST"]),
    ],
)
