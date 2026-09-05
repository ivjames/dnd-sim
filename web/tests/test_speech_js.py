"""Run the node tests for web/static/speech.js from pytest.

speech.js is the one piece of browser code with real logic in it (which voice
speaks a line, and what it says), and it is written to be requireable by node
for exactly this reason. Skipped where node is absent so the Python suite still
runs on a machine without it.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

TEST_JS = Path(__file__).with_name("speech_test.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_speech_js():
    proc = subprocess.run(
        [shutil.which("node"), str(TEST_JS)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
