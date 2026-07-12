---
name: pandora-protocol-reproduction
description: Use to reverse engineer and reproduce Pandora-like conversational web protocols through atomic browser experiments, stable evidence IDs, source tracing, browser-context request replay, state-machine experiments, schemas, and auditable reports. 中文：用于通过原子浏览器实验、稳定证据 ID、源码追踪、浏览器上下文请求重放和状态机实验复刻 Pandora 类对话协议。
---

# Pandora protocol reproduction

Use this Skill when the user wants to understand, reproduce, compare, or document a modern conversational web protocol, including first message, second message, regenerate, edit, stop, conversation state, authentication, request construction, and streaming events.

## Architecture boundary

Follow this separation throughout the task:

- This Skill decides the experiment sequence, one-variable mutations, evidence interpretation, and report contents.
- `runBrowserExperiment` performs atomic browser operations such as `capture_flow`, paired `replay_request`, `save_script_source`, and `cancel_experiment`.
- `inspectBrowserEvidence` reads bounded experiment, evidence, initiator, script, console, and stream summaries.
- Workspace tools read evidence files and write only derived reports, schemas, notes, and replay scripts.
- Never reconstruct browser fetches with arbitrary PowerShell or JavaScript when `replay_request` can perform the operation.
- Never copy Cookie, Authorization, CSRF, or other credential values into chat, reports, diffs, or generated scripts.

## Required references

Read these files when the corresponding stage begins:

- `docs/experiment-matrix.md` for the six scenario sequence and mutation matrix.
- `docs/evidence-contract.md` before interpreting or citing evidence.
- `docs/report-templates.md` before generating final reports and schemas.

## Workflow

### 1. Establish the analysis series

Open or reuse one browser session. Choose one `analysis_series_id` and keep all related experiments in the same session.

Each experiment should set:

```text
analysis_series_id
scenario_type
predecessor_experiment_id
sequence_index
conversation_key, when known
```

Do not infer a predecessor from timestamps. Use the explicit experiment chain.

### 2. Capture a baseline

Run `capture_baseline` with no mutation and record page snapshots, console errors, and relevant ordinary network selectors. Confirm that the page is aligned and the experiment reaches a terminal manifest.

### 3. Capture the first message

Run `capture_flow` for one message submission. Configure:

- the primary streaming request matcher;
- `network_evidence` selectors for conversation creation, account/session, configuration, message submission, and any stop/control endpoint;
- before/after page snapshots;
- console errors;
- a stream predicate or network terminal observation.

After completion:

1. Call `list_evidence`.
2. Select the primary `network_request` evidence.
3. Call `get_network_evidence` for its redacted summary.
4. Call `get_request_shape` before choosing a JSON mutation path.
5. Call `get_request_initiator`.
6. Use `search_scripts` and `get_script_source` for the identified request builder.
7. Persist the bounded source region with `save_script_source`, linked to the network evidence ID.

### 4. Determine field necessity with browser-context replay

Replay classification is always a pair:

```text
control replay
  replay_mode = control
  mutations = []
  volatile_bindings generate fresh IDs/timestamps/nonces

treatment replay
  replay_mode = treatment
  control_experiment_id = <control>
  mutations = exactly one
  reuses the control volatile values
```

Use RFC 6901 JSON Pointer paths, including array indices:

```text
/messages/0/id
/messages/0/author/role
/messages/0/content/parts/0
/parent_message_id
```

Wildcards and bracket expressions are not allowed. The single treatment mutation may be:

```text
remove_json_path
replace_json_path
remove_header
replace_header
remove_query_parameter
replace_query_parameter
```

Browser-managed headers such as Cookie, Origin, Referer, Host, Content-Length, and `Sec-*` cannot be mutated through browser-context fetch and must be rejected rather than classified. Classify a field only when `mutation_effective=true`, after comparing:

- replay HTTP status and response evidence;
- stream or network terminal behavior;
- page snapshot/state effects;
- conversation persistence or subsequent retrieval;
- console errors.

If the source response is `text/event-stream`, replay automatically enables stream capture and raw artifact requirements. A treatment that produces an exact non-stream 4xx response is recorded as a protocol rejection, not a collector failure.

Use `verification_flow` for reload, conversation detail retrieval, or reopening the conversation after fetch. Without persistent-state verification, do not classify a 2xx result beyond `partial` or `unknown`.

Use these classifications:

```text
required
conditionally_required
optional
tracking_only
unknown
```

Do not call a field optional merely because the HTTP request returned 2xx. Confirm the resulting conversation state.

### 5. Capture the state-machine sequence

Follow `docs/experiment-matrix.md` for:

1. first message;
2. second message;
3. regenerate;
4. edit an earlier user message;
5. stop generation;
6. retrieval or reload verification.

For Stop, observe facts instead of assuming cancellation. Accept and classify:

```text
expected_user_cancel
stop_followed_by_finished
stop_control_request_observed
stop_page_state_only
stop_outcome_unknown
```

### 6. Maintain evidence-backed conclusions

Every core conclusion must cite all available identifiers:

```text
experiment_id
evidence_id
artifact_id
```

Use `list_evidence` to discover stable IDs. Use workspace tools only when the actual artifact content is needed. Prefer redacted summaries and redacted artifacts. Do not read credential artifacts into model context.

### 7. Produce the reproduction package

Create the outputs in `docs/report-templates.md`. Reports and scripts belong under `reports/`, `schemas/`, `scripts/`, or `notes/`; never modify original evidence.

The generated HTTP replay script must use placeholders or environment variables for credentials. Browser-context replay remains the source of truth for authenticated behavior.

## Decision rules

- If no exact full network snapshot exists, capture a new experiment with `export_parts=["all"]`; do not approximate the request from a summary.
- If `get_request_shape` is unavailable, recapture the request; do not guess array paths from source text alone.
- If initiator evidence is absent, reproduce the request in a new experiment and capture it again before broad source searching.
- If a source match is minified, use bounded source offsets and persist it with `save_script_source`; document the loaded script URL/hash and initiator evidence ID.
- If a treatment reports `mutation_effective=false`, discard it as inconclusive and fix the matcher or mutation path.
- If a source request is stateful, require a successful control replay before running a treatment.
- If an experiment is submitted incorrectly, call `cancel_experiment`; do not close the session or restart the service.
- If `changed_during_read=true`, do not cite the file hash as stable. Re-read after the experiment reaches a terminal state.
- If an evidence or replay result is partial, preserve the uncertainty in reports and `notes/open-questions.md`.

## Completion criteria

The protocol reproduction is complete only when:

- all six scenarios have an explicit experiment chain;
- primary ordinary and streaming requests have stable evidence IDs;
- request shapes and redacted request bodies expose mutation paths without exposing values;
- request construction has initiator and source evidence;
- important fields have successful control plus one-variable treatment results;
- every treatment verifies the mutation on the actual outbound request;
- streaming requests have `stream_request` and `stream_event_range` evidence;
- stream events and conversation state transitions are documented;
- Stop behavior is observed rather than assumed;
- every core report conclusion is traceable to experiment, evidence, and artifact IDs;
- credentials are absent from natural-language outputs and generated source files;
- unresolved behavior is listed explicitly rather than guessed.
