"""conctune -- find the worker count a task should run at, and say why.

Pipeline, each stage usable on its own:

    machine  ->  task  ->  sweep  ->  model
    (specs)     (cases)   (runs)    (verdict)

Both ends are swappable: --task points at any TOML workload spec, --machine
at any probed host profile (so a profile captured on one box can be reused
to reason about another).
"""

from __future__ import annotations

import argparse
import contextlib
import random
import sys
from pathlib import Path

from . import machine as machine_mod
from . import model as model_mod
from . import report as report_mod
from . import sweep as sweep_mod
from . import task as task_mod

DEFAULT_OUT = Path("conctune-out")


def _workdir(args, spec=None) -> Path:
    if args.out:
        path = Path(args.out)
    else:
        name = spec.name if spec else "default"
        path = DEFAULT_OUT / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_machine(args) -> dict:
    if args.machine:
        profile = machine_mod.load(args.machine)
        print(f"machine profile: {args.machine} ({profile.get('hostname', '?')})")
        return profile
    return machine_mod.probe()


def _print_machine(profile: dict) -> None:
    print(f"  {profile.get('model')}")
    print(
        f"  {profile.get('cores_physical')} physical / "
        f"{profile.get('cores_logical')} logical cores, "
        f"{profile.get('mem_total', 0) / (1 << 30):.1f} GiB RAM"
    )
    print(f"  suggested sweep points: {profile.get('suggested_points')}")
    for warning in profile.get("warnings", []):
        print(f"  ! {warning}")


# --------------------------------------------------------------------------
# stages


def cmd_machine(args) -> int:
    profile = _load_machine(args)
    out = _workdir(args) / "machine.json"
    machine_mod.save(profile, out)
    print("MACHINE")
    _print_machine(profile)
    print(f"\nwrote {out}")
    return 0


def cmd_task(args) -> int:
    spec = task_mod.load_spec(args.task)
    work = _workdir(args, spec)

    if not spec.has_cases:
        print(
            f"task '{spec.name}' has no [cases] section -- nothing to profile.\n"
            "Add [cases].list and [cases].one to unlock the span ceiling and the "
            "load-balance analysis, or go straight to `conctune sweep`."
        )
        return 1

    print(f"TASK '{spec.name}' -- profiling cases serially at one worker")
    profile = task_mod.profile_cases(spec)
    out = work / "task.json"
    task_mod.save(profile, out)

    print()
    print(f"  cases measured : {profile['cases_measured']} of {profile['cases_total']}")
    print(f"  work  T1       : {profile['work']:.2f}s")
    print(f"  span  Tinf     : {profile['span']:.2f}s "
          f"({profile['span'] / profile['work'] * 100:.1f}% of all work)")
    print(f"  median case    : {profile['median']:.3f}s   p90 {profile['p90']:.3f}s")
    print(f"  avg parallelism: {profile['work'] / profile['span']:.1f}")
    if profile.get("startup"):
        print(f"  worker startup B: {profile['startup']['median']:.3f}s")
    if profile["failures"]:
        print(f"  ! {len(profile['failures'])} case(s) failed: {profile['failures'][:5]}")
    print(f"\nwrote {out}")
    return 0


def cmd_sweep(args) -> int:
    spec = task_mod.load_spec(args.task)
    profile = _load_machine(args)
    work = _workdir(args, spec)

    if args.repeats:
        spec.repeats = args.repeats
    if args.no_randomize:
        spec.randomize = False

    points = sweep_mod.plan(
        spec, profile, [int(p) for p in args.points.split(",")] if args.points else None
    )

    print(f"SWEEP '{spec.name}'")
    print(f"  points  : {points}")
    print(f"  repeats : {spec.repeats}  ({len(points) * spec.repeats} runs)")
    print(f"  order   : {'randomised' if spec.randomize else 'sequential'}")
    for warning in profile.get("warnings", []):
        print(f"  ! {warning}")
    print()

    machine_mod.save(profile, work / "machine.json")
    rows = sweep_mod.execute(spec, points, work / "runs.csv")

    failed = [r for r in rows if r["returncode"] != 0]
    if failed:
        print(f"\n! {len(failed)} of {len(rows)} runs failed and were dropped")
    print(f"\nwrote {work / 'runs.csv'}")
    return 0


def cmd_model(args) -> int:
    spec = task_mod.load_spec(args.task) if args.task else None
    work = _workdir(args, spec)

    runs_path = Path(args.runs) if args.runs else work / "runs.csv"
    if not runs_path.exists():
        print(f"no sweep data at {runs_path} -- run `conctune sweep` first", file=sys.stderr)
        return 1

    machine_path = Path(args.machine) if args.machine else work / "machine.json"
    profile = (
        machine_mod.load(machine_path) if machine_path.exists() else machine_mod.probe()
    )

    task_path = Path(args.task_profile) if args.task_profile else work / "task.json"
    task_profile = task_mod.load(task_path) if task_path.exists() else None

    rows = sweep_mod.read(runs_path)
    summary = sweep_mod.aggregate(rows)
    if 1 not in summary:
        print("sweep has no successful N=1 baseline; cannot compute speedup", file=sys.stderr)
        return 1

    result = model_mod.analyse(profile, summary, task_profile)
    name = spec.name if spec else runs_path.parent.name

    if args.json:
        text = report_mod.render_json(profile, result, summary, name)
    else:
        text = report_mod.render(profile, result, summary, name)
    print(text)

    out = work / ("report.json" if args.json else "report.txt")
    out.write_text(text + "\n")
    if not args.json:
        print(f"wrote {out}")
    return 0


def cmd_auto(args) -> int:
    # With --json, stdout carries only the report so it can be piped to jq.
    with contextlib.redirect_stdout(sys.stderr) if args.json else contextlib.nullcontext():
        _auto_stages(args)
    return cmd_model(args)


def _auto_stages(args) -> None:
    spec = task_mod.load_spec(args.task)
    profile = _load_machine(args)
    work = _workdir(args, spec)
    machine_mod.save(profile, work / "machine.json")

    print("MACHINE")
    _print_machine(profile)

    if spec.has_cases:
        print(f"\nTASK '{spec.name}' -- profiling cases serially at one worker")
        task_profile = task_mod.profile_cases(spec)
        task_mod.save(task_profile, work / "task.json")
        print(
            f"  work {task_profile['work']:.2f}s, span {task_profile['span']:.2f}s, "
            f"avg parallelism {task_profile['work'] / task_profile['span']:.1f}"
        )
    else:
        print("\nTASK -- no [cases] section; skipping case profiling")

    if args.repeats:
        spec.repeats = args.repeats
    points = sweep_mod.plan(
        spec, profile, [int(p) for p in args.points.split(",")] if args.points else None
    )
    print(f"\nSWEEP -- points {points}, {spec.repeats} repeats "
          f"({len(points) * spec.repeats} runs)\n")
    sweep_mod.execute(spec, points, work / "runs.csv")

    print()
    args.runs = str(work / "runs.csv")
    args.task_profile = str(work / "task.json")
    args.machine = str(work / "machine.json")


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="conctune",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--seed", type=int, help="seed the RNG for reproducible run order")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p, task_required=True):
        # SUPPRESS so a subcommand-level --seed overrides the top-level one
        # without clobbering it when absent.
        p.add_argument(
            "--seed", type=int, default=argparse.SUPPRESS,
            help="seed the RNG for reproducible run order",
        )
        p.add_argument("--task", required=task_required, help="task spec TOML")
        p.add_argument("--machine", help="machine profile JSON (default: probe this host)")
        p.add_argument("--out", help="output directory (default: conctune-out/<task>)")

    p = sub.add_parser("machine", help="stage 1: probe machine specs")
    p.add_argument("--seed", type=int, default=argparse.SUPPRESS)
    p.add_argument("--machine", help="load a profile instead of probing")
    p.add_argument("--out", help="output directory")
    p.set_defaults(func=cmd_machine, task=None)

    p = sub.add_parser("task", help="stage 2: profile per-case service times")
    common(p)
    p.set_defaults(func=cmd_task)

    p = sub.add_parser("sweep", help="stage 3: time the task at several worker counts")
    common(p)
    p.add_argument("--points", help="comma-separated worker counts, e.g. 1,2,4,8,16")
    p.add_argument("--repeats", type=int, help="repeats per point")
    p.add_argument("--no-randomize", action="store_true", help="run in order (not advised)")
    p.set_defaults(func=cmd_sweep)

    p = sub.add_parser("model", help="stage 4: fit the model and recommend N")
    common(p, task_required=False)
    p.add_argument("--runs", help="sweep CSV (default: <out>/runs.csv)")
    p.add_argument("--task-profile", help="case profile JSON (default: <out>/task.json)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_model)

    p = sub.add_parser("auto", help="run all four stages")
    common(p)
    p.add_argument("--points", help="comma-separated worker counts")
    p.add_argument("--repeats", type=int, help="repeats per point")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=cmd_auto, runs=None, task_profile=None, no_randomize=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "seed", None) is not None:
        random.seed(args.seed)
    try:
        return args.func(args)
    except task_mod.TaskError as error:
        print(f"task spec error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
