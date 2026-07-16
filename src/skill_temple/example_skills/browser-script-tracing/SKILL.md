---
name: browser-script-tracing
description: Use to trace an exact network request to initiator stacks, search loaded scripts narrowly, read bounded source ranges, and save source evidence. Do not use broad text matches as proof or analyze unrelated bundles.
---

# Browser script tracing workflow

Load `browser-action-protocol` with this Skill. Begin from exact request evidence and
preserve the link from request to initiator to source artifact.

## Required reading order

1. Read `docs/initiator-first.md` before searching scripts.
2. Read `docs/bounded-source.md` before source retrieval.
3. Read `docs/source-evidence.md` before persistence or reporting.
4. Read exact protocol contracts for `get_request_initiator`, `search_scripts`,
   `get_script_source`, and `save_script_source` when used.

## Invariants

- Start from one exact request evidence ID.
- Prefer initiator stack/script handles over broad text search.
- Use narrow URL filters and bounded line/offset ranges.
- Treat minified/bundled text matches as candidates, not proof.
- Persist only the source region needed for the claim.
- Link saved source evidence to the originating request evidence when possible.

## Completion standard

Return request evidence ID, initiator evidence, exact script handle, bounded source
range, saved source evidence ID, and a claim limited to what the source actually shows.
