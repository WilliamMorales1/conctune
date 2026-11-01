"""Human-readable rendering of the analysis."""

from __future__ import annotations

import json
import math

BAR = "=" * 72
RULE = "-" * 72

ADVICE = {
    "startup": (
        (
            "STARTUP-BOUND -- measured worker boot cost B dominates: past "
            "sqrt(work/B) workers, booting them costs more than they save."
        ),
        [
            "pool and reuse workers instead of booting one per batch",
            "move imports, model loads and connection setup out of the per-worker path",
            "batch small cases so each boot amortises over more work",
            "fork from a warm parent instead of spawning a cold interpreter",
        ],
    ),
    "overhead": (
        (
            "OVERHEAD-BOUND -- worker startup and coordination grow with N and "
            "eventually beat the work you are splitting."
        ),
        [
            "pool and reuse workers instead of spawning per batch",
            (
                "move one-time setup (imports, model loads, connections) out of the "
                "worker and into shared or inherited state"
            ),
            "batch small cases together so each worker boot amortises over more work",
            "adding workers past the peak makes the run SLOWER, not just flat",
        ],
    ),
    "parallelism": (
        (
            "PARALLELISM-BOUND -- the longest single case sets a floor no worker "
            "count can go under (makespan >= span)."
        ),
        [
            "split the fat cases; that is the only move that raises this ceiling",
            (
                "if the fat cases cannot be split, schedule them first (LPT) so nothing "
                "waits on them at the end"
            ),
            "this is the classic straggler / tail-at-scale shape",
        ],
    ),
    "hardware": (
        "HARDWARE-BOUND -- the cores are already busy doing useful work.",
        [
            "more workers only add context switches and cache pressure",
            (
                "if cases block rather than compute, this ceiling sits above the core "
                "count on purpose -- that headroom is the blocked time"
            ),
            "the win is in per-case cost now: algorithm, memory traffic, vectorisation",
            "check the roofline before assuming compute rather than memory bandwidth",
        ],
    ),
}


def _fmt(value: float, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "inf"
    return f"{value:.{digits}f}"


def render(machine: dict, result: dict, summary: dict, task_name: str) -> str:
    out: list[str] = []
    add = out.append

    add(BAR)
    add(f" conctune report -- task '{task_name}' on {machine.get('hostname', '?')}")
    add(BAR)

    # ---- machine ---------------------------------------------------------
    add("")
    add("MACHINE")
    add(f"  {machine.get('model', 'unknown')}")
    add(
        f"  {machine.get('cores_physical')} physical / "
        f"{machine.get('cores_logical')} logical cores, "
        f"{machine.get('mem_total', 0) / (1 << 30):.1f} GiB RAM"
    )
    governors = "/".join(machine.get("governors") or ["?"])
    turbo = {True: "on", False: "off", None: "unknown"}[machine.get("turbo")]
    add(f"  governor {governors}, turbo {turbo}")
    for warning in machine.get("warnings", []):
        add(f"  ! {warning}")

    # ---- workload --------------------------------------------------------
    add("")
    add("WORKLOAD")
    add(f"  work  T1   = {_fmt(result['work'], 2)}s  ({result['work_source']})")
    if result.get("work_overstated"):
        add(
            "  ! the parallel run burned less CPU than these service times imply, "
            "so they are overstated."
        )
        add(
            "    Each case was timed in its own process; add [cases].startup to the "
            "task spec so that per-invocation boot cost is netted out."
        )
    if math.isfinite(result.get("span", float("nan"))):
        span = result["span"]
        add(
            f"  span  Tinf = {_fmt(span, 2)}s  "
            f"({span / result['work'] * 100:.1f}% of all work in one case)"
        )
        add(f"  average parallelism T1/Tinf = {_fmt(result['work'] / span)}")
    else:
        add("  span  Tinf = not measured (no [cases] section in the task spec)")

    blocking = result.get("blocking_factor")
    if blocking and blocking > 1.05:
        add(
            f"  cases block rather than compute: {blocking:.1f}x more wall time than "
            "CPU time, so"
        )
        add(
            "  oversubscribing the cores pays until the waiting, not the cores, "
            "runs out"
        )

    # ---- measurements ----------------------------------------------------
    add("")
    add("MEASURED")
    add(
        f"  {'N':>4}  {'wall (s)':>10}  {'speedup':>8}  {'efficiency':>10}  "
        f"{'spread':>7}  {'failed':>7}"
    )
    add("  " + RULE[:58])
    for n in result["points"]:
        entry = summary[n]
        speedup = result["speedup"][n]
        failed = entry.get("failed", 0)
        flag = f"{failed}/{entry.get('reps_total', entry['reps'])}" if failed else "-"
        add(
            f"  {n:>4}  {entry['wall_median']:>10.3f}  {speedup:>8.2f}  "
            f"{speedup / n * 100:>9.0f}%  {entry['spread'] * 100:>6.1f}%  {flag:>7}"
        )
    for n, entry in sorted(result.get("unreliable", {}).items()):
        if entry.get("wall_median") is None:
            add(
                f"  {n:>4}  {'--':>10}  {'--':>8}  {'--':>10}  {'--':>7}  "
                f"{entry['failed']}/{entry['reps_total']:<3} every run failed"
            )

    noisy = [n for n in result["points"] if summary[n]["spread"] > 0.15]
    if noisy:
        add(
            f"  ! repeat spread over 15% at N={noisy} -- the machine was not quiet; "
            "treat the fit as provisional"
        )

    # ---- fit -------------------------------------------------------------
    fit = result["fit"]
    add("")
    add("UNIVERSAL SCALABILITY LAW")
    if not fit.get("ok"):
        add(f"  not fitted: {fit.get('reason')}")
    else:
        add(
            f"  sigma (contention) = {fit['sigma']:.4f}   -> Amdahl ceiling "
            f"{_fmt(fit['amdahl_ceiling'], 1)}x"
        )
        add(f"  kappa (coherency)  = {fit['kappa']:.5f}")
        add(f"  N*                 = {_fmt(fit['n_star'])}")
        add(f"  peak speedup       = {_fmt(fit['peak_speedup'], 2)}x")
        add(f"  R^2                = {fit['r2']:.4f}")
        if fit["r2"] < 0.90:
            add(
                "  ! R^2 below 0.90 -- the USL does not describe this workload. "
                "Usually load imbalance that varies with N, or thermal drift. "
                "Fix scheduling and re-run before trusting N*."
            )
        if fit["kappa"] < 1e-6:
            add(
                "  note: kappa ~ 0, no retrograde term. Scaling is Amdahl-shaped; "
                "add worker counts well past the core count if you need to pin it down."
            )

    # ---- ceilings --------------------------------------------------------
    add("")
    add("CEILINGS ON USEFUL CONCURRENCY")
    if not result["ceilings"]:
        add("  none computable")
    for name, entry in sorted(result["ceilings"].items(), key=lambda kv: kv[1]["n"]):
        mark = "<== BINDING" if name == result["binding"] else ""
        add(f"  {name:<13} N <= {_fmt(entry['n']):>6}   {entry['label']}  {mark}")
        add(f"                {entry['detail']}")

    # ---- scheduling ------------------------------------------------------
    imbalance = result.get("imbalance")
    if imbalance:
        add("")
        add("LOAD BALANCE  (observed vs a greedy LPT schedule of the same cases,")
        add("               with one worker boot added back)")
        add(f"  {'N':>4}  {'observed':>9}  {'LPT+boot':>9}  {'bound':>9}  {'ratio':>8}")
        add("  " + RULE[:48])
        for n, row in sorted(imbalance["by_n"].items()):
            if n == 1:
                continue
            mark = "" if row.get("comparable", True) else "   (saturated)"
            add(
                f"  {n:>4}  {row['observed']:>9.2f}  {row['achievable']:>9.2f}  "
                f"{row['lower_bound']:>9.2f}  {row['overhead_vs_lpt']:>8.2f}{mark}"
            )
        if imbalance.get("rows_dropped"):
            add(
                f"  rows past N={imbalance['compared_up_to']:.0f} are marked saturated: "
                "the cores are full there, so a case no longer runs at the"
            )
            add(
                "  service time measured without contention and the comparison "
                "says nothing about scheduling."
            )
        if imbalance.get("bound_violated"):
            add("")
            add(
                "  ! observed makespan is below the lower bound for these cases, "
                "which is impossible -- the service times are overstated."
            )
            add(
                "    Add [cases].startup to the task spec (or ignore this table); "
                "scheduling cannot be judged from it as it stands."
            )
        elif imbalance["scheduling_suspect"]:
            add("")
            add(
                f"  ! the real run trails a greedy schedule by up to "
                f"{imbalance['worst_overhead_vs_lpt']:.2f}x."
            )
            add(
                "    Fix scheduling BEFORE tuning the worker count: a pull queue "
                "(workers take the next case when free) is a few lines and drives"
            )
            add(
                "    imbalance to ~1. Sorting cases longest-first on that queue (LPT) "
                "tightens the tail further."
            )
        else:
            add("")
            add(
                "  balance is fine (within 25% of a greedy schedule) -- N is worth tuning."
            )

    # ---- verdict ---------------------------------------------------------
    add("")
    add(BAR)
    recommended = result["recommended_n"]
    add(f" RECOMMENDED WORKER COUNT: {recommended['n']}")
    add(f"   {recommended['reason']}")
    add(BAR)
    add(
        f"   fastest measured : N={recommended['best_measured_n']} at "
        f"{recommended['best_measured_wall']:.3f}s "
        f"({result['baseline'] / recommended['best_measured_wall']:.2f}x over serial)"
    )
    add(f"   knee of the curve: N={recommended['knee']}")
    if result["binding"]:
        ceiling = result["ceilings"][result["binding"]]
        add(
            f"   binding constraint: {result['binding']} "
            f"(estimates N <= {_fmt(ceiling['n'])})"
        )
    for note in recommended.get("notes", []):
        add(f"   ! {note}")

    suspect = bool((result.get("imbalance") or {}).get("scheduling_suspect"))
    if suspect:
        add(
            "   ! load balance is the first problem, not the worker count -- the "
            "number above"
        )
        add(
            "     will move once scheduling is fixed. Re-run this after switching "
            "to a pull queue."
        )

    add("")
    if suspect:
        add(
            "SCHEDULING-BOUND -- workers sit idle while others finish, so no worker "
            "count is being used properly."
        )
        for bullet in [
            (
                "give workers a shared queue they pull from when free, rather than a "
                "fixed slice assigned up front"
            ),
            (
                "sort cases longest-first onto that queue (LPT); Graham's bound puts "
                "the result within 4/3 of optimal"
            ),
            "only then tune N -- and re-measure, because the curve changes shape",
        ]:
            add(f"  - {bullet}")
        if result["binding"] in ADVICE:
            add("")
            add(f"After that, the next constraint is likely {result['binding']}:")
            add(f"  {ADVICE[result['binding']][0]}")
    elif result["binding"] in ADVICE:
        headline, bullets = ADVICE[result["binding"]]
        add(headline)
        for bullet in bullets:
            add(f"  - {bullet}")

    add("")
    return "\n".join(out)


def render_short(machine: dict, result: dict) -> str:
    """render_short gives the verdict as one line plus one line per note, for agents."""
    r2 = result["fit"].get("r2")
    head = [
        f"N={result['recommended_n']['n']}",
        f"binding={result['binding'] or 'none'}",
        f"r2={r2:.2f}" if r2 is not None and math.isfinite(r2) else "r2=n/a",
    ]
    if (result.get("imbalance") or {}).get("scheduling_suspect"):
        head.append("scheduling_suspect")
    if result.get("unreliable"):
        head.append(
            "unreliable=" + ",".join(str(n) for n in sorted(result["unreliable"]))
        )
    if result.get("work_overstated"):
        head.append("work_overstated")
    lines = [" ".join(head)]
    lines += [f"note: {note}" for note in result["recommended_n"]["notes"]]
    lines += [f"warn: {warning}" for warning in machine.get("warnings", [])]
    return "\n".join(lines)


def render_json(machine: dict, result: dict, summary: dict, task_name: str) -> str:
    payload = {
        "task": task_name,
        "machine": machine,
        "summary": {str(k): v for k, v in summary.items()},
        "analysis": _jsonable(result),
    }
    return json.dumps(payload, indent=2, default=str)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj
