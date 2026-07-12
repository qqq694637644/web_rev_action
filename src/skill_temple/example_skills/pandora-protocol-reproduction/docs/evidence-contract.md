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
console_message
page_screenshot
page_snapshot
replay_attempt
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
2. `get_network_evidence`, `get_request_initiator`, or `list_console_errors` for bounded summaries.
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

A broad script search is supporting evidence, not proof that a function initiated the request. Prefer saved initiator evidence.

## Replay evidence

A replay attempt should link:

```text
source_experiment_id
source_evidence_id
mutation list
request diff artifact
replay response artifact
new network evidence IDs
page and console evidence IDs
```

A replay response status alone is insufficient to determine field necessity. Compare the persisted conversation state and subsequent retrieval when relevant.

## Evidence quality

Use these labels in reports:

```text
confirmed     direct artifact and repeatable experiment
supported     multiple consistent observations, one indirect edge
partial       usable evidence with known missing dimensions
unknown       no reliable observation
contradicted  repeatable evidence conflicts with the current hypothesis
```
