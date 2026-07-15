from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from skill_temple.workspace_models import (
    WorkspaceInspectRequest,
    WorkspaceReadFilesRequest,
    WorkspaceSearchRequest,
)
from skill_temple.workspace_service import AnalysisWorkspaceService
from tests.workspace.common import WorkspaceTestCase


class InspectWorkspaceTests(WorkspaceTestCase):
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

    def test_file_growth_during_single_pass_read_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "growing.jsonl"
            path.write_text("line\n" * 2_000_000, encoding="utf-8")
            service = AnalysisWorkspaceService(root)

            def append_later() -> None:
                time.sleep(0.01)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("appended\n")

            thread = threading.Thread(target=append_later)
            thread.start()
            response = asyncio.run(
                service.read_files(
                    WorkspaceReadFilesRequest(
                        paths=["growing.jsonl"],
                        start_line=1,
                        max_lines=2,
                        max_bytes_per_file=1_000,
                        max_bytes=2_000,
                        include_sha256=True,
                    )
                )
            )
            thread.join(timeout=5)
            item = response.files[0]
            self.assertTrue(item.changed_during_read)
            self.assertIsNone(item.sha256)
            self.assertGreater(item.total_lines or 0, 1_000_000)

    def test_read_can_skip_full_sha256_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "small.txt").write_text("one\ntwo\n", encoding="utf-8")
            service = AnalysisWorkspaceService(root)
            response = asyncio.run(
                service.read_files(
                    WorkspaceReadFilesRequest(
                        paths=["small.txt"],
                        include_sha256=False,
                    )
                )
            )
            item = response.files[0]
            self.assertIsNone(item.sha256)
            self.assertFalse(item.changed_during_read)
            self.assertEqual(item.bytes, (root / "small.txt").stat().st_size)

    def test_read_without_sha_stops_after_requested_range(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            payload = b"one\ntwo\n" + (b"x" * 70_000) + b"\x00binary-tail"
            (root / "range-only.txt").write_bytes(payload)
            service = AnalysisWorkspaceService(root)
            response = asyncio.run(
                service.read_files(
                    WorkspaceReadFilesRequest(
                        paths=["range-only.txt"],
                        start_line=1,
                        max_lines=1,
                        max_bytes_per_file=1_000,
                        max_bytes=2_000,
                        include_sha256=False,
                    )
                )
            )
            item = response.files[0]
            self.assertIsNone(item.error)
            self.assertEqual(item.content, "1: one")
            self.assertIsNone(item.total_lines)
            self.assertIsNone(item.sha256)
            self.assertEqual(item.bytes, len(payload))
            self.assertTrue(item.truncated)

    def test_credential_artifacts_are_hidden_unless_explicitly_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment = root / "experiments" / "exp_credential"
            credential = experiment / "js-reverse" / "network" / "request.json"
            public = experiment / "reports" / "summary.txt"
            credential.parent.mkdir(parents=True)
            public.parent.mkdir(parents=True)
            credential.write_text(
                '{"authorization":"Bearer workspace-secret"}\n',
                encoding="utf-8",
            )
            public.write_text("public-marker\n", encoding="utf-8")
            (experiment / "manifest.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp_credential",
                        "status": "completed",
                        "artifacts": [
                            {
                                "artifactId": "art_credential",
                                "relativePath": (
                                    "experiments/exp_credential/js-reverse/"
                                    "network/request.json"
                                ),
                                "sensitivity": "credential",
                                "containsCredentials": True,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            service = AnalysisWorkspaceService(root)
            relative = (
                "experiments/exp_credential/js-reverse/network/request.json"
            )

            hidden_read = asyncio.run(
                service.read_files(WorkspaceReadFilesRequest(paths=[relative]))
            )
            explicit_read = asyncio.run(
                service.read_files(
                    WorkspaceReadFilesRequest(
                        paths=[relative],
                        include_credentials=True,
                    )
                )
            )
            hidden_search = asyncio.run(
                service.search(
                    WorkspaceSearchRequest(
                        query="workspace-secret",
                        paths=["experiments/exp_credential"],
                    )
                )
            )
            explicit_search = asyncio.run(
                service.search(
                    WorkspaceSearchRequest(
                        query="workspace-secret",
                        paths=["experiments/exp_credential"],
                        include_credentials=True,
                    )
                )
            )
            hidden_inspect = asyncio.run(
                service.inspect(
                    WorkspaceInspectRequest(
                        paths=["experiments/exp_credential"],
                        queries=["workspace-secret"],
                    )
                )
            )
            explicit_inspect = asyncio.run(
                service.inspect(
                    WorkspaceInspectRequest(
                        paths=["experiments/exp_credential"],
                        queries=["workspace-secret"],
                        include_credentials=True,
                    )
                )
            )

            self.assertIn("hidden by default", hidden_read.files[0].error or "")
            self.assertNotIn("workspace-secret", hidden_read.files[0].content)
            self.assertIn("workspace-secret", explicit_read.files[0].content)
            self.assertEqual(hidden_search.match_count, 0)
            self.assertEqual(explicit_search.match_count, 1)
            self.assertEqual(hidden_inspect.searches[0].match_count, 0)
            self.assertEqual(explicit_inspect.searches[0].match_count, 1)
            self.assertIn(
                "workspace-secret",
                explicit_inspect.searches[0].matches[0].line,
            )

    def test_running_raw_body_paths_are_hidden_before_manifest_indexing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            experiment = root / "experiments" / "exp_running"
            network = experiment / "js-reverse" / "network" / "ev_one"
            replay = experiment / "replay"
            network.mkdir(parents=True)
            replay.mkdir(parents=True)
            raw_body = network / "requestBody.bin"
            all_snapshot = network / "all.json"
            request_spec = replay / "request-spec.json"
            for path in [raw_body, all_snapshot, request_spec]:
                path.write_text("running-path-secret\n", encoding="utf-8")
            (experiment / "manifest.json").write_text(
                json.dumps(
                    {
                        "experiment_id": "exp_running",
                        "status": "running",
                        "artifacts": [],
                    }
                ),
                encoding="utf-8",
            )
            service = AnalysisWorkspaceService(root)

            for path in [raw_body, all_snapshot, request_spec]:
                relative = path.relative_to(root).as_posix()
                with self.subTest(path=relative):
                    hidden = asyncio.run(
                        service.read_files(
                            WorkspaceReadFilesRequest(paths=[relative])
                        )
                    )
                    explicit = asyncio.run(
                        service.read_files(
                            WorkspaceReadFilesRequest(
                                paths=[relative],
                                include_credentials=True,
                            )
                        )
                    )
                    self.assertIn(
                        "hidden by default",
                        hidden.files[0].error or "",
                    )
                    self.assertIn(
                        "running-path-secret",
                        explicit.files[0].content,
                    )

            hidden_search = asyncio.run(
                service.search(
                    WorkspaceSearchRequest(
                        query="running-path-secret",
                        paths=["experiments/exp_running"],
                    )
                )
            )
            explicit_search = asyncio.run(
                service.search(
                    WorkspaceSearchRequest(
                        query="running-path-secret",
                        paths=["experiments/exp_running"],
                        include_credentials=True,
                    )
                )
            )
            self.assertEqual(hidden_search.match_count, 0)
            self.assertEqual(explicit_search.match_count, 3)

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
