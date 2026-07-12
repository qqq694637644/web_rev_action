---
name: pandora-protocol-reproduction
description: Use to reverse engineer and reproduce Pandora-like conversational web protocols through atomic browser experiments, stable evidence IDs, source tracing, browser-context request replay, state-machine experiments, schemas, and auditable reports. 中文：用于通过原子浏览器实验、稳定证据 ID、源码追踪、浏览器上下文请求重放和状态机实验复刻 Pandora 类对话协议。
---

# Pandora protocol reproduction

Use this Skill when the user wants to understand, reproduce, compare, or document a modern conversational web protocol, including first message, second message, regenerate, edit, stop, conversation state, authentication, request construction, and streaming events.

## Architecture boundary

Follow this separation throughout the task:

- This Skill decides the experiment sequence, one-variable mutations, evidence interpretation, and report contents.
- `runBrowserExperiment` performs atomic browser operations such as `capture_flow`, `replay_request`, and `cancel_experiment`.
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
4. Call `get_request_initiator`.
5. Use `search_scripts` and `get_script_source` for the identified request builder.

### 4. Determine field necessity with browser-context replay

Use `replay_request` with exactly one mutation per experiment:

```text
remove_json_path
replace_json_path
remove_header
replace_header
remove_query_parameter
replace_query_parameter
```

Keep the source `experiment_id` and `evidence_id` fixed while changing one field. Classify a field only after comparing:

- replay HTTP status and response evidence;
- stream or network terminal behavior;
- page snapshot/state effects;
- conversation persistence or subsequent retrieval;
- console errors.

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
- If initiator evidence is absent, reproduce the request in a new experiment and capture it again before broad source searching.
- If a source match is minified, use bounded source offsets and document the loaded script URL/hash.
- If an experiment is submitted incorrectly, call `cancel_experiment`; do not close the session or restart the service.
- If `changed_during_read=true`, do not cite the file hash as stable. Re-read after the experiment reaches a terminal state.
- If an evidence or replay result is partial, preserve the uncertainty in reports and `notes/open-questions.md`.

## Completion criteria

The protocol reproduction is complete only when:

- all six scenarios have an explicit experiment chain;
- primary ordinary and streaming requests have stable evidence IDs;
- request construction has initiator and source evidence;
- important fields have one-variable replay results;
- stream events and conversation state transitions are documented;
- Stop behavior is observed rather than assumed;
- every core report conclusion is traceable to experiment, evidence, and artifact IDs;
- credentials are absent from natural-language outputs and generated source files;
- unresolved behavior is listed explicitly rather than guessed.
