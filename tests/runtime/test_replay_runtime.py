from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from skill_temple.browser.replay_runtime import load_replay_runtime


def test_python_loader_returns_standalone_runtime_asset() -> None:
    runtime_path = Path("src/skill_temple/browser/replay_runtime.js")
    adapter_source = Path("src/skill_temple/browser_adapters.py").read_text(encoding="utf-8")

    assert load_replay_runtime() == runtime_path.read_text(encoding="utf-8")
    assert "function = load_replay_runtime()" in adapter_source
    assert "async ({localFile}) => {" not in adapter_source


def test_node_replay_runtime_suite() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for replay runtime tests")

    result = subprocess.run(
        [node, "--test", "tests/runtime/replay_runtime.test.js"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
