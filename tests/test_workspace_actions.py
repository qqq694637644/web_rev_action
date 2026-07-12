from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import (
    _acquire_single_process_guard,
    _release_single_process_guard,
    create_app,
)
from skill_temple.workspace_models import (
    WorkspaceApplyPatchRequest,
    WorkspaceExecPwshRequest,
    WorkspaceInspectRequest,
    WorkspaceReadFilesRequest,
    WorkspaceSearchRequest,
    WorkspaceWriteFileRequest,
)
from skill_temple.workspace_service import AnalysisWorkspaceService
from skill_temple.workspace_text_ops import WorkspaceToolError


class WorkspaceActionTests(unittest.TestCase):
    def make_client(self, root: Path) -> TestClient:
        service = AnalysisWorkspaceService(root, allow_network=False)
        env = {
            "WEB_REV_EVIDENCE_DIR": str(root),
            "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
        }
        with patch.dict(os.environ, env, clear=False):
            app = create_app(workspace_service=service)
        return TestClient(app)

    def test_workspace_actions_are_exposed_with_correct_consequential_flags(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self.make_client(Path(temp_dir))
            schema = client.get("/openapi.json").json()
        expected = {
            "workspaceInspect": False,
            "workspaceSearch": False,
            "workspaceReadFiles": False,
            "workspaceWriteFile": True,
            "workspaceApplyPatch": True,
            "workspaceExecPwsh": True,
        }
        operations = {
            operation["operationId"]: operation["x-openai-isConsequential"]
            for path in schema["paths"].values()
            for operation in path.values()
            if operation["operationId"] in expected
        }
        self.assertEqual(operations, expected)

    def test_write_read_search_and_inspect_share_the_analysis_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self.make_client(root)
            with client:
                written = client.post(
                    "/v1/workspace/write-file",
                    json={
                        "path": "reports/protocol.md",
                        "content": "# Protocol\n\nconversation_id appears here\n",
                        "mode": "create_only",
                        "line_ending": "lf",
                    },
                )
                read = client.post(
                    "/v1/workspace/read-files",
                    json={
                        "paths": ["reports/protocol.md"],
                        "start_line": 2,
                        "max_lines": 2,
                    },
                )
                search = client.post(
                    "/v1/workspace/search",
                    json={
                        "query": "conversation_id",
                        "paths": ["reports"],
                        "context_lines": 1,
                    },
                )
                inspect = client.post(
                    "/v1/workspace/inspect",
                    json={
                        "paths": ["reports"],
                        "queries": ["conversation_id"],
                        "max_depth": 3,
                    },
                )
            self.assertEqual(written.status_code, 200, written.text)
            self.assertEqual(written.json()["operation"], "added")
            self.assertTrue((root / "reports" / "protocol.md").is_file())
            self.assertEqual(read.status_code, 200, read.text)
            self.assertIn("3: conversation_id appears here", read.json()["files"][0]["content"])
            self.assertEqual(search.status_code, 200, search.text)
            self.assertEqual(search.json()["matches"][0]["path"], "reports/protocol.md")
            self.assertEqual(inspect.status_code, 200, inspect.text)
            self.assertTrue(
                any(item["path"] == "reports/protocol.md" for item in inspect.json()["tree"])
            )
            self.assertTrue(inspect.json()["searches"][0]["matches"])

    def test_write_file_supports_sha_guard_dry_run_and_line_endings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self.make_client(root)
            with client:
                created = client.post(
                    "/v1/workspace/write-file",
                    json={"path": "notes.txt", "content": "one\ntwo\n"},
                )
                current_sha = created.json()["new_sha256"]
                dry_run = client.post(
                    "/v1/workspace/write-file",
                    json={
                        "path": "notes.txt",
                        "content": "changed\n",
                        "mode": "overwrite_if_sha256_matches",
                        "expected_sha256": current_sha,
                        "dry_run": True,
                    },
                )
                content_after_dry_run = (root / "notes.txt").read_text(
                    encoding="utf-8"
                )
                conflict = client.post(
                    "/v1/workspace/write-file",
                    json={
                        "path": "notes.txt",
                        "content": "changed\n",
                        "mode": "overwrite_if_sha256_matches",
                        "expected_sha256": "0" * 64,
                    },
                )
                replaced = client.post(
                    "/v1/workspace/write-file",
                    json={
                        "path": "notes.txt",
                        "content": "changed\n",
                        "mode": "overwrite_if_sha256_matches",
                        "expected_sha256": current_sha,
                        "line_ending": "crlf",
                    },
                )
            self.assertEqual(dry_run.status_code, 200, dry_run.text)
            self.assertFalse(dry_run.json()["written"])
            self.assertEqual(content_after_dry_run, "one\ntwo\n")
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(replaced.status_code, 200, replaced.text)
            self.assertEqual((root / "notes.txt").read_bytes(), b"changed\r\n")

    def test_apply_patch_supports_dry_run_update_add_and_delete_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "schema.json").write_text('{"version":1}\n', encoding="utf-8")
            client = self.make_client(root)
            patch_text = """*** Begin Patch
*** Update File: schema.json
@@
-{"version":1}
+{"version":2}
*** Add File: replay.ps1
+$body = Get-Content schema.json -Raw
*** End Patch"""
            with client:
                dry_run = client.post(
                    "/v1/workspace/apply-patch",
                    json={"patch": patch_text, "dry_run": True},
                )
                applied = client.post(
                    "/v1/workspace/apply-patch",
                    json={"patch": patch_text},
                )
                replay_exists_after_apply = (root / "replay.ps1").is_file()
                rejected_delete = client.post(
                    "/v1/workspace/apply-patch",
                    json={
                        "patch": "*** Begin Patch\n*** Delete File: replay.ps1\n*** End Patch"
                    },
                )
                allowed_delete = client.post(
                    "/v1/workspace/apply-patch",
                    json={
                        "patch": "*** Begin Patch\n*** Delete File: replay.ps1\n*** End Patch",
                        "allow_delete": True,
                    },
                )
            self.assertEqual(dry_run.status_code, 200, dry_run.text)
            self.assertFalse(dry_run.json()["applied"])
            self.assertEqual((root / "schema.json").read_text(encoding="utf-8"), '{"version":2}\n')
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertTrue(replay_exists_after_apply)
            self.assertEqual(rejected_delete.status_code, 403)
            self.assertEqual(allowed_delete.status_code, 200, allowed_delete.text)
            self.assertFalse((root / "replay.ps1").exists())

    def test_pwsh_handles_binary_base64_hash_and_utf8_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = bytes(range(256))
            (root / "raw.bin").write_bytes(payload)
            client = self.make_client(root)
            with client:
                text_read = client.post(
                    "/v1/workspace/read-files",
                    json={"paths": ["raw.bin"]},
                )
                pwsh = client.post(
                    "/v1/workspace/exec-pwsh",
                    json={
                        "script": (
                            "$bytes = [IO.File]::ReadAllBytes('raw.bin')\n"
                            "$hash = [Security.Cryptography.SHA256]::HashData($bytes)\n"
                            "$sha = [Convert]::ToHexString($hash).ToLowerInvariant()\n"
                            "$head = [Convert]::ToBase64String($bytes[0..31])\n"
                            "Write-Output \"sha=$sha\"\n"
                            "Write-Output \"head=$head\"\n"
                            "Write-Output '中文输出'"
                        ),
                        "plain_output": True,
                        "utf8_output": True,
                    },
                )
            self.assertEqual(text_read.status_code, 200, text_read.text)
            self.assertIsNotNone(text_read.json()["files"][0]["error"])
            self.assertEqual(pwsh.status_code, 200, pwsh.text)
            expected_sha = hashlib.sha256(payload).hexdigest()
            self.assertIn(f"sha={expected_sha}", pwsh.json()["stdout"])
            self.assertIn("中文输出", pwsh.json()["stdout"])
            self.assertFalse(pwsh.json()["truncated"])

    def test_pwsh_blocks_network_by_default_and_bounds_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self.make_client(Path(temp_dir))
            with client:
                blocked = client.post(
                    "/v1/workspace/exec-pwsh",
                    json={"script": "Invoke-WebRequest https://example.com"},
                )
                bounded = client.post(
                    "/v1/workspace/exec-pwsh",
                    json={
                        "script": "'x' * 10000",
                        "max_output_bytes": 200,
                    },
                )
            self.assertEqual(blocked.status_code, 403)
            self.assertEqual(bounded.status_code, 200, bounded.text)
            self.assertTrue(bounded.json()["truncated"])
            self.assertLessEqual(
                len(bounded.json()["stdout"].encode("utf-8")),
                200,
            )

    def test_pwsh_timeout_returns_structured_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self.make_client(Path(temp_dir))
            with client:
                response = client.post(
                    "/v1/workspace/exec-pwsh",
                    json={
                        "script": "Start-Sleep -Seconds 5",
                        "timeout_seconds": 1,
                    },
                )
            self.assertEqual(response.status_code, 408)
            self.assertEqual(
                response.json()["detail"]["error"]["code"],
                "workspace_timeout",
            )

    def test_inspect_and_search_hide_dotenv_but_allow_templates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            (root / ".env.example").write_text("TOKEN=replace-me\n", encoding="utf-8")
            client = self.make_client(root)
            with client:
                inspect = client.post(
                    "/v1/workspace/inspect",
                    json={"paths": ["."], "max_depth": 2},
                )
                search = client.post(
                    "/v1/workspace/search",
                    json={"query": "TOKEN", "paths": [".env.example"]},
                )
                hidden = client.post(
                    "/v1/workspace/search",
                    json={"query": "TOKEN", "paths": [".env"]},
                )
            self.assertEqual(inspect.status_code, 200, inspect.text)
            paths = {item["path"] for item in inspect.json()["tree"]}
            self.assertNotIn(".env", paths)
            self.assertIn(".env.example", paths)
            self.assertEqual(search.status_code, 200, search.text)
            self.assertEqual(
                {item["path"] for item in search.json()["matches"]},
                {".env.example"},
            )
            self.assertEqual(hidden.status_code, 422)

    def test_inspect_depth_is_relative_to_each_requested_base_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            raw = (
                root
                / "experiments"
                / "exp_deep"
                / "js-reverse"
                / "capture-one"
                / "raw.txt"
            )
            raw.parent.mkdir(parents=True)
            raw.write_text("evidence\n", encoding="utf-8")
            service = AnalysisWorkspaceService(root)
            response = asyncio.run(
                service.inspect(
                    WorkspaceInspectRequest(
                        paths=["experiments/exp_deep/js-reverse"],
                        max_depth=2,
                        max_tree_entries=20,
                    )
                )
            )
            entries = {item.path: item.depth for item in response.tree}
            self.assertEqual(
                entries["experiments/exp_deep/js-reverse/capture-one"],
                1,
            )
            self.assertEqual(
                entries[
                    "experiments/exp_deep/js-reverse/capture-one/raw.txt"
                ],
                2,
            )

    def test_large_text_read_streams_without_path_read_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "events.jsonl"
            content = "".join(
                json.dumps({"index": index, "value": "中文"}, ensure_ascii=False)
                + "\n"
                for index in range(50_000)
            )
            path.write_text(content, encoding="utf-8", newline="\n")
            expected_bytes = path.stat().st_size
            expected_sha = hashlib.sha256(
                content.encode("utf-8")
            ).hexdigest()
            service = AnalysisWorkspaceService(root)
            with patch.object(
                Path,
                "read_bytes",
                side_effect=AssertionError("read_bytes must not be used"),
            ):
                response = asyncio.run(
                    service.read_files(
                        WorkspaceReadFilesRequest(
                            paths=["events.jsonl"],
                            start_line=20_000,
                            max_lines=3,
                            max_bytes_per_file=2_000,
                            max_bytes=4_000,
                        )
                    )
                )
            item = response.files[0]
            self.assertIsNone(item.error)
            self.assertEqual(item.bytes, expected_bytes)
            self.assertEqual(item.sha256, expected_sha)
            self.assertIn("20000:", item.content)
            self.assertIn("20002:", item.content)
            self.assertTrue(item.truncated)

    def test_search_stops_at_match_limit_without_buffering_all_results(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "many.txt").write_text(
                "".join(f"needle {index}\n" for index in range(20_000)),
                encoding="utf-8",
            )
            service = AnalysisWorkspaceService(root)
            response = asyncio.run(
                service.search(
                    WorkspaceSearchRequest(
                        query="needle",
                        paths=["many.txt"],
                        max_matches=5,
                        max_bytes=16_000,
                    )
                )
            )
            self.assertEqual(response.match_count, 5)
            self.assertTrue(response.truncated)

    def test_original_evidence_is_read_only_but_derived_directories_are_writable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment = root / "experiments" / "exp_done"
            raw = experiment / "js-reverse" / "capture-one" / "events.jsonl"
            trace = experiment / "playwright" / "traces" / "trace.txt"
            raw.parent.mkdir(parents=True)
            trace.parent.mkdir(parents=True)
            raw.write_text('{"event":"done"}\n', encoding="utf-8")
            trace.write_text("trace\n", encoding="utf-8")
            (experiment / "manifest.json").write_text(
                json.dumps({"experiment_id": "exp_done", "status": "completed"}),
                encoding="utf-8",
            )
            (root / "sessions").mkdir()
            (root / "sessions" / "one.json").write_text("{}\n", encoding="utf-8")
            service = AnalysisWorkspaceService(root)

            for protected in [
                "sessions/one.json",
                "experiments/exp_done/manifest.json",
                "experiments/exp_done/js-reverse/capture-one/events.jsonl",
                "experiments/exp_done/playwright/traces/trace.txt",
            ]:
                with self.assertRaises(WorkspaceToolError) as raised:
                    asyncio.run(
                        service.write_file(
                            WorkspaceWriteFileRequest(
                                path=protected,
                                content="changed\n",
                                mode="overwrite",
                            )
                        )
                    )
                self.assertEqual(raised.exception.status_code, 403)

            written = asyncio.run(
                service.write_file(
                    WorkspaceWriteFileRequest(
                        path="experiments/exp_done/reports/analysis.md",
                        content="# Analysis\n",
                    )
                )
            )
            self.assertTrue(written.written)

            with self.assertRaises(WorkspaceToolError) as patch_error:
                asyncio.run(
                    service.apply_patch(
                        WorkspaceApplyPatchRequest(
                            patch=(
                                "*** Begin Patch\n"
                                "*** Update File: experiments/exp_done/js-reverse/"
                                "capture-one/events.jsonl\n"
                                "@@\n"
                                "-{\"event\":\"done\"}\n"
                                "+{\"event\":\"changed\"}\n"
                                "*** End Patch"
                            )
                        )
                    )
                )
            self.assertEqual(patch_error.exception.status_code, 403)

            read_result = asyncio.run(
                service.exec_pwsh(
                    WorkspaceExecPwshRequest(
                        script=(
                            "$bytes = [IO.File]::ReadAllBytes("
                            "'experiments/exp_done/js-reverse/capture-one/events.jsonl')\n"
                            "Write-Output $bytes.Length"
                        ),
                        plain_output=True,
                    )
                )
            )
            self.assertEqual(read_result.exit_code, 0)

            with self.assertRaises(WorkspaceToolError) as pwsh_error:
                asyncio.run(
                    service.exec_pwsh(
                        WorkspaceExecPwshRequest(
                            script=(
                                "Set-Content "
                                "'experiments/exp_done/js-reverse/capture-one/events.jsonl' "
                                "'changed'"
                            )
                        )
                    )
                )
            self.assertEqual(pwsh_error.exception.status_code, 403)

    def test_running_experiment_blocks_workspace_mutation_and_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment = root / "experiments" / "exp_running"
            experiment.mkdir(parents=True)
            (experiment / "manifest.json").write_text(
                json.dumps(
                    {"experiment_id": "exp_running", "status": "running"}
                ),
                encoding="utf-8",
            )
            service = AnalysisWorkspaceService(root)
            with self.assertRaises(WorkspaceToolError) as write_error:
                asyncio.run(
                    service.write_file(
                        WorkspaceWriteFileRequest(
                            path="experiments/exp_running/reports/note.md",
                            content="not yet\n",
                        )
                    )
                )
            self.assertEqual(write_error.exception.status_code, 409)
            with self.assertRaises(WorkspaceToolError) as pwsh_error:
                asyncio.run(
                    service.exec_pwsh(
                        WorkspaceExecPwshRequest(script="Write-Output 'read-only'")
                    )
                )
            self.assertEqual(pwsh_error.exception.status_code, 409)

    @unittest.skipUnless(os.name == "nt", "Windows single-process lock")
    def test_single_process_guard_rejects_a_second_process_for_same_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            key = _acquire_single_process_guard(root)
            try:
                script = (
                    "from pathlib import Path; "
                    "from skill_temple.app import _acquire_single_process_guard; "
                    f"_acquire_single_process_guard(Path({str(root)!r}))"
                )
                child = subprocess.run(
                    [sys.executable, "-c", script],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                self.assertNotEqual(child.returncode, 0)
                self.assertIn("exactly one worker", child.stderr + child.stdout)
            finally:
                _release_single_process_guard(key)

    def test_paths_cannot_escape_analysis_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = self.make_client(Path(temp_dir))
            with client:
                write = client.post(
                    "/v1/workspace/write-file",
                    json={"path": "../outside.txt", "content": "no"},
                )
                inspect = client.post(
                    "/v1/workspace/inspect",
                    json={"paths": ["../"]},
                )
            self.assertEqual(write.status_code, 400)
            self.assertEqual(inspect.status_code, 400)


if __name__ == "__main__":
    unittest.main()
