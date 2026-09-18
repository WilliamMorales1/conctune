"""Stage 1: machine-specific specs.

Probes the host for everything that bounds parallel speedup, and emits a
JSON profile.  The profile is a plain dict so it can be written by hand or
copied from another box -- that is what makes the machine swappable.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
from pathlib import Path


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _lscpu() -> dict[str, str]:
    try:
        out = subprocess.run(
            ["lscpu"], capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return {}
    fields = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


def _meminfo() -> dict[str, int]:
    text = _read("/proc/meminfo") or ""
    mem = {}
    for line in text.splitlines():
        match = re.match(r"(\w+):\s+(\d+) kB", line)
        if match:
            mem[match.group(1)] = int(match.group(2)) * 1024
    return mem


def _cache_bytes(fields: dict[str, str]) -> dict[str, str]:
    return {
        key.split("(")[0].strip(): value
        for key, value in fields.items()
        if key.startswith(("L1d", "L1i", "L2", "L3"))
    }


def _turbo_enabled() -> bool | None:
    no_turbo = _read("/sys/devices/system/cpu/intel_pstate/no_turbo")
    if no_turbo is not None:
        return no_turbo == "0"
    boost = _read("/sys/devices/system/cpu/cpufreq/boost")
    if boost is not None:
        return boost == "1"
    return None


def _governors() -> list[str]:
    base = Path("/sys/devices/system/cpu")
    found = set()
    for path in sorted(base.glob("cpu[0-9]*/cpufreq/scaling_governor")):
        value = _read(str(path))
        if value:
            found.add(value)
    return sorted(found)


def probe() -> dict:
    """Collect the host profile."""
    fields = _lscpu()
    mem = _meminfo()

    logical = os.cpu_count() or 1
    threads_per_core = int(fields.get("Thread(s) per core", 1) or 1)
    physical = max(1, logical // max(1, threads_per_core))

    loadavg = os.getloadavg()

    profile = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "model": fields.get("Model name", "unknown"),
        "cores_physical": physical,
        "cores_logical": logical,
        "threads_per_core": threads_per_core,
        "sockets": int(fields.get("Socket(s)", 1) or 1),
        "numa_nodes": int(fields.get("NUMA node(s)", 1) or 1),
        "mhz_max": float(fields.get("CPU max MHz", 0) or 0),
        "mhz_min": float(fields.get("CPU min MHz", 0) or 0),
        "cache": _cache_bytes(fields),
        "governors": _governors(),
        "turbo": _turbo_enabled(),
        "mem_total": mem.get("MemTotal", 0),
        "mem_available": mem.get("MemAvailable", 0),
        "swap_total": mem.get("SwapTotal", 0),
        "loadavg_1m": loadavg[0],
    }
    profile["warnings"] = warnings(profile)
    profile["suggested_points"] = suggested_points(profile)
    return profile


def warnings(profile: dict) -> list[str]:
    """Conditions that make a sweep untrustworthy."""
    notes = []

    load = profile.get("loadavg_1m", 0.0)
    logical = profile.get("cores_logical", 1)
    if load > 0.3 * logical:
        notes.append(
            f"load average {load:.2f} on {logical} logical CPUs -- the machine is "
            "busy; sweep timings will be contaminated"
        )

    governors = profile.get("governors") or []
    if any(g in ("powersave", "conservative", "ondemand") for g in governors):
        notes.append(
            f"scaling governor {'/'.join(governors)} -- frequency ramps with load, "
            "so low worker counts run at lower clock and speedup looks inflated; "
            "consider 'performance' for the sweep"
        )

    if profile.get("turbo"):
        notes.append(
            "turbo boost is on -- single-worker runs boost higher than saturated "
            "runs, which understates speedup; randomised run order mitigates but "
            "does not remove this"
        )

    if profile.get("threads_per_core", 1) > 1:
        notes.append(
            f"SMT is on ({profile['cores_physical']} physical / "
            f"{profile['cores_logical']} logical) -- expect the speedup curve to "
            f"bend at N={profile['cores_physical']}, not N={profile['cores_logical']}"
        )

    available = profile.get("mem_available", 0)
    if available and available < 2 << 30:
        notes.append(
            f"only {available / (1 << 30):.1f} GiB RAM available -- high worker "
            "counts may swap, which shows up as fake coherency cost"
        )

    return notes


def suggested_points(profile: dict) -> list[int]:
    """Worker counts worth measuring.

    Dense below the physical core count (that is where the curve is steep),
    sparse above it out to 2x logical (that is where retrograde scaling, if
    any, becomes visible -- without those points kappa is unidentifiable).
    """
    physical = profile.get("cores_physical", 1)
    logical = profile.get("cores_logical", physical)

    # Dense at the low end, where the curve is steep and every worker still
    # buys most of a core -- but capped, because each extra point costs a full
    # set of repeats. Then geometric out to 2x logical: those points past
    # saturation are what make kappa identifiable at all.
    points = set(range(1, min(physical, 8) + 1))
    points.update({physical, physical + 1, logical})

    n = max(min(physical, 8), 2)
    while n < 2 * logical:
        n = int(round(n * 1.5))
        points.add(min(n, 2 * logical))
    points.add(2 * logical)

    return sorted(p for p in points if p >= 1)


def load(path: str | os.PathLike) -> dict:
    with open(path) as handle:
        profile = json.load(handle)
    profile.setdefault("suggested_points", suggested_points(profile))
    profile.setdefault("warnings", [])
    return profile


def save(profile: dict, path: str | os.PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as handle:
        json.dump(profile, handle, indent=2)
        handle.write("\n")
