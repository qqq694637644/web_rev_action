from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path

TEST_ROOT = Path("tests")


def test_legacy_giant_test_files_are_removed() -> None:
    for path in [
        TEST_ROOT / "test_browser_actions.py",
        TEST_ROOT / "test_protocol_evidence.py",
        TEST_ROOT / "test_workspace_actions.py",
    ]:
        assert not path.exists(), path


def test_capability_test_directories_have_direct_entry_points() -> None:
    required = {
        "browser/test_capture.py",
        "browser/test_steps.py",
        "browser/test_replay_execution.py",
        "browser/test_replay_comparison.py",
        "browser/test_replay_extractors.py",
        "browser/test_replay_readers.py",
        "browser/test_finalization.py",
        "browser/test_sessions.py",
        "browser/test_transports.py",
        "evidence/test_network_observations.py",
        "evidence/test_streams.py",
        "protocol/test_mutations.py",
        "protocol/test_matching.py",
        "protocol/test_response_analyzers.py",
        "workspace/test_inspect.py",
        "workspace/test_search.py",
        "workspace/test_write.py",
        "workspace/test_powershell.py",
        "runtime/test_replay_runtime.py",
        "runtime/replay_runtime.test.js",
        "smoke/test_synthetic_fixture.py",
        "fakes/browser.py",
        "fakes/scenarios.py",
    }
    observed = {
        path.relative_to(TEST_ROOT).as_posix()
        for path in TEST_ROOT.rglob("*")
        if path.is_file()
    }
    assert required.issubset(observed)


def test_no_python_test_file_returns_to_giant_single_file_scale() -> None:
    oversized = {}
    for path in TEST_ROOT.rglob("*.py"):
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        if line_count > 1_000:
            oversized[path.as_posix()] = line_count
    assert oversized == {}


def test_replay_runtime_is_standalone_packaged_and_node_tested() -> None:
    runtime_path = Path("src/skill_temple/browser/replay_runtime.js")
    adapter_source = Path(
        "src/skill_temple/browser/adapters/js_reverse.py"
    ).read_text(encoding="utf-8")
    runtime_tests = Path("tests/runtime/replay_runtime.test.js").read_text(encoding="utf-8")
    package_config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package_data = package_config["tool"]["setuptools"]["package-data"]["skill_temple"]

    assert runtime_path.is_file()
    assert runtime_path.stat().st_size > 10_000
    assert "function = load_replay_runtime()" in adapter_source
    assert "async ({localFile}) => {" not in adapter_source
    assert "browser/*.js" in package_data
    for required_case in [
        "SSE accepts LF, CRLF, CR-only",
        "UTF-8 decoder preserves multibyte",
        "NDJSON handles chunk boundaries",
        "raw stream records exact chunk boundaries",
        "byte and event limits cancel",
        "exact byte boundary is complete and not truncated",
        "SSE max_events counts complete events",
        "raw max_events counts accepted chunks",
        "network_close waits through delayed reads without idle_window",
        "CR-only EOF exact marker terminates correctly",
        "idle-window and text-pattern termination",
    ]:
        assert required_case in runtime_tests


def test_fakes_and_scenario_builders_do_not_own_business_conclusions() -> None:
    fake_source = Path("tests/fakes/browser.py").read_text(encoding="utf-8")
    scenario_source = Path("tests/fakes/scenarios.py").read_text(encoding="utf-8")

    assert "class FakePlaywright" in fake_source
    assert "class FakeJsReverse" in fake_source
    assert "class BrowserScenario" in scenario_source
    assert "artifact_failure_scenario" in scenario_source
    assert "timeout_scenario" in scenario_source
    assert "cancellation_scenario" in scenario_source
    assert "analyze_replay_response" not in fake_source
    assert "protocol_evidence" not in fake_source
    assert "quality_summary" not in scenario_source
    assert "validation_rejection" not in scenario_source


def test_generic_smoke_has_no_historical_product_contract() -> None:
    paths = [
        Path("tests/fixtures/toolchain_validation/app.js"),
        Path("tools/toolchain_validation_server.py"),
        Path("tools/browser_action_smoke.py"),
        Path("tests/smoke/test_synthetic_fixture.py"),
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in paths)
    forbidden_patterns = [
        r"\bPandora\b",
        r"/conversation",
        r"\bconversation_key\b",
        r"\bparent_message_id\b",
        r"\[DONE\]",
        r"buildConversationRequest",
        r'"messages"\s*:',
        r'event:\s*message',
    ]
    for pattern in forbidden_patterns:
        assert re.search(pattern, source, flags=re.IGNORECASE) is None, pattern

    for expected_status in ["200", "401", "409", "422", "500"]:
        assert expected_status in Path("tests/smoke/test_synthetic_fixture.py").read_text(
            encoding="utf-8"
        )


def test_product_specific_replay_conclusion_tests_are_absent() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in TEST_ROOT.rglob("*.py")
        if path.name != "test_stage_f_boundaries.py"
    )
    for forbidden in [
        "control_preset",
        "treatment_preset",
        "exploratory_preset",
        "ReplayControlPayload",
        "ReplayTreatmentPayload",
        '"replay_mode": "control"',
        "fixed six scenarios",
    ]:
        assert forbidden not in source


def test_stage_f_tests_use_stage_e_direct_capability_imports() -> None:
    forbidden_evidence_names = {
        "analyze_replay_response",
        "binding_value_from_snapshot",
        "build_replay_spec",
        "network_checkpoint",
        "network_request_matches",
        "redacted_request_body_from_snapshot",
        "request_shape_from_snapshot",
        "requests_after_checkpoint",
    }
    violations: list[str] = []
    for path in TEST_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "skill_temple.browser_adapters":
                violations.append(f"{path}: legacy browser_adapters import")
            if node.module == "skill_temple.protocol_evidence":
                imported = {alias.name for alias in node.names}
                legacy = sorted(imported & forbidden_evidence_names)
                if legacy:
                    violations.append(f"{path}: protocol_evidence legacy names {legacy}")
    assert violations == []


def test_test_method_names_are_unique_after_mechanical_split() -> None:
    owners: dict[str, list[str]] = {}
    for path in TEST_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
                "test_"
            ):
                owners.setdefault(node.name, []).append(path.as_posix())
    duplicates = {name: paths for name, paths in owners.items() if len(paths) > 1}
    assert duplicates == {}
