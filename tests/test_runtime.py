from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import create_app
from skill_temple.browser.registry import OPERATION_REGISTRY
from skill_temple.evals import evaluate_file
from skill_temple.prompt_builder import build_instructions, render_catalog
from skill_temple.runtime import (
    DEFAULT_MAX_SKILLS,
    SKILL_DESCRIPTION_MAX_CHARS,
    SKILL_NAME_MAX_CHARS,
    SkillLineLimitError,
    SkillNotFoundError,
    SkillPathError,
    SkillRuntime,
    SkillRuntimeError,
    load_runtime,
)


def _write_skill(
    skills_root: Path,
    skill_id: str,
    description: str,
    body: str,
    docs: dict[str, str] | None = None,
) -> Path:
    skill_root = skills_root / skill_id
    skill_root.mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                f"name: {skill_id}",
                f"description: {description}",
                "---",
                "",
                body,
                "",
            ]
        ),
        encoding="utf-8",
    )
    for relative_path, content in (docs or {}).items():
        path = skill_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return skill_root


class RuntimeTests(unittest.TestCase):
    def test_packaged_catalog_has_static_protocol_and_workflow_skills(self) -> None:
        runtime = load_runtime()
        catalog = runtime.list_skills()["skills"]

        self.assertEqual(
            [item["skill_id"] for item in catalog],
            [
                "browser-action-protocol",
                "browser-evidence-inspection",
                "browser-experiment-recovery",
                "browser-request-replay",
                "browser-script-tracing",
                "browser-session-capture",
                "browser-stream-diagnostics",
                "current-site-analysis",
                "pandora-protocol-reproduction",
            ],
        )
        for item in catalog:
            self.assertEqual(item["entrypoint"], f"{item['skill_id']}/SKILL.md")
            self.assertTrue(item["content_hash"].startswith("sha256:"))
        self.assertFalse(hasattr(runtime, "resolve"))
        self.assertFalse(hasattr(runtime, "retrieve"))
        self.assertFalse(hasattr(runtime, "search"))

    def test_load_skills_returns_complete_wrapped_entrypoints_and_references(self) -> None:
        runtime = load_runtime()
        result = runtime.load_skills(
            ["current-site-analysis", "browser-action-protocol", "current-site-analysis"]
        )

        self.assertEqual(
            result["loaded_skill_ids"],
            ["current-site-analysis", "browser-action-protocol"],
        )
        current, protocol = result["skills"]
        self.assertEqual(current["source_path"], "current-site-analysis/SKILL.md")
        self.assertTrue(current["content"].startswith("<skill>\n<name>current-site-analysis</name>"))
        self.assertTrue(current["content"].endswith("</skill>"))
        self.assertIn("Do not begin with a fixed scenario list", current["content"])
        self.assertEqual(
            set(current["referenced_paths"]),
            {
                "docs/experiment-design.md",
                "docs/inventory-checklist.md",
                "docs/report-contract.md",
            },
        )
        self.assertIn("docs/transport-envelope.md", protocol["referenced_paths"])
        self.assertIn("docs/operation-index.md", protocol["referenced_paths"])

    def test_load_skills_is_ordered_bounded_and_all_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            for skill_id in ["alpha", "beta", "gamma", "delta"]:
                _write_skill(root, skill_id, f"Use for {skill_id}.", f"# {skill_id}")
            runtime = SkillRuntime(root)

            loaded = runtime.load_skills(["beta", "alpha", "beta"])
            self.assertEqual(loaded["loaded_skill_ids"], ["beta", "alpha"])
            with self.assertRaises(SkillRuntimeError):
                runtime.load_skills(["alpha", "beta", "gamma", "delta"])
            with self.assertRaises(SkillNotFoundError):
                runtime.load_skills(["alpha", "missing"])
            with self.assertRaises(SkillRuntimeError):
                runtime.load_skills([])

    def test_read_returns_continuation_and_rejects_unsafe_paths(self) -> None:
        runtime = load_runtime()
        first = runtime.read("browser-action-protocol", "SKILL.md", max_lines=5)
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_start_line"], 6)
        second = runtime.read(
            "browser-action-protocol",
            "SKILL.md",
            start_line=first["next_start_line"],
            max_lines=5,
        )
        self.assertEqual(second["start_line"], 6)
        for path in ["../README.md", "/etc/passwd", "docs/../../SKILL.md"]:
            with self.subTest(path=path):
                with self.assertRaises(SkillPathError):
                    runtime.read("browser-action-protocol", path)

    def test_skill_content_hash_ignores_line_ending_style(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            skill_root = _write_skill(
                root,
                "demo",
                "Use for line-ending tests.",
                "# Demo\n\nExact content.",
            )
            skill_path = skill_root / "SKILL.md"
            lf_text = skill_path.read_text(encoding="utf-8").replace("\r\n", "\n")
            lf_hash = SkillRuntime(root).list_skills()["skills"][0]["content_hash"]

            skill_path.write_bytes(lf_text.replace("\n", "\r\n").encode("utf-8"))
            crlf_runtime = SkillRuntime(root)
            crlf_hash = crlf_runtime.list_skills()["skills"][0]["content_hash"]

            self.assertEqual(crlf_hash, lf_hash)
            self.assertEqual(
                crlf_runtime.read("demo", "SKILL.md")["content_hash"],
                lf_hash,
            )

    def test_read_rejects_one_oversized_line(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            long_line = "X" * 100
            _write_skill(
                root,
                "demo",
                "Use for long-line tests.",
                "# Demo",
                {"docs/long.txt": long_line},
            )
            with self.assertRaises(SkillLineLimitError) as raised:
                SkillRuntime(root).read("demo", "docs/long.txt", max_chars=10)
            self.assertEqual(raised.exception.line_number, 1)
            self.assertEqual(raised.exception.actual_chars, 100)
            self.assertEqual(raised.exception.max_chars, 10)

    def test_read_action_reports_oversized_line_details(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir) / "skills"
            shutil.copytree(Path("src/skill_temple/example_skills"), skills)
            protocol_root = skills / "browser-action-protocol"
            (protocol_root / "docs/long.txt").write_text("X" * 32_001, encoding="utf-8")
            skill_path = protocol_root / "SKILL.md"
            skill_path.write_text(
                skill_path.read_text(encoding="utf-8") + "\n- `docs/long.txt`\n",
                encoding="utf-8",
            )

            with TestClient(create_app(skills_dir=skills)) as client:
                response = client.post(
                    "/v1/skills/read",
                    json={
                        "skill_id": "browser-action-protocol",
                        "path": "docs/long.txt",
                    },
                )

        self.assertEqual(response.status_code, 422, response.text)
        error = response.json()["detail"]["error"]
        self.assertEqual(error["code"], "skill_line_exceeds_limit")
        self.assertEqual(error["line_number"], 1)
        self.assertEqual(error["actual_chars"], 32_001)
        self.assertEqual(error["max_chars"], 32_000)

    def test_skill_references_fail_fast_for_missing_unsafe_and_directory_paths(self) -> None:
        cases = [
            ("`docs/missing.md`", "Missing Skill reference"),
            ("[unsafe](../outside.md)", "Unsafe Skill reference"),
            ("`docs/`", "Skill reference is not a file"),
        ]
        for body, message in cases:
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir) / "skills"
                skill_root = _write_skill(
                    root,
                    "demo",
                    "Use for strict reference tests.",
                    f"# Demo\n\n{body}",
                )
                (skill_root / "docs").mkdir(exist_ok=True)
                with self.assertRaisesRegex(
                    SkillRuntimeError,
                    f"{message}.*skill_id='demo'.*source='SKILL.md'",
                ):
                    SkillRuntime(root)

    def test_runtime_loads_skills_from_cwd_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            root = tmp / "custom_skills"
            _write_skill(root, "demo", "Use for demo tasks.", "# Demo")
            (tmp / ".env").write_text(
                f'SKILL_TEMPLE_SKILLS_DIR = "{root}"\n', encoding="utf-8"
            )
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {"SKILL_TEMPLE_SKILLS_DIR": ""}, clear=False):
                    runtime = load_runtime()
            finally:
                os.chdir(previous)
            self.assertEqual(runtime.skills_dir, root.resolve())
            self.assertEqual(runtime.load_skills(["demo"])["loaded_skill_ids"], ["demo"])

    def test_runtime_does_not_implicitly_use_a_repository_root_skills_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            _write_skill(tmp / "skills", "shadow", "Do not auto-load.", "# Shadow")
            previous = Path.cwd()
            try:
                os.chdir(tmp)
                with patch.dict(os.environ, {"SKILL_TEMPLE_SKILLS_DIR": ""}, clear=False):
                    runtime = load_runtime()
            finally:
                os.chdir(previous)
            self.assertNotIn(
                "shadow",
                {item["skill_id"] for item in runtime.list_skills()["skills"]},
            )

    def test_nested_scan_duplicate_and_frontmatter_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            accepted = root.joinpath(*[f"a{index}" for index in range(5)])
            rejected = root.joinpath(*[f"b{index}" for index in range(6)])
            _write_skill(accepted, "accepted", "Use for accepted tasks.", "# Accepted")
            _write_skill(rejected, "rejected", "Use for rejected tasks.", "# Rejected")
            ids = {item["skill_id"] for item in SkillRuntime(root).list_skills()["skills"]}
            self.assertIn("accepted", ids)
            self.assertNotIn("rejected", ids)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(root / "one", "same", "Use for one.", "# One")
            _write_skill(root / "two", "same", "Use for two.", "# Two")
            with self.assertRaisesRegex(SkillRuntimeError, "Duplicate skill name"):
                SkillRuntime(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(
                root,
                "n" * (SKILL_NAME_MAX_CHARS + 1),
                "Use for long names.",
                "# Long",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "name exceeds"):
                SkillRuntime(root)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "skills"
            _write_skill(
                root,
                "long-description",
                "D" * (SKILL_DESCRIPTION_MAX_CHARS + 1),
                "# Long",
            )
            with self.assertRaisesRegex(SkillRuntimeError, "description exceeds"):
                SkillRuntime(root)

    def test_prompt_builder_is_deterministic_and_requires_placeholder(self) -> None:
        runtime = load_runtime()
        catalog = render_catalog(runtime)
        self.assertEqual(catalog, render_catalog(runtime))
        self.assertIn("browser-action-protocol", catalog)
        self.assertNotIn("# Browser Action protocol", catalog)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            template = root / "template.md"
            output = root / "dist" / "instructions.md"
            template.write_text("Header\r\n{{SKILL_CATALOG}}\r\n", encoding="utf-8")
            build_instructions(runtime=runtime, template_path=template, output_path=output)
            rendered = output.read_bytes()
            self.assertNotIn(b"\r", rendered)
            self.assertIn(b"browser-action-protocol", rendered)
            self.assertNotIn(b"{{SKILL_CATALOG}}", rendered)

            template.write_text("missing", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                build_instructions(runtime=runtime, template_path=template, output_path=output)

    def test_prompt_builder_output_is_deterministic(self) -> None:
        runtime = load_runtime(Path("src/skill_temple/example_skills"))
        with tempfile.TemporaryDirectory() as temp_dir:
            generated = Path(temp_dir) / "first.md"
            regenerated = Path(temp_dir) / "second.md"
            build_instructions(
                runtime=runtime,
                template_path=Path("GPT_ACTION_PROMPT.md"),
                output_path=generated,
            )
            build_instructions(
                runtime=runtime,
                template_path=Path("GPT_ACTION_PROMPT.md"),
                output_path=regenerated,
            )
            self.assertEqual(
                generated.read_bytes(),
                regenerated.read_bytes(),
            )

    def test_openapi_exposes_only_new_skill_and_stable_browser_actions(self) -> None:
        schema = create_app().openapi()
        operation_ids = {
            operation["operationId"]
            for path_item in schema["paths"].values()
            for operation in path_item.values()
        }
        self.assertEqual(
            operation_ids,
            {
                "loadSkills",
                "readSkillContent",
                "inspectBrowserEvidence",
                "runBrowserExperiment",
                "workspaceInspect",
                "workspaceSearch",
                "workspaceReadFiles",
                "workspaceWriteFile",
                "workspaceApplyPatch",
                "workspaceExecPwsh",
            },
        )
        self.assertNotIn("/v1/skills/retrieve", schema["paths"])
        self.assertNotIn("/v1/skills/search", schema["paths"])
        load_schema = schema["components"]["schemas"]["LoadSkillsRequest"]
        self.assertEqual(set(load_schema["properties"]), {"skill_ids"})
        self.assertEqual(load_schema["properties"]["skill_ids"]["maxItems"], DEFAULT_MAX_SKILLS)

    def test_server_url_optional_bearer_and_http_endpoints(self) -> None:
        client = TestClient(create_app())
        response = client.get(
            "/openapi.json",
            headers={
                "x-forwarded-proto": "https",
                "x-forwarded-host": "skills.example.com",
            },
        )
        self.assertEqual(response.json()["servers"], [{"url": "https://skills.example.com"}])

        loaded = client.post(
            "/v1/skills/load",
            json={"skill_ids": ["browser-action-protocol"]},
        )
        read = client.post(
            "/v1/skills/read",
            json={
                "skill_id": "browser-action-protocol",
                "path": "docs/transport-envelope.md",
                "max_lines": 5,
            },
        )
        missing = client.post("/v1/skills/load", json={"skill_ids": ["missing"]})
        unsafe = client.post(
            "/v1/skills/read",
            json={"skill_id": "browser-action-protocol", "path": "../README.md"},
        )
        hidden_catalog = client.get("/v1/skills")
        console = client.get("/console")
        console_load = client.post(
            "/console/load",
            json={"skill_ids": ["browser-action-protocol"]},
        )
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["loaded_skill_ids"], ["browser-action-protocol"])
        self.assertEqual(read.status_code, 200, read.text)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["error"]["code"], "skill_not_found")
        self.assertEqual(unsafe.status_code, 404)
        self.assertEqual(hidden_catalog.status_code, 404)
        self.assertEqual(console.status_code, 404)
        self.assertEqual(console_load.status_code, 404)

        with patch.dict(
            os.environ,
            {"SKILL_TEMPLE_BEARER_TOKEN": "secret-token"},
            clear=False,
        ):
            protected = TestClient(create_app())
            unauthorized = protected.post(
                "/v1/skills/load", json={"skill_ids": ["current-site-analysis"]}
            )
            authorized = protected.post(
                "/v1/skills/load",
                json={"skill_ids": ["current-site-analysis"]},
                headers={"Authorization": "Bearer secret-token"},
            )
            schema = protected.get("/openapi.json").json()
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(authorized.status_code, 200)
        self.assertIn("BearerAuth", schema["components"]["securitySchemes"])

    def test_browser_binding_uses_the_configured_runtime_protocol_skill_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            custom_skills = Path(temp_dir) / "skills"
            shutil.copytree(Path("src/skill_temple/example_skills"), custom_skills)
            protocol_path = custom_skills / "browser-action-protocol" / "SKILL.md"
            protocol_path.write_text(
                protocol_path.read_text(encoding="utf-8")
                + "\nCustom deployment marker.\n",
                encoding="utf-8",
            )
            with TestClient(create_app(skills_dir=custom_skills)) as client:
                loaded = client.post(
                    "/v1/skills/load",
                    json={"skill_ids": ["browser-action-protocol"]},
                )
                self.assertEqual(loaded.status_code, 200, loaded.text)
                custom_hash = loaded.json()["skills"][0]["content_hash"]
                response = client.post(
                    "/v1/browser/inspect",
                    json={
                        "contract_version": "2.0",
                        "operation": "list_experiments",
                        "payload_json": "{}",
                        "skill_id": "browser-action-protocol",
                        "skill_content_hash": custom_hash,
                        "operation_contract_hash": OPERATION_REGISTRY.contract_hash(
                            "list_experiments"
                        ),
                    },
                )
            self.assertEqual(response.status_code, 200, response.text)

    def test_eval_file_passes(self) -> None:
        report = evaluate_file(Path("evals/skill_queries.jsonl"))
        self.assertEqual(report["failed"], 0)
        self.assertGreaterEqual(report["passed"], 2)


if __name__ == "__main__":
    unittest.main()
