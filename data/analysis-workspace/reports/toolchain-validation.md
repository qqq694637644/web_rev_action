# Stage 0 Toolchain Validation

## Scope

This report validates the existing toolchain for the minimum browser-analysis loop.
The shared CDP endpoint is treated as a confirmed prerequisite and is not itself
evaluated. The run used an isolated local fixture and a loopback-only browser
session.
No cookie, token, browser profile path, or CDP endpoint is recorded here.

## Reproduction

```powershell
python tools/toolchain_validation.py
```

The fixture application is stored under
`tests/fixtures/toolchain_validation/`; JavaScript is loaded from `app.js` rather
than constructed inline by the validation runner.

## Environment

- Python: `3.13.0`
- playwright-cli: `0.1.17`
- js-reverse-mcp: `4.0.1`
- Browser: system Google Chrome, isolated temporary profile, headless mode

## Results

| Requirement | Result |
| --- | --- |
| Playwright current page aligns with js-reverse target | **PASS** |
| Network request capture | **PASS** |
| Request and response body export | **PASS** |
| SSE event sequence preservation | **FAIL** |
| Request initiator | **PASS** |
| Script read and search | **PASS** |
| XHR/fetch breakpoint pause and resume | **PASS** |
| Workspace file write | **PASS** |

## Evidence

### Playwright current page aligns with js-reverse target: PASS

- Both tools selected the same loopback fixture page.
- Title matched: Stage 0 Toolchain Validation

### Network request capture: PASS

- Captured POST /api/echo.
- Captured EventSource /api/sse.

### Request and response body export: PASS

- Request body: 82 bytes, sha256=126f75c8ae90a5f7a57eb8d65d1ca54ea7b07df75cce43772ce2ddf632a2122b
- Response body: 132 bytes, sha256=3f3b8dc647c422f8581c8f1e056d2b21cab85d4669e50159faa3b4d903a15b2a

### SSE event sequence preservation: FAIL

- Captured the EventSource request itself as network evidence.
- The fixture page observed two ordered data events and the [DONE] marker.
- Error: `js-reverse-mcp 4.0.1 returned INTERNAL: response body is not available because the EventSource body was evicted after navigation.`

### Request initiator: PASS

- Initiator stack identifies app.js and sendEcho().

### Script read and search: PASS

- search_in_sources located stage0RequestBuilder in app.js.
- get_script_source returned the expected source marker.

### XHR/fetch breakpoint pause and resume: PASS

- Future fetch to /api/echo paused with reason XHR in sendEcho().
- Execution resumed successfully.
- XHR breakpoint was removed after the check.

### Workspace file write: PASS

- Python wrote and read back a UTF-8 file in the reports directory.

## Conclusion

Seven of eight required checks passed. The current toolchain is not yet sufficient for the complete Stage 0 acceptance set because it cannot export a completed EventSource response as ordered network evidence. Add EventSource message capture or raw CDP stream capture before relying on SSE evidence in the full Action.

## Limitations

- The SSE check covers a normally completed local EventSource stream with two
  ordered data events and a `[DONE]` marker.
- It does not validate chunk arrival timing, cancellation, network interruption,
  heartbeats, or incomplete streams.
- The test page runs in the main page target; Worker and Service Worker metadata
  remain outside this Stage 0 acceptance set.
