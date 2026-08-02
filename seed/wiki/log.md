---
title: Log
slug: log
created: {date}
updated: {date}
tags: [meta]
sources: []
related: []
---

# Log

<details><summary>Agent notes</summary>

Append-only. Every mutating operation adds one entry at the top of the entry list (newest first).

- Entry header: `## [YYYY-MM-DD] <op> | <subject>`. `<op>` is one of `ingest`, `digest`, `lint`, `backfill`, `bootstrap`, `init`, `update`, `rebuild`, or a new verb chosen deliberately.
- Body: one paragraph naming the pages touched, linked inline. Short prose, not a bullet list.
- `ingest | <title>` per raw ingest. `digest | <start>..<end>` per `mind digest` run. `lint | <summary>` when the fuzzy lint pass writes findings.
- Do not edit older entries. If a prior entry was wrong, append a correction entry referencing it.

</details>

Append-only chronological record of wiki operations. Each entry starts with `## [YYYY-MM-DD] <op> | <subject>` so it's parseable with `grep "^## \[" log.md`.

## [{date}] init | wiki created

Seeded by `mind init`: created [Index](index.md), [Journal](journal.md), [Open Threads](open-threads.md), [Contradictions](contradictions.md), and this log.
