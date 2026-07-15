from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from skill_temple.app import (
    _acquire_single_process_guard,
    _release_single_process_guard,
)
from skill_temple.workspace_models import (
    WorkspaceExecPwshRequest,
    WorkspaceWriteFileRequest,
)
from skill_temple.workspace_service import AnalysisWorkspaceService
from skill_temple.workspace_text_ops import WorkspaceToolError
from tests.workspace.common import WorkspaceTestCase


class PowershellWorkspaceTests(WorkspaceTestCase):
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

    @unittest.skipUnless(os.name == "nt", "Windows process-tree behavior")
    def test_canceling_powershell_terminates_child_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            child_pid_file = root / "pwsh-child.pid"
            marker = root / "pwsh-child-finished.txt"
            service = AnalysisWorkspaceService(root)
            python_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(2); Path({str(marker)!r}).write_text('finished')"
            )
            script = "\n".join(
                [
                    "$child = Start-Process -FilePath python -ArgumentList @(",
                    "  '-c',",
                    f"  {json.dumps(python_code)}",
                    ") -PassThru",
                    f"Set-Content -Path {json.dumps(str(child_pid_file))} -Value $child.Id",
                    "Start-Sleep -Seconds 30",
                ]
            )

            async def exercise() -> None:
                task = asyncio.create_task(
                    service.exec_pwsh(
                        WorkspaceExecPwshRequest(
                            script=script,
                            timeout_seconds=60,
                            plain_output=True,
                        )
                    )
                )
                for _ in range(100):
                    if child_pid_file.is_file():
                        break
                    await asyncio.sleep(0.02)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

            asyncio.run(exercise())
            child_pid = child_pid_file.read_text(encoding="utf-8").strip()
            time.sleep(2.2)
            self.assertFalse(marker.exists())
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {child_pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            self.assertNotIn(f'"{child_pid}"', listing.stdout)

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

    @unittest.skipUnless(os.name == "nt", "Windows single-process lock")
    def test_second_process_cannot_recover_running_manifest_before_lock(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
            manifest = root / "experiments" / "exp_live" / "manifest.json"
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                json.dumps(
                    {
                        "experiment_id": "exp_live",
                        "session_id": "session_live",
                        "status": "running",
                    }
                ),
                encoding="utf-8",
            )
            key = _acquire_single_process_guard(root)
            try:
                env = {
                    **os.environ,
                    "WEB_REV_EVIDENCE_DIR": str(root),
                    "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
                }
                child = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from skill_temple.app import create_app; create_app()",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                self.assertNotEqual(child.returncode, 0)
                self.assertIn(
                    "already owns this analysis workspace",
                    child.stdout + child.stderr,
                )
                current = json.loads(manifest.read_text(encoding="utf-8"))
                self.assertEqual(current["status"], "running")
            finally:
                _release_single_process_guard(key)
