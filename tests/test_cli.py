"""CLI behaviour through a real subprocess: exit codes, output streams, end to end."""

from __future__ import annotations

import csv
import json
import shutil
import time
from pathlib import Path

import pytest

from conctune import __version__

from .conftest import FIXTURES, REPO, run_cli

TRUE_SPEC = 'name = "t"\n[run]\ncmd = "{python} -c pass {j}"\n[sweep]\npoints = [1, 2]\nrepeats = 1\nwarmup = 0\n'


def test_exit_0_version():
    proc = run_cli("--version")
    assert proc.returncode == 0
    assert proc.stdout.strip() == f"conctune {__version__}"


def test_exit_1_model_without_runs(tmp_path):
    proc = run_cli("model", "--out", str(tmp_path))
    assert proc.returncode == 1
    assert "no sweep data" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_exit_1_task_without_cases(write_spec, tmp_path):
    proc = run_cli("task", "--task", str(write_spec(TRUE_SPEC)), "--out", str(tmp_path))
    assert proc.returncode == 1


def test_exit_2_missing_spec(tmp_path):
    proc = run_cli("task", "--task", str(tmp_path / "missing.toml"))
    assert proc.returncode == 2
    assert proc.stderr.count("\n") == 1
    assert "Traceback" not in proc.stderr


def test_unwritable_out_is_one_line(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    proc = run_cli("model", "--out", str(blocker / "sub"))
    assert proc.returncode != 0
    assert proc.stderr.count("\n") == 1
    assert "Traceback" not in proc.stderr


def test_auto_json_stdout_is_pure_json(write_spec, tmp_path):
    proc = run_cli(
        "auto", "--task", str(write_spec(TRUE_SPEC)), "--out", str(tmp_path), "--json"
    )
    assert proc.returncode == 0, proc.stderr
    assert "analysis" in json.loads(proc.stdout)
    assert "SWEEP" in proc.stderr


def test_auto_no_randomize_runs_in_order(write_spec, tmp_path):
    spec = TRUE_SPEC.replace("points = [1, 2]", "points = [1, 2, 3]").replace(
        "repeats = 1", "repeats = 2"
    )
    proc = run_cli(
        "auto",
        "--task",
        str(write_spec(spec)),
        "--out",
        str(tmp_path),
        "--no-randomize",
    )
    assert proc.returncode == 0, proc.stderr
    with open(tmp_path / "runs.csv") as handle:
        rows = sorted(csv.DictReader(handle), key=lambda r: int(r["order"]))
    assert [int(r["n"]) for r in rows] == [1, 1, 2, 2, 3, 3]


def test_model_json_names_the_task_from_the_runs(tmp_path):
    out = tmp_path / "some-dir"
    out.mkdir()
    for name in ("runs.csv", "machine.json", "task.json"):
        shutil.copy(FIXTURES / "golden" / name, out)
    proc = run_cli("model", "--json", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["task"] == "demo"


@pytest.mark.slow
def test_auto_demo_end_to_end(tmp_path):
    budget = 120.0
    start = time.monotonic()
    proc = run_cli(
        "auto",
        "--task",
        str(REPO / "examples" / "demo.toml"),
        "--points",
        "1,2,4,8",
        "--repeats",
        "1",
        "--seed",
        "0",
        "--out",
        str(tmp_path),
        cwd=Path(tmp_path),
        timeout=budget,
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, proc.stderr
    assert "RECOMMENDED WORKER COUNT" in proc.stdout
    assert elapsed < budget
    for name in ("machine.json", "task.json", "runs.csv", "report.txt"):
        assert (tmp_path / name).exists()


def test_auto_short_stdout_is_only_the_verdict(write_spec, tmp_path):
    proc = run_cli(
        "auto", "--task", str(write_spec(TRUE_SPEC)), "--out", str(tmp_path), "--short"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines()[0].startswith("N=")
    assert "SWEEP" not in proc.stdout
    assert "SWEEP" in proc.stderr
