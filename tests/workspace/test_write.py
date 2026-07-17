from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from skill_temple.workspace_models import (
    WorkspaceApplyPatchRequest,
    WorkspaceExecPwshRequest,
    WorkspaceWriteFileRequest,
)
from skill_temple.workspace_service import AnalysisWorkspaceService
from skill_temple.workspace_text_ops import WorkspaceToolError
from tests.workspace.common import WorkspaceTestCase


class WriteWorkspaceTests(WorkspaceTestCase):
    def test_invalid_manifest_blocks_only_that_experiment(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            bad = root / "experiments" / "exp_bad" / "manifest.json"
            bad.parent.mkdir(parents=True)
            bad.write_text("{broken", encoding="utf-8")
            service = AnalysisWorkspaceService(root)

            with self.assertRaises(WorkspaceToolError) as raised:
                asyncio.run(
                    service.write_file(
                        WorkspaceWriteFileRequest(
                            path="experiments/exp_bad/reports/note.md",
                            content="note\n",
                        )
                    )
                )
            self.assertEqual(raised.exception.code, "manifest_invalid")
            self.assertIn("experiments/exp_bad/manifest.json", str(raised.exception))

            result = asyncio.run(
                service.write_file(
                    WorkspaceWriteFileRequest(path="reports/other.md", content="ok\n")
                )
            )
            self.assertTrue(result.written)

    def test_write_read_search_and_inspect_share_the_analysis_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            client = self.make_client(root)
            with client:
                written = client.post(
                    "/v1/workspace/write-file",
                    json={
                        "path": "reports/protocol.md",
                        "content": "# Protocol\n\nresource_id appears here\n",
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
                        "query": "resource_id",
                        "paths": ["reports"],
                        "context_lines": 1,
                    },
                )
                inspect = client.post(
                    "/v1/workspace/inspect",
                    json={
                        "paths": ["reports"],
                        "queries": ["resource_id"],
                        "max_depth": 3,
                    },
                )
            self.assertEqual(written.status_code, 200, written.text)
            self.assertEqual(written.json()["operation"], "added")
            self.assertTrue((root / "reports" / "protocol.md").is_file())
            self.assertEqual(read.status_code, 200, read.text)
            self.assertIn("3: resource_id appears here", read.json()["files"][0]["content"])
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
            schema = root / "schema.json"
            schema.write_text('{"version":1}\n', encoding="utf-8")
            original_schema = schema.read_bytes()
            client = self.make_client(root)
            patch_text = (
                "*** Begin Patch\n"
                "*** Update File: schema.json\n"
                "@@\n"
                "-{\"version\":1}\n"
                "+{\"version\":2}\n"
                "*** Add File: replay.ps1\n"
                "+$body = Get-Content schema.json -Raw\n"
                "*** End Patch"
            )
            with client:
                dry_run = client.post(
                    "/v1/workspace/apply-patch",
                    json={"patch": patch_text, "dry_run": True},
                )
                content_after_dry_run = schema.read_bytes()
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
            self.assertEqual(content_after_dry_run, original_schema)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(schema.read_bytes(), original_schema.replace(b"1", b"2"))
            self.assertTrue(replay_exists_after_apply)
            self.assertEqual(rejected_delete.status_code, 403)
            self.assertEqual(allowed_delete.status_code, 200, allowed_delete.text)
            self.assertFalse((root / "replay.ps1").exists())

    def test_apply_patch_preserves_crlf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "sample.txt"
            original = b"one\r\ntwo\r\nthree\r\n"
            target.write_bytes(original)
            client = self.make_client(root)
            patch_text = (
                "*** Begin Patch\n"
                "*** Update File: sample.txt\n"
                "@@\n"
                " one\n"
                "-two\n"
                "+changed\n"
                " three\n"
                "*** End Patch"
            )
            with client:
                dry_run = client.post(
                    "/v1/workspace/apply-patch",
                    json={"patch": patch_text, "dry_run": True},
                )
                content_after_dry_run = target.read_bytes()
                applied = client.post(
                    "/v1/workspace/apply-patch",
                    json={"patch": patch_text},
                )
            self.assertEqual(dry_run.status_code, 200, dry_run.text)
            self.assertFalse(dry_run.json()["applied"])
            self.assertEqual(content_after_dry_run, original)
            self.assertEqual(applied.status_code, 200, applied.text)
            self.assertEqual(target.read_bytes(), b"one\r\nchanged\r\nthree\r\n")

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
