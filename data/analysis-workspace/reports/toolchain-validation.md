# Stage 0 Toolchain Validation

## Scope

This report validates the existing toolchain for the minimum browser-analysis loop.
The shared CDP endpoint is treated as a confirmed prerequisite and is not itself
evaluated. The run used an isolated local fixture and a loopback-only browser
session.
No cookie, token, browser profile path, or CDP endpoint is recorded here.

## Reproduction

```powershell
python tools/toolchain_validation.py --js-reverse-entry <path-to-js-reverse-mcp>/build/src/main.js
```

The fixture application is stored under
`tests/fixtures/toolchain_validation/`; JavaScript is loaded from `app.js` rather
than constructed inline by the validation runner.

## Environment

- Python: `3.13.0`
- playwright-cli: `0.1.17`
- js-reverse-mcp: `4.0.1 (local entry)`
- Browser: system Google Chrome, isolated temporary profile, headless mode

## Results

| Requirement | Result |
| --- | --- |
| Playwright current page aligns with js-reverse target | **PASS** |
| Network request capture | **PASS** |
| Request and response body export | **PASS** |
| SSE event sequence preservation | **PASS** |
| Request initiator | **PASS** |
| Script read and search | **PASS** |
| XHR/fetch breakpoint pause and resume | **PASS** |
| Local evidence file write | **PASS** |

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

### SSE event sequence preservation: PASS

- start_stream_capture was armed before the Playwright click.
- raw.bin exactly matched all fixture SSE bytes in order.
- events.jsonl preserved both message events and the [DONE] event.
- get_stream_status matched [DONE] internally without returning its body.
- Artifacts used the stage0-toolchain namespace and relative paths.
- raw.bin: 131 bytes, sha256=86c529b54b4a660d3ddd6645a1af517578127f5183a78fab699ed8aea79b9398

### Request initiator: PASS

- Initiator stack identifies app.js and sendEcho().

### Script read and search: PASS

- search_in_sources located stage0RequestBuilder in app.js.
- get_script_source returned the expected source marker.

### XHR/fetch breakpoint pause and resume: PASS

- Future fetch to /api/echo paused with reason XHR in sendEcho().
- Execution resumed successfully.
- XHR breakpoint was removed after the check.

### Local evidence file write: PASS

- Python wrote and read back a UTF-8 file in the Action-local evidence directory.

## Conclusion

All required Stage 0 checks passed. The toolchain now validates the actual Raw Stream Capture lifecycle: the collector is armed before the browser action, exact raw bytes and ordered semantic events are written under an experiment namespace, the [DONE] predicate is matched inside the collector, and the capture finalizes with relative artifact paths.

## Limitations

- The SSE check covers a normally completed local EventSource stream with two
  ordered data events and a `[DONE]` marker.
- It validates exact normal-completion bytes, event ordering, namespace,
  collector-side predicate matching, and finalization.
- Cancellation, network interruption, heartbeats, and incomplete streams remain
  later experiment-specific validation cases.
- The test page runs in the main page target; Worker and Service Worker metadata
  remain outside this Stage 0 acceptance set.
