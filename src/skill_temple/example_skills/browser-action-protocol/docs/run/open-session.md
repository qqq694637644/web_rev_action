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
  "operation_contract_hash": "sha256:4d59376ce04dad1ac0455239ddbb20d5a2dea63c5cd147ac7dbd196ea1f4ad60",
  "payload_json": "{\"deadline_ms\":15000,\"session_id\":\"analysis-main\",\"target\":{\"page_index\":0}}",
  "skill_content_hash": "sha256:f66c3d13f268880a753a0f46098997becee5f0b3ef299232d63f7fe0ef5f7d24",
  "skill_id": "browser-action-protocol"
}
```
## Result and recovery

Expected response handles: `session_id`, session status, selected page metadata, and alignment metadata.

Safe retry: correct validation errors only when `dispatch_started=false`. If dispatch started or the outcome is unknown, call `get_session` with the intended ID before deciding whether to retry.

Typical errors: `invalid_operation_payload`, `browser_endpoint_mismatch`, `browser_busy`, `page_alignment_failed`, `operation_outcome_unknown`.

Next recommended inspect operation: `get_session`.

Contract hash: `sha256:4d59376ce04dad1ac0455239ddbb20d5a2dea63c5cd147ac7dbd196ea1f4ad60`. Send it in `operation_contract_hash`.

<!-- BEGIN GENERATED CONTRACT -->
## Generated structural contract

> Generated from `OperationRegistry` and Pydantic. Do not edit this block.

- Request model: `OpenSessionRequest`
- Payload model: `OpenSessionPayload`
- Registry handler: `dispatch_open_session`
- Consequential: `true`
- Operation contract hash: `sha256:4d59376ce04dad1ac0455239ddbb20d5a2dea63c5cd147ac7dbd196ea1f4ad60`

```json
{
  "$defs": {
    "BrowserTarget": {
      "additionalProperties": false,
      "properties": {
        "expected_url_contains": {
          "anyOf": [
            {
              "maxLength": 4096,
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Expected Url Contains"
        },
        "page_index": {
          "anyOf": [
            {
              "maximum": 100,
              "minimum": 0,
              "type": "integer"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Page Index"
        },
        "start_url": {
          "anyOf": [
            {
              "maxLength": 8192,
              "type": "string"
            },
            {
              "type": "null"
            }
          ],
          "default": null,
          "title": "Start Url"
        }
      },
      "title": "BrowserTarget",
      "type": "object"
    }
  },
  "additionalProperties": false,
  "properties": {
    "browser_endpoint": {
      "anyOf": [
        {
          "maxLength": 8192,
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Browser Endpoint"
    },
    "deadline_ms": {
      "default": 15000,
      "maximum": 42000,
      "minimum": 1000,
      "title": "Deadline Ms",
      "type": "integer"
    },
    "session_id": {
      "anyOf": [
        {
          "maxLength": 128,
          "pattern": "^[a-zA-Z0-9_.-]+$",
          "type": "string"
        },
        {
          "type": "null"
        }
      ],
      "default": null,
      "title": "Session Id"
    },
    "target": {
      "$ref": "#/$defs/BrowserTarget"
    }
  },
  "title": "OpenSessionPayload",
  "type": "object"
}
```
<!-- END GENERATED CONTRACT -->
