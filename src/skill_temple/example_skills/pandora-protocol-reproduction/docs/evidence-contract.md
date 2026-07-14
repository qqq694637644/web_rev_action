# Evidence contract

## Stable reference tuple

Every core conclusion should cite:

```text
experiment_id
evidence_id
artifact_id
```

`experiment_id` identifies the atomic browser run. `evidence_id` identifies a semantic fact within that run. `artifact_id` identifies the supporting file.

## Evidence kinds

Current evidence kinds include:

```text
network_request
stream_request
stream_event_range
console_message
page_screenshot
page_snapshot
replay_attempt
script_source
```

A network evidence entry contains stable request identities, selector ID, collector generation, step IDs, artifact IDs, artifact paths, and a redacted summary.

## Credential boundary

Artifacts marked:

```text
sensitivity = credential
containsCredentials = true
```

must not be copied into chat, reports, schemas, source code, or diffs. Public Action responses return metadata and redacted summaries only.

For replay, the backend reads the exact artifact locally and creates the browser-context request. The Skill supplies only evidence IDs and structured mutations.

## Reading artifacts

Use the following order:

1. `list_evidence` to discover stable evidence IDs.
2. `get_network_evidence`, paginated `get_request_shape`, `get_request_initiator`, or `list_console_errors` for bounded summaries.
3. `workspaceReadFiles` for non-credential text artifacts.
4. `workspaceExecPwsh` for binary, offset, compressed, or hash analysis.

If `changed_during_read=true`, the file changed during inspection. Do not cite its SHA or treat the response as a stable snapshot.

## Source evidence

For request-construction conclusions, preserve:

```text
network evidence ID
initiator artifact ID
script URL or script ID
source line/offset range
source artifact or bounded source response
```

A broad script search is supporting evidence, not proof that a function initiated the request. Prefer saved initiator evidence, then persist the selected source region with `save_script_source` so URL/script ID/range/SHA-256 remain auditable after navigation.

## Replay evidence

A replay attempt should link:

```text
source_experiment_id
source_evidence_id
mutation list
extractor definitions and extractor observations
binding metadata and binding observations
requested_replay_protocol_hash
replay_protocol_hash
setup_flow and setup step results
replay_attempt_id and dispatch time
exact replay network evidence ID
mutation observations
optional comparison references and dimension results
optional response analyzer evidence ID
pre-dispatch, post-response, and post-verification environments
request diff artifact
replay response artifact
extractor snapshot artifact IDs
new network evidence IDs
stream request/event-range evidence IDs
page and console evidence IDs
```

Request context is `observed` only when exact header completeness is proven by an explicit marker or associated request headers + ExtraInfo/associatedCookies evidence. Empty or ordinary header arrays are insufficient. Post environments do not reuse the pre-dispatch request context.

Replay primary stream evidence must reference the exact replay ordinary evidence ID.
Same-URL streams without that stable association are supporting evidence.

`requested_replay_protocol_hash` identifies the normalized caller request.
`replay_protocol_hash` identifies the effective protocol after stream detection has
upgraded capture and evidence requirements. Stream comparison must use the unique
observation whose `sources.network_evidence_id` equals the replay evidence ID.

A replay response status alone is insufficient to determine field necessity.
Comparison is optional and fact-based. Each reference must contain an exact
`evidence_id` or `observation_id`; selecting the first request in an experiment is
not allowed. Results use `equivalent`, `different`, `missing`, `ambiguous`, or
`unknown`. Environment dimensions are opt-in pre-dispatch facts. Post-response and
post-verification environments are outcomes.

Field validation must use an exact structured response body. Priority is exact
network response body, then a complete bounded replay response body, then preview
as an inconclusive fallback. Remove + field-required can support required;
replace rejection supports a value constraint; 409 is conflict evidence.

These are evidence interpretations, not backend verdicts. The Action must not emit
final field-necessity conclusions; the Skill and analyst combine response facts,
wire mutation evidence, environment context, and persisted UI/server state.

Ordinary network snapshot integrity and stream artifact integrity are separate. An exact request snapshot may prove request headers/body completeness, but it cannot upgrade missing `raw.bin`, event JSONL, or stream metadata.

For non-stream errors, record response header/body completeness separately. A
request object or `all.json` alone does not make the response evidence complete.

## Evidence quality

Use these labels in reports:

```text
confirmed     direct artifact and repeatable experiment
supported     multiple consistent observations, one indirect edge
partial       usable evidence with known missing dimensions
unknown       no reliable observation
contradicted  repeatable evidence conflicts with the current hypothesis
```
