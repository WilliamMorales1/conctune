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
import sys
import time
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


def _linux() -> dict:
    fields = _lscpu()
    mem = _meminfo()
    logical = os.cpu_count() or 1
    threads_per_core = int(fields.get("Thread(s) per core", 1) or 1)
    return {
        "model": fields.get("Model name", "unknown"),
        "cores_physical": max(1, logical // max(1, threads_per_core)),
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
    }


def _output(cmd: list[str]) -> str:
    # No check: sysctl exits nonzero over one unknown key but prints the rest.
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False).stdout
    except OSError:
        return ""


def _size(value: int) -> str:
    """_size renders bytes the way lscpu does, so reports read alike."""
    for unit, scale in (("MiB", 1 << 20), ("KiB", 1 << 10)):
        if value >= scale:
            return f"{value / scale:g} {unit}"
    return f"{value} B"


def _swap_bytes(text: str) -> int:
    # vm.swapusage: "total = 2048.00M  used = 1069.50M  free = 978.50M"
    match = re.search(r"total = ([\d.]+)([KMG])", text)
    if not match:
        return 0
    return int(
        float(match.group(1))
        * {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}[match.group(2)]
    )


def _darwin_available(vm_stat: str) -> int:
    page = re.search(r"page size of (\d+) bytes", vm_stat)
    if not page:
        return 0
    pages = 0
    # Inactive and speculative pages are reclaimable without swapping.
    for key in ("free", "inactive", "speculative"):
        match = re.search(rf"^Pages {key}:\s+(\d+)", vm_stat, re.MULTILINE)
        if match:
            pages += int(match.group(1))
    return pages * int(page.group(1))


def _darwin() -> dict:
    sysctl = {}
    keys = [
        "hw.logicalcpu",
        "hw.physicalcpu",
        "hw.packages",
        "hw.memsize",
        "hw.cpufrequency_max",
        "hw.cpufrequency_min",
        "hw.l1dcachesize",
        "hw.l1icachesize",
        "hw.l2cachesize",
        "hw.l3cachesize",
        "machdep.cpu.brand_string",
        "vm.swapusage",
    ]
    for line in _output(["sysctl", *keys]).splitlines():
        key, sep, value = line.partition(":")
        if sep:
            sysctl[key.strip()] = value.strip()

    def number(key: str) -> int:
        try:
            return int(sysctl.get(key, 0))
        except ValueError:
            return 0

    logical = number("hw.logicalcpu") or os.cpu_count() or 1
    physical = number("hw.physicalcpu") or logical
    cache = {
        label: _size(number(key))
        for label, key in (
            ("L1d", "hw.l1dcachesize"),
            ("L1i", "hw.l1icachesize"),
            ("L2", "hw.l2cachesize"),
            ("L3", "hw.l3cachesize"),
        )
        if number(key)
    }
    return {
        "model": sysctl.get("machdep.cpu.brand_string", "unknown"),
        "cores_physical": physical,
        "threads_per_core": max(1, logical // physical),
        "sockets": number("hw.packages") or 1,
        # Apple Silicon reports no frequency; Intel Macs report Hz.
        "mhz_max": number("hw.cpufrequency_max") / 1e6,
        "mhz_min": number("hw.cpufrequency_min") / 1e6,
        "cache": cache,
        "mem_total": number("hw.memsize"),
        "mem_available": _darwin_available(_output(["vm_stat"])),
        "swap_total": _swap_bytes(sysctl.get("vm.swapusage", "")),
    }


if sys.platform == "win32":
    import ctypes
    import winreg
    from ctypes import wintypes

    class _FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

        def seconds(self) -> float:
            return ((self.high << 32) | self.low) / 1e7

    def system_cpu_times() -> tuple[float, float] | None:
        """system_cpu_times returns (busy, idle) seconds summed over all CPUs."""
        idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
        if not ctypes.windll.kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        # Kernel time includes idle time.
        return kernel.seconds() + user.seconds() - idle.seconds(), idle.seconds()

    class _CacheDescriptor(ctypes.Structure):
        _fields_ = [
            ("level", wintypes.BYTE),
            ("associativity", wintypes.BYTE),
            ("line_size", wintypes.WORD),
            ("size", wintypes.DWORD),
            ("type", ctypes.c_int),
        ]

    class _InfoUnion(ctypes.Union):
        _fields_ = [("cache", _CacheDescriptor), ("reserved", ctypes.c_ulonglong * 2)]

    class _ProcessorInfo(ctypes.Structure):
        _fields_ = [
            ("mask", ctypes.c_size_t),
            ("relationship", ctypes.c_int),
            ("u", _InfoUnion),
        ]

    class _MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("load", wintypes.DWORD),
            ("total_phys", ctypes.c_ulonglong),
            ("avail_phys", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("avail_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("avail_virtual", ctypes.c_ulonglong),
            ("avail_extended_virtual", ctypes.c_ulonglong),
        ]

    def _processor_info() -> list[_ProcessorInfo]:
        get = ctypes.windll.kernel32.GetLogicalProcessorInformation
        needed = wintypes.DWORD(0)
        get(None, ctypes.byref(needed))
        count = needed.value // ctypes.sizeof(_ProcessorInfo)
        buffer = (_ProcessorInfo * count)()
        if not count or not get(buffer, ctypes.byref(needed)):
            return []
        return list(buffer)

    def _registry_cpu() -> tuple[str, float]:
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                name = str(winreg.QueryValueEx(key, "ProcessorNameString")[0])
                mhz = float(winreg.QueryValueEx(key, "~MHz")[0])
        except OSError:
            return "unknown", 0.0
        return name.strip(), mhz

    def _windows() -> dict:
        logical = os.cpu_count() or 1
        info = _processor_info()
        # Relationship values: 0 core, 1 NUMA node, 2 cache, 3 package.
        physical = sum(1 for i in info if i.relationship == 0) or logical
        caches: dict[str, list[int]] = {}
        for i in info:
            if i.relationship != 2:
                continue
            cache = i.u.cache
            # Cache type: 0 unified, 1 instruction, 2 data.
            label = f"L{cache.level}" + {1: "i", 2: "d"}.get(cache.type, "")
            caches.setdefault(label, []).append(cache.size)

        memory = _MemoryStatus()
        memory.length = ctypes.sizeof(_MemoryStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            memory = _MemoryStatus()

        model, mhz = _registry_cpu()
        return {
            "model": model,
            "cores_physical": physical,
            "threads_per_core": max(1, logical // physical),
            "sockets": sum(1 for i in info if i.relationship == 3) or 1,
            "numa_nodes": sum(1 for i in info if i.relationship == 1) or 1,
            "mhz_max": mhz,
            "cache": {
                label: f"{_size(sum(sizes))} ({len(sizes)} instances)"
                for label, sizes in sorted(caches.items())
            },
            "mem_total": memory.total_phys,
            "mem_available": memory.avail_phys,
            # The commit limit is RAM plus page file.
            "swap_total": max(0, memory.total_page_file - memory.total_phys),
        }


def _load(logical: int) -> float:
    if sys.platform != "win32":
        try:
            return os.getloadavg()[0]
        except OSError:
            return 0.0
    # No load average on Windows: sample busy CPUs over a short window and
    # express it in the same units (runnable CPUs).
    first = system_cpu_times()
    time.sleep(0.5)
    second = system_cpu_times()
    if first and second:
        busy = second[0] - first[0]
        total = busy + second[1] - first[1]
        if total > 0:
            return busy / total * logical
    return 0.0


def probe() -> dict:
    """Collect the host profile."""
    logical = os.cpu_count() or 1
    profile = {
        "hostname": platform.node(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "os": sys.platform,
        "model": platform.processor() or "unknown",
        "cores_physical": logical,
        "cores_logical": logical,
        "threads_per_core": 1,
        "sockets": 1,
        "numa_nodes": 1,
        "mhz_max": 0.0,
        "mhz_min": 0.0,
        "cache": {},
        # Frequency governors and turbo state are only readable on Linux.
        "governors": [],
        "turbo": None,
        "mem_total": 0,
        "mem_available": 0,
        "swap_total": 0,
    }
    if sys.platform.startswith("linux"):
        profile.update(_linux())
    elif sys.platform == "darwin":
        profile.update(_darwin())
    elif sys.platform == "win32":
        profile.update(_windows())
    profile["loadavg_1m"] = _load(logical)
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
        n = round(n * 1.5)
        points.add(min(n, 2 * logical))
    points.add(2 * logical)

    return sorted(p for p in points if p >= 1)


def load(path: str | os.PathLike) -> dict:
    with open(path, encoding="utf-8") as handle:
        profile = json.load(handle)
    profile.setdefault("suggested_points", suggested_points(profile))
    profile.setdefault("warnings", [])
    return profile


def save(profile: dict, path: str | os.PathLike) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(profile, handle, indent=2)
        handle.write("\n")
