from __future__ import annotations

import sys
from pathlib import Path

import pytest

from conctune import machine, task

SYSCTL = """\
hw.logicalcpu: 10
hw.physicalcpu: 10
hw.packages: 1
hw.memsize: 17179869184
hw.l1dcachesize: 65536
hw.l1icachesize: 131072
hw.l2cachesize: 4194304
machdep.cpu.brand_string: Apple M1 Pro
vm.swapusage: total = 2048.00M  used = 1069.50M  free = 978.50M  (encrypted)
"""

VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               10000.
Pages active:                            200000.
Pages inactive:                           20000.
Pages speculative:                         1000.
"""


def test_darwin_probe_parses_sysctl_and_vm_stat(monkeypatch):
    outputs = {"sysctl": SYSCTL, "vm_stat": VM_STAT}
    monkeypatch.setattr(machine, "_output", lambda cmd: outputs[cmd[0]])
    host = machine._darwin()
    assert host["model"] == "Apple M1 Pro"
    assert host["cores_physical"] == 10
    assert host["threads_per_core"] == 1
    assert host["mem_total"] == 16 << 30
    assert host["mem_available"] == 31000 * 16384
    assert host["swap_total"] == 2048 << 20
    assert host["cache"] == {"L1d": "64 KiB", "L1i": "128 KiB", "L2": "4 MiB"}
    assert host["mhz_max"] == 0


def test_darwin_probe_survives_missing_tools(monkeypatch):
    monkeypatch.setattr(machine, "_output", lambda cmd: "")
    host = machine._darwin()
    assert host["cores_physical"] >= 1
    assert host["mem_total"] == 0


def test_probe_fills_every_field_on_an_unknown_os(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd14")
    profile = machine.probe()
    assert profile["os"] == "freebsd14"
    assert profile["cores_physical"] == profile["cores_logical"]
    assert profile["governors"] == []
    assert profile["suggested_points"][0] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rusage path")
def test_rusage_counts_child_cpu(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert task.cpu_source() == "rusage"
    spin = "import time\nend = time.process_time() + 0.3\nwhile time.process_time() < end: pass"
    spec = task.TaskSpec(name="t", run_cmd="", cwd=Path.cwd(), shell=False)
    result = task.run_cmd(f"{{python}} -c {task.quote(spin)}", spec)
    assert result.returncode == 0
    assert 0.25 < result.cpu < 1.0


def test_quote_survives_the_shell(tmp_path):
    case = "it's a case & more"
    spec = task.TaskSpec(name="t", run_cmd="", cwd=tmp_path)
    cmd = '{python} -c "import sys; print(sys.argv[1])" ' + task.quote(case)
    assert task.run_cmd(cmd, spec).stdout.strip() == case


def test_probe_on_this_host():
    profile = machine.probe()
    assert profile["cores_logical"] >= profile["cores_physical"] >= 1
    assert profile["mem_total"] > 0
    assert profile["loadavg_1m"] >= 0
