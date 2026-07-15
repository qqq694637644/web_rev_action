from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from skill_temple.workspace_models import (
    WorkspaceSearchRequest,
)
from skill_temple.workspace_service import AnalysisWorkspaceService
from tests.workspace.common import WorkspaceTestCase


class SearchWorkspaceTests(WorkspaceTestCase):
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
