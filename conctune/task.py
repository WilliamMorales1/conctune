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

import json
import os
import random
import re
import shlex
import statistics
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


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
    with open(path, "rb") as handle:
        raw = tomllib.load(handle)

    run = raw.get("run") or {}
    if not run.get("cmd"):
        raise TaskError(f"{path}: [run].cmd is required")
    if "{j}" not in run["cmd"]:
        raise TaskError(
            f"{path}: [run].cmd must contain the placeholder {{j}}, which is "
            "substituted with the worker count"
        )

    cases = raw.get("cases") or {}
    if bool(cases.get("list")) != bool(cases.get("one")):
        raise TaskError(
            f"{path}: [cases].list and [cases].one must be given together"
        )
    if cases.get("one") and "{case}" not in cases["one"]:
        raise TaskError(f"{path}: [cases].one must contain the placeholder {{case}}")
    if bool(cases.get("log")) != bool(cases.get("case_pattern")):
        raise TaskError(
            f"{path}: [cases].log and [cases].case_pattern must be given together"
        )
    if cases.get("case_pattern"):
        try:
            compiled = re.compile(cases["case_pattern"])
        except re.error as error:
            raise TaskError(f"{path}: [cases].case_pattern is not a valid regex: {error}")
        names = set(compiled.groupindex)
        if not names & {"ms", "sec", "s"}:
            raise TaskError(
                f"{path}: [cases].case_pattern needs a named group giving the "
                "duration -- (?P<ms>...), (?P<sec>...) or (?P<s>...)"
            )

    sweep = raw.get("sweep") or {}
    cwd = Path(run.get("cwd", path.parent)).expanduser()
    if not cwd.is_absolute():
        cwd = (path.parent / cwd).resolve()

    return TaskSpec(
        name=raw.get("name") or path.stem,
        run_cmd=run["cmd"],
        cwd=cwd,
        env={str(k): str(v) for k, v in (run.get("env") or {}).items()},
        timeout=run.get("timeout"),
        shell=bool(run.get("shell", True)),
        list_cmd=cases.get("list"),
        one_cmd=cases.get("one"),
        startup_cmd=cases.get("startup"),
        sample=int(cases.get("sample", 0)),
        log_glob=cases.get("log"),
        case_pattern=cases.get("case_pattern"),
        boot_pattern=cases.get("boot_pattern"),
        profile_jobs=int(cases.get("profile_jobs", 1)),
        points=[int(p) for p in (sweep.get("points") or [])],
        repeats=int(sweep.get("repeats", 3)),
        randomize=bool(sweep.get("randomize", True)),
        warmup=int(sweep.get("warmup", 1)),
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


_CLK_TCK = os.sysconf("SC_CLK_TCK")


def run_cmd(
    cmd: str, spec: TaskSpec, capture: bool = True
) -> RunResult:
    env = {**os.environ, **spec.env}
    before = _cpu_jiffies()
    start = time.perf_counter()
    proc = subprocess.run(
        cmd if spec.shell else shlex.split(cmd),
        shell=spec.shell,
        cwd=str(spec.cwd),
        env=env,
        capture_output=capture,
        text=True,
        timeout=spec.timeout,
    )
    wall = time.perf_counter() - start
    after = _cpu_jiffies()

    cpu = 0.0
    if before is not None and after is not None:
        cpu = max(0.0, (after - before) / _CLK_TCK)

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
    jobs = max(1, spec.profile_jobs)
    log(f"  profiling run at {jobs} worker(s) to populate the log")
    result = run_cmd(spec.run_cmd.replace("{j}", str(jobs)), spec)
    if result.returncode != 0:
        log(f"  ! profiling run exited {result.returncode}; parsing the log anyway")

    pattern = re.compile(spec.case_pattern)
    paths = sorted(Path(spec.cwd).glob(spec.log_glob)) if not os.path.isabs(
        spec.log_glob
    ) else sorted(Path("/").glob(spec.log_glob.lstrip("/")))
    if not paths:
        raise TaskError(f"[cases].log matched no files: {spec.log_glob}")

    timings: dict[str, float] = {}
    duplicates = 0
    for path in paths:
        for line in path.read_text(errors="replace").splitlines():
            match = pattern.search(line)
            if not match:
                continue
            groups = match.groupdict()
            seconds = _duration(groups)
            if seconds is None:
                continue
            # Case id: every named group but the duration, in pattern order.
            parts = [
                value for name, value in groups.items()
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
        "invocation_overhead": 0.0,   # the workload timed the work itself
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
    text = path.read_text(errors="replace").splitlines()
    return text[0] if text else "(empty)"


def _boot_from_log(spec: TaskSpec, paths: list[Path], log=print) -> dict | None:
    """Per-worker boot cost B, reported by the workload itself."""
    if not spec.boot_pattern:
        return None
    pattern = re.compile(spec.boot_pattern)
    samples = []
    for path in paths:
        for line in path.read_text(errors="replace").splitlines():
            match = pattern.search(line)
            if match:
                seconds = _duration(match.groupdict())
                if seconds is not None:
                    samples.append(seconds)
    if not samples:
        log("  ! [cases].boot_pattern matched nothing; B left to the USL fit")
        return None
    log(f"  worker boot from log: {len(samples)} sample(s), "
        f"median {statistics.median(samples):.3f}s")
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

    cases = list_cases(spec)
    total = len(cases)

    if spec.sample and spec.sample < total:
        cases = random.sample(cases, spec.sample)
        log(f"  sampling {spec.sample} of {total} cases")

    timings: dict[str, float] = {}
    failures: list[str] = []
    for index, case in enumerate(sorted(cases), 1):
        result = run_cmd(spec.one_cmd.replace("{case}", shlex.quote(case)), spec)
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
    net = {
        case: max(duration - overhead, 1e-6) for case, duration in timings.items()
    }

    gross_values = sorted(timings.values())
    values = sorted(net.values())
    return {
        "cases_total": total,
        "cases_measured": len(timings),
        "sampled": bool(spec.sample and spec.sample < total),
        "failures": failures,
        "timings": net,                  # service times, boot removed
        "timings_gross": timings,        # as measured, boot included
        "invocation_overhead": overhead,
        "work": sum(values),             # T_1  (sum of service times)
        "work_gross": sum(gross_values),
        "span": max(values),             # T_inf (longest single case)
        "span_gross": max(gross_values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "p90": values[max(0, int(0.90 * len(values)) - 1)],
        "startup": startup,              # B, per-worker boot cost
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
    with open(path, "w") as handle:
        json.dump(profile, handle, indent=2)
        handle.write("\n")


def load(path: str | os.PathLike) -> dict:
    with open(path) as handle:
        return json.load(handle)
