"""Several suites' C test files staged as one pool (``langadapter.sources.
StagedPool``), as LangAdapter stages Python's: each file named for its suite,
``test_<stem>_<slot>.c``, with the way back to its suite's own names. A C test
file is a program of its own, so its tests keep their names and only the
file's path tells the suites apart.

A pool is refused when its names would change what a file does: a file that
is not C, one that does not parse, or one that includes a file of its own
suite by name. A file that reads the name of the file being compiled, or
names a file of its suite in a string, is staged with that noted in its
``self_references``: its verdicts in the pool may differ from its own run.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Sequence, Tuple

from langadapter.sources import (
    PoolMember, SourceObservation, SourceStatus, StagedFile, StagedPool, check_members,
    check_setup, unique_paths,
)

from langadapter.c import _syntax

#: The macros that name the file being compiled, which the staged name changes.
FILE_MACROS = ("__FILE__", "__FILE_NAME__", "__BASE_FILE__")


def staged_name(name: str, slot: str) -> str:
    """The pooled path of the suite file ``name`` of ``slot``: its directory and
    ``test_<stem>_<slot>.c``, a ``test_`` stem kept as it is."""
    path = PurePosixPath(name)
    staged = f"{path.stem}_{slot}.c" if path.stem.startswith("test_") else \
        f"test_{path.stem}_{slot}.c"
    return (path.parent / staged).as_posix()


def self_references(source: str, names: Sequence[str]) -> Tuple[str, ...]:
    """Where ``source`` reads the name of the file being compiled, or names
    one of the files ``names`` in a string."""
    tree = _syntax.parse(_syntax.masked(source.encode("utf-8")))
    words = [(name, re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?!\w)"))
             for name in dict.fromkeys(names)]
    found = set()
    for node in _syntax.walk(tree.root_node):
        text = node.text.decode("utf-8", "replace")
        if node.type == "identifier" and text in FILE_MACROS:
            found.add(f"reads its file's name ({text})")
        elif node.type == "string_literal":
            found.update(f"names the file {name} in a string"
                         for name, word in words if word.search(text))
    return tuple(sorted(found))


def stage(members: Sequence[PoolMember], setup: Sequence[Tuple[str, str]] = ()):
    """The pool of ``members`` with the shared ``setup`` files staged as they
    are, or an observation that says why it cannot be staged."""
    members = check_members(members)
    setup = check_setup(setup)
    for member in members:
        if PurePosixPath(member.name).suffix != ".c":
            return SourceObservation(SourceStatus.UNSUPPORTED,
                                     reason=f"{member.name} is not a C file")
    files = []
    for member in members:
        problem = _syntax.first_error(member.source.encode("utf-8"), test_file=True)
        if problem is not None:
            return SourceObservation(SourceStatus.PARSE_ERROR, parser=_syntax.PARSER,
                                     reason=f"{member.name} of suite {member.slot} does not "
                                            f"parse: {problem.message}")
        suite = [PurePosixPath(other.name).name for other in members
                 if other.slot == member.slot]
        own = PurePosixPath(member.name).name
        for included in _syntax.quoted_includes(member.source.encode("utf-8")):
            if PurePosixPath(included).name in suite and PurePosixPath(included).name != own:
                return SourceObservation(
                    SourceStatus.UNSUPPORTED,
                    reason=f"{member.name} of suite {member.slot} includes {included}, a file "
                           "of its own suite, whose name the pool changes")
        files.append(StagedFile(member.slot, PurePosixPath(member.name).as_posix(),
                                staged_name(member.name, member.slot), member.source,
                                self_references=self_references(member.source, suite)))
    pool = StagedPool(tuple(files), setup)
    issue = unique_paths(pool, by_name=True)
    if issue is not None:
        return SourceObservation(SourceStatus.UNSUPPORTED, reason=issue)
    return pool
