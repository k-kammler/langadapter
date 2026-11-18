"""The C adapter's validity policy for generated C tests: a test must call
the target's functions, not read or copy its implementation. A finding names
its test (``suite.name``), or none for code outside a test, which taints the
whole source. The rules:

- ``target_source_read``: a call that opens a file (``fopen``, ``freopen``,
  ``open``, ``openat``) is given a string that names the target's source
  file, by its path or its file name.
- ``copied_target``: the test brings its own copy of the implementation, by
  an ``#include`` of the target's source file or a function definition named
  like one the target defines.
- ``target_redefined``: a macro is named like a function the target defines,
  so the test's calls of it no longer reach the implementation.

Review-only screening signals: ``target_literal`` (another string names the
target's source file or path), ``source_discovery`` (directories are listed
or walked: ``opendir``, ``readdir``, ``scandir``, ``glob``, ``nftw``,
``ftw``, ``fts_open``) and ``keyword_override`` (a macro redefines a storage
class or qualifier such as ``static``, as is done to reach a file's internal
functions). A source that does not parse gets one ``unparseable_source``
flag.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import List, Optional, Set, Tuple

from langadapter.sources import SuiteAuditFlag, SuiteValidityFinding, TargetIdentity

from langadapter.c import _syntax

POLICY_VERSION = "c-generated-test-validity-v1"

#: The calls that open a file their string argument names.
_OPENS = frozenset({"fopen", "fopen64", "freopen", "open", "open64", "openat"})
#: The calls that list or walk directories.
_DISCOVERY = frozenset({"opendir", "fdopendir", "readdir", "scandir", "glob", "nftw", "ftw",
                        "fts_open"})
#: The keywords a macro may redefine to change how the target's code compiles.
_KEYWORDS = frozenset({"static", "inline", "extern", "const", "register", "volatile"})


def target_identity(path: Path, root: Path) -> TargetIdentity:
    """The identity of the C file ``path`` below ``root``. Its module is the
    file's path without ``.c``."""
    text = path.read_text(encoding="utf-8", errors="replace")
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return TargetIdentity(
        source_path=relative, project_root=str(root), module_name=relative.removesuffix(".c"),
        language="c", policy_version=POLICY_VERSION,
        target_source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        target_source=text)


class _Reader:
    """One pass over a test source for a target."""

    def __init__(self, source: str, identity: TargetIdentity) -> None:
        self.data = source.encode("utf-8")
        self.root = _syntax.parse(_syntax.masked(self.data)).root_node
        self.units = _syntax.macro_units(self.data)
        self.heads = [unit.head for unit in self.units]
        self.identity = identity
        self.needles = {PurePosixPath(identity.source_path).name, identity.source_path}
        target = identity.target_source.encode("utf-8")
        self.functions = {name for name, _ in _syntax.top_functions(target)}

    def enclosing_test(self, node) -> Optional[str]:
        for unit in self.units:
            if unit.macro in _syntax.TEST_MACROS and unit.span[0] <= node.start_byte < unit.span[1]:
                return unit.name
        return None

    def names_target(self, value: str) -> bool:
        return any(value == needle or value.endswith("/" + needle) for needle in self.needles)

    def masked(self, node) -> bool:
        """Whether ``node`` lies in a utest.h macro head, read masked."""
        return any(start <= node.start_byte < end for start, end in self.heads)


def _string(node) -> Optional[str]:
    """A string literal's value, escapes as written, or None for other nodes."""
    if node.type not in ("string_literal", "system_lib_string"):
        return None
    text = node.text.decode("utf-8", "replace")
    start = text.find('"') if node.type == "string_literal" else 0
    return text[start + 1:-1]


def _call_name(node) -> Optional[str]:
    function = node.child_by_field_name("function")
    if function is None or function.type != "identifier":
        return None
    return function.text.decode("utf-8", "replace")


def _function_name(node) -> Optional[str]:
    declarator = node.child_by_field_name("declarator")
    while declarator is not None and declarator.type != "identifier":
        inner = declarator.child_by_field_name("declarator")
        if inner is None:
            return None
        declarator = inner
    return declarator.text.decode("utf-8", "replace") if declarator is not None else None


def _span(node) -> Tuple[int, int, int, int]:
    return (node.start_point[0] + 1, node.start_point[1] + 1,
            node.end_point[0] + 1, node.end_point[1] + 1)


def analyze(source: str, identity: TargetIdentity) -> Tuple[SuiteValidityFinding, ...]:
    """The policy's findings on ``source``. It raises SyntaxError if
    ``source`` does not parse."""
    problem = _syntax.first_error(source.encode("utf-8"), test_file=True)
    if problem is not None:
        raise SyntaxError(problem.message)
    reader = _Reader(source, identity)
    findings: List[SuiteValidityFinding] = []
    seen: Set[Tuple[str, int, int]] = set()

    def add(node, rule: str, operation: str, diagnostic: str) -> None:
        start_line, start_col, end_line, end_col = _span(node)
        if (rule, start_line, start_col) in seen:
            return
        seen.add((rule, start_line, start_col))
        findings.append(SuiteValidityFinding(
            rule_id=rule, policy_version=POLICY_VERSION, operation=operation,
            diagnostic=diagnostic, start_line=start_line, start_col=start_col,
            end_line=end_line, end_col=end_col, enclosing_test=reader.enclosing_test(node)))

    for node in _syntax.walk(reader.root):
        if reader.masked(node):
            continue
        found = _directive_finding(node, reader) or _code_finding(node, reader)
        if found is not None:
            add(node, *found)
    return tuple(findings)


def _directive_finding(node, reader: _Reader) -> Optional[Tuple[str, str, str]]:
    """The rule a preprocessor directive ``node`` breaks, with its operation
    and diagnostic: an include of the target's source, a macro named like
    one of its functions. None for another node."""
    if node.type == "preproc_include":
        path = node.child_by_field_name("path")
        value = _string(path) if path is not None else None
        if value is not None and reader.names_target(value):
            return ("copied_target", "#include",
                    f"includes the target's source {value}, so the test compiles its own copy")
    elif node.type in ("preproc_def", "preproc_function_def"):
        name = node.child_by_field_name("name")
        text = name.text.decode("utf-8", "replace") if name is not None else ""
        if text in reader.functions:
            return ("target_redefined", "#define",
                    f"defines a macro named like the target's function {text}")
    return None


def _code_finding(node, reader: _Reader) -> Optional[Tuple[str, str, str]]:
    """The rule a definition or call ``node`` breaks, with its operation and
    diagnostic: its own definition of a target's function, a call that opens
    the target's source. None for another node."""
    if node.type == "function_definition":
        name = _function_name(node)
        if name in reader.functions:
            return ("copied_target", "definition",
                    f"defines its own {name}, a function the target defines")
    elif node.type == "call_expression" and _call_name(node) in _OPENS:
        arguments = node.child_by_field_name("arguments")
        for inner in _syntax.walk(arguments) if arguments is not None else ():
            value = _string(inner)
            if value is not None and reader.names_target(value):
                return ("target_source_read", _call_name(node),
                        f"opens the target's source {value}")
    return None


def screen(source: str, identity: TargetIdentity) -> Tuple[SuiteAuditFlag, ...]:
    """The policy's review-only signals on ``source``."""
    problem = _syntax.first_error(source.encode("utf-8"), test_file=True)
    if problem is not None:
        return (SuiteAuditFlag("unparseable_source", problem.message, problem.line,
                               problem.column, problem.line, problem.column),)
    reader = _Reader(source, identity)
    flags: List[SuiteAuditFlag] = []
    for node in _syntax.walk(reader.root):
        if reader.masked(node):
            continue
        if node.type == "string_literal" and node.parent is not None and (
                node.parent.type != "preproc_include"):
            value = _string(node)
            if value is not None and reader.names_target(value):
                flags.append(SuiteAuditFlag("target_literal",
                                            f"a string names the target's source {value}",
                                            *_span(node)))
        elif node.type == "call_expression" and _call_name(node) in _DISCOVERY:
            flags.append(SuiteAuditFlag("source_discovery",
                                        f"{_call_name(node)} lists or walks directories",
                                        *_span(node)))
        elif node.type in ("preproc_def", "preproc_function_def"):
            name = node.child_by_field_name("name")
            text = name.text.decode("utf-8", "replace") if name is not None else ""
            if text in _KEYWORDS:
                flags.append(SuiteAuditFlag("keyword_override",
                                            f"a macro redefines the keyword {text}",
                                            *_span(node)))
    return tuple(flags)
