# CLAUDE.md — Claude Code instructions for this wiki

Welcome. This is a personal LLM wiki. Your job is to maintain it.

**First step, always:** read `AGENTS.md`. It's the canonical contract — schema, workflows, hard rules. Everything below is Claude-Code-specific layering on top.

## Project summary

- `llm-wiki.md` — the philosophy. Read once if new to the pattern.
- `AGENTS.md` — the operational contract. Read every session.
- `wiki/` — the markdown graph you maintain. Canonical.
- `raw/` — immutable user inputs across modalities. Do not edit originals.
- `cache/` — derived indexes. Disposable; regeneratable via `mind rebuild-cache`.
- `config.yaml` — tunable knobs (preprocessing toggles, agent command, defaults).
- `bin/mind` — the CLI. Your primary tool.
- `ui/` — the read-only human browsing app. Irrelevant to you.

## Per-page guidance

Page-scoped rules live in a `<details><summary>Agent notes</summary>…</details>` block at the top of each page's body. Read it before editing that page. Hidden by default in the UI, revealable on click; strict-lint (`A9-agent-notes-format`) validates the canonical format.

## Hard rules (same as AGENTS.md, repeated so you see them inline)

1. `wiki/` is the only source of truth. `cache/` is disposable.
2. Never modify files in `raw/` except to add sibling text artifacts from preprocessing.
3. Prefer standard markdown links `[text](slug.md)` when writing. Tolerate `[[wikilinks]]` on read.
4. Every ingest updates `wiki/index.md` and appends one line to `wiki/log.md`.
5. Run `mind commit` after every mutating operation (auto-commit is on by default per `config.yaml`).
6. When scope, taxonomy, or structure is unclear — **ask the user** before guessing.

## Claude-Code-specific tips

- **Start broad, then narrow.** Your first tool call in any non-trivial task should be `mind index --titles-only` (cheap scan of the whole wiki) followed by `mind search "<terms>"` or `mind index --tag <t>`. Only read full page bodies after you've shortlisted them. This keeps context small as the wiki grows.
- **Batch edits.** When an ingest touches many pages (common), use one TodoWrite-style task per page, but make the Edits in parallel tool calls to save round trips.
- **Don't dump "See also" sections.** Link related pages inline in prose, the way a good Wikipedia article does. Back-link from the other side too.
- **Preserve frontmatter.** When you Edit a page, be careful not to mangle the YAML. If you change outbound body links, update the `related:` list to match.
- **Ask, don't guess, for taxonomy changes.** Creating a new `tags[0]` value or restructuring a heavily-linked page — surface to the user.
- **Preserve naive framing.** Exploration pages keep the user's voice; established vocabulary lives on separate concept pages linked inline. Don't silently translate. See `AGENTS.md` → "Handling naive-structure input".
- **Use `mind graph --from <slug> --depth 2`** to see a page's neighborhood before you restructure it.
- **Check `mind pending` when the user mentions voice capture.** Unconfirmed clips in `inbox/` — especially `ui-voice_*` files captured via the web UI's record button — are the user's newest input and the most likely context for what they're asking about. `state: parsed` means a sibling `.transcript.md` is ready to read; `state: captured` means only bytes landed and whisper hasn't run yet.

## Common CLI one-liners

```bash
./bin/mind init                         # bootstrap a fresh clone (seed wiki/, data repo, cache)
./bin/mind index --titles-only          # quick map of the wiki
./bin/mind index --tag concept          # filter by frontmatter tag
./bin/mind search "Anthropic cache"     # FTS over bodies
./bin/mind graph --from claude-code --depth 2 --format mermaid
./bin/mind rebuild-cache                # after manual edits
./bin/mind commit -m "note on X"        # manual commit
./bin/mind serve                        # launch the web UI (for the human)
./bin/mind ingest raw.mp3 --no-verify   # skip the preprocessed-text review gate
./bin/mind ingest raw.mp3 --batch       # non-interactive; also implies --no-verify
./bin/mind pull -y                      # non-interactive; skip confirm prompt
./bin/mind pending                      # list captures staged in inbox/ (captured vs parsed)
./bin/mind pending --json               # machine-readable form
./bin/mind digest                       # synthesize across daily/source entries since last digest
./bin/mind digest --since 14d           # explicit window (Nd/Nw or ISO date)
```

**Agent mode:** every confirmation prompt has a bypass flag — `pull -y`, `ingest --batch` / `--no-verify`. Commands refuse to prompt on non-TTY stdin; pass the flag up front. Prefer these when invoking `mind` from a script or agent loop.


When the user runs `mind ingest` on audio / image / PDF input, the CLI pauses after preprocessing and shows the sibling text artifact to the user. If the user types natural-language feedback (instead of `y`/`n`), you will be spawned with a narrow "correct this sibling file" prompt — in that mode, edit only the specified sibling and do not touch `wiki/`.

## Workflows

See `AGENTS.md` sections "Ingest", "Query", "Digest", and "Lint". They apply to you unchanged.

## Notes

- This project runs on macOS and Windows. Per-machine differences (source folders, whisper paths, model choice) live in `config.local.yaml` (gitignored) — there is no platform switch in code. System tools (`whisper-cli` for audio) may or may not be installed — check `config.yaml` for what preprocessing is enabled. If a preprocessor is enabled but its binary is missing, fall back gracefully and leave a note in `wiki/log.md`. Images are NOT OCR'd; the ingest agent reads them directly via its vision model and honors any user-provided instructions stored in the sibling `.caption.md` frontmatter.
- **whisper.cpp is gitignored** and per-machine. Mac: `brew install whisper-cpp`. Windows: prebuilt release zip or cmake build (see README). `bin/mind` auto-discovers the binary and model; see README for the resolution order. Don't commit binaries or models.
- This project has no network dependencies. All state lives on disk.
- **Two git repos share this directory** (see README "Two repos, one directory"): `.git` is the code repo (publishable), `.git-data` is the data repo tracking `wiki/` + `raw/` + `config.wiki.yaml` (private, local-only by default). The shipped `.gitignore` hides those paths from the code repo; `mind commit` targets the data repo and stages with `git add -f` to bypass it. Never stage them in the code repo (never use `git add -f` there), and never add a public remote to `.git-data`.
