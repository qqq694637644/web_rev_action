# Stale contract recovery

`stale_operation_contract` is a pre-dispatch version mismatch.

1. Reload `browser-action-protocol` with `loadSkills`.
2. Use the returned `content_hash` as `skill_content_hash`.
3. Read the exact operation contract again.
4. Copy its generated `operation_contract_hash`.
5. Rebuild the complete six-field envelope.
6. Retry only after all expected hashes match.

Do not edit, truncate, or infer hashes. A stale Builder schema should be re-imported.
