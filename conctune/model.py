"""Stage 4: fit the scalability model and pick a worker count.

Three independent ceilings on useful concurrency, plus a fitted Universal
Scalability Law curve.  The recommendation is the minimum of the ceilings;
which one binds determines what is worth optimising next.

    overhead-bound   N* = sqrt((1 - sigma) / kappa)   USL peak
    hardware-bound   N  = cores * work / cpu_total    no idle cores left
    parallelism-bound N = work / span                 average parallelism

The USL fit is linearised with Gunther's transform, so no scipy is needed:

    y = n/S(n) - 1 = sigma*(n-1) + kappa*n*(n-1)
                   = (sigma + kappa)*x + kappa*x^2     with x = n-1

which is an ordinary least-squares quadratic through the origin.
"""

from __future__ import annotations

import math

import numpy as np

# --------------------------------------------------------------------------
# Universal Scalability Law


def usl(n, sigma: float, kappa: float):
    n = np.asarray(n, dtype=float)
    return n / (1.0 + sigma * (n - 1.0) + kappa * n * (n - 1.0))


def fit_usl(points: list[int], speedup: list[float]) -> dict:
    n = np.asarray(points, dtype=float)
    s = np.asarray(speedup, dtype=float)

    mask = (n > 1) & (s > 0)
    if mask.sum() < 2:
        return {"ok": False, "reason": "need at least two points with N > 1"}

    x = n[mask] - 1.0
    y = n[mask] / s[mask] - 1.0

    # Least squares on [x, x^2] with no intercept: the model is exact at N=1.
    design = np.column_stack([x, x * x])
    coeffs, *_ = np.linalg.lstsq(design, y, rcond=None)
    a, b = float(coeffs[0]), float(coeffs[1])

    kappa = max(0.0, b)
    # Superlinear data earn a negative sigma [Gunther et al. 2015]; elsewhere it is noise.
    floor = -math.inf if bool((s[mask] > n[mask]).any()) else 0.0
    sigma = max(floor, a - kappa)
    if not _feasible(n, sigma, kappa):
        sigma = max(0.0, sigma)

    sigma, kappa = _refine(n, s, sigma, kappa, floor)

    fitted = usl(n, sigma, kappa)
    residual = s - fitted
    denominator = float(((s - s.mean()) ** 2).sum())
    r2 = 1.0 - float((residual**2).sum()) / denominator if denominator else float("nan")

    if kappa > 0 and sigma < 1:
        n_star = math.sqrt((1.0 - sigma) / kappa)
        peak = float(usl(n_star, sigma, kappa))
    else:
        n_star = float("inf")
        peak = float("inf") if sigma <= 0 else 1.0 / sigma

    return {
        "ok": True,
        "sigma": sigma,
        "kappa": kappa,
        "n_star": n_star,
        "peak_speedup": peak,
        "amdahl_ceiling": (1.0 / sigma) if sigma > 0 else float("inf"),
        "r2": r2,
        "fitted": {int(k): float(v) for k, v in zip(n, fitted)},
    }


def _feasible(n, sigma: float, kappa: float) -> bool:
    return bool((1.0 + sigma * (n - 1.0) + kappa * n * (n - 1.0) > 0).all())


def _refine(
    n, s, sigma: float, kappa: float, floor: float = 0.0, iterations: int = 60
) -> tuple[float, float]:
    """Gauss-Newton polish on the untransformed residual.

    The linearised fit weights large N too heavily because the transform
    divides by S.  A few Gauss-Newton steps on S itself fix that; the step is
    rejected whenever it fails to reduce the residual, so this can only help.
    """

    def residual(sig, kap):
        return s - usl(n, sig, kap)

    best = (sigma, kappa)
    best_cost = float((residual(sigma, kappa) ** 2).sum())

    for _ in range(iterations):
        sig, kap = best
        denominator = 1.0 + sig * (n - 1.0) + kap * n * (n - 1.0)
        common = -n / (denominator**2)
        jacobian = np.column_stack([common * (n - 1.0), common * n * (n - 1.0)])
        r = residual(sig, kap)
        try:
            step, *_ = np.linalg.lstsq(jacobian, -r, rcond=None)
        except np.linalg.LinAlgError:
            break

        candidate = (
            max(floor, min(1.0, sig + float(step[0]))),
            max(0.0, kap + float(step[1])),
        )
        if not _feasible(n, *candidate):
            break
        cost = float((residual(*candidate) ** 2).sum())
        if not math.isfinite(cost) or cost >= best_cost - 1e-15:
            break
        best, best_cost = candidate, cost

    return best


# --------------------------------------------------------------------------
# scheduling simulation


def lpt_makespan(service_times: list[float], workers: int) -> float:
    """Longest-processing-time-first greedy makespan.

    Graham's bound puts this within 4/3 of optimal, so it stands in for the
    best a real scheduler could do with these cases.
    """
    if workers <= 0:
        return float("inf")
    loads = [0.0] * workers
    for duration in sorted(service_times, reverse=True):
        index = min(range(workers), key=loads.__getitem__)
        loads[index] += duration
    return max(loads)


def roundrobin_makespan(service_times: list[float], workers: int) -> float:
    """Static round-robin partitioning, the usual naive split."""
    if workers <= 0:
        return float("inf")
    loads = [0.0] * workers
    for index, duration in enumerate(service_times):
        loads[index % workers] += duration
    return max(loads)


def schedule_analysis(service_times: list[float], points: list[int]) -> dict:
    work = sum(service_times)
    span = max(service_times) if service_times else 0.0

    table = {}
    for n in points:
        ideal = work / n
        lower_bound = max(ideal, span)
        lpt = lpt_makespan(service_times, n)
        table[n] = {
            "ideal_split": ideal,
            "lower_bound": lower_bound,
            "lpt": lpt,
            "roundrobin": roundrobin_makespan(service_times, n),
            "lpt_imbalance": lpt / ideal if ideal else float("nan"),
        }
    return {"work": work, "span": span, "by_n": table}


# --------------------------------------------------------------------------
# ceilings and recommendation


def analyse(
    machine: dict,
    summary: dict[int, dict],
    task_profile: dict | None = None,
    carriers: int | None = None,
) -> dict:
    timed = {n: e for n, e in summary.items() if e.get("wall_median")}
    points = sorted(timed)
    if not points or 1 not in timed:
        raise ValueError("sweep must include a successful N=1 baseline")

    # Worker counts the task did not survive. Faster is irrelevant if the run
    # does not finish, so these are excluded from the recommendation and
    # reported separately.
    unreliable = {n: e for n, e in summary.items() if e.get("fail_rate", 0.0) > 0.0}

    baseline = timed[1]["wall_median"]
    speedup = [baseline / timed[n]["wall_median"] for n in points]

    fit = fit_usl(points, speedup)

    cores_physical = machine.get("cores_physical", 1)
    # An event loop or M:N runtime runs its tasks on fewer kernel threads than
    # the machine has cores; those threads are the cores it can fill.
    cores = carriers or cores_physical

    # --- work and span -----------------------------------------------------
    if task_profile and task_profile.get("timings"):
        service_times = list(task_profile["timings"].values())
        work = task_profile["work"]
        span = task_profile["span"]
        work_source = "measured per-case service times"
    else:
        service_times = []
        work = baseline
        span = float("nan")
        work_source = "N=1 wall time (no per-case data)"

    ceilings: dict[str, dict] = {}

    startup = (task_profile or {}).get("startup") or {}
    boot = startup.get("median", 0.0)
    if boot > 0 and work > 0:
        # Closed form for T(N) = work/N + N*B, differentiated and solved.
        ceilings["startup"] = {
            "n": math.sqrt(work / boot),
            "label": "sqrt(work / B), measured boot cost",
            "detail": f"worker boot B={boot:.3f}s against {work:.1f}s of work",
        }

    if fit.get("ok") and math.isfinite(fit["n_star"]):
        ceilings["overhead"] = {
            "n": fit["n_star"],
            "label": "USL peak, sqrt((1-sigma)/kappa)",
            "detail": (
                f"coherency kappa={fit['kappa']:.5f}, contention sigma={fit['sigma']:.4f}"
            ),
        }

    # --- hardware ceiling from the runs' CPU accounting --------------------
    probe_n = min(points, key=lambda n: abs(n - cores))
    cpu_total = summary[probe_n]["cpu_median"]
    # A ratio above 1 is not an error: it means a second of work costs less
    # than a second of CPU, because cases spend part of their time blocked
    # (sleeping, waiting on I/O, on a language server, on a subprocess). That
    # is exactly the condition that makes oversubscribing the cores pay.
    blocking_factor = work / cpu_total if cpu_total > 0 and work > 0 else None
    if blocking_factor:
        core_seconds = cpu_total / work
        if blocking_factor > 1.05:
            detail = (
                f"at N={probe_n}: {cpu_total:.1f}s CPU for {work:.1f}s of work -- "
                f"a case occupies {core_seconds:.2f} of a core while it runs, so "
                f"about {blocking_factor:.1f} of them fit per core"
            )
        else:
            detail = (
                f"at N={probe_n}: {cpu_total:.1f}s CPU for {work:.1f}s of work "
                f"({min(blocking_factor, 1.0) * 100:.0f}% of burned cycles useful)"
            )
        source = summary[probe_n].get("cpu_source")
        if source in ("proc_stat", "system_times"):
            where = "/proc/stat" if source == "proc_stat" else "GetSystemTimes"
            detail += (
                f"; CPU from {where} counts every process on the machine, so "
                "other load lowers this ceiling"
            )
        elif source == "rusage":
            detail += (
                "; CPU from getrusage misses processes the run detaches, so "
                "this ceiling may read high"
            )
        ceilings["hardware"] = {
            "n": cores * blocking_factor,
            "label": f"{'carriers' if carriers else 'cores'} * work / cpu_total",
            "detail": detail,
        }

    # Service times can only be overstated in the mode that reconstructs them
    # one process per case, and only when the boot they each paid was never
    # measured.
    work_overstated = bool(
        task_profile
        and task_profile.get("source") != "log"
        and not (task_profile.get("startup") or {}).get("measured")
        and blocking_factor
        and blocking_factor > 1.05
    )

    # --- parallelism ceiling ------------------------------------------------
    if service_times and span > 0:
        ceilings["parallelism"] = {
            "n": work / span,
            "label": "work / span",
            "detail": (
                f"work={work:.1f}s, span={span:.1f}s "
                f"(longest single case is {span / work * 100:.1f}% of all work)"
            ),
        }

    binding = min(ceilings.items(), key=lambda kv: kv[1]["n"]) if ceilings else None

    # --- best measured, and the knee ---------------------------------------
    best_n = min(points, key=lambda n: summary[n]["wall_median"])
    knee = _knee(points, speedup)

    recommended = _recommend(timed, points, binding, knee, unreliable=unreliable)

    schedule = schedule_analysis(service_times, points) if service_times else None
    # Past saturation a case no longer runs at its contention-free service
    # time, so a simulation built from those times stops being a fair
    # comparison. Up to the core count every worker has its own core; only a
    # blocking workload fits more. Not the hardware ceiling itself: per-worker
    # boot and stray load push it under the core count, which would hide the
    # very imbalance this check exists to catch.
    valid_to = cores * max(1.0, blocking_factor or 1.0)
    # Tasks sharing a carrier wake in bursts and queue for it long before it
    # is saturated (a convoy): contention the LPT simulation cannot tell from
    # imbalance, so there is no row it could fairly judge.
    imbalance = (
        _imbalance(summary, schedule, boot, valid_to)
        if schedule and not carriers
        else None
    )

    return {
        "points": points,
        "carriers": carriers,
        "speedup": dict(zip(points, speedup)),
        "baseline": baseline,
        "work": work,
        "work_source": work_source,
        "span": span,
        "fit": fit,
        "ceilings": ceilings,
        "binding": binding[0] if binding else None,
        "best_measured_n": best_n,
        "best_measured_wall": summary[best_n]["wall_median"],
        "knee": knee,
        "recommended_n": recommended,
        "unreliable": {n: e for n, e in unreliable.items()},
        "schedule": schedule,
        "imbalance": imbalance,
        "work_overstated": work_overstated,
        "blocking_factor": blocking_factor,
    }


def _knee(points: list[int], speedup: list[float], threshold: float = 0.10) -> int:
    """Largest N whose marginal speedup per added worker still pays."""
    knee = points[0]
    for (n0, s0), (n1, s1) in zip(zip(points, speedup), zip(points[1:], speedup[1:])):
        gain = (s1 - s0) / max(1, n1 - n0)
        if gain < threshold:
            break
        knee = n1
    return knee


def _recommend(
    summary: dict[int, dict],
    points: list[int],
    binding,
    knee: int,
    tolerance: float = 0.03,
    unreliable: dict[int, dict] | None = None,
) -> dict:
    """Pick N from the measurements, not from the ceilings.

    The ceilings are model estimates and diagnose *what* limits scaling; the
    sweep is ground truth for *where* the run is fastest.  So: take the
    cheapest worker count that lands within `tolerance` of the best measured
    makespan -- no point paying for workers that buy under 3%.
    """
    unreliable = unreliable or {}
    clean = [n for n in points if n not in unreliable] or points

    best_n = min(clean, key=lambda n: summary[n]["wall_median"])
    best_wall = summary[best_n]["wall_median"]

    within = [
        n for n in clean if summary[n]["wall_median"] <= best_wall * (1 + tolerance)
    ]
    chosen = min(within)

    notes = []
    if unreliable:
        listed = ", ".join(
            f"N={n} ({e['failed']}/{e['reps_total']} runs)"
            for n, e in sorted(unreliable.items())
        )
        notes.append(f"the task failed at {listed} -- excluded from the choice")
        faster = [
            n
            for n, e in unreliable.items()
            if e.get("wall_median") and e["wall_median"] < best_wall
        ]
        if faster:
            notes.append(
                f"N={min(faster)} was faster still but did not finish reliably; "
                "a worker count the task cannot survive is not an optimum"
            )
    if best_n == max(points):
        notes.append(
            f"the fastest point is the largest swept (N={best_n}); the peak may lie "
            "beyond it -- extend --points and re-run"
        )
    if binding and binding[1]["n"] < chosen * 0.75:
        notes.append(
            f"the {binding[0]} ceiling estimates N<={binding[1]['n']:.1f}, below the "
            f"measured optimum of {chosen} -- the ceiling is a first-order estimate "
            "and the workers past it still win, just sub-linearly"
        )
    if chosen < best_n:
        saving = (summary[chosen]["wall_median"] / best_wall - 1) * 100
        notes.append(
            f"N={chosen} costs {saving:.1f}% more wall time than N={best_n} but "
            f"{best_n - chosen} fewer workers"
        )

    return {
        "n": chosen,
        "reason": f"cheapest N within {tolerance * 100:.0f}% of the best measured wall",
        "best_measured_n": best_n,
        "best_measured_wall": best_wall,
        "knee": knee,
        "notes": notes,
        "candidates": {
            "best measured": best_n,
            "knee of the curve": knee,
            **({f"{binding[0]} ceiling": binding[1]["n"]} if binding else {}),
        },
    }


def _imbalance(
    summary: dict[int, dict],
    schedule: dict,
    boot: float = 0.0,
    valid_to: float = float("inf"),
) -> dict:
    """Observed makespan against what a good scheduler would have achieved.

    The simulated schedule is pure work placement, so worker boot is added
    back before comparing: workers boot concurrently, costing one B of wall
    time. Without that, startup cost masquerades as load imbalance.
    """
    rows = {}
    for n, entry in schedule["by_n"].items():
        if n not in summary:
            continue
        observed = summary[n]["wall_median"]
        ideal = entry["ideal_split"]
        achievable = entry["lpt"] + boot
        rows[n] = {
            "observed": observed,
            "ideal_split": ideal,
            "lpt": entry["lpt"],
            "achievable": achievable,
            "lower_bound": entry["lower_bound"] + boot,
            "observed_imbalance": observed / ideal if ideal else float("nan"),
            "lpt_imbalance": entry["lpt_imbalance"],
            "overhead_vs_lpt": observed / achievable if achievable else float("nan"),
            # Beyond core saturation the simulated times are optimistic by an
            # unknown factor, so this row says nothing about scheduling.
            "comparable": n <= valid_to,
        }

    # Scheduling is the problem when the real run trails a greedy schedule of
    # the same cases by a wide margin at the worker counts that matter.
    interesting = [r for n, r in rows.items() if n > 1 and r["comparable"]]
    worst = max(
        (
            r["overhead_vs_lpt"]
            for r in interesting
            if math.isfinite(r["overhead_vs_lpt"])
        ),
        default=float("nan"),
    )

    # A real run cannot beat the lower bound for its own cases. When it does,
    # the service times are overstated (measurement overhead in s_i), so the
    # comparison says nothing about scheduling.
    impossible = any(r["observed"] < r["lower_bound"] * 0.95 for r in interesting)

    return {
        "by_n": rows,
        "worst_overhead_vs_lpt": worst,
        "compared_up_to": valid_to,
        "rows_dropped": sum(
            1 for n, r in rows.items() if n > 1 and not r["comparable"]
        ),
        "bound_violated": impossible,
        "scheduling_suspect": (
            not impossible and math.isfinite(worst) and worst > 1.25
        ),
    }
