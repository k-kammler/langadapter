"""Read the processors a process may run on, and the machine's memory and
load for a progress log.

This module uses only the standard library and can run in a project interpreter.
"""

import json
import os
import re


def allowed_cpus():
    """The processors this process may run on, sorted: its affinity, the set
    the operating system allows it, not the processors it ran on. None where
    the platform reports no affinity."""
    getaffinity = getattr(os, "sched_getaffinity", None)
    if getaffinity is None:
        return None
    return sorted(getaffinity(os.getpid()))


def _probed_allowed_cpus(done):
    """The processors the ``allowed`` probe's finished run ``done`` names,
    or None when it failed or named none a process can run on."""
    lines = (done.stdout or "").strip().splitlines()
    if done.returncode != 0 or not lines:
        return None
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return None
    if (not isinstance(value, list) or not value
            or any(type(cpu) is not int or cpu < 0 for cpu in value)):
        return None
    return tuple(value)


def _status_allowed_cpus(done):
    """The processors the ``Cpus_allowed_list`` line of a ``/proc/self/status``
    text names, from the finished run ``done`` that wrote it, or None when
    the run failed, the line is missing or repeated, or its value is not a
    list of ascending ranges of distinct processors."""
    rows = [line.split(":", 1)[1].strip() for line in (done.stdout or "").splitlines()
            if line.startswith("Cpus_allowed_list:")]
    if done.returncode != 0 or len(rows) != 1:
        return None
    value = rows[0]
    if re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value) is None:
        return None
    cpus = []
    for part in value.split(","):
        limits = [int(cpu) for cpu in part.split("-")]
        start, end = limits[0], limits[-1]
        if end < start or end - start > 1_000_000:
            return None
        cpus.extend(range(start, end + 1))
    if len(set(cpus)) != len(cpus):
        return None
    return tuple(cpus)


def hardware_usage() -> str:
    """One line of system memory use and CPU load for a progress log, or ''
    when neither can be read.

    The Python mutation engine logs it while its workers run, so the log
    shows the memory use and load under the chosen worker count. It reads
    ``/proc/meminfo`` and ``os.getloadavg``, and returns '' on platforms
    without them, such as Windows."""
    parts = []
    try:
        info = {}
        with open("/proc/meminfo", encoding="ascii") as fh:
            for line in fh:
                key, _, value = line.partition(":")
                info[key.strip()] = value.strip()
        total_kb = int(info["MemTotal"].split()[0])
        avail_kb = int(info["MemAvailable"].split()[0])
        total_gb = total_kb / 1048576.0
        avail_gb = avail_kb / 1048576.0
        used_gb = total_gb - avail_gb
        pct = 100.0 * (1.0 - avail_kb / total_kb) if total_kb else 0.0
        parts.append(
            f"RAM {used_gb:.1f}/{total_gb:.1f} GB used ({pct:.0f}%, "
            f"{avail_gb:.1f} GB free)"
        )
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    try:
        load1 = os.getloadavg()[0]
        parts.append(f"CPU load {load1:.1f} / {os.cpu_count() or '?'} cores")
    except (OSError, AttributeError):
        pass
    return " | ".join(parts)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] == ["allowed"]:
        print(json.dumps(allowed_cpus()))
