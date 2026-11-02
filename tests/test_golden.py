"""Golden report: committed sweep data must render to the committed report."""

from __future__ import annotations

import json

from .conftest import FIXTURES, run_cli

GOLDEN = FIXTURES / "golden"


def _model(tmp_path, *extra):
    return run_cli(
        "model",
        "--runs",
        str(GOLDEN / "runs.csv"),
        "--machine",
        str(GOLDEN / "machine.json"),
        "--task-profile",
        str(GOLDEN / "task.json"),
        "--out",
        str(tmp_path),
        *extra,
    )


def test_text_report_matches_golden(tmp_path):
    proc = _model(tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "report.txt").read_text() == (GOLDEN / "report.txt").read_text()


def test_json_has_the_fields_the_skill_reads(tmp_path):
    proc = _model(tmp_path, "--json")
    assert proc.returncode == 0, proc.stderr
    analysis = json.loads(proc.stdout)["analysis"]
    # Mirrors the jq filter in skills/conctune/SKILL.md.
    assert isinstance(analysis["recommended_n"]["n"], int)
    assert isinstance(analysis["recommended_n"]["notes"], list)
    assert analysis["binding"] in analysis["ceilings"]
    assert all("n" in c for c in analysis["ceilings"].values())
    assert 0.0 <= analysis["fit"]["r2"] <= 1.0
    assert isinstance(analysis["imbalance"]["scheduling_suspect"], bool)
    assert "unreliable" in analysis
    assert "work_overstated" in analysis


def test_short_verdict(tmp_path):
    proc = _model(tmp_path, "--short")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.splitlines()
    assert lines[0] == "N=8 binding=startup r2=1.00"
    assert all(line.startswith(("note: ", "warn: ")) for line in lines[1:])
    assert (tmp_path / "report.txt").read_text() == (GOLDEN / "report.txt").read_text()
