from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from skill_temple.app import (
    create_app,
)
from skill_temple.workspace_service import AnalysisWorkspaceService


class WorkspaceTestCase(unittest.TestCase):
    def make_client(self, root: Path) -> TestClient:
        service = AnalysisWorkspaceService(root, allow_network=False)
        env = {
            "WEB_REV_EVIDENCE_DIR": str(root),
            "WEB_REV_BROWSER_CDP_URL": "http://127.0.0.1:9222",
        }
        with patch.dict(os.environ, env, clear=False):
            app = create_app(workspace_service=service)
        return TestClient(app)
