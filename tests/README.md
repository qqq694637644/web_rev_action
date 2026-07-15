# Test structure

Tests are organized by capability rather than by one end-to-end product scenario.

```text
tests/
  browser/     capture, steps, replay, sessions, finalization, transports
  evidence/    network observations and stream association
  protocol/    mutation, matching, analyzer and evidence primitives
  workspace/   inspect, search, write and focused PowerShell behavior
  runtime/     standalone browser replay JavaScript
  smoke/       generic synthetic authenticated stateful streaming fixture
  fakes/       adapter fakes and composable external-behavior scenarios
```

## Commands

Run the complete Python suite:

```powershell
$env:PYTHONPATH='src;.'
pytest -q
```

Run one capability:

```powershell
pytest -q tests/browser/test_replay_execution.py
pytest -q tests/evidence/test_streams.py
pytest -q tests/protocol/test_response_analyzers.py
pytest -q tests/workspace/test_inspect.py
```

Run the standalone replay runtime directly with Node:

```powershell
node --test tests/runtime/replay_runtime.test.js
```

Run only the generic HTTP fixture smoke:

```powershell
pytest -q tests/smoke/test_synthetic_fixture.py
```

The real-browser smoke remains:

```powershell
python tools/browser_action_smoke.py
```

It requires a real Chrome/CDP endpoint, Playwright CLI, the optional `mcp` package and the js-reverse MCP runtime.

## Fake and scenario rules

`tests/fakes/browser.py` implements external adapter contracts. It should not classify protocol validity or decide experiment quality.

`tests/fakes/scenarios.py` composes external facts such as timeout, cancellation, partial capture and artifact failure. Add new behavior there only when multiple capability tests need the same external scenario.

Analyzer tests should pass structured facts directly and assert observations or hints. Unknown or insufficient evidence is an expected result, not a failed test setup.

The generic smoke contract uses resource, record and cursor terminology with a custom `fixture-complete` terminal marker. Historical product-specific conversation/message/parent assumptions do not belong in these fixtures.
