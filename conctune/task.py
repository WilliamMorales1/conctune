"""Stage 2: task-specific specs.

A task is described by a TOML file, which is what makes the workload
swappable.  Two fidelity levels:

  minimal -- only [run].cmd, a command taking a worker count.  Enough for
             the sweep and the USL fit.
  full    -- also [cases].list and [cases].one, so individual case service
             times can be measured directly.  That unlocks the span
             (critical path) ceiling, the load-imbalance analysis, and a
             directly measured per-worker startup cost.
"""

from __future__ import annotations

import atexit
import json
import os
import random
import re
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import machine


class TaskError(Exception):
    pass


@dataclass
class TaskSpec:
    name: str
    run_cmd: str
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)
    timeout: float | None = None
    shell: bool = True
    # Kernel threads an M:N runtime multiplexes its tasks onto (1 for an
    # asyncio or Node event loop, GOMAXPROCS for Go); None means every core.
    carriers: int | None = None

    list_cmd: str | None = None
    one_cmd: str | None = None
    startup_cmd: str | None = None
    sample: int = 0

    # log-parsing source: harvest service times from a run that already
    # reports them, instead of re-running each case in its own process
    log_glob: str | None = None
    case_pattern: str | None = None
    boot_pattern: str | None = None
    profile_jobs: int = 1

    points: list[int] = field(default_factory=list)
    repeats: int = 3
    randomize: bool = True
    warmup: int = 1

    source: Path | None = None

    @property
    def has_cases(self) -> bool:
        return bool((self.list_cmd and self.one_cmd) or self.from_log)

    @property
    def from_log(self) -> bool:
        return bool(self.log_glob and self.case_pattern)


def load_spec(path: str | os.PathLike) -> TaskSpec:
    path = Path(path).expanduser().resolve()
    try:
        with open(path, "rb") as handle:
            raw = tomllib.load(handle)
    except OSError as error:
        raise TaskError(f"{path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise TaskError(f"{path}: invalid TOML: {error}") from None

    def table(key: str) -> dict:
        value = raw.get(key, {})
        if not isinstance(value, dict):
            raise TaskError(f"{path}: [{key}] must be a table")
        return value

    def field_of(section: dict, where: str, key: str, kind, default=None) -> Any:
        value = section.get(key, default)
        # bool is an int subclass; `repeats = true` is a mistake, not 1.
        if value is not None and (
            not isinstance(value, kind)
            or (kind is not bool and isinstance(value, bool))
        ):
            names = (
                kind.__name__
                if isinstance(kind, type)
                else " or ".join(k.__name__ for k in kind)
            )
            label = f"[{where}].{key}" if where else key
            raise TaskError(f"{path}: {label} must be {names}")
        return value

    run, cases, sweep = table("run"), table("cases"), table("sweep")

    def run_field(key, kind, default=None) -> Any:
        return field_of(run, "run", key, kind, default)

    def case_field(key, kind, default=None) -> Any:
        return field_of(cases, "cases", key, kind, default)

    def sweep_field(key, kind, default=None) -> Any:
        return field_of(sweep, "sweep", key, kind, default)

    run_cmd = run_field("cmd", str)
    if not run_cmd:
        raise TaskError(f"{path}: [run].cmd is required")
    env = {str(k): str(v) for k, v in run_field("env", dict, {}).items()}
    if "{j}" not in run_cmd and not any("{j}" in v for v in env.values()):
        raise TaskError(
            f"{path}: [run].cmd (or a [run].env value) must contain the "
            "placeholder {j}, which is substituted with the worker count"
        )

    list_cmd = case_field("list", str)
    one_cmd = case_field("one", str)
    log_glob = case_field("log", str)
    case_pattern = case_field("case_pattern", str)
    boot_pattern = case_field("boot_pattern", str)
    if bool(list_cmd) != bool(one_cmd):
        raise TaskError(f"{path}: [cases].list and [cases].one must be given together")
    if one_cmd and "{case}" not in one_cmd:
        raise TaskError(f"{path}: [cases].one must contain the placeholder {{case}}")
    if bool(log_glob) != bool(case_pattern):
        raise TaskError(
            f"{path}: [cases].log and [cases].case_pattern must be given together"
        )
    for key, pattern in (
        ("case_pattern", case_pattern),
        ("boot_pattern", boot_pattern),
    ):
        if not pattern:
            continue
        try:
            compiled = re.compile(pattern)
        except re.error as error:
            raise TaskError(
                f"{path}: [cases].{key} is not a valid regex: {error}"
            ) from None
        if not set(compiled.groupindex) & {"ms", "sec", "s"}:
            raise TaskError(
                f"{path}: [cases].{key} needs a named group giving the "
                "duration -- (?P<ms>...), (?P<sec>...) or (?P<s>...)"
            )

    points = sweep_field("points", list, [])
    if not all(isinstance(p, int) and not isinstance(p, bool) for p in points):
        raise TaskError(f"{path}: [sweep].points must be a list of integers")

    try:
        cwd = Path(run_field("cwd", str, str(path.parent))).expanduser()
    except RuntimeError:
        raise TaskError(f"{path}: [run].cwd names an unknown home directory") from None
    if not cwd.is_absolute():
        cwd = (path.parent / cwd).resolve()

    timeout = run_field("timeout", (int, float))
    carriers = run_field("carriers", int)
    if carriers is not None and carriers < 1:
        raise TaskError(f"{path}: [run].carriers must be at least 1")
    return TaskSpec(
        name=field_of(raw, "", "name", str) or path.stem,
        run_cmd=run_cmd,
        cwd=cwd,
        env=env,
        timeout=None if timeout is None else float(timeout),
        shell=run_field("shell", bool, True),
        carriers=carriers,
        list_cmd=list_cmd,
        one_cmd=one_cmd,
        startup_cmd=case_field("startup", str),
        sample=case_field("sample", int, 0),
        log_glob=log_glob,
        case_pattern=case_pattern,
        boot_pattern=boot_pattern,
        profile_jobs=case_field("profile_jobs", int, 1),
        points=points,
        repeats=sweep_field("repeats", int, 3),
        randomize=sweep_field("randomize", bool, True),
        warmup=sweep_field("warmup", int, 1),
        source=path,
    )


# --------------------------------------------------------------------------
# command execution


@dataclass
class RunResult:
    wall: float
    cpu: float
    returncode: int
    stdout: str
    stderr: str


def _cpu_jiffies() -> float | None:
    """Busy jiffies from /proc/stat -- total CPU across all cores.

    Used instead of GNU time so the CPU-saturation ceiling is available even
    when the workload spawns processes we do not directly parent.
    """
    try:
        with open("/proc/stat") as handle:
            parts = handle.readline().split()
    except OSError:
        return None
    if not parts or parts[0] != "cpu":
        return None
    values = [float(v) for v in parts[1:]]
    idle = values[3] + (values[4] if len(values) > 4 else 0.0)
    return sum(values) - idle


class _Cgroup:
    # Counts only the run's process tree (forkserver workers included), so an
    # unrelated busy process cannot drag the hardware ceiling down.

    def __init__(self, path: Path) -> None:
        self.path = path

    @classmethod
    def create(cls) -> _Cgroup | None:
        try:
            with open("/proc/self/cgroup") as handle:
                line = handle.readline().strip()
            if not line.startswith("0::"):
                return None
            path = Path("/sys/fs/cgroup" + line[3:]) / f"conctune-{os.getpid()}"
            path.mkdir()
        except OSError:
            return None
        group = cls(path)
        atexit.register(group.remove)
        if group.usage() is None:
            return None
        return group

    def enter(self) -> None:
        # Runs in the child between fork and exec.
        with open(self.path / "cgroup.procs", "w") as handle:
            handle.write("0")

    def usage(self) -> float | None:
        try:
            with open(self.path / "cpu.stat") as handle:
                for line in handle:
                    key, value = line.split()
                    if key == "usage_usec":
                        return int(value) / 1e6
        except (OSError, ValueError):
            pass
        return None

    def remove(self) -> None:
        try:
            self.path.rmdir()
        except OSError:
            pass  # a daemon the workload left behind still lives in it


_cgroup: _Cgroup | None = None
_cgroup_tried = False


def _run_cgroup() -> _Cgroup | None:
    global _cgroup, _cgroup_tried
    if not _cgroup_tried:
        _cgroup_tried = True
        _cgroup = _Cgroup.create()
    return _cgroup


def cpu_source() -> str:
    """cpu_source names where run CPU totals come from.

    'cgroup' or 'proc_stat' on Linux, 'system_times' on Windows, and 'rusage'
    elsewhere.
    """
    if sys.platform.startswith("linux"):
        return "cgroup" if _run_cgroup() else "proc_stat"
    return "system_times" if sys.platform == "win32" else "rusage"


def _cpu_seconds(group: _Cgroup | None) -> float | None:
    if group:
        return group.usage()
    if sys.platform == "win32":
        times = machine.system_cpu_times()
        return None if times is None else times[0]
    if not sys.platform.startswith("linux"):
        import resource

        # Counts the run's whole process tree, as long as each process was
        # waited for; a daemon that detaches is missed.
        usage = resource.getrusage(resource.RUSAGE_CHILDREN)
        return usage.ru_utime + usage.ru_stime
    jiffies = _cpu_jiffies()
    return None if jiffies is None else jiffies / os.sysconf("SC_CLK_TCK")


def quote(arg: str) -> str:
    """quote makes arg one word for the platform's shell."""
    if sys.platform == "win32":
        return subprocess.list2cmdline([arg])
    return shlex.quote(arg)


def run_cmd(cmd: str, spec: TaskSpec, capture: bool = True, jobs: int = 1) -> RunResult:
    # {j} in env reaches the worker count without shell syntax such as
    # `VAR={j} cmd`, which cmd.exe and PowerShell do not parse.
    env = {
        **os.environ,
        **{
            k: v.replace("{j}", str(jobs)).replace("{tmp}", tempfile.gettempdir())
            for k, v in spec.env.items()
        },
    }
    # {python} lets one spec run under whatever interpreter conctune runs
    # under; `python3` is not on PATH on stock Windows.
    cmd = cmd.replace("{python}", quote(sys.executable))
    # {tmp} gives harnesses that chdir an absolute path without assuming /tmp.
    cmd = cmd.replace("{tmp}", quote(tempfile.gettempdir()))
    group = _run_cgroup() if sys.platform.startswith("linux") else None
    before = _cpu_seconds(group)
    start = time.perf_counter()
    proc = subprocess.run(
        # Windows takes a command line, not argv, so no splitting there.
        cmd if spec.shell or sys.platform == "win32" else shlex.split(cmd),
        check=False,
        shell=spec.shell,
        cwd=str(spec.cwd),
        env=env,
        capture_output=capture,
        text=True,
        errors="replace",
        timeout=spec.timeout,
        preexec_fn=group.enter if group else None,
    )
    wall = time.perf_counter() - start
    after = _cpu_seconds(group)

    cpu = 0.0
    if before is not None and after is not None:
        cpu = max(0.0, after - before)

    return RunResult(
        wall=wall,
        cpu=cpu,
        returncode=proc.returncode,
        stdout=proc.stdout or "" if capture else "",
        stderr=proc.stderr or "" if capture else "",
    )


# --------------------------------------------------------------------------
# case profiling


def list_cases(spec: TaskSpec) -> list[str]:
    if not spec.list_cmd:
        raise TaskError("[cases].list is not set")
    result = run_cmd(spec.list_cmd, spec)
    if result.returncode != 0:
        raise TaskError(
            f"[cases].list failed (exit {result.returncode}):\n{result.stderr.strip()}"
        )
    cases = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not cases:
        raise TaskError("[cases].list produced no cases")
    return cases


def profile_from_log(spec: TaskSpec, log=print) -> dict:
    """Harvest service times from a run that reports them itself.

    Preferred whenever the workload already times its own units of work: it
    costs one run instead of one process per case, and the times are the
    real ones rather than one-case-per-process reconstructions.  The run is
    done at [cases].profile_jobs workers (1 by default) so the times carry
    no contention from other workers.
    """
    if not (spec.log_glob and spec.case_pattern):
        raise TaskError("[cases].log and [cases].case_pattern are not set")
    log_glob = spec.log_glob.replace("{tmp}", tempfile.gettempdir())
    jobs = max(1, spec.profile_jobs)
    log(f"  profiling run at {jobs} worker(s) to populate the log")
    result = run_cmd(spec.run_cmd.replace("{j}", str(jobs)), spec, jobs=jobs)
    if result.returncode != 0:
        log(f"  ! profiling run exited {result.returncode}; parsing the log anyway")

    pattern = re.compile(spec.case_pattern)
    if os.path.isabs(log_glob):
        anchor = Path(log_glob).anchor
        paths = sorted(Path(anchor).glob(log_glob[len(anchor) :]))
    else:
        paths = sorted(Path(spec.cwd).glob(log_glob))
    if not paths:
        raise TaskError(f"[cases].log matched no files: {log_glob}")

    timings: dict[str, float] = {}
    duplicates = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.search(line)
            if not match:
                continue
            groups = match.groupdict()
            seconds = _duration(groups)
            if seconds is None:
                continue
            # Case id: every named group but the duration, in pattern order.
            parts = [
                value
                for name, value in groups.items()
                if name not in ("ms", "sec", "s") and value
            ]
            case = " ".join(parts).strip() or f"case{len(timings)}"
            if case in timings:
                duplicates += 1
            timings[case] = seconds

    if not timings:
        raise TaskError(
            f"[cases].case_pattern matched nothing in {len(paths)} log file(s). "
            f"First line of {paths[0]}:\n  {_first_line(paths[0])}"
        )

    log(f"  parsed {len(timings)} case times from {len(paths)} log file(s)")
    if duplicates:
        log(f"  ! {duplicates} duplicate case id(s) -- later times won")

    boot = _boot_from_log(spec, paths, log=log)

    values = sorted(timings.values())
    return {
        "cases_total": len(timings),
        "cases_measured": len(timings),
        "sampled": False,
        "failures": [],
        "source": "log",
        "profile_jobs": jobs,
        "profile_wall": result.wall,
        "timings": timings,
        "invocation_overhead": 0.0,  # the workload timed the work itself
        "work": sum(values),
        "span": max(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p90": values[max(0, int(0.90 * len(values)) - 1)],
        "startup": boot,
    }


def _duration(groups: dict[str, str]) -> float | None:
    for name, divisor in (("ms", 1000.0), ("sec", 1.0), ("s", 1.0)):
        raw = groups.get(name)
        if raw:
            try:
                return float(raw) / divisor
            except ValueError:
                return None
    return None


def _first_line(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return text[0] if text else "(empty)"


def _boot_from_log(spec: TaskSpec, paths: list[Path], log=print) -> dict | None:
    """Per-worker boot cost B, reported by the workload itself."""
    if not spec.boot_pattern:
        return None
    pattern = re.compile(spec.boot_pattern)
    samples = []
    for path in paths:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = pattern.search(line)
            if match:
                seconds = _duration(match.groupdict())
                if seconds is not None:
                    samples.append(seconds)
    if not samples:
        log("  ! [cases].boot_pattern matched nothing; B left to the USL fit")
        return None
    log(
        f"  worker boot from log: {len(samples)} sample(s), "
        f"median {statistics.median(samples):.3f}s"
    )
    return {
        "reps": len(samples),
        "median": statistics.median(samples),
        "min": min(samples),
        "measured": True,
        "source": "log",
    }


def profile_cases(spec: TaskSpec, log=print) -> dict:
    """Measure per-case service time s_i, serially, one worker.

    Serial measurement is deliberate: s_i must be free of contention, or the
    span and work figures inherit whatever interference the parallel run had.
    """
    if spec.from_log:
        return profile_from_log(spec, log=log)
    if not spec.one_cmd:
        raise TaskError("[cases].one is not set")
    one_cmd = spec.one_cmd

    cases = list_cases(spec)
    total = len(cases)

    if spec.sample and spec.sample < total:
        cases = random.sample(cases, spec.sample)
        log(f"  sampling {spec.sample} of {total} cases")

    timings: dict[str, float] = {}
    failures: list[str] = []
    for index, case in enumerate(sorted(cases), 1):
        result = run_cmd(one_cmd.replace("{case}", quote(case)), spec)
        if result.returncode != 0:
            failures.append(case)
            log(f"  [{index}/{len(cases)}] {case}: FAILED (exit {result.returncode})")
            continue
        timings[case] = result.wall
        log(f"  [{index}/{len(cases)}] {case}: {result.wall:.3f}s")

    if not timings:
        raise TaskError("every case failed; cannot profile")

    startup = measure_startup(spec, log=log)

    # Each case was timed in its own invocation, so every s_i carries one
    # process boot (interpreter start, imports, connections).  Left in, it
    # inflates the work total above the CPU time the parallel run actually
    # burns, and pushes the scheduling lower bound above makespans that were
    # really achieved.  Net it out when [cases].startup measured it.
    overhead = startup["median"] if startup else 0.0
    net = {case: max(duration - overhead, 1e-6) for case, duration in timings.items()}

    gross_values = sorted(timings.values())
    values = sorted(net.values())
    return {
        "cases_total": total,
        "cases_measured": len(timings),
        "sampled": bool(spec.sample and spec.sample < total),
        "failures": failures,
        "timings": net,  # service times, boot removed
        "timings_gross": timings,  # as measured, boot included
        "invocation_overhead": overhead,
        "work": sum(values),  # T_1  (sum of service times)
        "work_gross": sum(gross_values),
        "span": max(values),  # T_inf (longest single case)
        "span_gross": max(gross_values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p90": values[max(0, int(0.90 * len(values)) - 1)],
        "startup": startup,  # B, per-worker boot cost
    }


def measure_startup(spec: TaskSpec, reps: int = 5, log=print) -> dict | None:
    """Per-worker boot cost B.

    Measured directly when the task supplies [cases].startup -- a command
    that boots a worker and exits without doing work.  Otherwise it is left
    to fall out of the USL fit as the coherency coefficient.
    """
    if not spec.startup_cmd:
        return None
    log(f"  measuring worker startup ({reps} reps)")
    samples = []
    for _ in range(reps):
        result = run_cmd(spec.startup_cmd, spec)
        if result.returncode == 0:
            samples.append(result.wall)
    if not samples:
        return None
    return {
        "reps": len(samples),
        "median": statistics.median(samples),
        "min": min(samples),
        "measured": True,
    }


def save(profile: dict, path: str | os.PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2)
        handle.write("\n")


def load(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
