# GPT Builder Release Smoke Checklist

This checklist is the authenticated release gate for the Skill-driven Action contract.
Repository CI and `skill-temple-builder-preflight` must pass before starting it.

## Build under test

Record:

- repository commit SHA;
- generated `dist/GPT_INSTRUCTIONS.md` SHA-256;
- imported OpenAPI SHA-256 or export timestamp;
- `browser-action-protocol` content hash;
- tester, GPT identity, date, browser endpoint, and analysis workspace.

## Builder configuration

1. Generate and copy the complete `dist/GPT_INSTRUCTIONS.md` into GPT Builder Instructions.
2. Re-import the current `/openapi.json`; do not reuse a cached schema.
3. Confirm the only public operation IDs are:
   `loadSkills`, `readSkillContent`, `inspectBrowserEvidence`,
   `runBrowserExperiment`, and the six Workspace Actions.
4. Confirm both Browser tools render the six required fields:
   `contract_version`, `operation`, `payload_json`, `skill_id`,
   `skill_content_hash`, and `operation_contract_hash`.
5. Confirm no Browser request schema displays `oneOf`, `anyOf`, `allOf`, a discriminator,
   or operation-specific request objects.

## Model and Skill behavior

1. Give an unfamiliar-site analysis task.
2. Confirm the model selects `current-site-analysis` from the static catalog.
3. Confirm it calls `loadSkills` with exact IDs and does not query a Skill directory.
4. Confirm it loads `browser-action-protocol` before a Browser Action.
5. Treat each Skill's `referenced_paths` as recommended entry points. Confirm any other
   requested path remains inside the selected Skill directory.
6. Confirm it copies the current protocol Skill hash and the exact operation contract hash.

## Browser transport and recovery

1. Execute a valid `get_session` or `list_experiments` inspect request.
2. Execute `open_session` against the intended browser endpoint.
3. Submit malformed `payload_json`; confirm `dispatch_started=false` and safe correction.
4. Submit a stale Skill or operation hash; confirm `stale_operation_contract`, expected
   hashes, and no browser dispatch.
5. Execute the smallest valid `capture_flow`.
6. For job mode, poll `get_experiment` to a terminal state instead of repeating capture.
7. Simulate or observe an unknown outcome; confirm the model inspects state and does not
   automatically repeat the consequential call.
8. Confirm the terminal manifest records transport version, operation, Skill ID, Skill
   hash, and operation contract hash.

## Evidence and privacy

1. Confirm factual claims cite exact session, experiment, evidence, and artifact handles.
2. Confirm Cookie, Authorization, CSRF, session, and private payload values do not appear
   in chat, generated reports, logs, or copied Action arguments.

## Result

Mark the smoke as **PASS** only when every applicable step has evidence. Record failures,
Builder screenshots/exports, exact error responses, and whether dispatch started. Do not
release a build with a skipped stale-contract, unknown-outcome, or privacy check.
