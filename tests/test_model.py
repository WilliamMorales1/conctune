"""Model checks: recover known parameters from synthetic data."""

from __future__ import annotations

import math

import numpy as np
import pytest

from conctune import machine, model, report


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


def test_superlinear_fit_keeps_negative_sigma():
    """Gunther et al. 2015 fit Hadoop with sigma < 0; clamping it would bias kappa."""
    points = [1, 2, 4, 8, 16, 32, 48, 64, 96, 128, 150]
    sigma, kappa = -0.0288, 0.000447
    fit = model.fit_usl(points, list(model.usl(points, sigma, kappa)))
    assert fit["sigma"] == pytest.approx(sigma, abs=1e-6)
    assert fit["kappa"] == pytest.approx(kappa, abs=1e-7)
    assert fit["n_star"] == pytest.approx(math.sqrt((1 - sigma) / kappa), abs=1e-2)
    assert math.isinf(fit["amdahl_ceiling"])


def test_sublinear_fit_never_goes_negative():
    rng = np.random.default_rng(3)
    points = [1, 2, 4, 8]
    for _ in range(30):
        noisy = model.usl(points, 0.0, 0.02) * (1 + rng.normal(0, 0.03, len(points)))
        noisy = np.minimum(noisy, points)
        assert model.fit_usl(points, list(noisy))["sigma"] >= 0


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
            "cpu_median": 5 * work,  # other load inflates CPU
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


def test_carriers_replace_cores_in_the_hardware_ceiling():
    """An event loop fills one carrier, however many cores the machine has."""
    times = {f"c{i:02d}": 0.2 for i in range(40)}
    work = sum(times.values())
    summary = {
        n: {
            "n": n,
            "reps": 1,
            "wall_median": max(work / n, 0.4),
            "wall_min": 0.0,
            "wall_max": 0.0,
            "spread": 0.0,
            "cpu_median": work / 20,  # cases wait 19/20 of the time
        }
        for n in [1, 2, 4, 8, 16, 32]
    }
    profile = {"timings": times, "work": work, "span": 0.2, "startup": None}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}

    default = model.analyse(host, summary, profile)
    loop = model.analyse(host, summary, profile, carriers=1)
    assert default["ceilings"]["hardware"]["n"] == pytest.approx(160)
    assert loop["ceilings"]["hardware"]["n"] == pytest.approx(20)
    assert loop["carriers"] == 1
    assert default["imbalance"] is not None
    assert loop["imbalance"] is None


def _summary(walls: dict[int, float], cpus: dict[int, float]) -> dict[int, dict]:
    return {
        n: {
            "n": n,
            "reps": 3,
            "wall_median": w,
            "wall_min": w,
            "wall_max": w,
            "spread": 0.0,
            "cpu_median": cpus[n],
        }
        for n, w in walls.items()
    }


def test_blocking_hardware_ceiling_reads_cpu_near_saturation():
    """Latency-bound work burns more CPU per run far below saturation."""
    # A Go fetcher on GOMAXPROCS=2 that sleeps 15ms per request, measured.
    walls = {1: 4.912, 16: 0.390, 32: 0.213, 64: 0.120, 96: 0.101, 128: 0.111}
    cpus = {1: 0.386, 16: 0.298, 32: 0.173, 64: 0.107, 96: 0.110, 128: 0.146}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    result = model.analyse(host, _summary(walls, cpus), None, carriers=2)
    # Read at N=1 the ceiling was 2 * 4.912 / 0.386 = 25, a quarter of the peak.
    assert result["ceilings"]["hardware"]["n"] > 64
    assert "at N=96" in result["ceilings"]["hardware"]["detail"]


def test_cpu_bound_hardware_ceiling_stays_at_the_cores():
    """SMT siblings that add nothing double the CPU billed, not the ceiling."""
    walls = {1: 8.0, 2: 4.48, 4: 2.57, 8: 2.44, 16: 2.65}
    cpus = {1: 7.94, 2: 8.83, 4: 9.90, 8: 18.35, 16: 18.2}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    result = model.analyse(host, _summary(walls, cpus), None)
    assert result["ceilings"]["hardware"]["n"] == pytest.approx(8 * 8.0 / 18.35)
    assert result["ceilings"]["hardware"]["n"] < 4


def test_hardware_ceiling_counts_what_smt_adds():
    """SMT worth 25% more throughput lifts the ceiling from 4 cores to 5."""
    walls = {1: 8.0, 2: 4.0, 4: 2.0, 8: 1.6}
    cpus = {1: 8.0, 2: 8.0, 4: 8.0, 8: 12.8}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    result = model.analyse(host, _summary(walls, cpus), None)
    assert result["ceilings"]["hardware"]["n"] == pytest.approx(5.0)


def _build_profile() -> dict:
    # A make -j build: 24 compile units plus a near-free link.
    times = {f"u{i:02d}": 0.31 for i in range(24)}
    times["main"] = 0.01
    work = sum(times.values())
    return {
        "timings": times,
        "work": work,
        "span": 0.31,
        "startup": {"median": 0.02, "measured": True},
    }


def test_clock_drop_under_load_is_not_imbalance():
    """make -j already pulls work; slower cases at N=4 are the clock, not idle workers."""
    profile = _build_profile()
    walls = {1: 7.6, 2: 4.2, 3: 2.95, 4: 2.5}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    # CPU grows with N by as much as the wall trails the greedy schedule.
    slowed = model.analyse(
        host, _summary(walls, {1: 7.5, 2: 8.3, 3: 8.7, 4: 9.6}), profile
    )
    assert not slowed["imbalance"]["scheduling_suspect"]
    # Same walls with flat CPU: the extra time was spent idle, so it is imbalance.
    flat = model.analyse(host, _summary(walls, dict.fromkeys(walls, 7.5)), profile)
    assert flat["imbalance"]["scheduling_suspect"]


def test_case_times_past_the_serial_run_are_overstated():
    """One process per short case pays costs the serial run never does."""
    times = {"fat": 0.66}
    times.update({f"c{i:02d}": 0.09 for i in range(40)})
    work = sum(times.values())  # 4.26s against a 2.74s serial run
    walls = {1: 2.74, 2: 1.58, 4: 0.97, 8: 1.18}
    cpus = {1: 2.80, 2: 3.04, 4: 3.51, 8: 5.74}
    profile = {
        "timings": times,
        "work": work,
        "span": 0.66,
        "startup": {"median": 0.137, "measured": True},
    }
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    result = model.analyse(host, _summary(walls, cpus), profile)
    assert result["work_overstated"]
    assert result["work_exceeds_serial"]
    # Inflated case times read as blocking (ratio 1.2) and lifted the ceiling past 8.
    assert result["ceilings"]["hardware"]["n"] == pytest.approx(8 * 2.74 / 5.74)


def test_ceiling_past_the_sweep_does_not_bind():
    """A still-rising curve whose ceilings all lie beyond it is not yet bound."""
    walls = {1: 33.5, 2: 17.4, 4: 9.4}
    cpus = {1: 1.43, 2: 1.41, 4: 1.39}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    result = model.analyse(host, _summary(walls, cpus), None, carriers=1)
    assert result["ceilings"]["hardware"]["n"] > 4
    assert result["binding"] is None
    assert result["nearest_ceiling"] == "hardware"
    text = report.render(host, result, _summary(walls, cpus), "t")
    assert "NOT YET BOUND" in text
    assert "HARDWARE-BOUND" not in text
    assert report.render_short(host, result).startswith("N=4 binding=none")


def test_ceiling_inside_the_sweep_still_binds():
    walls = {1: 8.0, 2: 4.48, 4: 2.57, 8: 2.44, 16: 2.65}
    cpus = {1: 7.94, 2: 8.83, 4: 9.90, 8: 18.35, 16: 18.2}
    host = {"cores_physical": 4, "cores_logical": 8, "hostname": "t"}
    result = model.analyse(host, _summary(walls, cpus), None)
    assert result["binding"] == result["nearest_ceiling"] == "hardware"
