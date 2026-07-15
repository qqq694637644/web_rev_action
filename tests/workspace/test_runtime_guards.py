from __future__ import annotations

import tempfile
from pathlib import Path

from tests.workspace.common import WorkspaceTestCase


class RuntimeGuardsWorkspaceTests(WorkspaceTestCase):
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
