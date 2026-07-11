# Shared CDP validation

This validation answers one narrow question from `PLAN.md`: can `playwright-cli` and
`js-reverse-mcp` attach to the same Chrome CDP endpoint and complete the following loop?

```text
open an authenticated page
→ attach both tools to one CDP endpoint
→ send one message with playwright-cli
→ capture the POST request body with js-reverse-mcp
→ capture the SSE response body
→ read the request initiator
→ locate and read the related JavaScript source
→ save all evidence as a workflow artifact
```

## Automatic fixture mode

Pull requests run `fixture` mode. The runner starts a local application that:

- requires an authentication cookie before `/app` can open;
- sends a JSON request to `/api/conversation`;
- returns a multi-event `text/event-stream` response ending in `[DONE]`;
- serves the request construction code from `/app.js`.

The workflow restores the authentication cookie through a Playwright storage-state file,
launches Chromium with a remote debugging port, and then connects both upstream tools.
This mode proves the integration mechanics without external credentials or a changing website.

## Live mode

Run **Validate shared CDP reverse-engineering flow** manually and select `live`. Supply:

- `target_url`: the page that should open in its authenticated state;
- `auth_check_text`: text visible only after login;
- `message_locator_text`: unique text on the message input's Playwright snapshot line;
- `submit_locator_text`: unique text on the submit control's snapshot line, or leave empty to press Enter;
- `request_url_filter`: a unique substring of the message POST URL;
- `script_search_query`: source text to locate, normally the endpoint substring;
- `test_message`: the message to send;
- `wait_after_submit_ms`: enough time for the SSE response to finish.

Create the repository secret `BROWSER_STORAGE_STATE_B64` from a Playwright storage-state JSON:

```bash
base64 -w 0 auth-state.json
```

On macOS use:

```bash
base64 < auth-state.json | tr -d '\n'
```

The storage-state JSON is decoded into a temporary runner directory and is not included in the
uploaded validation artifact.

The validation orchestration is implemented entirely in `run_validation.py`. It launches
Chromium, invokes `playwright-cli` with argument arrays, uses the Python MCP SDK to call
`js-reverse-mcp`, runs the authenticated fixture server, and writes the final evidence bundle.

## Result

The workflow fails unless all of these checks pass:

- the CDP endpoint is reachable;
- `playwright-cli` attached and sees the target page;
- `js-reverse-mcp` attached and sees the same page;
- the authenticated marker text is visible;
- the target POST request and its body were captured;
- the response contains SSE `data:` events;
- request initiator evidence was returned;
- a matching loaded script was found and read;
- request, response, initiator, source, and summary files were saved.

The `dual-cdp-validation-<run_id>` artifact contains `summary.md`, `summary.json`, exported
request/response files, MCP call results, browser logs, and Playwright command results.
