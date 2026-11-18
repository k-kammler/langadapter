"""The validity policy's rules for Python tests of a C extension, beside
Python's own.

Python's policy reads Python tests for the extension's module. It counts a
read of the target only for its exact path or a path from the module, and
reads no copy of a C file's text. A Python test of an extension may still
open the C file by a path that names it, or hold its text to build a copy,
and then it would not test the measured extension. Two rules add findings:

- ``target_source_read``: a call that reads a file (``open``, ``io.open``,
  ``codecs.open``, ``os.open``, ``read_text``, ``read_bytes``, a path's
  ``open``) holds a string naming the C file, by its file name or its path
  in the project.
- ``copied_target``: a string holds a top-level function of the C file, or
  200 characters or more of its text, whitespace aside.

A finding of a rule Python's policy already makes on the same code is not
repeated. ``VERSION_SUFFIX`` names these rules in the report's version,
after Python's.
"""
from __future__ import annotations

import ast
from pathlib import PurePosixPath
from typing import List, Optional, Set, Tuple

from langadapter.sources import SuiteValidityFinding, TargetIdentity

from langadapter.c import _syntax

#: The rules' part of the version of a report on Python tests of an extension.
VERSION_SUFFIX = "c-extension-v1"
#: The calls that read a file, by their name.
_READ_CALLS = frozenset({"open", "io.open", "codecs.open", "os.open"})
_READ_METHODS = frozenset({"read_text", "read_bytes", "open"})
#: The length of a verbatim piece of the C file a string may not hold.
_COPY_LENGTH = 200


def version(python_version: str) -> str:
    """The version of the policy for Python tests of an extension."""
    return f"{python_version}+{VERSION_SUFFIX}"


def overlaps(finding: SuiteValidityFinding, findings) -> bool:
    """Whether one of ``findings`` of ``finding``'s rule covers code it
    covers, so that Python's policy already reports it."""
    start, end = (finding.start_line, finding.start_col), (finding.end_line, finding.end_col)
    return any(other.rule_id == finding.rule_id
               and (other.start_line, other.start_col) < end
               and start < (other.end_line, other.end_col) for other in findings)


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _strings(node: ast.AST) -> List[str]:
    return [inner.value for inner in ast.walk(node)
            if isinstance(inner, ast.Constant) and isinstance(inner.value, str)]


def _normal(text: str) -> str:
    return " ".join(text.split())


class _Reader:

    def __init__(self, source: str, identity: TargetIdentity) -> None:
        self.tree = ast.parse(source)
        self.parents = {child: parent for parent in ast.walk(self.tree)
                        for child in ast.iter_child_nodes(parent)}
        path = PurePosixPath(identity.source_path)
        root = PurePosixPath(identity.project_root)
        self.needles = {path.name}
        if root in path.parents:
            self.needles.add(path.relative_to(root).as_posix())
        target = identity.target_source
        self.target = _normal(target)
        data = target.encode("utf-8")
        self.functions = {_normal(data[node.start_byte:node.end_byte].decode("utf-8", "replace"))
                          for _, node in _syntax.top_functions(data)}

    def names_target(self, value: str) -> bool:
        value = value.replace("\\", "/")
        return any(value == needle or value.endswith("/" + needle) for needle in self.needles)

    def copies_target(self, value: str) -> bool:
        text = _normal(value)
        if any(function and function in text for function in self.functions):
            return True
        return len(text) >= _COPY_LENGTH and text in self.target

    def enclosing_test(self, node: ast.AST) -> Optional[str]:
        names: List[str] = []
        test = None
        current = self.parents.get(node)
        while current is not None:
            if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
                test = current.name if current.name.startswith("test") else test
            elif isinstance(current, ast.ClassDef) and test is not None:
                names.append(current.name)
            current = self.parents.get(current)
        return ".".join([*reversed(names), test]) if test is not None else None


def analyze(source: str, identity: TargetIdentity,
            policy_version: str) -> Tuple[SuiteValidityFinding, ...]:
    """The two rules' findings on the Python test ``source``. It raises
    SyntaxError if ``source`` does not parse."""
    reader = _Reader(source, identity)
    findings: List[SuiteValidityFinding] = []
    seen: Set[Tuple[str, int, int]] = set()

    def add(node: ast.AST, rule: str, operation: str, diagnostic: str) -> None:
        key = (rule, node.lineno, node.col_offset + 1)
        if key in seen:
            return
        seen.add(key)
        findings.append(SuiteValidityFinding(
            rule_id=rule, policy_version=policy_version, operation=operation,
            diagnostic=diagnostic, start_line=node.lineno, start_col=node.col_offset + 1,
            end_line=node.end_lineno or node.lineno,
            end_col=(node.end_col_offset or node.col_offset) + 1,
            enclosing_test=reader.enclosing_test(node)))

    for node in ast.walk(reader.tree):
        if isinstance(node, ast.Call):
            called = _name(node.func)
            method = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            if called in _READ_CALLS or method in _READ_METHODS:
                named = next((value for value in _strings(node) if reader.names_target(value)),
                             None)
                if named is not None:
                    add(node, "target_source_read", method or called,
                        f"reads the extension's C source {named}")
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and reader.copies_target(node.value)):
            add(node, "copied_target", "string",
                "holds the extension's C source, as a copy to build or read")
    return tuple(findings)
