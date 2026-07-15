from __future__ import annotations

import ast
import tomllib
import unittest
from pathlib import Path

from skill_temple import browser_adapters
from skill_temple.browser.adapters import contracts
from skill_temple.browser.operations.capture import BrowserCaptureOperations
from skill_temple.browser.operations.evidence import BrowserEvidenceOperations
from skill_temple.browser.operations.finalization import BrowserFinalizationOperations
from skill_temple.browser.operations.inspection import BrowserInspectionOperations
from skill_temple.browser.operations.replay import BrowserReplayOperations
from skill_temple.browser.operations.session import BrowserSessionOperations
from skill_temple.browser.replay_runtime import load_replay_runtime
from skill_temple.browser_models import RequestMatcher
from skill_temple.browser_service import BrowserActionService
from skill_temple.protocol.analyzers.differences import (
    aggregate_dimension_status,
    compare_dimension,
    compare_environment_facts,
    select_current_stream_summary,
)
from skill_temple.protocol.analyzers.response import analyze_replay_response
from skill_temple.protocol.matching import network_request_matches
from skill_temple.protocol_evidence import analyze_replay_response as compatibility_analyzer


class StageEBoundaryTests(unittest.TestCase):
    def test_browser_action_service_is_a_thin_facade(self) -> None:
        path = Path("src/skill_temple/browser_service.py")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        service = next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "BrowserActionService"
        )
        methods = {
            node.name
            for node in service.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        self.assertEqual(methods, {"__init__", "run", "close"})
        self.assertLess(len(source.splitlines()), 400)
        self.assertIn("return await dispatch_browser_request(self, request)", source)
        self.assertNotIn("def _capture_flow", source)
        self.assertNotIn("def _finalize_experiment_runtime", source)
        self.assertNotIn("def _export_network_evidence", source)

    def test_specialized_operation_boundaries_own_expected_methods(self) -> None:
        self.assertTrue(hasattr(BrowserCaptureOperations, "_capture_flow"))
        self.assertTrue(
            hasattr(BrowserFinalizationOperations, "_finalize_experiment_runtime")
        )
        self.assertTrue(hasattr(BrowserEvidenceOperations, "_export_network_evidence"))
        self.assertTrue(hasattr(BrowserReplayOperations, "_prepare_replay_execution"))
        self.assertTrue(hasattr(BrowserInspectionOperations, "inspect"))
        self.assertTrue(hasattr(BrowserSessionOperations, "_open_session"))

        expected = {
            BrowserCaptureOperations,
            BrowserFinalizationOperations,
            BrowserReplayOperations,
            BrowserEvidenceOperations,
            BrowserInspectionOperations,
            BrowserSessionOperations,
        }
        self.assertTrue(expected.issubset(set(BrowserActionService.__mro__)))

    def test_replay_runtime_is_a_standalone_javascript_asset(self) -> None:
        runtime_path = Path("src/skill_temple/browser/replay_runtime.js")
        runtime = runtime_path.read_text(encoding="utf-8")
        adapter = Path("src/skill_temple/browser_adapters.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(load_replay_runtime(), runtime)
        self.assertIn("async ({localFile}) => {", runtime)
        self.assertIn("function = load_replay_runtime()", adapter)
        self.assertNotIn("async ({localFile}) => {", adapter)
        package_config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        package_data = package_config["tool"]["setuptools"]["package-data"]["skill_temple"]
        self.assertIn("browser/*.js", package_data)

    def test_external_adapter_contracts_remain_compatible_exports(self) -> None:
        for name in [
            "AdapterError",
            "AlignmentResult",
            "CommandRunner",
            "JsReverseAdapter",
            "McpToolTransport",
            "PlaywrightAdapter",
            "StreamCheckpoint",
        ]:
            with self.subTest(name=name):
                self.assertIs(
                    getattr(browser_adapters, name),
                    getattr(contracts, name),
                )

    def test_difference_analyzer_is_pure_and_factual(self) -> None:
        reference = {
            "facts": {
                "raw_event_count": 2,
                "semantic_event_count": 2,
                "terminal_reason": "network_close",
                "primary_event_source": "fetch-stream",
            },
            "sources": {"network_evidence_id": "ev_exact"},
        }
        summary, status = select_current_stream_summary([reference], "ev_exact")
        missing, missing_status = select_current_stream_summary([reference], "ev_missing")

        self.assertIsNone(status)
        self.assertEqual(summary["raw_event_count"], 2)
        self.assertIsNone(missing)
        self.assertEqual(missing_status, "missing")
        self.assertEqual(
            compare_dimension(summary, dict(summary))["status"],
            "equivalent",
        )
        self.assertEqual(
            compare_dimension(summary, None, current_status="ambiguous")["status"],
            "ambiguous",
        )
        environment = compare_environment_facts(
            {"page_id": "page-a"},
            {"page_id": "page-b"},
            ["page_id", "request_origin"],
        )
        self.assertEqual(environment["status"], "different")
        self.assertEqual(
            aggregate_dimension_status(environment["dimensions"]),
            "different",
        )

        analyzer_source = Path(
            "src/skill_temple/protocol/analyzers/differences.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn("browser_service", analyzer_source)
        self.assertNotIn("ExperimentStore", analyzer_source)

    def test_matching_and_response_analyzer_have_direct_boundaries(self) -> None:
        self.assertTrue(
            network_request_matches(
                {
                    "reqid": 7,
                    "url": "https://example.test/api/items",
                    "method": "POST",
                    "resourceType": "fetch",
                },
                RequestMatcher(
                    url_contains="/api/items",
                    method="POST",
                    resource_types=["fetch"],
                ),
            )
        )
        self.assertIs(analyze_replay_response, compatibility_analyzer)
        result = analyze_replay_response(
            status=500,
            content_type="application/json",
            response_value={"error": "server failure"},
            mutation=None,
        )
        self.assertEqual(result["classification"], "server_failure")

    def test_workspace_capabilities_remain_available(self) -> None:
        routes = Path("src/skill_temple/workspace_routes.py").read_text(
            encoding="utf-8"
        )
        service = Path("src/skill_temple/workspace_service.py").read_text(
            encoding="utf-8"
        )
        for operation in [
            "workspaceInspect",
            "workspaceSearch",
            "workspaceReadFiles",
            "workspaceWriteFile",
            "workspaceExecPwsh",
        ]:
            with self.subTest(operation=operation):
                self.assertIn(operation, routes)
        self.assertIn("class AnalysisWorkspaceService", service)
        self.assertIn("async def inspect", service)
        self.assertIn("async def search", service)
        self.assertIn("async def exec_pwsh", service)

    def test_stage_e_did_not_add_generic_architecture_layers(self) -> None:
        forbidden = {"BaseService", "Manager", "Factory", "Repository"}
        observed: set[str] = set()
        for path in Path("src/skill_temple/browser").rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            observed.update(
                node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)
            )
        self.assertTrue(forbidden.isdisjoint(observed))


if __name__ == "__main__":
    unittest.main()
