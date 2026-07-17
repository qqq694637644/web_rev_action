---
name: browser-evidence-inspection
description: Use to move from bounded experiment summaries to exact evidence IDs, inspect request shapes and initiators, separate facts from inferences, and document evidence gaps. Do not use to execute mutations or manage session lifecycle.
---

# Browser evidence inspection workflow

Load `browser-action-protocol` with this Skill. Use exact experiment and evidence
handles; never infer identity from list order or a similar URL.

## Required reading order

1. Read `docs/evidence-selection.md` before choosing evidence.
2. Read `docs/fact-inference-boundary.md` before writing conclusions.
3. Read `docs/evidence-gaps.md` when expected artifacts are missing or partial.
4. Read exact protocol contracts only for the inspect operations used:
   `list_experiments`, `get_experiment`, `list_evidence`, `get_network_evidence`,
   `get_request_shape`, `get_request_initiator`, and `list_console_errors`.

## Invariants

- Begin with a bounded experiment summary, then select one exact ID.
- Verify terminal status and quality before interpreting evidence.
- Preserve experiment/evidence association in every note and report.
- State observed facts separately from hypotheses and conclusions.
- Missing evidence remains unknown; it is not proof of absence.
- Never expose credential artifact contents.

## Completion standard

Every factual claim cites stable experiment/evidence/artifact handles. Every inference
states its supporting observations and uncertainty. Every unresolved question names the
smallest next capture or inspection needed.
