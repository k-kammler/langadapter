"""The processors a process may run on, and the machine's memory and load."""
from __future__ import annotations

from langadapter import resources


def test_the_allowed_processors_are_the_affinity(monkeypatch):
    """The sorted processors a process may run on, or None where the
    platform reports no affinity."""
    monkeypatch.setattr(resources.os, "sched_getaffinity",
                        lambda pid: {3, 1}, raising=False)
    assert resources.allowed_cpus() == [1, 3]
    monkeypatch.delattr(resources.os, "sched_getaffinity", raising=False)
    assert resources.allowed_cpus() is None


def test_the_module_prints_the_allowed_processors_when_asked():
    """A benchmark reads the set by sending this module to the project
    interpreter with the argument ``allowed``."""
    import json
    import subprocess
    import sys
    from pathlib import Path

    completed = subprocess.run(
        [sys.executable, "-I", "-S", "-", "allowed"],
        input=Path(resources.__file__).read_text(encoding="utf-8"),
        capture_output=True, text=True, timeout=60, check=True)
    assert json.loads(completed.stdout) == resources.allowed_cpus()


def test_hardware_usage_returns_a_string_and_never_raises():
    # Linux gives RAM and CPU load, a host without /proc and getloadavg "".
    assert isinstance(resources.hardware_usage(), str)


def test_hardware_usage_formats_linux_meminfo(monkeypatch):
    import builtins
    import io
    fake_meminfo = (
        "MemTotal:       131072000 kB\n"   # 125.0 GB
        "MemFree:         10000000 kB\n"
        "MemAvailable:    65536000 kB\n"   # 62.5 GB free -> 62.5 used, 50%
    )
    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/proc/meminfo":
            return io.StringIO(fake_meminfo)
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", fake_open)
    # raising=False sets os.getloadavg on Windows too, where it is missing.
    monkeypatch.setattr(resources.os, "getloadavg", lambda: (12.5, 8.0, 4.0),
                        raising=False)
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 64, raising=False)
    out = resources.hardware_usage()
    assert "RAM 62.5/125.0 GB used (50%, 62.5 GB free)" in out
    assert "CPU load 12.5 / 64 cores" in out
