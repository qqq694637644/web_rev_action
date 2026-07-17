# Stage 0 Toolchain Validation

## Scope

This report validates the existing toolchain for the minimum browser-analysis loop.
The shared CDP endpoint is treated as a confirmed prerequisite and is not itself
evaluated. The run used an isolated local fixture and a loopback-only browser
session.
No cookie, token, browser profile path, or CDP endpoint is recorded here.

## Reproduction

```powershell
python tools/toolchain_validation.py --js-reverse-entry <path-to-js-reverse-mcp>/build/src/index.js
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

- Request body: 88 bytes, sha256=179a1efe15cfacc313030d72c086516db71d3e42682413003ca9fdaa2849aadc
- Response body: 138 bytes, sha256=341ef45f5bac809f42453d72ecbcc065f7fd9a9a645ffc3c35381219cc0ccb36

### SSE event sequence preservation: PASS

- start_stream_capture was armed before the Playwright click.
- raw.bin exactly matched all fixture SSE bytes in order.
- events.jsonl preserved both message events and fixture-complete.
- get_stream_status matched fixture-complete inside the collector.
- Artifacts used the stage0-toolchain namespace and relative paths.
- raw.bin: 141 bytes, sha256=4c93f4ed585a9833e0296d1a131f06f54464c3e6136d74baf88e024f70eec5cc

### Request initiator: PASS

- Initiator stack identifies app.js and sendEcho().

### Script read and search: PASS

- search_in_sources located buildEchoRequest in app.js.
- get_script_source returned the expected source marker.

### XHR/fetch breakpoint pause and resume: PASS

- Future fetch to /api/echo paused with reason XHR in sendEcho().
- Execution resumed successfully.
- XHR breakpoint was removed after the check.

### Local evidence file write: PASS

- Python wrote and read back a UTF-8 file in the Action-local evidence directory.

## Conclusion

All required Stage 0 checks passed. The toolchain now validates the actual Raw Stream Capture lifecycle: the collector is armed before the browser action, exact raw bytes and ordered semantic events are written under an experiment namespace, the fixture-complete predicate is matched inside the collector, and the capture finalizes with relative artifact paths.

## Limitations

- The SSE check covers a normally completed local EventSource stream with two
  ordered data events and a `fixture-complete` marker.
- It validates exact normal-completion bytes, event ordering, namespace,
  collector-side predicate matching, and finalization.
- Cancellation, network interruption, heartbeats, and incomplete streams remain
  later experiment-specific validation cases.
- The test page runs in the main page target; Worker and Service Worker metadata
  remain outside this Stage 0 acceptance set.
