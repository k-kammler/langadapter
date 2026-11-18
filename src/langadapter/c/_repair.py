"""Load repair and import pruning of C test files, by their compile, and
the local fix of their syntax repair (``syntax_fix``).

A C test file loads when it compiles and links as a run builds it, against
the same target build. GCC names the line of each error, ld the line of a
reference it cannot resolve, and the unit around it goes: a utest.h test, a
fixture block or another top-level declaration (``removable_units``). A
utest.h test compiles with ``MACRO_EXPANSION_LINES``, so that an error inside
a utest.h macro names the test file's line. An
``#include`` of a file that does not exist ends the compile at its line, and
import pruning removes such lines one at a time. Implicit declarations are
errors here, so that a call of a function the target does not declare has a
line, rather than a link error without one. GCC reports such a function at
its first call only, so every call of it is an error line
(``error_lines``).
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence, Tuple

from langadapter.c import _syntax

_ERROR = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:\d+:)?\s*(?:fatal )?error:", re.MULTILINE)
_MISSING = re.compile(r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:\d+:)?\s*fatal error:\s*(?P<name>[^:]+):"
                      r"\s*No such file or directory", re.MULTILINE)
#: ld's message on a reference it can place: the source's path and line from
#: the object's debug information, then the section and offset.
_LINK = re.compile(r"^(?:\S*ld: )?(?P<file>[^:\n]+):(?P<line>\d+):\([^)\n]*\): ", re.MULTILINE)
#: ld's message on a reference it cannot resolve, with the symbol. ld names
#: five references to a symbol at most.
_UNDEFINED = re.compile(r"^(?:\S*ld: )?(?P<file>[^:\n]+):\d+:\([^)\n]*\): undefined reference to "
                        r"\W(?P<name>[A-Za-z_]\w*)\W", re.MULTILINE)
#: ld's message on a symbol defined twice names each definition it can place
#: with its source's path and line: the second (multiple definition) or the
#: first (first defined here).
_DEFINED_TWICE = re.compile(r"(?P<file>[^\s:;]+):(?P<line>\d+): (?:multiple definition of|"
                            r"first defined here)")
_IMPLICIT = re.compile(r"^(?P<file>[^:\n]+):\d+:(?:\d+:)?\s*error: implicit declaration of "
                       r"function \W(?P<name>[A-Za-z_]\w*)\W", re.MULTILINE)
#: The flag that makes an implicit declaration an error with a line.
IMPLICIT_DECLARATION = "-Werror=implicit-function-declaration"
#: The flag that places an error inside a macro, such as utest.h's assertion
#: used outside a test, at the line of the test file that expands it.
MACRO_EXPANSION_LINES = "-ftrack-macro-expansion=0"


def error_lines(output: str, file_name: str, source: str) -> Tuple[int, ...]:
    """The lines of ``source``, the test file GCC printed as ``file_name``,
    that the compiler's or ld's ``output`` finds an error on: each error's
    own line (with ``MACRO_EXPANSION_LINES`` also an error inside a utest.h
    macro), each line ld names, a definition's line ld names for a symbol
    defined twice, every call of a symbol ld cannot resolve, since it names
    five references at most, and every call of a function whose implicit
    declaration is an error and which the file declares or defines nowhere,
    since GCC reports such a function at its first call only."""
    lines = {int(match.group("line")) for pattern in (_ERROR, _LINK, _DEFINED_TWICE)
             for match in pattern.finditer(output) if match.group("file").endswith(file_name)}
    data = source.encode("utf-8")
    calls = {match.group("name") for match in _IMPLICIT.finditer(output)
             if match.group("file").endswith(file_name)} - _syntax.declared_names(data)
    calls |= {match.group("name") for match in _UNDEFINED.finditer(output)
              if match.group("file").endswith(file_name)}
    return tuple(sorted(lines.union(_syntax.call_lines(data, calls))))


def missing_includes(output: str, file_name: str) -> Tuple[Tuple[int, str], ...]:
    """The ``(line, header)`` of each include of ``file_name`` whose file
    does not exist."""
    return tuple((int(match.group("line")), match.group("name").strip())
                 for match in _MISSING.finditer(output)
                 if match.group("file").endswith(file_name))


def removable_units(source: str) -> List[Tuple[str, int, int]]:
    """The units load repair may remove, as ``(label, first line, last
    line)``: each top-level utest.h test or fixture block, function, and
    declaration. Includes, macro definitions and ``UTEST_MAIN()`` are not."""
    data = source.encode("utf-8")
    masked = _syntax.masked(data)
    tree = _syntax.parse(masked)
    units: List[Tuple[str, int, int]] = []
    tests = {unit.span[0]: unit for unit in _syntax.macro_units(data)}
    for node in tree.root_node.children:
        if node.type in ("preproc_include", "preproc_def", "preproc_function_def",
                         "preproc_call", "comment"):
            continue
        first, last = node.start_point[0] + 1, node.end_point[0] + 1
        unit = tests.get(node.start_byte)
        if unit is not None:
            units.append((unit.name, first, last))
            continue
        # A standalone macro statement (``UTEST_MAIN()``) is masked as ``int m(void);``.
        if masked[node.start_byte:node.end_byte].startswith(b"int m(void);"):
            continue
        text = data[node.start_byte:node.end_byte].decode("utf-8", "replace")
        label = next((name for name, found in _syntax.top_functions(data, tree)
                      if found.start_byte == node.start_byte), None)
        units.append((label or text.splitlines()[0].strip()[:60], first, last))
    return units


def without_units(source: str, lines: Sequence[int]) -> Optional[Tuple[str, List[str]]]:
    """``source`` without the units around ``lines``, and their labels, or
    None when a line lies in no removable unit."""
    units = removable_units(source)
    chosen = []
    for line in lines:
        unit = next((u for u in units if u[1] <= line <= u[2]), None)
        if unit is None:
            return None
        if unit not in chosen:
            chosen.append(unit)
    kept = source.splitlines(keepends=True)
    for _, first, last in sorted(chosen, key=lambda u: u[1], reverse=True):
        del kept[first - 1:last]
    return "".join(kept), [label for label, _, _ in chosen]


def without_lines(source: str, lines: Sequence[int]) -> str:
    """``source`` without the given lines."""
    gone = set(lines)
    return "".join(line for number, line in enumerate(source.splitlines(keepends=True), 1)
                   if number not in gone)


def has_a_test(source: str) -> bool:
    data = source.encode("utf-8")
    return any(unit.macro in _syntax.TEST_MACROS for unit in _syntax.macro_units(data))


#: The tokens a syntax repair inserts where the parser finds them missing.
INSERTED = frozenset({";", "}", ")", "]"})
_MISSING_TOKEN = re.compile(r"missing (\S+) at line")


def syntax_fix(lines: List[str], verdict) -> bool:
    """Fix the syntax error ``verdict`` names in ``lines``, numbered from 1:
    insert a missing token of ``INSERTED`` at its place, else blank the
    line. False when the line is already empty."""
    missing = _MISSING_TOKEN.match(verdict.message)
    number, column = verdict.line, verdict.column
    if missing and missing.group(1) in INSERTED and number >= 1:
        token = missing.group(1)
        if number > len(lines):
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(token + "\n")
            return True
        data = lines[number - 1].encode("utf-8")
        at = min(max(column - 1, 0), len(data))
        lines[number - 1] = (data[:at] + token.encode("utf-8") + data[at:]).decode("utf-8")
        return True
    if not number or number > len(lines) or not lines[number - 1].strip():
        return False
    ended = lines[number - 1].endswith("\n")
    lines[number - 1] = "\n" if ended else ""
    return True
