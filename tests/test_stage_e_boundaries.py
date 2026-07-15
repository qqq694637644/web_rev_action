from __future__ import annotations

import ast
import importlib.util
import tomllib
import unittest
from pathlib import Path

import skill_temple.protocol_evidence as protocol_evidence
from skill_temple.browser.adapters import js_reverse
from skill_temple.browser.operations.capture import BrowserCaptureOperations
from skill_temple.browser.operations.evidence import BrowserEvidenceOperations
from skill_temple.browser.operations.finalization import BrowserFinalizationOperations
from skill_temple.browser.operations.inspection import BrowserInspectionOperations
from skill_temple.browser.operations.replay import BrowserReplayOperations
from skill_temple.browser.operations.replay_analysis import BrowserReplayAnalysisOperations
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
        self.assertTrue(
            hasattr(BrowserReplayAnalysisOperations, "_analyze_replay_evidence_stage")
        )
        self.assertTrue(hasattr(BrowserInspectionOperations, "inspect"))
        self.assertTrue(hasattr(BrowserSessionOperations, "_open_session"))

        expected = {
            BrowserCaptureOperations,
            BrowserFinalizationOperations,
            BrowserReplayOperations,
            BrowserReplayAnalysisOperations,
            BrowserEvidenceOperations,
            BrowserInspectionOperations,
            BrowserSessionOperations,
        }
        self.assertTrue(expected.issubset(set(BrowserActionService.__mro__)))

    def test_operation_modules_use_explicit_imports(self) -> None:
        root = Path("src/skill_temple/browser/operations")
        self.assertFalse((root / "_support.py").exists())
        for path in root.glob("*.py"):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            wildcard_imports = [
                node
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and any(alias.name == "*" for alias in node.names)
            ]
            with self.subTest(path=path.name):
                self.assertEqual(wildcard_imports, [])

    def test_capture_flow_calls_stages_not_runtime_or_analyzers(self) -> None:
        path = Path("src/skill_temple/browser/operations/capture.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        capture_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        capture_flow = next(
            node
            for node in capture_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_capture_flow"
        )
        calls = {
            node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
            for node in ast.walk(capture_flow)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Attribute, ast.Name))
        }
        self.assertLessEqual(capture_flow.end_lineno - capture_flow.lineno + 1, 700)
        self.assertTrue(
            {
                "_prepare_replay_dispatch_stage",
                "_execute_replay_dispatch",
                "_collect_post_flow_evidence",
                "_analyze_replay_evidence_stage",
                "_assemble_observations_stage",
                "_finalize_experiment_runtime",
                "_complete_capture_record",
            }.issubset(calls)
        )
        self.assertTrue(
            {
                "evaluate_browser_replay",
                "analyze_replay_response",
                "build_replay_spec",
                "assess_mutation_effectiveness",
                "_export_network_evidence",
                "_export_console_evidence",
                "_build_replay_comparison_results",
            }.isdisjoint(calls)
        )

    def test_replay_runtime_is_a_standalone_javascript_asset(self) -> None:
        runtime_path = Path("src/skill_temple/browser/replay_runtime.js")
        runtime = runtime_path.read_text(encoding="utf-8")
        adapter = Path("src/skill_temple/browser/adapters/js_reverse.py").read_text(
            encoding="utf-8"
        )

        self.assertEqual(load_replay_runtime(), runtime)
        self.assertIn("async ({localFile}) => {", runtime)
        self.assertIn("function = load_replay_runtime()", adapter)
        self.assertNotIn("async ({localFile}) => {", adapter)
        package_config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
        package_data = package_config["tool"]["setuptools"]["package-data"]["skill_temple"]
        self.assertIn("browser/*.js", package_data)

    def test_adapter_implementations_are_split_by_transport(self) -> None:
        root = Path("src/skill_temple/browser/adapters")
        self.assertFalse(Path("src/skill_temple/browser_adapters.py").exists())
        self.assertTrue((root / "command.py").is_file())
        self.assertTrue((root / "playwright.py").is_file())
        self.assertTrue((root / "mcp.py").is_file())
        self.assertTrue((root / "js_reverse.py").is_file())
        self.assertIs(js_reverse.JsReverseMcpAdapter.__mro__[1], object)

    def test_breaking_legacy_import_surfaces_are_removed(self) -> None:
        self.assertIsNone(importlib.util.find_spec("skill_temple.browser_adapters"))
        for name in [
            "analyze_replay_response",
            "build_replay_spec",
            "network_request_matches",
            "request_shape_from_snapshot",
        ]:
            with self.subTest(name=name):
                self.assertFalse(hasattr(protocol_evidence, name))

    def test_mutation_execution_lives_in_protocol_mutations(self) -> None:
        mutation_tree = ast.parse(
            Path("src/skill_temple/protocol/mutations.py").read_text(encoding="utf-8")
        )
        evidence_tree = ast.parse(
            Path("src/skill_temple/protocol_evidence.py").read_text(encoding="utf-8")
        )
        mutation_functions = {
            node.name
            for node in mutation_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        evidence_functions = {
            node.name
            for node in evidence_tree.body
            if isinstance(node, ast.FunctionDef)
        }
        required = {
            "build_replay_spec",
            "binding_value_from_snapshot",
            "assess_mutation_effectiveness",
            "observe_binding_application",
            "replay_operation_overwritten_by_later",
            "_mutate_headers",
            "_mutate_query",
            "_mutate_json_body",
        }
        self.assertTrue(required.issubset(mutation_functions))
        self.assertTrue(required.isdisjoint(evidence_functions))

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
