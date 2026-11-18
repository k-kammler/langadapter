"""How the C runners read and keep a test process's output, without GCC:
the kept streams, a timed-out test's output, and the joins the judges read
and the runs keep."""
from __future__ import annotations

from pathlib import Path

import pytest


def test_a_stream_is_kept_whole_up_to_its_bound_else_its_ends_and_utest_lines():
    from langadapter.c._utest import LINE_BYTES, OUTPUT_KEPT, KeptOutput

    def kept(data: bytes) -> bytes:
        stream = KeptOutput()
        for start in range(0, len(data), 65536):
            stream.write(data[start:start + 65536])
        return stream.getvalue()

    whole = "é within the bound\r\n".encode("utf-8") * (2 * OUTPUT_KEPT // 21)
    assert len(whole) <= 2 * OUTPUT_KEPT and kept(whole) == whole
    marked = (b"no line break\x1b[32m[       OK ]\x1b[0m a.glued (5ns)\n",
              b"\x1b[32m[       OK ] a.first (5ns)\x1b[0m\r\n", b"[ RUN      ] a.second\x0c")
    long = b"[ RUN      ] a.long" + b"x" * LINE_BYTES + b"\n"
    data = (b"start\n" * 300000 + marked[0] + b"noise [ OK ] not.utest\n" + marked[1] + long
            + b"middle\n" * 300000 + marked[2] + b"end\n" * 300000)
    out = kept(data)
    head, rest = out.split(b"[... ", 1)
    note, between = rest.split(b" ...]\n", 1)
    assert head == b"start\n" * (OUTPUT_KEPT // 6) and len(head) <= OUTPUT_KEPT
    assert between.startswith(b"".join(marked)) and between.endswith(b"end\n" * 1000)
    tail = between[len(b"".join(marked)):]
    assert len(tail) <= OUTPUT_KEPT
    assert note == b"%d bytes of the test output omitted" % (len(data) - len(head) - len(tail))


def test_output_without_utest_lines_is_kept_at_about_the_speed_it_arrives():
    """Beyond the bound only bytes that may hold a utest line are read line
    by line, so a test that writes fast is not slowed into its timeout:
    output without one is kept at a quarter or more of a plain append's
    rate, the best of five runs each, alternating, wide enough for a
    loaded machine."""
    import time

    from langadapter.c._utest import KeptOutput

    def seconds(write, data: bytes) -> float:
        began = time.perf_counter()
        for start in range(0, len(data), 65536):
            write(data[start:start + 65536])
        return time.perf_counter() - began

    for line in (b"                1,\n", b'    "values": [1, 2, 3],\n', b"x" * 65536):
        data = line * (32 * 1024 * 1024 // len(line))
        kept, appended = [], []
        for _ in range(5):
            kept.append(seconds(KeptOutput().write, data))
            appended.append(seconds(bytearray().extend, data))
        assert min(kept) < 4 * min(appended), (line[:24], min(kept), min(appended))


@pytest.mark.parametrize("stdout, stderr, expected", [
    (b"first\r\nsecond\rthird\n", b"err\r\n", "first\nsecond\nthird\nerr\n"),
    (b"partial", b"err\n", "partial\nerr\n")])
def test_a_timed_out_tests_output_has_its_line_breaks_as_a_finished_ones(tmp_path, stdout,
                                                                         stderr, expected):
    """Output of a test past its timeout is kept as a finished test's is:
    every carriage return becomes a line break, and stdout's last line ends
    before stderr."""
    import json
    import subprocess

    from langadapter.c import _ctest, _programs, _utest

    def timed_out(argv, **options):
        raise subprocess.TimeoutExpired(argv, 1, output=stdout, stderr=stderr)

    assert _utest.run_one(tmp_path, "a.b", timed_out, cwd=".", env={},
                          timeout_s=1).output == expected
    assert _programs.run_one(tmp_path, "main", timed_out, cwd=".", env={},
                             timeout_s=1).output == expected
    assert _utest.run_program(tmp_path, timed_out, cwd=".", env={},
                              timeout_s=1).output == expected
    (tmp_path / _ctest.SPECS).write_text(json.dumps({"t": {
        "program": "p", "arguments": [], "cwd": ".", "environment": [], "timeout": None,
        "will_fail": False, "skip_code": None, "pass": [], "fail": [], "skip": [],
        "reason": None}}), encoding="utf-8")
    assert _ctest.run_one(tmp_path, "t", timed_out, cwd=".", env={},
                          timeout_s=1).output == expected


def test_a_runs_judges_read_its_output_as_it_came_while_it_is_kept_line_ended():
    """The output a run keeps ends stdout's last line before stderr. The
    judges of a program and of ctest read stdout and stderr joined as they
    came, and utest's judge reads stdout alone, where utest prints."""
    import subprocess

    from langadapter.c import _ctest, _programs, _utest

    def finished(stdout, stderr, code):
        return lambda argv, **options: subprocess.CompletedProcess(argv, code, stdout, stderr)

    utest = _utest.run_one(Path("p"), "a.b",
                           finished("[ RUN      ] a.b\n[       OK ] a.b (5ns)", "late\n", 0),
                           cwd=".", env={}, timeout_s=1)
    assert utest.output == "[ RUN      ] a.b\n[       OK ] a.b (5ns)\nlate\n"
    assert (utest.outcome, utest.message) == ("passed", "")
    program = _programs.run_one(Path("p"), "main", finished("one\nlast", "err\n", 1),
                                cwd=".", env={}, timeout_s=1)
    assert program.output == "one\nlast\nerr\n"
    assert program.message == "the program ended with exit 1\none\nlasterr"
    spec = {"skip_code": None, "skip": [], "pass": ["okwarning"], "fail": [], "will_fail": False}
    ctest = _ctest.verdict("t", spec, subprocess.CompletedProcess([], 0, "all ok", "warning\n"))
    assert (ctest.outcome, ctest.output) == ("passed", "all ok\nwarning\n")
    whole = _utest.run_program(Path("p"), finished("started", "crashed\n", 1),
                               cwd=".", env={}, timeout_s=1)
    assert (whole.problem, whole.output) == ("the program ended with exit 1",
                                             "started\ncrashed\n")


def test_a_timed_out_programs_running_test_keeps_its_lines_and_stderr_apart():
    """When the timeout ends a program in a test, that test's output keeps
    its stdout's last line ended before the program's stderr, both decoded
    as a finished run's are."""
    import subprocess

    from langadapter.c import _utest

    def timed_out(argv, **options):
        raise subprocess.TimeoutExpired(argv, 1, output=b"[ RUN      ] a.b\r\npartial",
                                        stderr=b"err")

    run = _utest.run_program(Path("p"), timed_out, cwd=".", env={}, timeout_s=1)
    [test] = run.tests
    assert (test.name, test.outcome) == ("a.b", "timed_out")
    assert test.output == "[ RUN      ] a.b\npartial\nerr\n"
