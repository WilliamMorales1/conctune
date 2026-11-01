"""Stage 3: measure the machine running the task at several worker counts.

The output is a CSV of (points x repeats) rows.  Run order is randomised
across the whole matrix by default so that thermal drift and frequency
ramping do not correlate with the worker count -- without that, a laptop
that heats up during the sweep manufactures fake retrograde scaling.
"""

from __future__ import annotations

import csv
import random
import statistics
from pathlib import Path

from .task import TaskSpec, cpu_source, run_cmd

FIELDS = ["n", "rep", "wall", "cpu", "returncode", "order", "cpu_source"]


def plan(spec: TaskSpec, machine: dict, points: list[int] | None = None) -> list[int]:
    if points:
        chosen = points
    elif spec.points:
        chosen = spec.points
    else:
        chosen = machine.get("suggested_points") or [1, 2, 4, 8]

    chosen = sorted({int(p) for p in chosen if int(p) >= 1})
    if 1 not in chosen:
        # N=1 is the T_1 baseline; every speedup is relative to it.
        chosen.insert(0, 1)
    return chosen


def execute(
    spec: TaskSpec,
    points: list[int],
    out_path: str | Path,
    log=print,
) -> list[dict]:
    matrix = [(n, rep) for n in points for rep in range(spec.repeats)]
    if spec.randomize:
        random.shuffle(matrix)

    if spec.warmup:
        log(f"warmup: {spec.warmup} run(s) at N={points[-1]} (discarded)")
        for _ in range(spec.warmup):
            run_cmd(spec.run_cmd.replace("{j}", str(points[-1])), spec)

    rows: list[dict] = []
    for order, (n, rep) in enumerate(matrix, 1):
        cmd = spec.run_cmd.replace("{j}", str(n))
        result = run_cmd(cmd, spec)
        status = "" if result.returncode == 0 else f"  !! exit {result.returncode}"
        log(
            f"[{order}/{len(matrix)}] N={n:<3} rep={rep}  "
            f"wall={result.wall:8.3f}s  cpu={result.cpu:8.3f}s{status}"
        )
        if result.returncode != 0 and result.stderr.strip():
            log("    " + result.stderr.strip().splitlines()[-1])
        rows.append(
            {
                "n": n,
                "rep": rep,
                "wall": round(result.wall, 6),
                "cpu": round(result.cpu, 6),
                "returncode": result.returncode,
                "order": order,
                "cpu_source": cpu_source(),
            }
        )

    write(rows, out_path)
    return rows


def write(rows: list[dict], path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: (r["n"], r["rep"])))


def read(path: str | Path) -> list[dict]:
    with open(path, newline="") as handle:
        return [
            {
                "n": int(row["n"]),
                "rep": int(row["rep"]),
                "wall": float(row["wall"]),
                "cpu": float(row["cpu"]),
                "returncode": int(row["returncode"]),
                "order": int(row.get("order", 0)),
                # CSVs from before cgroup accounting were all /proc/stat
                "cpu_source": row.get("cpu_source") or "proc_stat",
            }
            for row in csv.DictReader(handle)
        ]


def aggregate(rows: list[dict]) -> dict[int, dict]:
    """Collapse repeats to one figure per worker count.

    Median, not mean: on a thermally throttled laptop the distribution has a
    long right tail and the mean chases it.
    """
    by_n: dict[int, list[dict]] = {}
    for row in rows:
        by_n.setdefault(row["n"], []).append(row)

    summary = {}
    for n, group in sorted(by_n.items()):
        good = [r for r in group if r["returncode"] == 0]
        failed = len(group) - len(good)
        if not good:
            # Nothing usable to time, but the failures still matter: a worker
            # count the task cannot survive is not a candidate at any speed.
            summary[n] = {
                "n": n, "reps": 0, "reps_total": len(group), "failed": failed,
                "fail_rate": 1.0, "wall_median": None, "wall_min": None,
                "wall_max": None, "spread": 0.0, "cpu_median": 0.0,
                "cpu_source": group[0]["cpu_source"],
            }
            continue

        walls = sorted(r["wall"] for r in good)
        cpus = [r["cpu"] for r in good]
        median = statistics.median(walls)
        summary[n] = {
            "n": n,
            "reps": len(walls),
            "reps_total": len(group),
            "failed": failed,
            "fail_rate": failed / len(group),
            "wall_median": median,
            "wall_min": walls[0],
            "wall_max": walls[-1],
            "spread": (walls[-1] - walls[0]) / median if median else 0.0,
            "cpu_median": statistics.median(cpus) if cpus else 0.0,
            "cpu_source": good[0]["cpu_source"],
        }
    return summary
