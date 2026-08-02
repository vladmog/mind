---
title: Index
slug: index
created: {date}
updated: {date}
tags: [meta]
sources: []
related: []
---

# Index

<details><summary>Agent notes</summary>

Touched on every ingest — add a new line or refresh the summary for the affected page.

- Entry format: `- [Title](slug.md) — one-line summary.` Keep the em-dash and lowercase-sentence summary style so unix tools can parse entries consistently.
- Section layout, top to bottom: `## Entities` (sub-sectioned by kind), `## Concepts` (sub-sectioned by hub/area), `## Sources`, `## Daily`, `## Queries`, `## Digests`, `## Meta`. `## Digests` sits directly above `## Meta`; create it lazily on the first digest.
- `## Meta` always lists this file, `log.md`, `contradictions.md`, and `open-threads.md`. Add other meta/hub pages here.
- A new top-level entity/concept hub (a new `tags[1]` area) is a taxonomy change — surface to the user before creating a new subsection.

</details>

The catalog of every page in this wiki. Agents update this on every ingest; one entry per page with a one-line summary.

Format for entries (kept consistent so unix tools can parse):

```
- [Title](slug.md) — one-line summary.
```

## Entities

_(none yet)_

## Concepts

- [Journal](journal.md) — linear record of free-form entries.

## Sources

_(none yet)_

## Daily

_(none yet)_

## Queries

_(none yet)_

## Meta

- [Index](index.md) — this file.
- [Log](log.md) — chronological record of operations.
- [Contradictions](contradictions.md) — conflicts flagged by lint.
- [Open Threads](open-threads.md) — unresolved questions and TODOs surfaced across entries.
