"""Model checks: recover known parameters from synthetic data."""

from __future__ import annotations

import math

import numpy as np
import pytest

from conctune import machine, model


@pytest.mark.parametrize(
    ("sigma", "kappa"), [(0.05, 0.005), (0.10, 0.02), (0.02, 0.0005)]
)
def test_usl_recovers_parameters(sigma, kappa):
    points = [1, 2, 3, 4, 5, 6, 8, 10, 12, 16, 24, 32]
    fit = model.fit_usl(points, list(model.usl(points, sigma, kappa)))
    assert fit["sigma"] == pytest.approx(sigma, abs=1e-6)
    assert fit["kappa"] == pytest.approx(kappa, abs=1e-6)
    assert fit["n_star"] == pytest.approx(math.sqrt((1 - sigma) / kappa), abs=1e-3)


def test_usl_survives_noise():
    rng = np.random.default_rng(7)
    points = [1, 2, 3, 4, 6, 8, 12, 16, 24]
    sigma, kappa = 0.06, 0.008
    truth = math.sqrt((1 - sigma) / kappa)
    errors = []
    for _ in range(30):
        noisy = model.usl(points, sigma, kappa) * (1 + rng.normal(0, 0.02, len(points)))
        fit = model.fit_usl(points, list(noisy))
        errors.append(abs(fit["n_star"] - truth) / truth)
    assert max(errors) < 0.15


def test_amdahl_limit():
    points = [1, 2, 4, 8, 16, 32]
    fit = model.fit_usl(points, list(model.usl(points, 0.04, 0.0)))
    assert math.isinf(fit["n_star"])
    assert fit["amdahl_ceiling"] == pytest.approx(25.0, abs=0.5)


def test_lpt_beats_roundrobin_and_respects_span():
    times = [10.0] + [1.0] * 12
    lpt = model.lpt_makespan(times, 4)
    assert lpt >= max(times)
    assert lpt <= model.roundrobin_makespan(times, 4)
    assert lpt <= (4 / 3) * max(sum(times) / 4, *times) + 1e-9


def test_span_ceiling_binds_on_a_fat_case():
    times = {f"c{i}": 0.1 for i in range(20)}
    times["fat"] = 5.0
    work = sum(times.values())
    summary = {
        n: {
            "n": n,
            "reps": 3,
            "wall_median": max(work / n, 5.0) + 0.05 * n,
            "wall_min": 0.0,
            "wall_max": 0.0,
            "spread": 0.01,
            "cpu_median": work,
        }
        for n in [1, 2, 4, 8, 16]
    }
    profile = {"timings": times, "work": work, "span": 5.0, "startup": None}
    result = model.analyse(
        {"cores_physical": 8, "cores_logical": 16, "hostname": "t"}, summary, profile
    )
    assert result["binding"] == "parallelism"


def test_recommendation_prefers_measurement():
    """A measured optimum must not be overridden by a lower model ceiling."""
    summary = {
        n: {
            "n": n,
            "reps": 3,
            "wall_median": w,
            "wall_min": w,
            "wall_max": w,
            "spread": 0.0,
            "cpu_median": 4.0,
        }
        for n, w in {1: 10.0, 2: 5.2, 4: 2.9, 6: 2.4, 8: 2.41}.items()
    }
    result = model.analyse(
        {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}, summary, None
    )
    assert result["recommended_n"]["n"] == 6


def test_suggested_points_bracket_the_cores():
    points = machine.suggested_points({"cores_physical": 4, "cores_logical": 8})
    assert 1 in points
    assert 4 in points
    assert max(points) >= 16


def test_imbalance_checked_on_a_busy_machine():
    """A low hardware ceiling must not hide a bad schedule at N <= cores."""
    times = {f"c{i:02d}": 0.1 for i in range(24)}
    times["c00"], times["c01"] = 0.8, 0.4
    work = sum(times.values())
    # Static slices strand workers behind the fat case: ~1.7x a greedy schedule.
    walls = {1: work + 0.2, 2: 2.6, 4: 2.0}
    summary = {
        n: {
            "n": n,
            "reps": 1,
            "wall_median": w,
            "wall_min": w,
            "wall_max": w,
            "spread": 0.0,
            "cpu_median": 2.5 * work,  # other load inflates CPU
        }
        for n, w in walls.items()
    }
    profile = {
        "timings": times,
        "work": work,
        "span": 0.8,
        "startup": {"median": 0.2, "measured": True},
    }
    result = model.analyse(
        {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}, summary, profile
    )
    assert result["ceilings"]["hardware"]["n"] < 2
    assert result["imbalance"]["scheduling_suspect"]
