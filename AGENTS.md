# AGENTS.md — Contract for any agent operating on this wiki

You are a disciplined maintainer of a personal LLM wiki. The philosophy lives in `llm-wiki.md` (read it once if you haven't). This file is your operational contract. Claude Code sees a thin wrapper at `CLAUDE.md` that defers here.

## The three layers

1. **`raw/`** — immutable inputs across all modalities (text, audio, image, pdf, video). You MAY add sibling text artifacts produced by preprocessing (e.g. `foo.mp3` → `foo.transcript.md`). You MUST NOT modify the originals.
2. **`wiki/`** — the LLM-maintained markdown graph. **This is the only source of truth.** You own it. You create, update, cross-reference, and maintain it.
3. **`cache/`** — derived indexes (JSON, SQLite). Disposable. Any cache file must be regeneratable from `wiki/` via `mind rebuild-cache`. Never treat cache as authoritative.

A separate **agent contract** (`AGENTS.md`, `CLAUDE.md`) and a **CLI** (`bin/mind`) live at the root to support you.

## Per-page guidance

Page-scoped rules live in a `<details><summary>Agent notes</summary>…</details>` block at the top of each page's body, placed directly after the H1. Read it before editing that page. Hidden by default in rendered markdown, visible when expanded; always greppable as plain text. Strict-lint validates the canonical format (rule `A9-agent-notes-format`).

## Hard rules

- `wiki/` is canonical. If something is in `cache/` but not inferable from `wiki/`, that is a bug.
- Never modify `raw/` originals. Only sibling text files (`*.transcript.md`, `*.text.md`, `*.caption.md`) may be added.
- Prefer **standard markdown links** `[text](slug.md)` over `[[wikilinks]]` when you write. Tolerate both on read.
- Every ingest touches `wiki/index.md` (updates or adds an entry) and appends one line to `wiki/log.md`.
- Run `mind commit` after every mutating operation (ingest, query-with-filing, lint). Config default is auto-commit on.
- When in doubt about scope or structure, ask the user before guessing. See "When to ask" below.

## Frontmatter schema (every wiki page)

```yaml
---
title: Claude Code
slug: claude-code
created: 2026-04-18
updated: 2026-04-18
tags: [concept, software]   # tags[0] MUST be the canonical type: entity | concept | source | daily | query | digest | meta
sources: [raw/2026-04-18_article-foo.md]
related: [anthropic.md, mcp.md]      # mirror of body outbound links; rebuild-cache can regenerate
---
```

Rules:
- Body uses standard markdown links. **Exactly one H1 per page** and it must match `title` verbatim.
- Filename is `<slug>.md`; slug MUST match the frontmatter `slug` field. Lowercase kebab-case.
- `tags[0]` is the page type (one of the canonical seven above).
- `tags[1:]` must come from the canonical secondary-tag vocabulary (`lint.canonical_tags` — generic defaults in `config.yaml`, per-wiki extensions in `config.wiki.yaml`, which is tracked by the data repo). Adding a new tag is a deliberate config bump, not a freelance decision — surface it to the user.
- `created` and `updated` are ISO `YYYY-MM-DD`. `updated >= created`.
- `related:` must be a subset of the page's body outbound links (catalog/hub pages listed in `lint.meta_exempt_slugs` are exempt — they accumulate inbound links by nature).
- **`summary:`** is required on `tags[0]: daily` pages — a short phrase (≤ 80 chars) naming the entry's subject. Not a sentence; no trailing period; modality (`voice`, etc.) is already implied by the tags so don't repeat it. Other page types may set `summary:` optionally. Referring pages use it as link text; see the daily link-text convention below.

## CLI you can shell out to

All commands live in `bin/mind`. From the project root:

| Command | Use |
|---|---|
| `mind init` | Bootstrap a fresh clone: creates `wiki/` from `seed/wiki/` templates, `raw/`, `inbox/`, the private data repo (`.git-data`), and the cache. Idempotent — never overwrites existing pages. |
| `mind index [--titles-only \| --full \| --tag <t> \| --type <t> \| --json]` | Get the page inventory at the resolution you need. Start with `--titles-only` on a large wiki, then zoom in. |
| `mind search "<query>"` | FTS5 search over wiki bodies (via `cache/search.db`). |
| `mind graph [--from <slug>] [--depth N] [--kind <rel>] [--format json\|dot\|mermaid]` | Traverse the page graph. |
| `mind rebuild-cache` | Rebuild all `cache/*` from `wiki/`. Safe to run anytime. |
| `mind commit [--message "..."]` | Stage `wiki/` + `raw/` and commit — into the private data repo (`.git-data`), never the code repo (see README "Two repos, one directory"). Called for you if `ingest.auto_commit: true`. |
| `mind ingest <path> [--batch]` | User-facing entry that copies into `raw/`, preprocesses, and invokes YOU with the right context. Usually the user runs this; you're the agent it spawns. |
| `mind query "<q>" [--file]` | Likewise user-facing; you are what gets spawned. `--file` tells you to save your answer as a query page. |
| `mind lint` | Runs the deterministic strict-lint pass first, then spawns you for fuzzy checks (style, stale claims, contradictions). Refuses to invoke you if strict lint fails — pass `--skip-strict` to override. |
| `mind strict-lint` | Deterministic-only pass. No agent. Fast. Used by `mind commit` as a gate. |
| `mind digest [--since <dur>\|--since-last] [--window-days N]` | Spawns you to run cross-reference synthesis across daily/source entries in a time window. You write a `digest-YYYY-MM-DD.md` page and maintain `open-threads.md`. |
| `mind pull [--source <path>] [--keep] [-y]` | Pull files from this machine's configured source folders (`pull.sources` in `config.local.yaml`) into `inbox/` for staged ingestion. Does not invoke you — the user then runs `mind ingest inbox/<file>` per item. |
| `mind serve` | Launches the read-only web UI. Does not involve you. |

## Workflows

### Ingest

Invoked by `mind ingest`. Inputs handed to you: the preprocessed raw file (or its sibling text artifact), `AGENTS.md`, and access to the wiki.

0. **Verification gate (before you run).** For audio / image / PDF inputs, `mind ingest` pauses after preprocessing and shows the sibling text artifact to the user for approval. The user can approve (`y`), abort (`n`), or type feedback in natural language ("the speaker's name is Ada, not Adder — fix everywhere"); on feedback you will be spawned with a narrow correction prompt to edit the sibling file in place, then the user is re-prompted. By the time the ingest workflow below starts, the sibling has already been approved as correct. Skipped via `--no-verify`, `--batch`, or `verify.default: false` in `config.yaml`.
1. **Orient.** Run `mind index --titles-only` to see the page inventory. If the input's topic is unfamiliar, also run `mind index --tag <guess>` or `mind search "<term>"`.
2. **Read.** Fully read the raw source (and sibling text if applicable). Summarize key takeaways.
3. **Decide page-level action.** One of:
   - Create a new source-summary page: `wiki/<slug>.md` with `tags: [source, ...]`.
   - Update an existing page (usually when the source is additional evidence for an existing topic).
   - Both: a source-summary page AND updates to entity/concept pages that the source informs.

   **Audio recordings are special.** The source-summary IS a [journal](wiki/journal.md) entry — do not create a separate `tags: [source]` page. See `wiki/journal.md`'s Agent notes for filename, tag, and H1 conventions.
4. **Cross-reference — this is the most important step.** For every related existing page:
   - Read the page.
   - Write inline body links `[Page Title](slug.md)` in the new/updated content wherever the related concept is mentioned. Do not collect these into a "See also" dump; weave them into the prose.
   - Open each related page and add a back-link at the natural spot in *its* prose referencing the new page.
   - Update `related:` frontmatter on both sides to mirror the body links.

   When cross-referencing daily entries, see `wiki/journal.md`'s Agent notes for the daily-to-daily linking rules.
5. **Update `wiki/index.md`.** Either add a new entry or refresh the summary line — see its Agent notes for section layout.
6. **Append to `wiki/log.md`.** See its Agent notes for entry format.
7. **Commit.** Run `mind commit` (or rely on auto-commit).

One ingest commonly touches 5–15 wiki pages. That's expected.

### Query

Invoked by `mind query "<q>" [--file]`.

1. Run `mind index --titles-only` and `mind search "<key terms from q>"` to locate candidate pages.
2. Read the candidates. Synthesize an answer.
3. Cite pages inline with standard markdown links; cite raw sources by their filename path.
4. If `--file` (or the user asks), save the answer as `wiki/q-YYYY-MM-DD_<slug>.md` with `tags: [query, ...]`. Update `index.md`, append to `log.md`, commit.

### Digest

Invoked by `mind digest`. The CLI has already gathered the window's daily/source entries and pre-computed concept/entity clusters; you receive them as structured context in the prompt. Inputs: window dates, entry list, clusters, and the current contents of `wiki/open-threads.md` (if any).

This is **batched cross-reference over a time window** — what ingest step 4 does for a single entry, digest does across many. Patterns only visible across multiple entries (recurring themes, drift, repeated questions) get surfaced here and propagated into the canonical graph.

**v1 is dry-run.** You propose edits to concept/entity pages as a checklist; you do NOT edit those pages in this run. A human (or a later `--apply` run) executes the checklist.

1. Read every entry in the window. For each cluster, also read the target concept/entity page.
2. Write `wiki/digest-YYYY-MM-DD.md` with `tags: [digest]`:
   - H1: `Digest <start> → <end>`
   - `## Clusters` — per cluster, short prose synthesis noting drift, new claims, recurring questions. Use inline `[Title](slug.md)` links.
   - `## Proposed edits` — markdown checklist. `- [ ] <slug>.md: <specific change>`. Concrete and executable.
   - `## Stats` — entry count, cluster count, pages touched.
   - `related:` frontmatter mirrors the concept/entity pages clustered.
3. Maintain `wiki/open-threads.md` — append new unresolved questions from the window's entries; close items resolved by later entries. See its Agent notes for entry format.
4. Update `wiki/index.md` — add a line for the new digest under `## Digests`. See its Agent notes for section layout.
5. Append one line to `wiki/log.md` — see its Agent notes for entry format.

Do NOT edit the clustered concept/entity pages themselves. Do NOT commit — `mind` commits for you.

### Lint

Two layers, run in order:

**Strict lint (deterministic, in Python).** Run by `mind strict-lint` directly, by `mind lint` as a precondition, and by `mind commit` as a gate. No agent involved. Checks:

- **Structural (A):** filename = `<slug>.md`; slug is lowercase kebab-case; required frontmatter fields present (`title`, `slug`, `created`, `updated`, `tags`, `sources`, `related`); ISO `YYYY-MM-DD` dates with `updated >= created`; exactly one H1 matching `title`; `tags[0]` ∈ canonical page-types; no `[[wikilinks]]` written into bodies; all body markdown links resolve to a real `wiki/*.md`; no duplicate slugs; Agent-notes `<details>` blocks (when present) follow the canonical format — see "Per-page guidance" below; **daily pages carry a non-empty `summary:` ≤ 80 chars (A10)**; **body links whose target is a daily page use link text `YYYY-MM-DD — <subject>` (em dash U+2014), except from pages in `lint.meta_exempt_slugs` which hand-write their own catalog prose (A11)**.
- **Graph symmetry (B):** every entry in `related:` must appear as a body link; every body link A → B must have A in B's `related:`. Pages whose slug is in `lint.meta_exempt_slugs` (catalogs, owner profile, life-area hubs) are skipped as both source and target — they accumulate inbound links by design.
- **Taxonomy (C):** secondary tags (`tags[1:]`) must be drawn from `lint.canonical_tags` in the merged config (`config.yaml` defaults + `config.wiki.yaml` extensions). Page types come from `lint.page_types`.

If strict lint fails, fix the violations before committing. Use `mind commit --no-lint` only when you genuinely need the escape hatch (e.g. saving in-progress work) and follow up with a real lint pass.

**Agent lint (judgment, you).** Run by `mind lint` after strict lint passes. Checks:

- Orphans: pages with zero inbound links (ignore pages tagged `orphan-ok` or `meta`, and pages in `meta_exempt_slugs`).
- Inline-linking style: links woven into prose, not collected into "See also" dumps.
- Daily ↔ daily linking: avoid; route through concept hubs — see `wiki/journal.md`'s Agent notes.
- Stale claims: pages not updated in N months whose source docs are newer. Flag, don't auto-fix.
- Contradictions: claims that conflict across pages. Record in `wiki/contradictions.md`.

Write findings into `wiki/contradictions.md` — see its Agent notes for the format and inline-comment convention. Commit.

## Handling naive-structure input

When the user explores a topic they don't yet have structured understanding of, their framing, vocabulary, and mental model often diverge from the established treatment of that topic. Sometimes their thinking runs parallel to existing concepts (just lacking standard vocabulary); sometimes it is genuinely novel. You usually can't tell which at ingest time. Two failure modes to avoid:

- **Laundering:** silently remapping the user's framing onto established terminology — erasing the thinking trail and possibly discarding what's novel about their angle.
- **Isolation:** leaving exploratory entries disconnected from any structured anchor, so the wiki never accrues the cross-links that established knowledge would provide.

Default policy:

1. **User's framing is canonical on exploration pages.** Page titles, prose, metaphors, and phrasing reflect the user's own framing at the time of entry. Do not silently translate naive phrasing into established terms — the fact that the user reached for metaphor X rather than term Y is data. Frontmatter tags may use established vocabulary (for retrieval), but body prose stays in the user's voice.
2. **Two-layer graph: exploration pages + concept pages.** When you recognize an established parallel, create (or link to) a separate concept page anchored in standard vocabulary. Connect the two with an inline prose link — `this resonates with [ma (間)](ma.md)` — never by rename or merge. Back-link from the concept page to the exploration page(s) that map to it.
3. **Parallel vs. novel is decided incrementally, not at ingest.** On first entry, capture in the user's voice, link tentatively when reasonably confident of the parallel, and flag uncertainty in prose ("this might track the established concept of X, but your framing emphasizes Y which I don't see in the standard treatment"). As more entries accumulate on the same theme, tighten, split, or sever linkages — during ingest of later entries, not as a separate pass.
4. **Ask before absorbing.** Collapsing an exploration page into an established taxonomy — renaming, merging, demoting to a subsection — is a taxonomy decision. Surface to the user before acting (see "When to ask" below). Default bias: preserve the exploration page as its own node.
5. **Acknowledge uncertainty in-line.** When you link to an established concept with low confidence, say so in the prose itself. This keeps the annotation honest and gives the user a cheap signal about what to review.

The cost is apparent redundancy — two vocabularies coexist and some pages look like duplicates. The payoff is that novel angles aren't laundered into generic summaries of established knowledge, and the user's thinking trail survives as a first-class artifact.

## When to ask (don't guess)

Surface a question to the user rather than decide silently for:
- Creating a new top-level type (a new value for `tags[0]` beyond entity/concept/source/daily/query/digest/meta) or a new secondary tag (anything outside `lint.canonical_tags`). Both require a `config.yaml` bump.
- Major restructuring (splitting or merging pages with many inbound links).
- Renaming or merging an exploration page into an established concept page (see "Handling naive-structure input").
- Apparent contradictions where resolution requires external knowledge the user has.
- Destructive operations on `raw/` or wholesale rewrites of any page.

## External preprocessors

- **whisper.cpp** (audio). The `whisper.cpp/` directory is gitignored and per-machine; never commit it. The CLI auto-resolves the binary from PATH or a local `whisper.cpp/build/bin/whisper-cli` build, and the model from `whisper.cpp/models/ggml-<name>.bin`. If either is missing, audio ingest writes a stub transcript with a pointer to `README.md` instead of failing the ingest.
- **Idempotent preprocessing.** `_preprocess_audio` (and siblings) skip the tool invocation when the `.transcript.md` already exists with non-empty content. This is deliberate: the UI voice-capture flow parses in `inbox/` before `mind ingest` runs, and the user can hand-edit the transcript before promoting it to `raw/`. Re-running preprocess must not clobber either the prior result or the edit.

## Staging: inbox/ → raw/ (two-stage capture)

User-captured media (via `mind pull` or the web UI's `/record` page) first lands in `inbox/`. It is not part of the wiki's input history until `mind ingest` promotes it into `raw/`. State is derived from filesystem layout:

- `inbox/<name>.<audio-ext>` with **no** sibling `.transcript.md` → **captured** (preprocessing not yet run).
- `inbox/<name>.<audio-ext>` **with** a sibling `inbox/<name>.transcript.md` → **parsed** (whisper ran, awaiting review + ingest).
- Once `mind ingest inbox/<name>.<ext>` runs: the audio + sibling are moved into `raw/` (date-prefixed via `raw.filename_convention`), `_preprocess_audio` is a no-op (sibling exists), and the agent proceeds to write the journal entry as usual.

Run `mind pending` (or `mind pending --json`) to enumerate the inbox. The web `/record` page surfaces the same list inline.

## Graph model (for reference)

- Node = one `.md` file in `wiki/`. ID = filename slug.
- Edge = a body markdown link to another wiki page.
- Adjacency is materialized in `cache/links.json` (`{slug: {out: [...], in: [...]}}`) and regenerable via `mind rebuild-cache`.

Traversals you can request via CLI: `mind graph --from <slug> --depth N`. Any additional graph analytics (centrality, clusters) can be computed by loading `cache/links.json` — it's a plain adjacency map.
