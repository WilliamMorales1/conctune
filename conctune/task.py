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

    points: list[int] = field(default_factory=list)
    repeats: int = 3
    randomize: bool = True
    warmup: int = 1

    source: Path | None = None

    @property
    def has_cases(self) -> bool:
        return bool(self.list_cmd and self.one_cmd)


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


def profile_cases(spec: TaskSpec, log=print) -> dict:
    """Measure per-case service time s_i, serially, one worker.

    Serial measurement is deliberate: s_i must be free of contention, or the
    span and work figures inherit whatever interference the parallel run had.
    """
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
