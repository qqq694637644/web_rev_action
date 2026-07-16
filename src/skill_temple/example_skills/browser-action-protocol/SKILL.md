---
name: browser-action-protocol
description: Load whenever inspectBrowserEvidence or runBrowserExperiment will be called. Provides the six-field version-bound transport envelope, exact generated operation contracts and hashes, strict payload_json encoding, stale-contract recovery, and safe dispatch-state handling.
---

# Browser Action protocol

Use this Skill whenever a task will call `inspectBrowserEvidence` or
`runBrowserExperiment`. It defines transport and operation syntax only; combine it
with a workflow Skill such as `current-site-analysis` for experiment design.

## Required reading order

1. Read `docs/transport-envelope.md` once before the first Browser Action call.
2. Read `docs/operation-index.md` and select the exact operation.
3. From the operation index, read only the exact operation contract it names.
4. Read `docs/json-encoding.md` when constructing or correcting `payload_json`.
5. Read `docs/error-recovery.md` only after a Browser Action error.

## Invariants

- The public request has exactly `contract_version`, `operation`, `payload_json`,
  `skill_id`, `skill_content_hash`, and `operation_contract_hash`.
- Always send `contract_version` as `"2.0"`.
- Always send `skill_id` as `"browser-action-protocol"`.
- Copy `skill_content_hash` from this Skill's `loadSkills` result.
- Copy `operation_contract_hash` from the generated block in the exact operation doc.
- `payload_json` is a JSON-encoded string whose decoded top level is one object.
- The selected operation determines whether to use the run or inspect Action.
- Do not send a nested transport wrapper or domain `contract_version` inside
  `payload_json`.
- Do not invent fields from neighboring operations. The server rejects extras.
- Never retry a consequential operation when `dispatch_started=true` or
  `outcome=unknown`; inspect current state first.

## Completion standard

A call is protocol-correct only when the envelope is version 2.0, the operation is
listed in `docs/operation-index.md`, the decoded object validates against the exact
operation contract, all binding hashes match the server build, and error recovery
respects dispatch state.
