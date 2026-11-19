"""The Java adapter's validity policy for generated tests: a test must
exercise the target's behavior, not observe its implementation. A finding
names its test (``Class.method``, nested classes included), or none for a
declaration outside a test, which taints the whole source. The rules:
``target_source_read`` (a file named by the target's source path is read,
as ``Files.readString(Path.of("src/main/java/demo/Calc.java"))`` or a
``FileReader``, ``Scanner`` or ``getResource`` on it), ``target_class_read``
(the target's ``.class`` bytes are read through ``getResource``,
``getResourceAsStream`` or a file) and ``copied_target`` (a declared type
named like the target's class shadows it, so the tests would run a copy).

Review-only screening signals: ``target_literal`` (a string names the
target's source file or path), ``source_discovery`` (files or directories
are listed or walked), ``reflective_access`` (``setAccessible`` opens a
private member, which the rule allows), ``sizable_source_literal`` (a
string of 200 characters or more is compiled at run time through the
compiler API). A source that does not parse gets one
``unparseable_source`` flag.
"""
from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional, Set, Tuple

from langadapter import _treesitter
from langadapter.java._java_source import (
    GRAMMAR, TEST_ANNOTATIONS, _annotations, _text, declared_package,
)
from langadapter.java._env import JavaEnv
from langadapter.sources import SuiteAuditFlag, SuiteValidityFinding, TargetIdentity

from langadapter.java._policy import POLICY_VERSION  # the policy's version

#: Calls that read a file or resource a string literal argument names.
_READS = frozenset({
    "readString", "readAllLines", "readAllBytes", "lines", "newBufferedReader",
    "newInputStream", "getResource", "getResourceAsStream", "getSystemResource",
    "getSystemResourceAsStream",
})
#: Constructors that read a file named by their argument.
_READERS = frozenset({"FileReader", "FileInputStream", "Scanner", "File", "RandomAccessFile"})
#: java.nio.file.Files's methods that discover files or directories.
_DISCOVERY = frozenset({"walk", "list", "find", "newDirectoryStream", "walkFileTree"})


def _string(node: Any) -> Optional[str]:
    """The value of a string literal, escapes left as written."""
    if node.type != "string_literal":
        return None
    text = _text(node)
    if text.startswith('"""'):
        return text[3:-3]
    return text[1:-1]


def target_identity(path: Path, root: Path, *, executor=None) -> TargetIdentity:
    """A main source file's identity under ``root``, its module ``package.Class``."""
    files = JavaEnv(executor)
    if files.container:
        root, path = files._canonical_paths(root, path)
        relative = path.relative_to(root).as_posix()
        text = files.read(path)
    else:
        path, root = Path(path), Path(root)
        text = path.read_text(encoding="utf-8", errors="replace")
        relative = path.resolve().relative_to(root.resolve()).as_posix()
    package = declared_package(text)
    module = (package + "." if package else "") + path.stem
    return TargetIdentity(
        source_path=relative, project_root=str(root), module_name=module,
        language="java", policy_version=POLICY_VERSION,
        target_source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        target_source=text)


class _Reader:
    """One pass over a test source for a target."""

    def __init__(self, source: str, identity: TargetIdentity) -> None:
        self.root = _treesitter.parse(GRAMMAR, source).root_node
        self.identity = identity
        self.class_name = identity.module_name.rsplit(".", 1)[-1]
        name = PurePosixPath(identity.source_path).name
        self.needles = {name, identity.source_path,
                        identity.module_name.replace(".", "/") + ".java"}
        self.class_needles = {f"{self.class_name}.class",
                              identity.module_name.replace(".", "/") + ".class"}

    def enclosing_test(self, node: Any) -> Optional[str]:
        names: List[str] = []
        test = None
        current = node.parent
        while current is not None:
            if current.type == "method_declaration" and test is None:
                if TEST_ANNOTATIONS.intersection(_annotations(current)):
                    test = _treesitter.name(current)
            elif current.type in ("class_declaration", "interface_declaration",
                                  "enum_declaration", "record_declaration"):
                names.append(_treesitter.name(current))
            current = current.parent
        if test is None:
            return None
        return ".".join([*reversed(names), test])

    def names_target(self, node: Any, needles: Set[str]) -> bool:
        for inner in _treesitter.walk(node):
            value = _string(inner)
            if value is not None and any(n and value.endswith(n) for n in needles):
                return True
        return False


def _span(node: Any) -> Tuple[int, int, int, int]:
    return (node.start_point[0] + 1, node.start_point[1] + 1,
            node.end_point[0] + 1, node.end_point[1] + 1)


def analyze(source: str, identity: TargetIdentity) -> Tuple[SuiteValidityFinding, ...]:
    """The policy's findings on ``source``. SyntaxError if it does not parse."""
    reader = _Reader(source, identity)
    if reader.root.has_error:
        check = _treesitter.check_structure(GRAMMAR, source)
        raise SyntaxError(_treesitter.structure_message(check))
    findings: List[SuiteValidityFinding] = []
    seen: Set[Tuple[str, int, int]] = set()

    def add(node: Any, rule: str, operation: str, diagnostic: str) -> None:
        start_line, start_col, end_line, end_col = _span(node)
        if (rule, start_line, start_col) in seen:
            return
        seen.add((rule, start_line, start_col))
        findings.append(SuiteValidityFinding(
            rule_id=rule, policy_version=POLICY_VERSION, operation=operation,
            diagnostic=diagnostic, start_line=start_line, start_col=start_col,
            end_line=end_line, end_col=end_col,
            enclosing_test=reader.enclosing_test(node)))

    for node in _treesitter.walk(reader.root):
        if node.type in ("class_declaration", "interface_declaration",
                         "enum_declaration", "record_declaration") \
                and _treesitter.name(node) == reader.class_name:
            add(node, "copied_target", "declaration",
                f"declares a type named like the target class {reader.class_name}")
        if node.type == "method_invocation":
            label = node.child_by_field_name("name")
            arguments = node.child_by_field_name("arguments")
            if label is None or arguments is None or _text(label) not in _READS:
                continue
            if reader.names_target(arguments, reader.needles):
                add(node, "target_source_read", _text(label),
                    "reads the target's source file")
            elif reader.names_target(arguments, reader.class_needles):
                add(node, "target_class_read", _text(label),
                    "reads the target's compiled class")
        elif node.type == "object_creation_expression":
            kind = node.child_by_field_name("type")
            arguments = node.child_by_field_name("arguments")
            if kind is None or arguments is None:
                continue
            if _text(kind).rsplit(".", 1)[-1] not in _READERS:
                continue
            if reader.names_target(arguments, reader.needles):
                add(node, "target_source_read", f"new {_text(kind)}",
                    "reads the target's source file")
            elif reader.names_target(arguments, reader.class_needles):
                add(node, "target_class_read", f"new {_text(kind)}",
                    "reads the target's compiled class")
    return tuple(sorted(findings, key=lambda f: (f.start_line, f.start_col, f.rule_id)))


def screen(source: str, identity: TargetIdentity) -> Tuple[SuiteAuditFlag, ...]:
    """The review-only screening signals on ``source``."""
    reader = _Reader(source, identity)
    if reader.root.has_error:
        check = _treesitter.check_structure(GRAMMAR, source)
        return (SuiteAuditFlag("unparseable_source", _treesitter.structure_message(check),
                               check.line, check.col, check.line, check.col),)
    flags: List[SuiteAuditFlag] = []
    seen: Set[Tuple[str, int, int]] = set()

    def add(node: Any, rule: str, diagnostic: str) -> None:
        start_line, start_col, end_line, end_col = _span(node)
        if (rule, start_line, start_col) in seen:
            return
        seen.add((rule, start_line, start_col))
        flags.append(SuiteAuditFlag(rule, diagnostic, start_line, start_col,
                                    end_line, end_col))

    for node in _treesitter.walk(reader.root):
        value = _string(node)
        if value is not None and any(n and n in value for n in reader.needles):
            add(node, "target_literal", "literal names the selected target")
        if node.type != "method_invocation":
            continue
        label = node.child_by_field_name("name")
        name = _text(label) if label is not None else ""
        # Discovery is Files's methods, a File's listFiles and list on a File
        # the test creates, not that name on another object (a collector's find).
        receiver = node.child_by_field_name("object")
        owner = _text(receiver) if receiver is not None else ""
        if ((name in _DISCOVERY and owner in ("Files", "java.nio.file.Files"))
                or name == "listFiles"
                or (name == "list" and receiver is not None
                    and receiver.type == "object_creation_expression"
                    and owner.split("(", 1)[0].split()[-1] in ("File", "java.io.File"))):
            add(node, "source_discovery", "discovers files or directories")
        if name == "setAccessible":
            add(node, "reflective_access",
                "opens a private member with setAccessible (allowed, for review only)")
        if name in ("getTask", "run") and any(
                (_string(n) or "") and len(_string(n)) >= 200
                for n in _treesitter.walk(node)):
            add(node, "sizable_source_literal", "compiles a sizable source literal")
    return tuple(sorted(flags, key=lambda f: (f.start_line, f.start_col, f.rule_id)))
