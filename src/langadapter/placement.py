"""Placement of timing runs: waiting for the process's other benchmarks, and
the one launcher of a timing process on this machine. The caller decides and
nothing reads a configuration. Serializing covers one benchmark's warm-up and
measured repetitions within one process (separate processes need external
coordination). The caller picks the processor set (``taskset -c``).
"""
from __future__ import annotations

import subprocess
import threading
from contextlib import contextmanager
from typing import Dict, Iterator, Optional, Sequence

from langadapter import _process_tree

# One lock for the whole process.
_lock = threading.Lock()


@contextmanager
def serialized(enabled: bool) -> Iterator[None]:
    """Hold the process-wide benchmark lock for the block when ``enabled``."""
    if not enabled:
        yield
        return
    with _lock:
        yield


def run_timed(
    command: Sequence[str],
    *,
    env: Optional[Dict[str, str]] = None,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess:
    """Run a timing process on this machine, its output captured as text. A
    timeout or interruption ends its process tree, and ``TimeoutExpired``
    propagates."""
    return _process_tree.run(
        list(command), text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env, cwd=cwd,
    )
