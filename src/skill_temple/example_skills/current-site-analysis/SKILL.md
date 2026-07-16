---
name: current-site-analysis
description: Use to investigate any current website through evidence-first UI, network, stream, worker, storage, authentication, source, and replay analysis without assuming a product-specific workflow. 中文：用于先观察当前网页，再按证据设计通用协议实验。
---

# Current-site web protocol analysis

Use this Skill when the user wants to understand how an unfamiliar current website works, identify request construction and state sources, reproduce selected requests, or document an auditable protocol model.

Do not begin with a fixed scenario list. First inventory the current page and available evidence, then choose the smallest experiment that can answer the next open question.

## Architecture boundary

- This Skill chooses observations, hypotheses, experiments, and report claims.
- `runBrowserExperiment` performs atomic capture and generic browser-context replay.
- `inspectBrowserEvidence` reads bounded manifests, evidence, initiators, scripts, streams, and artifacts.
- Workspace tools read saved evidence and write only derived reports, schemas, notes, and replay code.
- The Action records facts, completeness, associations, and optional comparison results. It does not decide business semantics for the analyst.
- Never copy Cookie, Authorization, CSRF, session, or private response values into chat, reports, diffs, or generated scripts.
- Before either Browser Action is called, load `browser-action-protocol` and read the
  exact operation contract. This workflow Skill does not duplicate transport syntax.

## Required references

Read these files when the matching stage begins:

- `docs/inventory-checklist.md` before planning experiments.
- `docs/experiment-design.md` before replay, mutation, or termination configuration.
- `docs/report-contract.md` before producing final artifacts.
- From `browser-action-protocol`, read
  `browser-action-protocol/docs/transport-envelope.md`,
  `browser-action-protocol/docs/operation-index.md`, and only the operation files
  used by the experiment.

Load supporting workflow Skills only when their stage begins, keeping each load to the
minimum set:

- `browser-session-capture` for session, target, baseline, and job polling;
- `browser-evidence-inspection` for exact evidence selection and claim boundaries;
- `browser-request-replay` for source selection, bindings, mutation, and comparison;
- `browser-script-tracing` for initiator-to-source evidence;
- `browser-stream-diagnostics` for stream completion and interruption;
- `browser-experiment-recovery` only after stale, busy, interrupted, or unknown state.

## Browser Action call discipline

- Before opening a session, read `browser-action-protocol/docs/inspect/get-session.md`
  and inspect a known session ID. Use `open_session` only when the session is absent,
  closed, or intentionally replaced.
- Before each run operation, read its exact file under
  `browser-action-protocol/docs/run/`. Before each inspect operation, read its exact
  file under `browser-action-protocol/docs/inspect/`.
- Submit the complete six-field public envelope from the exact generated operation
  document. Copy the protocol Skill hash from `loadSkills`; never reuse an operation
  hash for another operation. Do not send decoded fields at the Action top level.
- Job-mode responses with `status=running` are not complete. Poll `get_experiment`
  until `completed`, `partial`, `failed`, or `interrupted` before citing evidence.
- When validation fails with `dispatch_started=false`, correct only the reported
  paths and retry once. When dispatch started or outcome is unknown, inspect session
  and experiment state before deciding the next action; never repeat the run blindly.

## Workflow

### 1. Establish the current analysis context

Inspect a known session ID first, then open or reuse one browser session and choose
one `analysis_series_id`. Record the current page URL, title, page ID,
account/session state visible to the user, and the question being investigated.

Do not infer the target protocol from product names, historical fixtures, or a previously studied website.

### 2. Capture a current-site baseline

Run `capture_flow` with the minimum actions needed to observe the current page. Capture page snapshots, console errors, relevant ordinary network requests, and stream evidence where present.

Inventory before mutating:

```text
visible UI and state transitions
ordinary request endpoints and methods
stream transports and terminal behavior
request initiators and script sources
worker/service-worker involvement
storage and authentication provenance
stable and dynamic identifiers
evidence gaps and ambiguous associations
```

Use `docs/inventory-checklist.md` to distinguish observed facts from unknowns.

### 3. Select exact evidence

For each request under analysis:

1. Select the exact `network_request` evidence ID or canonical observation ID.
2. Inspect the redacted request shape and bounded response metadata.
3. Inspect the request initiator.
4. Search scripts and save only the bounded source region needed for the claim.
5. Record whether the request is ordinary, stream-backed, worker-originated, or still unknown.

Never select the first request in a manifest merely because the URL is similar. If stable IDs cannot uniquely associate evidence, preserve `ambiguous` or `missing`.

### 4. Form one small hypothesis

Examples:

```text
this field is generated by the page bundle
this header is injected by the browser context
this response value feeds the next request
this stream ends on an exact SSE data event
this worker owns the request
this UI state is persisted by a specific endpoint
```

State what observation would support or weaken the hypothesis. Do not run a mutation just because the API permits one.

### 5. Run a generic replay only when useful

Load `browser-request-replay`, then use the complete generated envelope in
`browser-action-protocol/docs/run/replay-request.md`. Do not maintain a second copy of
the operation payload or hash in this orchestration Skill.

Use bindings for generated, preserved, extracted, literal, or manual inputs. Bindings run before ordered mutations. Query mutation defaults to `query_serialization=preserve_raw`.

Comparison is optional. Every reference must contain `experiment_id` plus exactly one `evidence_id` or `observation_id`. `include_source=true` refers to the exact replay source.

### 6. Treat stream policy as observed configuration

Choose `response_reader.mode` from current evidence. Configure termination only when the site demonstrates a corresponding behavior:

```text
exact_sse_data
text_pattern
network_close
idle_window
```

Use at most one idle window. `text_pattern` matches decoded UTF-8 text. HTTP status is a response fact, not a stream-completeness verdict.

### 7. Verify state and source claims

Use `verification_flow`, page snapshots, follow-up requests, storage inspection, or bounded source evidence to verify the behavior relevant to the hypothesis. A replay response alone does not prove persistent state or field necessity.

### 8. Report facts, gaps, and next experiments

Produce the reports in `docs/report-contract.md`. Separate:

```text
observed facts
derived comparisons
analysis hypotheses
missing or ambiguous evidence
recommended next experiment
```

Stop when the user question is answered with traceable evidence. Do not force unrelated message, regenerate, edit, stop, or reload scenarios.

## Optional specialized template

When the current-site inventory actually reveals a conversational tree with first-message, follow-up, regenerate, edit, and stop semantics, the optional `pandora-protocol-reproduction` Skill may be used as a specialized experiment template. Its six scenarios are not default requirements for other websites.

## Completion standard

A current-site analysis is complete when:

- the relevant UI, network, stream, source, worker/storage/auth facts are inventoried;
- important claims cite exact experiment and evidence or observation IDs;
- replay comparisons use exact references and narrow dimensions;
- unknown, missing, and ambiguous evidence remain explicit;
- generated reports do not contain credentials or private artifact contents;
- the final protocol description answers the user question without unsupported product-specific assumptions.
