"""lizard's counts behind the structural metrics: the non-comment lines of
code (NLOC) of a text and the McCabe complexity of the functions in it.
lizard reads a text by the language its file name names and needs no parse
tree, so it also counts incomplete source."""
from __future__ import annotations

from typing import Tuple


def code_lines_and_complexities(text: str, suffix: str) -> Tuple[int, Tuple[int, ...]]:
    """The lines of code of ``text``, read as a file whose name ends in
    ``suffix`` (blank lines, comments and Python's docstrings left out), and
    the cyclomatic complexity of each function in it, in the order the
    functions end: a nested function before the one around it, so the text
    of one function gives its own complexity last. A function's complexity
    is one plus its own decisions (branches, loops, exception handlers,
    cases and short-circuit operators), and a nested function's decisions
    count only toward it. The structural metrics count both this way, with
    lizard, which needs no parse tree, so incomplete source is counted too."""
    from lizard import analyze_file

    info = analyze_file.analyze_source_code(f"source{suffix}", text)
    return info.nloc, tuple(function.cyclomatic_complexity for function in info.function_list)


def functions(text: str, suffix: str) -> Tuple[Tuple[str, int], ...]:
    """The functions lizard finds in ``text``, read as a file ending in
    ``suffix``, as (name, cyclomatic complexity), in the order lizard ends
    them. A function's name is bare at the top of the text and qualified
    by what encloses it there ("outer.inner" in Python, "Local::inner" and
    "(anonymous)::run" in Java)."""
    from lizard import analyze_file

    info = analyze_file.analyze_source_code(f"source{suffix}", text)
    return tuple((function.name, function.cyclomatic_complexity) for function in info.function_list)


def version() -> str:
    """The installed lizard's version, part of the metrics' definition."""
    from importlib.metadata import version as installed

    return installed("lizard")
