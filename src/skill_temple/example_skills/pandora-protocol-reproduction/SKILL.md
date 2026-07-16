---
name: pandora-protocol-reproduction
description: Optional specialized template for a current site that has already been observed to expose Pandora-like conversational tree, regenerate, edit, and stop semantics. 中文：仅在当前网页已确认具有 Pandora 类对话树语义时使用的可选专用模板。
---

# Optional Pandora protocol reproduction template

Use this Skill only after a current-site inventory has confirmed a conversational
tree with first message, follow-up message, regenerate, edit, stop, and reload
semantics. For an unfamiliar or non-conversational website, use
`current-site-analysis` instead.

## Architecture boundary

Follow this separation throughout the task:

- This Skill decides the experiment sequence, one-variable mutations, evidence interpretation, and report contents.
- `runBrowserExperiment` performs atomic browser operations such as `capture_flow`, generic `replay_request`, `save_script_source`, and `cancel_experiment`.
- `inspectBrowserEvidence` reads bounded experiment, evidence, initiator, script, console, and stream summaries.
- The Action records observations, quality, explicit comparison facts, and hints. It does not decide final `required`, `optional`, or protocol semantics for the analyst.
- Workspace tools read evidence files and write only derived reports, schemas, notes, and replay scripts.
- Never reconstruct browser fetches with arbitrary PowerShell or JavaScript when `replay_request` can perform the operation.
- Never copy Cookie, Authorization, CSRF, or other credential values into chat, reports, diffs, or generated scripts.
- Load `browser-action-protocol` before any Browser Action call and read only the exact
  operation contracts needed for the current experiment.

## Required references

Read these files when the corresponding stage begins:

- `docs/experiment-matrix.md` for the six scenario sequence and mutation matrix.
- `docs/evidence-contract.md` before interpreting or citing evidence.
- `docs/report-templates.md` before generating final reports and schemas.
- From `browser-action-protocol`, read `docs/transport-envelope.md`,
  `docs/operation-index.md`, and the selected run/inspect operation files.
- Load `browser-session-capture` for baseline/session work,
  `browser-evidence-inspection` for exact evidence selection,
  `browser-request-replay` for one-variable tests, `browser-script-tracing` for source
  claims, and `browser-stream-diagnostics` for stop/termination experiments. Load
  `browser-experiment-recovery` only after a recovery-class error.

## Browser Action call discipline

- Inspect a known session with `get_session` before deciding to call `open_session`.
- Use only complete generated six-field envelopes. Copy the current protocol Skill
  hash and exact operation hash; decoded operation fields belong inside serialized
  `payload_json`, never at the Action top level.
- For job-mode capture or replay, poll `get_experiment` to a terminal state before
  interpreting evidence or starting a dependent experiment.
- A validation error with `dispatch_started=false` may be corrected once from its
  issue paths. If dispatch started or the outcome is unknown, inspect the session and
  experiment first and do not repeat the consequential operation.

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

Run `capture_flow` with an explicit baseline objective, zero mutation, the minimum
snapshot/wait steps, and primary request counts that allow zero matches. Record page
snapshots, console errors, and relevant ordinary network selectors. Confirm that the
page is aligned and the experiment reaches a terminal manifest.

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
4. Call `get_request_shape` before choosing a JSON mutation path. Use
   `path_prefix`, pagination, depth, and array-item limits; request a bounded
   redacted subtree only when necessary.
5. Call `get_request_initiator`.
6. Use `search_scripts` and `get_script_source` for the identified request builder.
7. Persist the bounded source region with `save_script_source`, linked to the network evidence ID.

### 4. Determine field necessity with browser-context replay

Use the generated `replay_request` contract for every experiment. A baseline observation and a
mutation observation are separate requests; the backend does not provide Control or
Treatment modes and does not inherit settings between requests.

For a mutation experiment, submit the same explicit source, setup, reader,
termination, binding, and capture settings again. Add the mutation and point the
comparison at an exact evidence or observation source:

Build both the zero-mutation and one-variable requests from the exact generated
operation document so their transport and contract hashes remain current.

Never reference only an experiment ID. A capture or replay can contain multiple
requests, so every comparison reference must include exactly one `evidence_id` or
`observation_id`. `include_source=true` refers to the exact
`source.experiment_id + source.evidence_id` pair.

Binding value sources are explicit:

```text
generated       one fresh generated value for this replay
preserve_source exact value from the source snapshot
extractor       value produced by a named extractor
literal         caller-provided constant
manual_input    caller-provided experiment input
```

Bindings are applied first and mutations are applied in list order afterward.
For field-necessity analysis, prefer one target mutation per replay so the analyst
can attribute the observed delta. This is an experiment-design rule, not a backend
payload restriction.

Use RFC 6901 JSON Pointer paths, including array indices:

```text
/messages/0/id
/messages/0/author/role
/messages/0/content/parts/0
/parent_message_id
```

Wildcards and bracket expressions are not allowed. Mutations may be:

```text
remove_json_path
replace_json_path
remove_header
replace_header
remove_query_parameter
replace_query_parameter
```

JSON Pointer and query parameter names are case-sensitive. Header names are case-insensitive. Duplicate header/query values are compared as complete ordered lists, including multiplicity.

Browser-managed headers such as Cookie, Origin, Referer, Host, Content-Length, and `Sec-*` cannot be mutated through browser-context fetch and must be rejected rather than classified. Treat the following as evidence checks before making a field hypothesis:

```text
exact source evidence selected
exact comparison evidence or observation selected
requested mutation observed on the outbound wire request
resolved bindings observed on the outbound wire request
comparison dimensions are complete enough for the stated claim
state verification observes the expected persisted effect
```

Then compare:

- replay HTTP status and response evidence;
- stream or network terminal behavior;
- page snapshot/state effects;
- conversation persistence or subsequent retrieval;
- console errors.

For stateful endpoints, define `setup_flow` on every replay that requires it.
Nothing is inherited from a previous replay. Execution order is collector → setup
→ extractors/bindings → fetch → verification. Do not use `verification_flow` to
restore the precondition.

For a streaming response, configure `response_reader.mode` and
`termination.conditions` explicitly. The reader supports LF, CRLF, CR, mixed line
endings, and EOF flush. It parses complete SSE events and terminates only when an
event's combined `data` exactly equals an `exact_sse_data` condition and the
optional event name matches. A literal `[DONE]` inside JSON, model text, or tool
arguments is not terminal. Use `idle_window` only when idle is an intended
experimental terminal condition; otherwise network close or the overall job
deadline controls the wait. Use `text_pattern` for decoded UTF-8 text matching.
Missing terminal evidence, byte/event truncation, malformed semantic evidence,
or unexpected Content-Type can make evidence partial. HTTP 4xx/5xx alone does not.

Reaching the byte limit exactly is not truncation until a later read produces an extra byte. HTTP 3xx is `redirect_or_cache_response`, not success.

An exact non-stream error response makes the stream contract not applicable rather
than incomplete. HTTP status is a fact. Optional analyzer output may help interpret:

```text
validation_rejection      remove + HTTP 400/422 + structured field_required
value_constraint          replace + invalid enum/type/format
conflict                  HTTP 409, including duplicate ID/version conflict
authentication_failure    401/403
rate_limited              429
server_failure            5xx
unknown_rejection
unexpected_redirect
response_contract_mismatch
```

The optional analyzer response contains classification, observations, validation
evidence, and hints only. A strict remove-field `validation_rejection`
can support a required-field hypothesis, but the Skill or analyst must combine it
with persistent-state verification. A replace rejection supports a value-constraint
hypothesis, not requiredness. Natural-language field mentions are weak hints only.
Use an exact bounded response body artifact rather than an incomplete preview.

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

Do not reject a Stop experiment merely because the ideal before/after observations
are missing. Run it, preserve the evidence, mark cancellation attribution as
unclassified, and design the next experiment with stronger checkpoints.

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
- If a replay mutation observation reports `mutation_effective=false`, discard the
  field conclusion and fix the matcher or mutation path.
- If a source request is stateful, run an explicit zero-mutation baseline replay,
  then submit the setup and source settings again for each mutation replay.
- Use `generated` for one-time IDs, timestamps, and nonce values. Use
  `preserve_source` for existing parent/conversation context. Use `literal` or
  `manual_input` only when the experiment explicitly supplies that value.
- If replay request correlation is ambiguous, do not choose a candidate manually; recapture with a narrower source request.
- Environment comparison is opt-in. Compare only the selected generic dimensions
  in `pre_dispatch_environment`. `post_response_environment` and
  `post_verification_environment` are outcomes, not prerequisites.
- Browser-managed credential values stay local. The manifest stores only
  SHA-256 digests for Cookie name/value pairs, Authorization, CSRF, and the
  combined request context. This is change detection, not encryption or key
  management.
- Treat request context as observed only when exact request headers are proven complete, including ExtraInfo/associatedCookies or an explicit completeness marker. Preserve Cookie wire order in the hash. Ignore lists default to empty.
- Post-response and post-verification environments contain page state only unless a new probe is captured; they must not reuse pre-dispatch request credentials.
- Lock replay primary stream evidence to the exact ordinary replay request. Same-endpoint streams remain supporting evidence.
- Replay correlation requires a numeric observed timestamp inside the bounded
  dispatch window. Missing timestamps do not participate in automatic matching.
- If an experiment is submitted incorrectly, call `cancel_experiment`; do not close the session or restart the service.
- If `changed_during_read=true`, do not cite the file hash as stable. Re-read after the experiment reaches a terminal state.
- If an evidence or replay result is partial, preserve the uncertainty in reports and `notes/open-questions.md`.

## Completion criteria

The protocol reproduction is complete only when:

- all six scenarios have an explicit experiment chain;
- primary ordinary and streaming requests have stable evidence IDs;
- request shapes and redacted request bodies expose mutation paths without exposing values;
- request construction has initiator and source evidence;
- important fields have an explicit zero-mutation baseline and one-target mutation
  observations with exact source/comparison evidence IDs;
- every field conclusion verifies the target wire delta and the relevant persisted
  state on exact outbound requests;
- any environment comparison uses explicitly selected generic dimensions, with
  missing facts preserved as missing;
- streaming requests have `stream_request` and `stream_event_range` evidence;
- stream events and conversation state transitions are documented;
- Stop behavior is observed rather than assumed;
- every core report conclusion is traceable to experiment, evidence, and artifact IDs;
- credentials are absent from natural-language outputs and generated source files;
- unresolved behavior is listed explicitly rather than guessed.
