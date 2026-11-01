"""Spec validation, log harvest, and CPU accounting."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conctune import cli, task

from .conftest import FIXTURES

MINIMAL = '[run]\ncmd = "{python} -c pass {j}"\n'


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ('name = "t"\n', "[run].cmd"),
        ('[run]\ncmd = "make"\n', "{j}"),
        (MINIMAL + '[cases]\nlist = "ls"\n', "[cases].list"),
        (MINIMAL + '[cases]\none = "run {case}"\n', "[cases].list"),
        (MINIMAL + '[cases]\nlist = "ls"\none = "run"\n', "[cases].one"),
        (MINIMAL + '[cases]\nlog = "*.log"\n', "[cases].case_pattern"),
        (
            MINIMAL + "[cases]\nlog = '*.log'\ncase_pattern = '(?P<ms>'\n",
            "not a valid regex",
        ),
        (
            MINIMAL + "[cases]\nlog = '*.log'\ncase_pattern = '(?P<n>\\w+)'\n",
            "named group",
        ),
        (
            MINIMAL
            + "[cases]\nlog = '*.log'\ncase_pattern = '(?P<ms>\\d+)'\nboot_pattern = '('\n",
            "[cases].boot_pattern",
        ),
        ("[run\n", "invalid TOML"),
        ('run = "x"\n', "[run]"),
        (MINIMAL + "[sweep]\nrepeats = 'many'\n", "[sweep].repeats"),
        (MINIMAL + "[sweep]\npoints = 4\n", "[sweep].points"),
    ],
)
def test_spec_errors_exit_2_naming_the_field(write_spec, capsys, body, field):
    path = write_spec(body)
    assert cli.main(["task", "--task", str(path)]) == 2
    err = capsys.readouterr().err
    assert field in err
    assert err.count("\n") == 1


def test_missing_spec_exits_2(capsys, tmp_path):
    assert cli.main(["task", "--task", str(tmp_path / "missing.toml")]) == 2
    assert "No such file" in capsys.readouterr().err


def test_relative_cwd_resolves_against_the_spec(write_spec, tmp_path):
    (tmp_path / "sub").mkdir()
    spec = task.load_spec(write_spec(MINIMAL.replace("\n", '\ncwd = "sub"\n', 1)))
    assert spec.cwd == (tmp_path / "sub").resolve()


# --------------------------------------------------------------------------
# log harvest

PATTERN = (
    r"^(?:PASS|FAIL)\s+\[(?P<group>[^\]]+)\]\s+(?P<name>.+?)\s+"
    r"\((?:(?P<ms>\d+)ms|(?P<sec>[\d.]+)sec|(?P<s>[\d.]+)s)\)"
)


def _harvest_spec(tmp_path: Path, pattern: str = PATTERN, glob: str = "run.log.shard*"):
    for path in (FIXTURES / "harvest").iterdir():
        shutil.copy(path, tmp_path)
    return task.TaskSpec(
        name="h",
        run_cmd="{python} -c pass {j}",
        cwd=tmp_path,
        log_glob=glob,
        case_pattern=pattern,
        boot_pattern=r"ready in (?P<ms>\d+)ms",
    )


def test_harvest_reads_cases_units_and_boot(tmp_path):
    messages: list[str] = []
    profile = task.profile_cases(_harvest_spec(tmp_path), log=messages.append)
    timings = profile["timings"]
    assert timings == pytest.approx(
        {
            "core loads config": 0.040,  # duplicate: the later time wins
            "core keymaps": 1.5,
            "lsp attaches": 2.0,
            "ui statusline": 0.250,
        }
    )
    assert profile["source"] == "log"
    assert profile["span"] == pytest.approx(2.0)
    assert profile["startup"]["reps"] == 2
    assert profile["startup"]["median"] == pytest.approx(0.1)
    assert any("1 duplicate case id" in m for m in messages)


def test_harvest_absolute_glob(tmp_path):
    spec = _harvest_spec(tmp_path, glob=str(tmp_path / "run.log.shard*"))
    assert task.profile_cases(spec, log=lambda _: None)["cases_total"] == 4


def test_harvest_reports_a_pattern_that_matches_nothing(tmp_path):
    spec = _harvest_spec(tmp_path, pattern=r"^OK (?P<name>\S+) (?P<ms>\d+)$")
    with pytest.raises(task.TaskError, match="matched nothing") as info:
        task.profile_cases(spec, log=lambda _: None)
    assert "worker 1 ready in 120ms" in str(info.value)


def test_harvest_reports_a_glob_that_matches_no_files(tmp_path):
    spec = _harvest_spec(tmp_path, glob="absent*.log")
    with pytest.raises(task.TaskError, match="matched no files"):
        task.profile_cases(spec, log=lambda _: None)


def test_unmatched_boot_pattern_leaves_startup_to_the_fit(tmp_path):
    spec = _harvest_spec(tmp_path)
    spec.boot_pattern = r"never (?P<ms>\d+)"
    messages: list[str] = []
    assert task.profile_cases(spec, log=messages.append)["startup"] is None
    assert any("boot_pattern matched nothing" in m for m in messages)


# --------------------------------------------------------------------------
# CPU accounting


def test_cgroup_cpu_ignores_other_load():
    """Run CPU must count the run's own processes, not a busy neighbour."""
    if task.cpu_source() != "cgroup":
        pytest.skip("no delegated cgroup v2")
    spin = "import time\nend = time.time() + {}\nwhile time.time() < end: pass"
    neighbour = subprocess.Popen([sys.executable, "-c", spin.format(1.5)])
    try:
        time.sleep(0.1)
        spec = task.TaskSpec(name="t", run_cmd="", cwd=Path.cwd(), shell=False)
        result = task.run_cmd(f"{sys.executable} -c '{spin.format(0.5)}'", spec)
    finally:
        neighbour.wait()
    assert 0.35 < result.cpu < 0.75
