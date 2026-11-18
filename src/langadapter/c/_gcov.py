"""gcov's record of the target, read through gcovr into LangAdapter's coverage terms.

The target is compiled from a staged copy (``_build``), so its notes file
(``.gcno``) sits beside the staged object, and each run's counters
(``.gcda``) are written under a ``GCOV_PREFIX`` of the run's own.
``read_counts`` copies a notes file and a counters file into a fresh directory
and has gcovr (``python -m gcovr``) run the project's gcov on them and write
its JSON report. gcovr runs with its defaults: its exclusion markers apply,
and its unreachable, non-code and function-line exclusions are off. On
simplejson's ``_speedups.c`` its report gave the same lines, counts,
branches in order and functions as gcov's JSON read directly.

- Statements are the lines gcov lists for the target's file. Covered ones
  have a count above zero.
- Branches are gcov's branch records on those lines, numbered on their line
  in gcov's order (``BranchItem(file, line, ordinal)``), and taken when their
  count is above zero.
- The lines and branches gcovr flags as excluded (``gcovr/excluded``) are
  left out.
- Records of other files, such as headers, are not the target's: gcovr's
  root is the directory the compile ran in, which holds the staged target alone.
- Functions: gcovr names each function's first line and each line's
  function, so a function ends at its last line of code.
"""
from __future__ import annotations

import json
import posixpath
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, NamedTuple, Optional, Sequence, Set, Tuple

from langadapter.outcomes import FileIdentity
from langadapter.runs import CoverageObservation
from langadapter.suites import BranchItem

#: The version of gcovr's JSON format this reader reads.
GCOVR_FORMAT = "0.15"
#: gcovr's flag on a line or branch its exclusion rules leave out.
EXCLUDED = "gcovr/excluded"


@dataclass(frozen=True)
class Function:
    """A function of the target as gcov lists it: its name, first and last
    line, and how often it ran."""

    name: str
    start_line: int
    end_line: int
    executions: int


@dataclass(frozen=True)
class Counts:
    """The target's counts of one or more runs: per line its count, per line
    its branch counts in gcov's order, and its functions."""

    lines: Mapping[int, int]
    branches: Mapping[int, Tuple[int, ...]]
    functions: Tuple[Function, ...]

    def merged(self, other: "Counts") -> "Counts":
        """The counts of both runs, summed."""
        lines = dict(self.lines)
        for line, count in other.lines.items():
            lines[line] = lines.get(line, 0) + count
        branches = dict(self.branches)
        for line, counts in other.branches.items():
            mine = branches.get(line)
            branches[line] = (tuple(a + b for a, b in zip(mine, counts))
                              if mine is not None and len(mine) == len(counts) else counts)
        executions: Dict[Tuple[str, int], Function] = {
            (f.name, f.start_line): f for f in self.functions}
        for function in other.functions:
            key = (function.name, function.start_line)
            held = executions.get(key)
            executions[key] = function if held is None else Function(
                held.name, held.start_line, held.end_line, held.executions + function.executions)
        return Counts(lines, branches, tuple(sorted(executions.values(),
                                                    key=lambda f: f.start_line)))


def empty(notes: "Counts") -> "Counts":
    """``notes``'s lines, branches and functions with every count zero."""
    return Counts(dict.fromkeys(notes.lines, 0),
                  {line: tuple(0 for _ in counts) for line, counts in notes.branches.items()},
                  tuple(Function(f.name, f.start_line, f.end_line, 0) for f in notes.functions))


#: How long gcovr may read one counters file.
GCOVR_TIMEOUT_S = 120.0
#: gcovr's exit status when it could not read the coverage data (its
#: EXIT_READ_ERROR), an inconsistent count among other causes.
GCOVR_READ_ERROR = 64
#: gcovr's message on a count gcov read as negative or as 2^32 or more (its
#: suspicious hit value): the source's path and line, the kind, then the
#: line of gcov's output or of the source that holds it.
_INCONSISTENT_COUNT = re.compile(r"(?:Negative|Suspicious)HitsError: (.+):(\d+) "
                                 r"Got (negative|suspicious) hit value in: (.*)")


class CountsInconsistent(RuntimeError):
    """Counters from which gcov read a negative count or one of 2^32 or
    more, which gcovr refuses (GCC bug 68080). A test that corrupts memory
    or relies on undefined behavior can leave such counters. The message is
    gcovr's, with the source named as the target's compile named it."""


def read_counts(gcov: str, notes: Path, counters: Optional[Path], operand: str, counts_dir: Path,
                run, *, root: Path) -> Counts:
    """The target's counts from the notes file ``notes`` and the counters file
    ``counters`` (None: every count is zero, as for the notes alone), read in
    the empty ``counts_dir``. ``root`` is the directory the
    target's compile ran in, where gcov finds the staged source, and
    ``operand`` the source as that compile named it. Counters with a
    negative or suspicious count raise ``CountsInconsistent``, and any other
    failure of gcovr a ``RuntimeError``."""
    counts_dir.mkdir(parents=True, exist_ok=True)
    stem = notes.stem
    shutil.copyfile(notes, counts_dir / f"{stem}.gcno")
    if counters is not None:
        shutil.copyfile(counters, counts_dir / f"{stem}.gcda")
    report = counts_dir / "gcovr.json"
    done = run([sys.executable, "-m", "gcovr", "--root", str(root),
                "--object-directory", str(counts_dir), "--gcov-executable", gcov,
                "--json", str(report), str(counts_dir)], cwd=str(counts_dir), timeout=GCOVR_TIMEOUT_S)
    if done.returncode != 0 or not report.is_file():
        text = ((done.stdout or "") + (done.stderr or "")).strip()
        found = (_INCONSISTENT_COUNT.search(text) if done.returncode == GCOVR_READ_ERROR
                 else None)
        if found is not None:
            source, line, kind, read = found.groups()
            raise CountsInconsistent(f"{_report_name(source, root)}:{line} Got {kind} hit "
                                     f"value in: {read}")
        raise RuntimeError(f"gcovr could not read {notes.name} (exit {done.returncode}): "
                           f"{text[-500:]}")
    record = json.loads(report.read_text(encoding="utf-8"))
    if record.get("gcovr/format_version") != GCOVR_FORMAT:
        raise RuntimeError(f"gcovr wrote JSON format {record.get('gcovr/format_version')}, "
                           f"and this reader reads {GCOVR_FORMAT}")
    return _counts(record, _report_name(operand, root))


def _report_name(operand: str, root: Path) -> str:
    """``operand`` as gcovr names the file: relative to ``root`` when it lies
    below it, else absolute, without ``.`` and ``..`` parts."""
    base = posixpath.normpath(Path(root).as_posix())
    path = posixpath.normpath(posixpath.join(base, operand))
    relative = posixpath.relpath(path, base)
    return path if relative.startswith("..") else relative


def _counts(record: Mapping, name: str) -> Counts:
    """The counts of the file ``name`` in gcovr's report ``record``. A known
    limit: a function ends at its last line gcov lists, so lines gcov
    excludes shorten it, and a function gcov excludes stays listed, so a
    mutant on such a line maps to no function, runs every test and is
    never reported as not executed."""
    lines: Dict[int, int] = {}
    branches: Dict[int, List[int]] = {}
    last: Dict[str, int] = {}
    functions: List[Tuple[str, int, int]] = []
    for entry in _entries_of(record, name):
        for line in entry.get("lines", ()):
            if line.get(EXCLUDED):
                continue
            number = int(line["line_number"])
            lines[number] = lines.get(number, 0) + int(line["count"])
            counts = [int(b["count"]) for b in line.get("branches", ()) if not b.get(EXCLUDED)]
            if counts:
                branches.setdefault(number, []).extend(counts)
            name = line.get("function_name")
            if name:
                last[name] = max(last.get(name, number), number)
        for function in entry.get("functions", ()):
            name = function.get("name") or function.get("demangled_name")
            functions.append((name, int(function["lineno"]),
                              int(function.get("execution_count", 0))))
    return Counts(lines, {line: tuple(counts) for line, counts in branches.items()},
                  tuple(sorted((Function(name, start, max(start, last.get(name, start)), count)
                                for name, start, count in functions),
                               key=lambda f: f.start_line)))


def _entries_of(record: Mapping, name: str) -> List[Mapping]:
    """The entries of gcovr's report ``record`` for the file ``name``."""
    entries = record.get("files", ())
    found = [entry for entry in entries if posixpath.normpath(entry.get("file", "")) == name]
    if not found:
        # The notes list the target's lines, so a report without it is a
        # misread name, never a target that ran nothing.
        named = ", ".join(sorted(str(entry.get("file")) for entry in entries)) or "no file"
        raise RuntimeError(f"gcovr's report has no entry for {name}: it names {named}")
    return found


def observation(counts: Counts, target: Path, root: Path) -> CoverageObservation:
    """The coverage of the project file ``target`` from its ``counts``."""
    identity = FileIdentity.of(target, root)
    statements = tuple(sorted(counts.lines))
    covered = tuple(line for line in statements if counts.lines[line] > 0)
    branches: List[BranchItem] = []
    taken: List[BranchItem] = []
    for line in sorted(counts.branches):
        for ordinal, count in enumerate(counts.branches[line]):
            item = BranchItem(identity.path, line, ordinal)
            branches.append(item)
            if count > 0:
                taken.append(item)
    return CoverageObservation(target=identity, statements=statements, covered=covered,
                               branches=tuple(branches), taken=tuple(taken))


def counters_file(prefix: Path, notes: Path) -> Optional[Path]:
    """The counters file a run wrote under ``prefix`` for ``notes``, or None
    when the run wrote none. A process that executes nothing of the target
    writes a file of zero counts at its exit, so a missing file means a
    process that ended outside libgcov's exit dump, as by ``_exit`` or a
    signal, or an environment that kept the file from being written."""
    found = sorted(prefix.rglob(notes.stem + ".gcda")) if prefix.is_dir() else []
    return found[0] if found else None


def missed_branch_texts(coverage: CoverageObservation) -> Tuple[str, ...]:
    """Each line with untaken branches as ``line->k of n branches untaken``,
    the form Java's adapter writes for counted branches."""
    by_line: Dict[int, List[BranchItem]] = {}
    for branch in coverage.branches:
        by_line.setdefault(branch.source, []).append(branch)
    taken = set(coverage.taken)
    texts = []
    for line in sorted(by_line):
        items: Sequence[BranchItem] = by_line[line]
        missed = sum(1 for item in items if item not in taken)
        if missed:
            texts.append(f"{line}->{missed} of {len(items)} branches untaken")
    return tuple(texts)


def no_counters(test: str) -> str:
    """The reason of a measurement whose ``test`` wrote no counters file."""
    return f"{test}: no coverage counters were written for it"


class Stop(NamedTuple):
    """A test that stops a measurement: its file and name, what happened to
    it, whether its own counts are refused or missing (``counts``) and
    whether it started after the measurement's time was spent (``late``)."""

    file: str
    name: str
    what: str
    counts: bool = False
    late: bool = False


def named(stops: Iterable[Stop], listed: Mapping[str, Sequence[str]]) -> List[Tuple[str, str]]:
    """The tests ``stops`` names, as ``(file, name)`` in their order: none of
    a program every one of whose ``listed`` tests has counts refused or
    missing, since such a failure is the program's, as an exit handler's."""
    stops = list(stops)
    counted: Dict[str, Set[str]] = {}
    for stop in stops:
        if stop.counts:
            counted.setdefault(stop.file, set()).add(stop.name)
    programs = {file for file, names in counted.items()
                if listed.get(file) and names >= set(listed[file])}
    return [(stop.file, stop.name) for stop in stops
            if not (stop.counts and stop.file in programs)]
