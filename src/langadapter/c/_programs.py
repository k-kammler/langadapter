"""A project's own test programs (``c_tests`` ``programs``), as its baseline.

Some projects test each module with a program of its own whose ``main``
runs a fixed list of test functions, as c-algorithms' ``test/test-*.c`` do
through its ``framework.c``. Such a program cannot run one of its tests by
name, so each program is one test, named ``main``, run in a process of its
own under its own timeout. Exit status 0 passes it. Another status, or the
end by SIGABRT that a failed ``assert`` causes, fails it. Another signal is
an error named by that signal, and a program past its timeout has timed
out. The suite's setup files (a framework's sources) are compiled with the
target's shared flags and linked into every program.
"""
from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from typing import Tuple

from langadapter.c._utest import TestRun, last_lines, outputs, program_ended
from langadapter.execution import _as_text

#: The one test of a program.
NAME = "main"


def list_tests(program: Path, run, *, cwd: str, env, timeout_s: float) -> Tuple[str, ...]:
    """The one test of ``program``, without running it."""
    return (NAME,)


def run_one(program: Path, name: str, run, *, cwd: str, env, timeout_s: float) -> TestRun:
    """Run ``program`` and judge it by its exit status."""
    try:
        done = run([str(program)], cwd=cwd, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired as expired:
        output = outputs(_as_text(expired.stdout), _as_text(expired.stderr))[1]
        return TestRun(name, "timed_out", None,
                       f"the program reached its timeout of {timeout_s:g} s", output)
    return verdict(name, done)


def verdict(name: str, done) -> TestRun:
    """The verdict on the finished run ``done`` of a program: exit status 0
    passes, another status or SIGABRT fails, another signal is an error."""
    judged, output = outputs(done.stdout, done.stderr)
    code = done.returncode
    if code == 0:
        return TestRun(name, "passed", None, "", output)
    message = last_lines(program_ended(code), judged)
    aborted = code < 0 and -code == signal.SIGABRT
    return TestRun(name, "failed" if code > 0 or aborted else "error", None, message, output)
