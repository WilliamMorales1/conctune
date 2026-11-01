from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def run_cli(
    *args: str, cwd: Path = REPO, timeout: float = 120
) -> subprocess.CompletedProcess:
    """run_cli runs `python -m conctune` in a subprocess and captures its output."""
    env = {**os.environ, "PYTHONPATH": str(REPO)}
    return subprocess.run(
        [sys.executable, "-m", "conctune", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def write_spec(tmp_path: Path):
    def write(text: str, name: str = "t.toml") -> Path:
        path = tmp_path / name
        path.write_text(text)
        return path

    return write
