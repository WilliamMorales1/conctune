#!/usr/bin/env python3
"""Self-checks: recover known parameters from synthetic data."""

import math
import sys

import numpy as np

from conctune import machine, model

failures = []


def check(name, condition, detail=""):
    print(f"{'PASS' if condition else 'FAIL'}  {name}  {detail}")
    if not condition:
        failures.append(name)


def test_usl_recovers_parameters():
    """A fit on noiseless USL data must return the parameters that made it."""
    points = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 24, 32]
    for sigma, kappa in [(0.05, 0.005), (0.10, 0.02), (0.02, 0.0005)]:
        speedup = list(model.usl(points, sigma, kappa))
        fit = model.fit_usl(points, speedup)
        truth = math.sqrt((1 - sigma) / kappa)
        check(
            f"usl exact sigma={sigma} kappa={kappa}",
            abs(fit["sigma"] - sigma) < 1e-6
            and abs(fit["kappa"] - kappa) < 1e-6
            and abs(fit["n_star"] - truth) < 1e-3,
            f"-> sigma={fit['sigma']:.5f} kappa={fit['kappa']:.6f} N*={fit['n_star']:.2f}",
        )


def test_usl_survives_noise():
    """With 2% noise the fitted N* should stay within 15% of the truth."""
    rng = np.random.default_rng(7)
    points = [1, 2, 3, 4, 6, 8, 12, 16, 24]
    sigma, kappa = 0.06, 0.008
    truth = math.sqrt((1 - sigma) / kappa)
    errors = []
    for _ in range(30):
        clean = model.usl(points, sigma, kappa)
        noisy = clean * (1 + rng.normal(0, 0.02, len(points)))
        fit = model.fit_usl(points, list(noisy))
        errors.append(abs(fit["n_star"] - truth) / truth)
    worst = max(errors)
    check("usl under 2% noise", worst < 0.15, f"worst N* error {worst * 100:.1f}%")


def test_amdahl_limit():
    """kappa = 0 must give no retrograde point and an Amdahl ceiling of 1/sigma."""
    points = [1, 2, 4, 8, 16, 32]
    fit = model.fit_usl(points, list(model.usl(points, 0.04, 0.0)))
    check(
        "kappa=0 -> infinite N*, ceiling 1/sigma",
        math.isinf(fit["n_star"]) and abs(fit["amdahl_ceiling"] - 25.0) < 0.5,
        f"ceiling={fit['amdahl_ceiling']:.2f}",
    )


def test_lpt_beats_roundrobin_and_respects_span():
    """LPT must never go below the span, and must beat round-robin here."""
    times = [10.0] + [1.0] * 12
    lpt = model.lpt_makespan(times, 4)
    rr = model.roundrobin_makespan(times, 4)
    check("lpt >= span", lpt >= max(times), f"lpt={lpt} span={max(times)}")
    check("lpt <= roundrobin", lpt <= rr, f"lpt={lpt} rr={rr}")
    check(
        "lpt within Graham's 4/3 bound",
        lpt <= (4 / 3) * max(sum(times) / 4, max(times)) + 1e-9,
        f"lpt={lpt}",
    )


def test_span_ceiling_binds_on_a_fat_case():
    """One dominant case must make parallelism the binding ceiling."""
    times = {f"c{i}": 0.1 for i in range(20)}
    times["fat"] = 5.0
    work = sum(times.values())
    summary = {
        n: {"n": n, "reps": 3, "wall_median": max(work / n, 5.0) + 0.05 * n,
            "wall_min": 0.0, "wall_max": 0.0, "spread": 0.01, "cpu_median": work}
        for n in [1, 2, 4, 8, 16]
    }
    profile = {"timings": times, "work": work, "span": 5.0, "startup": None}
    result = model.analyse(
        {"cores_physical": 8, "cores_logical": 16, "hostname": "t"}, summary, profile
    )
    check(
        "fat case -> parallelism-bound",
        result["binding"] == "parallelism",
        f"binding={result['binding']}, ceilings="
        + ", ".join(f"{k}={v['n']:.1f}" for k, v in result["ceilings"].items()),
    )


def test_recommendation_prefers_measurement():
    """A measured optimum must not be overridden by a lower model ceiling."""
    summary = {
        n: {"n": n, "reps": 3, "wall_median": w, "wall_min": w, "wall_max": w,
            "spread": 0.0, "cpu_median": 4.0}
        for n, w in {1: 10.0, 2: 5.2, 4: 2.9, 6: 2.4, 8: 2.41}.items()
    }
    result = model.analyse(
        {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}, summary, None
    )
    check(
        "recommends the measured optimum",
        result["recommended_n"]["n"] == 6,
        f"recommended={result['recommended_n']['n']}",
    )


def test_suggested_points_bracket_the_cores():
    profile = {"cores_physical": 4, "cores_logical": 8}
    points = machine.suggested_points(profile)
    check(
        "sweep points bracket the core count",
        1 in points and 4 in points and max(points) >= 16,
        f"{points}",
    )


for test in [
    test_usl_recovers_parameters,
    test_usl_survives_noise,
    test_amdahl_limit,
    test_lpt_beats_roundrobin_and_respects_span,
    test_span_ceiling_binds_on_a_fat_case,
    test_recommendation_prefers_measurement,
    test_suggested_points_bracket_the_cores,
]:
    test()

print()
print(f"{len(failures)} failure(s)" if failures else "all checks passed")
sys.exit(1 if failures else 0)
