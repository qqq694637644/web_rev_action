# Strict JSON encoding

Construct the domain payload as a JSON object, serialize it exactly once, then put
that serialized text in `payload_json`.

Correct:

```json
{
  "contract_version": "2.0",
  "operation": "list_experiments",
  "payload_json": "{\"session_id\":\"analysis-main\",\"limit\":20}",
  "skill_id": "browser-action-protocol",
  "skill_content_hash": "sha256:<copy from loadSkills>",
  "operation_contract_hash": "sha256:<copy from docs/inspect/list-experiments.md>"
}
```

Incorrect forms include a nested object instead of a string, double serialization,
duplicate object keys, `NaN`, `Infinity`, arrays, scalars, and trailing comments.

Use standard JSON escaping for quotes, backslashes, control characters, and Unicode.
Do not hand-copy credentials into payloads. Browser replay identifies local source
evidence by IDs.
