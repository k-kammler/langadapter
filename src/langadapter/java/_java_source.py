"""What the JVM tools read of a Java source file: its text and the package
it declares, as Java reads its declaration, and the helpers that read a
parsed declaration: a node's text and its annotations, and the annotations
that mark a test."""
from __future__ import annotations

from pathlib import Path
from typing import Any, List
from langadapter import _treesitter


#: The grammar the declaration is read with.
GRAMMAR = "tree_sitter_java"


def read_source(path: Any) -> str:
    """A Java file's text: UTF-8 (a byte-order mark aside), else Latin-1,
    which reads every byte, as a package or a class name needs."""
    data = Path(path).read_bytes()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def declared_package(source: str) -> str:
    """The package ``source`` declares, read as Java does (after a
    byte-order mark, comments and annotations), or "" for the default."""
    root = _treesitter.parse(GRAMMAR, source.lstrip("\ufeff")).root_node
    declaration = next((c for c in root.named_children
                        if c.type == "package_declaration"), None)
    if declaration is None:
        return ""
    name = next((c for c in declaration.named_children
                 if c.type in ("scoped_identifier", "identifier")), None)
    if name is None:
        return ""
    return "".join(name.text.decode("utf-8", "replace").split())


#: The annotations that mark a JUnit 4 to 6 test.
TEST_ANNOTATIONS = frozenset({
    "Test", "ParameterizedTest", "RepeatedTest", "TestFactory",
    "TestTemplate",
})


def _text(node: Any) -> str:
    return node.text.decode("utf-8", "replace")


def _annotations(node: Any) -> List[str]:
    """The simple names of a declaration's annotations."""
    names = []
    for child in node.children:
        if child.type != "modifiers":
            continue
        for annotation in child.named_children:
            if annotation.type in ("marker_annotation", "annotation"):
                name = annotation.child_by_field_name("name")
                if name is not None:
                    names.append(_text(name).rsplit(".", 1)[-1])
    return names
