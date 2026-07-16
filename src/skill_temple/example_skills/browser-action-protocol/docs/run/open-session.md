# `open_session`

## Contract

- **Operation:** `open_session`
- **Action:** `runBrowserExperiment`
- **Purpose:** attach one owned Playwright session to the configured browser and optionally select or navigate a page.
- **Consequential:** yes; it can create session state and navigate.
- **Prerequisites:** read `docs/transport-envelope.md`; inspect a known ID with `get_session` before reopening it.

## Decoded payload schema

Required fields: none.

Optional fields and defaults:

- `session_id`: safe identifier matching `[A-Za-z0-9_.-]+`; generated when omitted; max 128 characters.
- `browser_endpoint`: absolute configured CDP endpoint; max 8192 characters.
- `target`: page selector with optional `start_url`, `expected_url_contains`, or `page_index`.
- `deadline_ms`: 1000–42000; default 15000.

Constraints: when the private MCP endpoint is configured, `browser_endpoint` must match it. Prefer page selection over navigation unless navigation is intentional.

Decoded example:

```json
{"session_id":"analysis-main","target":{"page_index":0},"deadline_ms":15000}
```

## Complete Action envelope

> Generated binding values are build-specific. Copy all six fields exactly.

```json
{
  "contract_version": "2.0",
  "operation": "open_session",
  "operation_contract_hash": "sha256:ffad1100a262f17bb106b9584a2d7d839a1b1d0e6b3d6e863dbe9b279639c941",
  "payload_json": "{\"deadline_ms\":15000,\"session_id\":\"analysis-main\",\"target\":{\"page_index\":0}}",
  "skill_content_hash": "sha256:c946be3a448d82b76d66ab102a92b09185bf02beda384e1db695c229ab3a45ba",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `session_id`, session status, selected page metadata, and alignment metadata.

Safe retry: correct validation errors only when `dispatch_started=false`. If dispatch started or the outcome is unknown, call `get_session` with the intended ID before deciding whether to retry.

Typical errors: `invalid_operation_payload`, `browser_endpoint_mismatch`, `browser_busy`, `page_alignment_failed`, `operation_outcome_unknown`.

Next recommended inspect operation: `get_session`.

Contract hash: `sha256:ffad1100a262f17bb106b9584a2d7d839a1b1d0e6b3d6e863dbe9b279639c941`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Contract binding

> Generated from the public operation contract. Do not edit this block.

- Action: `run`
- Consequential: `true`
- Operation contract hash: `sha256:ffad1100a262f17bb106b9584a2d7d839a1b1d0e6b3d6e863dbe9b279639c941`
<!-- END GENERATED CONTRACT -->
