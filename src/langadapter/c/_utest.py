"""utest.h test programs: their compile, their test list and their runs.

Each test file becomes a program of its own, linked with a build of the
target. A suite run runs it whole (``run_program``), and a test runs in a
process of its own (``--filter=<name>``, ``run_one``) under its own
timeout where per-test evidence and mutants need it. In its own process, a
crash, an abort or a hang ends that test only. Exit status 0 with utest's
``OK`` or ``SKIPPED`` line passes or skips the test,
exit status 1 with its ``FAILED`` line fails it, anything else is an error
named by its signal or status, and a test past its timeout has timed out.
"""
from __future__ import annotations

import re
import signal
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import List, Optional, Tuple

from langadapter.execution import _as_text

#: The directory holding utest.h, as the test compile's include directory.
def header_dir() -> Path:
    return Path(str(resources.files("langadapter.c") / "data"))


#: utest's result line. ``match`` reads it at a line's start, ``search``
#: also where it ends a line after a test's output without a line break.
_RESULT = re.compile(r"\[\s*(OK|FAILED|SKIPPED)\s*\]\s+(\S+)\s+\((-?\d+)(ns|us|ms|s)\)\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SCALE = {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1.0}
_RUN = re.compile(r"^\[\s*RUN\s*\]\s+(\S+)\s*$")
#: utest's closing line after its last test.
_RAN = re.compile(r"^\[=+\]\s+\d+ test cases ran\.$")
_OUTCOMES = {"OK": "passed", "FAILED": "failed", "SKIPPED": "skipped"}
#: The command before a program that keeps its stdout line-buffered, as in a
#: terminal, so that the lines before a crash or a timeout are not lost and a
#: crash names its test. Its stderr stays unbuffered, so that text without a
#: line break reaches the output in the test that wrote it. A known limit:
#: stdbuf works through LD_PRELOAD, so the environment of a suite run's and
#: a run as delivered's program holds LD_PRELOAD, which a test can read.
LINE_BUFFERED = ("stdbuf", "-oL")
#: The command before a whole program that writes its stderr into its stdout,
#: so that each test's section of the output holds the test's own lines of
#: both, in the order they came.
ONE_STREAM = ("sh", "-c", 'exec "$@" 2>&1', "sh")
#: The variable whose lines name the tests the program a crash restarts
#: leaves out (``data/utest_skip.c``).
SKIP_TESTS = "LANGADAPTER_C_SKIP_TESTS"


@dataclass(frozen=True)
class TestRun:
    """One test's process: its name, outcome (``passed``, ``failed``,
    ``error``, ``skipped`` or ``timed_out``), utest's time in seconds, the
    message and the process's whole output."""

    __test__ = False

    name: str
    outcome: str
    time_s: Optional[float]
    message: str
    output: str


def plain(text: str) -> str:
    """``text`` without terminal color codes."""
    return _ANSI.sub("", text or "")


def line_ended(text: str) -> str:
    """``text`` with a line break at its end, unless it is empty or has one,
    so that texts joined one after another keep their lines apart."""
    return text if not text or text.endswith("\n") else text + "\n"


def outputs(stdout: Optional[str], stderr: Optional[str]) -> Tuple[str, str]:
    """A test process's output without color codes, as its judges read it
    (stdout, then stderr) and as it is kept and shown (stdout's last line
    ended before stderr)."""
    stdout, stderr = stdout or "", stderr or ""
    return plain(stdout + stderr), line_ended(plain(stdout)) + plain(stderr)


#: The bytes of a test process's stream kept whole, and beyond that the
#: bytes kept of its start and of its end (``KeptOutput``).
OUTPUT_KEPT = 1024 * 1024
#: The bytes left out that ``KeptOutput`` reads for utest's lines at once.
_BATCH_BYTES = 64 * 1024
#: The longest line ``KeptOutput`` reads among the bytes it leaves out, in
#: bytes: every line of up to 64 Ki characters, at most 4 bytes each.
LINE_BYTES = 4 * 64 * 1024
#: The line breaks of ``str.splitlines`` in UTF-8, and the bytes that turn
#: each into as many ``\n``.
_BREAK = re.compile(rb"\r\n|[\n\r\x0b\x0c\x1c-\x1e]|\xc2\x85|\xe2\x80[\xa8\xa9]")
_SINGLE_BREAKS = bytes.maketrans(b"\r\x0b\x0c\x1c\x1d\x1e", b"\n" * 6)
_MULTI_BREAKS = (b"\xc2\x85", b"\xe2\x80\xa8", b"\xe2\x80\xa9")
_BREAKS = (b"\n", b"\r", b"\x0b", b"\x0c", b"\x1c", b"\x1d", b"\x1e", *_MULTI_BREAKS)
#: The words of utest's RUN and result lines.
_WORDS = (b"RUN", b"OK", b"FAILED", b"SKIPPED")
#: A terminal color code, as ``plain`` removes it, in bytes, and a ``\n``
#: and a line that may be a utest RUN or result line once its color codes
#: and surrounding whitespace go (any byte above 127 may be whitespace): a
#: ``[`` and the start of the word, color codes allowed between them.
_COLOR = rb"(?:" + _ANSI.pattern.encode("ascii") + rb")"
_MAYBE_MARKED = re.compile(
    rb"\n(?:[ \t\x0b\x0c\x1c-\x1f\x80-\xff]|" + _COLOR + rb")*\["
    rb"(?:[ \t\x0b\x0c\x1c-\x1f\x80-\xff]|" + _COLOR + rb")*[ROFS]")
#: The end of a line that may be a utest result line glued to a test's own
#: output: the time in brackets, then whitespace or color codes and a ``\n``.
_MAYBE_RESULT_END = re.compile(
    rb" \(-?\d+(?:ns|us|ms|s)\)(?:[ \t\x0b\x0c\x1c-\x1f\x80-\xff]|" + _COLOR + rb")*\n")
#: A letter of one of utest's words right before a color code, as in a word
#: that color codes split.
_SPLITS = tuple(sorted({word[index:index + 1] + b"\x1b" for word in _WORDS
                        for index in range(len(word) - 1)}))


class KeptOutput:
    """One stream of a test process as the adapter keeps it: whole up to
    ``2 * OUTPUT_KEPT`` bytes, else its first and its last ``OUTPUT_KEPT``
    bytes, each cut at a line break, and between them one line that counts
    the bytes left out, then the utest RUN and result lines among those in
    their order, a result line with the test's own output before it on its
    line, each of up to ``LINE_BYTES`` bytes. A binary file with
    ``getvalue`` for ``_process_tree.run``."""

    def __init__(self) -> None:
        self._head = bytearray()
        self._after = bytearray()
        self._lines: List[bytes] = []
        self._line = b""
        self._long = False
        self._left_out = 0
        self._cut = False
        self._head_ends_line = True

    def write(self, data: bytes) -> None:
        if not self._cut:
            self._head += data
            if len(self._head) <= 2 * OUTPUT_KEPT:
                return
            self._cut = True
            end = max((at + len(found) for found in _BREAKS
                       if (at := self._head.rfind(found, 0, OUTPUT_KEPT)) >= 0), default=None)
            self._head_ends_line = end is not None
            self._long = end is None
            end = OUTPUT_KEPT if end is None else end
            data = self._head[end:]
            del self._head[end:]
        self._after += data
        if len(self._after) >= OUTPUT_KEPT + _BATCH_BYTES:
            self._read_left_out()

    def _read_left_out(self) -> None:
        """Read the bytes before the last ``OUTPUT_KEPT`` for utest's lines and
        leave them out, a batch at a time, so that small writes do not each
        read the unfinished line again."""
        count = len(self._after) - OUTPUT_KEPT
        with memoryview(self._after) as view:
            data = self._line + view[:count]
        found, self._line, self._long = _marked_lines(data, self._long)
        self._lines += found
        self._left_out += count
        del self._after[:count]

    def getvalue(self) -> bytes:
        if not self._cut:
            return bytes(self._head)
        count = len(self._after) - OUTPUT_KEPT
        tail = bytes(self._after[count:])
        first = _BREAK.search(tail)
        start = first.end() if first is not None else 0
        found, _, _ = _marked_lines(self._line + bytes(self._after[:count]) + tail[:start],
                                    self._long)
        note = b"[... %d bytes of the test output omitted ...]\n" % (
            self._left_out + count + start)
        return b"".join([bytes(self._head), b"" if self._head_ends_line else b"\n", note,
                         *self._lines, *found, tail[start:]])


def _marked_lines(data: bytes, long: bool) -> Tuple[List[bytes], bytes, bool]:
    """The utest RUN and result lines of ``data``, each with its line break,
    its unfinished last line, and whether that line is longer than
    ``LINE_BYTES`` (``long``: the line ``data`` starts in already is, and
    then only its last bytes stay, where a line break may begin). Only
    ``data`` that may hold such a line is read line by line, so output
    without one is read at about the speed it arrives."""
    lines = _lines_read(data, long) if _may_be_marked(data) else []
    start = _unfinished(data)
    rest = data[start:]
    long = (long and start == 0) or len(rest) > LINE_BYTES
    return lines, rest[-2:] if long else rest, long


def _may_be_marked(data: bytes) -> bool:
    """Whether ``data`` holds what every RUN and result line holds: a ``[``
    and one of utest's words, whole or split by color codes. Each word has
    a K, N or D, which the quicker search of a single byte finds first."""
    return (b"[" in data and (b"K" in data or b"N" in data or b"D" in data)
            and (any(word in data for word in _WORDS)
                 or b"\x1b" in data and any(split in data for split in _SPLITS)))


def _unfinished(data: bytes) -> int:
    """Where the unfinished last line of ``data`` starts, after its last line
    break. Breaks are looked for after its last line feed and in its last
    ``LINE_BYTES`` bytes only: a line that starts before those is too long
    to read either way, so its start does not matter."""
    window = max(data.rfind(b"\n") + 1, len(data) - LINE_BYTES - 3)
    breaks = _BREAKS[1:] if b"\xc2" in data or b"\xe2" in data else _BREAKS[1:-3]
    return max([window, *(at + len(found) for found in breaks
                          if (at := data.rfind(found, window)) >= 0)])


def _lines_read(data: bytes, long: bool) -> List[bytes]:
    """The utest RUN and result lines among the whole lines of ``data``, a
    result line also where it ends a line of the test's own output."""
    # Each line break one or more \n, after one \n before the first line:
    # a byte of ``data`` has an index one less than in ``flat``.
    flat = (b"\n" + data).translate(_SINGLE_BREAKS)
    for found in _MULTI_BREAKS:
        flat = flat.replace(found, b"\n" * len(found))
    starts = {candidate.start() for candidate in _MAYBE_MARKED.finditer(flat)}
    starts.update(flat.rfind(b"\n", 0, candidate.start())
                  for candidate in _MAYBE_RESULT_END.finditer(flat))
    lines = []
    for begin in sorted(starts):
        end = flat.find(b"\n", begin + 1) - 1
        if end < 0:
            break
        if (end - begin <= LINE_BYTES and not (long and begin == 0)
                and _is_marked(data[begin:end].decode("utf-8", "replace"))):
            lines.append(data[begin:_BREAK.match(data, end).end()])
    return lines


def _is_marked(line: str) -> bool:
    """Whether ``line`` is a utest RUN or result line, as its readers read it:
    a result line also after the test's own output (``_result_for``)."""
    text = plain(line).strip()
    return bool(_RUN.match(text) or _RESULT.search(text))


def list_tests(program: Path, run, *, cwd: str, env, timeout_s: float) -> Tuple[str, ...]:
    """The tests ``program`` holds, in utest's order, its list kept whole."""
    done = run([str(program), "--list-tests"], cwd=cwd, env=env, timeout=timeout_s,
               whole=True)
    if done.returncode != 0:
        raise RuntimeError(f"{program.name} --list-tests ended with exit {done.returncode}: "
                           f"{plain(done.stdout or '')[-500:]}{plain(done.stderr or '')[-500:]}")
    return tuple(line.strip() for line in plain(done.stdout).splitlines() if line.strip())


def run_one(program: Path, name: str, run, *, cwd: str, env, timeout_s: float) -> TestRun:
    """Run the test ``name`` of ``program`` alone and read its outcome from
    its stdout, where utest prints its lines."""
    try:
        done = run([str(program), f"--filter={name}"], cwd=cwd, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired as expired:
        output = outputs(_as_text(expired.stdout), _as_text(expired.stderr))[1]
        return TestRun(name, "timed_out", None,
                       f"the test reached its timeout of {timeout_s:g} s", output)
    judged, output = plain(done.stdout), outputs(done.stdout, done.stderr)[1]
    result, seconds = _result_line(judged, name)
    code = done.returncode
    if code == 0 and result == "OK":
        return TestRun(name, "passed", seconds, "", output)
    if code == 0 and result == "SKIPPED":
        return TestRun(name, "skipped", seconds, _message(judged, name), output)
    if code == 1 and result == "FAILED":
        return TestRun(name, "failed", seconds, _message(judged, name), output)
    return TestRun(name, "error", seconds, _ended(code, result), output)


@dataclass(frozen=True)
class ProgramRun:
    """A test program run once by utest.h's own main: the tests it ran, in
    order, whether its timeout ended it, why it ended before its first test
    (else empty), its whole output, and its exit status (None after the
    timeout)."""

    tests: Tuple[TestRun, ...]
    timed_out: bool
    problem: str
    output: str
    code: Optional[int]


def run_program(program: Path, run, *, cwd: str, env, timeout_s: float) -> ProgramRun:
    """Run every test of ``program`` in its one process, in their order of
    definition, as utest.h's own main does, its output line-buffered. A test
    with its result line has that outcome. The test that was running when a
    crash or the timeout ended the program is an error or timed out, also
    after a result line it printed itself, and the tests after it have
    none."""
    try:
        done = run([*ONE_STREAM, *LINE_BUFFERED, str(program)], cwd=cwd, env=env,
                   timeout=timeout_s)
        stdout, stderr, code, timed_out = (_as_text(done.stdout), _as_text(done.stderr),
                                           done.returncode, False)
    except subprocess.TimeoutExpired as expired:
        stdout, stderr, code, timed_out = (_as_text(expired.stdout), _as_text(expired.stderr),
                                           None, True)
    lines = plain(stdout).splitlines()
    tests: List[TestRun] = []
    running, start, found = None, 0, None

    def settle() -> None:
        """The running test with its last result line, as ``_result_line``
        reads one test's process."""
        index, result = found
        outcome = _OUTCOMES[result.group(1)]
        section = line_ended("\n".join(lines[start:index + 1]))
        tests.append(TestRun(running, outcome, int(result.group(3)) * _SCALE[result.group(4)],
                             "" if outcome == "passed" else _message(section, running),
                             section))

    for index, line in enumerate(lines):
        begun = _RUN.match(line.strip())
        if begun:
            if found is not None:
                settle()
            running, start, found = begun.group(1), index, None
            continue
        result = _result_for(line, running) if running is not None else None
        if result:
            found = (index, result)
    # utest's own result line is followed by the next test's RUN line or by
    # its closing line. A last result line with neither after it was the
    # test's own text, and the program ended in that test.
    if found is not None and _closed_after(lines, found[0]):
        settle()
    elif running is not None:
        section = line_ended("\n".join(lines[start:])) + line_ended(plain(stderr))
        if timed_out:
            tests.append(TestRun(running, "timed_out", None,
                                 f"the program reached its timeout of {timeout_s:g} s in this "
                                 "test", section))
        else:
            tests.append(TestRun(running, "error", None, f"{program_ended(code)} in this test",
                                 section))
    problem = ""
    if not tests and (timed_out or code != 0):
        problem = (f"the program reached its timeout of {timeout_s:g} s" if timed_out
                   else program_ended(code))
    return ProgramRun(tuple(tests), timed_out, problem, outputs(stdout, stderr)[1], code)


def program_ended(code: Optional[int]) -> str:
    """How a program that did not pass ended: its signal or status."""
    if code is not None and code < 0:
        return f"the program ended by {signal_name(-code)}"
    return f"the program ended with exit {code}"


def signal_name(number: int) -> str:
    """Signal ``number`` by its name, such as ``SIGSEGV``, else ``signal N``."""
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


#: The last lines of a failed test's output its message keeps.
MESSAGE_LINES = 5


def last_lines(first: str, output: str) -> str:
    """``first``, then the last ``MESSAGE_LINES`` non-blank lines of ``output``."""
    shown = [line.rstrip() for line in output.splitlines() if line.strip()][-MESSAGE_LINES:]
    return "\n".join([first, *shown])


def _closed_after(lines: List[str], index: int) -> bool:
    """Whether utest's closing line comes after line ``index`` of ``lines``."""
    return any(_RAN.match(line.strip()) for line in lines[index + 1:])


def _result_for(line: str, name: str):
    """utest's result line for the test ``name`` where it ends ``line``,
    also after the test's own output without a line break, or None. A
    test's result is the last such line: utest prints it after the test's
    output, which may hold such a line itself."""
    match = _RESULT.search(line.strip())
    return match if match and match.group(2) == name else None


def _result_line(output: str, name: str) -> Tuple[Optional[str], Optional[float]]:
    """The outcome and time of the last result line for ``name``
    (``_result_for``)."""
    for line in reversed(output.splitlines()):
        match = _result_for(line, name)
        if match:
            return match.group(1), int(match.group(3)) * _SCALE[match.group(4)]
    return None, None


def failed_names(stdout: str) -> List[str]:
    """The tests a program's ``stdout`` reports as failed, in order: each by
    its last result line, also one glued to its output, as ``_result_for``
    reads a test's."""
    last = {}
    for line in stdout.splitlines():
        match = _RESULT.search(line.strip())
        if match:
            last[match.group(2)] = match.group(1)
    return [name for name, result in last.items() if result == "FAILED"]


def _message(output: str, name: str) -> str:
    """The lines printed for the test between its ``RUN`` line and its last
    result line, the one its readers take (``_result_for``): the failed
    checks with their values, and the test's own text before a result glued
    to it."""
    lines = output.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip().startswith("[ RUN") and line.strip().endswith(name)), None)
    if start is None:
        return ""
    end = next((index for index in range(len(lines) - 1, start, -1)
                if _result_for(lines[index], name)), len(lines))
    body = [line.rstrip() for line in lines[start + 1:end]]
    if end < len(lines):
        text = lines[end].strip()
        body.append(text[:_result_for(text, name).start()])
    return "\n".join(body).strip()


def _ended(code: Optional[int], result: Optional[str]) -> str:
    """Why a test process is an error: its signal or status."""
    if code is not None and code < 0:
        return f"the test process ended by {signal_name(-code)}"
    if code is not None and code > 128:
        try:
            return f"the test process ended by {signal.Signals(code - 128).name}"
        except ValueError:
            pass
    shown = f" after {result}" if result else " without a result line"
    return f"the test process ended with exit {code}{shown}"


def compile_errors(output: str) -> Tuple[str, ...]:
    """The first five error lines of a compiler's or ld's output: the
    compiler's errors, and ld's causes (an undefined reference, a multiple
    definition) and its exit line."""
    found = [line.strip() for line in plain(output).splitlines()
             if ": error:" in line or ": fatal error:" in line or "undefined reference" in line
             or "multiple definition of" in line]
    return tuple(found[:5])
