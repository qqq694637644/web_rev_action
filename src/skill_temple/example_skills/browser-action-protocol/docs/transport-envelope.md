# Transport envelope

Both Browser Actions expose the same stable six-field object:

```json
{
  "contract_version": "2.0",
  "operation": "get_session",
  "payload_json": "{\"session_id\":\"analysis-main\"}",
  "skill_id": "browser-action-protocol",
  "skill_content_hash": "sha256:<copy from loadSkills>",
  "operation_contract_hash": "sha256:<copy from the exact generated operation contract>"
}
```

Decoded payload:

```json
{"session_id":"analysis-main"}
```

Fields:

- `contract_version`: required literal `2.0`.
- `operation`: required plain string from `docs/operation-index.md`.
- `payload_json`: required strict JSON string, maximum 262144 characters, decoding
  to one object.
- `skill_id`: required literal `browser-action-protocol`.
- `skill_content_hash`: required exact `content_hash` returned when this Skill is loaded.
- `operation_contract_hash`: required exact hash printed in the selected operation doc.

Use `runBrowserExperiment` only for operations in the run section. Use
`inspectBrowserEvidence` only for operations in the inspect section. A cross-Action
operation is rejected before dispatch.

The envelope must not contain extra fields. The decoded object must not contain an
operation, transport version, binding hash, or wrapper object. A hash mismatch returns
`stale_operation_contract` before dispatch.
