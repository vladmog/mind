---
title: Journal
slug: journal
created: {date}
updated: {date}
tags: [concept, practice]
sources: []
related: []
---

# Journal

<details><summary>Agent notes</summary>

Extras beyond the user-facing convention below:

- **Audio ingests become journal entries, not source pages.** When an audio recording is ingested, the source-summary IS the new daily entry — do not also create a `tags: [source]` page. Filename `wiki/YYYY-MM-DD_<slug>.md`, H1 = the date (optionally with a topic suffix), `tags: [daily, voice, ...]`, `sources: [raw/<the-audio-file>]`. Body is a cleaned, lightly-edited rendering of the transcript with cross-references woven inline.
- **Every daily entry needs a `summary:` in frontmatter.** One short phrase (≤ 80 chars) naming the subject, e.g. `summary: "First session at the new gym & gear notes"`. No trailing period; don't re-state the modality (`voice`, etc.) since the tags already carry it. Strict-lint rule `A10-daily-summary-required` enforces this.
- **Link text for daily references.** When another page links *to* a daily entry, link text MUST be `YYYY-MM-DD — <summary>` with an em dash (U+2014), e.g. `[2026-04-19 — First session at the new gym](2026-04-19.md)`. Use the target entry's `summary` field verbatim or a truncation of it. Strict-lint rule `A11-daily-link-text` enforces this for every page not in `lint.meta_exempt_slugs`. Catalogs like `journal.md`, `index.md`, `log.md` are exempt — they hand-write richer prose.
- **Avoid daily↔daily links.** Link daily entries to concept/entity hubs, not to other daily entries. `journal.md` is the chronological aggregator; temporal threads across dailies are the job of `mind digest`.
- **Exception — explicit follow-ups.** If a new entry directly continues or resolves a question from one specific earlier entry, link that one predecessor inline. Do not back-edit the older daily.

</details>

Free-form journal entries the wiki's owner wants kept as a **linear record** — chronological, preserved in order — while still linking out to other pages in the wiki.

Convention for entries: one page per entry, `tags[0]: daily`, filename `YYYY-MM-DD.md` (or `YYYY-MM-DD_<slug>.md` when multiple exist on a day). Each entry should:

1. Start with the date as its H1 / title.
2. Be added at the top of the "Entries" list on this page so the list reads newest-first — that's the linear record.
3. Freely link to other wiki pages in its body the way any other page does.

**Voice-derived entries** (produced by ingesting an audio recording) follow the same convention but carry an additional `voice` tag, e.g. `tags: [daily, voice, ...]`. This makes them filterable (`mind index --tag voice`) and groupable without splitting the linear record across two pages.

## Entries

_(none yet)_
