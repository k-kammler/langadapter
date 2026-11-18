"""Two utest.h test files folded into one.

The first file stays as it is, without its ``UTEST_MAIN()``. After the
separator comment line come the second file's items, then one
``UTEST_MAIN()``. Of the second file's items, an ``#include`` the first
already makes and its ``UTEST_MAIN()`` go. A name both files declare at the
top level (a function, variable, type, tag, enumerator or macro, or a
fixture with its setup and teardown) is shared when each of the second
file's declarations of it has an identical one in the first, or only
declares it (a function's prototype or an ``extern`` declaration) while the
first file only declares it too. The identical declarations go, the others
stay. Any other such name is renamed throughout the second file
(``name_2``, or the next free number). A test whose suite and name the first
file has too gets the next free name. A file with its own ``main``
(``UTEST_STATE()``) is refused.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

from langadapter.treesitter import whole_lines

from langadapter.c import _syntax

#: A top-level macro statement that stands alone, from its text.
_STANDALONE = re.compile(rb"\s*(UTEST_MAIN|UTEST_STATE)\s*\(")
_WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]*")
#: The nodes whose text is not code: no rename reaches into them.
_LITERALS = frozenset({"string_literal", "char_literal", "comment", "system_lib_string",
                       "raw_string_literal"})
#: The kinds of names a declaration's declarator holds, below its wrappers.
_DECLARATORS = frozenset({"init_declarator", "pointer_declarator", "array_declarator",
                          "function_declarator", "parenthesized_declarator",
                          "attributed_declarator"})
_SPECIFIERS = frozenset({"struct_specifier", "union_specifier", "enum_specifier"})
_CONDITIONALS = frozenset({"preproc_if", "preproc_ifdef", "preproc_elif", "preproc_else",
                           "preproc_elifdef"})


@dataclass
class Item:
    """A top-level item of a test file: its kind (``include``, ``main``,
    ``test``, ``other``), its byte span, the names it declares, and for a
    utest.h macro block its head's span and the words after the macro's
    name (a test's suite and name, a fixture's name) with their spans.
    ``external`` is true when it only declares names defined elsewhere."""

    kind: str
    span: Tuple[int, int]
    names: Set[str] = field(default_factory=set)
    head: Optional[Tuple[int, int]] = None
    words: Tuple[Tuple[str, int, int], ...] = ()
    external: bool = False

    @property
    def test(self) -> Optional[Tuple[str, str]]:
        """A test's suite and name."""
        if self.kind != "test" or len(self.words) < 2:
            return None
        return self.words[0][0], self.words[1][0]


@dataclass(frozen=True)
class Merge:
    """A merged source, or why the files do not merge. It also holds the
    renames and the shared names."""

    source: Optional[str]
    problem: str = ""
    renamed: Tuple[Tuple[str, str], ...] = ()
    shared: Tuple[str, ...] = ()


def items(data: bytes) -> Tuple[List[Item], object]:
    """The top-level items of a test file that parses, and its masked tree."""
    units = {unit.span[0]: unit for unit in _syntax.macro_units(data)}
    tree = _syntax.parse(_syntax.masked(data))
    children = tree.root_node.children
    found: List[Item] = []
    index = 0
    while index < len(children):
        node = children[index]
        start, end = node.start_byte, node.end_byte
        if (node.type in _SPECIFIERS and index + 1 < len(children)
                and children[index + 1].type == ";"):
            index += 1
            end = children[index].end_byte
        index += 1
        if node.type == ";":
            continue
        found.append(_item(node, data, start, end, units.get(start)))
        if node.type == "function_definition" and _defines_main(node):
            found[-1].kind = "main"
    return found, tree


def _item(node, data: bytes, start: int, end: int, unit) -> Item:
    """The item of the top-level ``node`` from ``start`` to ``end``, with
    ``unit`` when it is a utest.h macro block."""
    if node.type == "preproc_include":
        return Item("include", (start, end))
    if _STANDALONE.match(data[start:end]):
        return Item("main", (start, end))
    if unit is not None:
        words = tuple((m.group().decode(), m.start(), m.end())
                      for m in _WORD.finditer(data, *unit.head))[1:]
        test = unit.macro in _syntax.TEST_MACROS
        return Item("test" if test else "other", (start, unit.span[1]),
                    set() if test else {words[0][0]} if words else set(),
                    unit.head, words)
    return Item("other", (start, end), set(declared(node)), external=_external(node))


def _defines_main(node) -> bool:
    return "main" in declared(node)


def declared(node) -> Iterable[str]:
    """The names a top-level ``node`` declares."""
    kind = node.type
    if kind in ("preproc_def", "preproc_function_def"):
        name = node.child_by_field_name("name")
        if name is not None:
            yield _text(name)
        return
    if kind in _CONDITIONALS:
        for child in node.named_children:
            yield from declared(child)
        return
    if kind in _SPECIFIERS:
        yield from _tags(node)
        return
    if kind == "function_definition":
        head = node.child_by_field_name("type")
        if head is not None and _text(head) in _syntax.FIXTURE_MACROS:
            # A fixture's setup or teardown, as UTEST_F_SETUP(name) reads.
            yield from (_text(n) for n in _syntax.walk(node.child_by_field_name("declarator"))
                        if n.type == "identifier")
            return
    if kind in ("declaration", "type_definition", "function_definition"):
        for child in node.named_children:
            if child.type in _SPECIFIERS:
                yield from _tags(child)
        for declarator in node.children_by_field_name("declarator"):
            yield from _declarator_names(declarator)


def _external(node) -> bool:
    """Whether a top-level ``node`` only declares names defined elsewhere:
    functions' prototypes or ``extern`` declarations, neither ``static`` nor
    initialized."""
    if node.type != "declaration":
        return False
    classes = {_text(child) for child in node.children
               if child.type == "storage_class_specifier"}
    declarators = node.children_by_field_name("declarator")
    return "static" not in classes and bool(declarators) and all(
        declarator.type != "init_declarator" and ("extern" in classes or _function(declarator))
        for declarator in declarators)


def _function(node) -> bool:
    """Whether a declarator declares a function: the declarator nearest its
    name is a function's (``int *f(void)``, not ``int (*f)(void)``)."""
    nearest = None
    while node is not None and node.type in _DECLARATORS:
        if node.type not in ("parenthesized_declarator", "attributed_declarator"):
            nearest = node.type
        inner = node.child_by_field_name("declarator")
        node = inner if inner is not None else next(iter(node.named_children), None)
    return nearest == "function_declarator"


def _tags(node) -> Iterable[str]:
    """A struct, union or enum specifier's tag, when it has a body, and an
    enum's enumerators."""
    name = node.child_by_field_name("name")
    body = node.child_by_field_name("body")
    if name is not None and body is not None:
        yield _text(name)
    if node.type == "enum_specifier" and body is not None:
        for enumerator in body.named_children:
            if enumerator.type == "enumerator":
                yield _text(enumerator.child_by_field_name("name"))


def _declarator_names(node) -> Iterable[str]:
    if node.type in ("identifier", "type_identifier"):
        yield _text(node)
        return
    if node.type in _DECLARATORS:
        inner = node.child_by_field_name("declarator")
        for child in [inner] if inner is not None else node.named_children:
            yield from _declarator_names(child)


def _text(node) -> str:
    return node.text.decode("utf-8", "replace")


def merge(first: str, second: str, separator: str) -> Merge:
    """``second`` folded into ``first`` after the ``separator`` line. Both
    parse as test files."""
    one, two = first.encode("utf-8"), second.encode("utf-8")
    first_items, _ = items(one)
    second_items, tree = items(two)
    if any(item.kind == "main" and not _is_main_macro(data, item)
           for data, found in ((one, first_items), (two, second_items)) for item in found):
        return Merge(None, "a test file with its own main (UTEST_STATE()) does not merge")
    facts = _first_file(one, first_items)
    taken = {m.group().decode() for m in _WORD.finditer(one + b"\n" + two)}
    shared, renames = _clashes(second_items, two, facts, taken)
    drops, edits = _second_edits(second_items, two, facts, shared, renames, taken)
    heads = [item.head for item in second_items if item.head is not None]
    edits.extend(_renames(two, tree, renames, heads))
    # What the drops leave of the second file, with its blank lines folded.
    rest = re.sub(rb"\n(?:[ \t]*\n)+", b"\n\n", _applied(two, drops, edits))
    head = _applied(one, [whole_lines(one, *item.span) for item in first_items
                          if item.kind == "main"], [])
    mains = any(item.kind == "main" for item in first_items + second_items)
    merged = (head.rstrip().decode("utf-8") + "\n\n" + separator + "\n"
              + rest.strip(b"\n").decode("utf-8") + "\n"
              + ("\nUTEST_MAIN()\n" if mains else ""))
    return Merge(merged, renamed=tuple(sorted(renames.items())), shared=tuple(shared))


@dataclass(frozen=True)
class _FirstFile:
    """What a merge needs of the first file: its items' normalized texts,
    its includes' texts, the names it declares and those it defines, and
    its tests' suites and names."""

    texts: Set[bytes]
    includes: Set[bytes]
    names: Set[str]
    defined: Set[str]
    tests: Set[Tuple[str, str]]


def _first_file(data: bytes, found: List[Item]) -> _FirstFile:
    """The facts of the first file ``data`` with its items ``found``."""
    texts = {_normal(data[slice(*item.span)]) for item in found}
    includes = {text for item, text in ((i, _normal(data[slice(*i.span)])) for i in found)
                if item.kind == "include"}
    return _FirstFile(texts, includes, set().union(*(item.names for item in found)),
                      set().union(*(item.names for item in found if not item.external)),
                      {item.test for item in found if item.test is not None})


def _clashes(second_items: List[Item], data: bytes, first: _FirstFile,
             taken: Set[str]) -> Tuple[List[str], Dict[str, str]]:
    """The names the second file (``data``) shares with the first, and a
    fresh name for each other name both declare, taken from ``taken``."""
    declaring: Dict[str, List[Item]] = {}
    for item in second_items:
        for name in item.names & first.names:
            declaring.setdefault(name, []).append(item)
    shared = _shared(declaring, data, first.texts, first.defined)
    renames: Dict[str, str] = {}
    for name in sorted(set(declaring) - set(shared)):
        renames[name] = _fresh(name, taken)
    return shared, renames


def _second_edits(second_items: List[Item], data: bytes, first: _FirstFile,
                  shared: List[str], renames: Dict[str, str], taken: Set[str]
                  ) -> Tuple[List[Tuple[int, int]], List[Tuple[int, int, bytes]]]:
    """What the merge drops of the second file (``data``): its includes and
    shared items the first file holds, and its main, and the edits of its
    macro heads' words."""
    tests = set(first.tests)
    drops: List[Tuple[int, int]] = []
    edits: List[Tuple[int, int, bytes]] = []
    for item in second_items:
        text = _normal(data[slice(*item.span)])
        if (item.kind == "include" and text in first.includes) or item.kind == "main" or (
                item.names and item.names <= set(shared) and text in first.texts):
            drops.append(whole_lines(data, *item.span))
            continue
        # A macro head reads masked in the tree: its words are renamed here,
        # and a test's name when the first file has its suite and name.
        final = [renames.get(word, word) for word, _, _ in item.words]
        if item.test is not None:
            if (final[0], final[1]) in tests:
                final[1] = _fresh(item.test[1], taken)
            tests.add((final[0], final[1]))
        edits.extend((start, end, text.encode())
                     for (word, start, end), text in zip(item.words, final) if text != word)
    return drops, edits


def _shared(declaring: Dict[str, List[Item]], data: bytes, texts: Set[bytes],
            defined: Set[str]) -> List[str]:
    """The names the second file (``data``) need not rename: each of its
    ``declaring`` items is identical to one of the first file's ``texts``,
    or only declares a name the first file does not define."""
    return sorted(name for name, found in declaring.items()
                  if all(_normal(data[slice(*i.span)]) in texts
                         or (i.external and name not in defined) for i in found))


def _is_main_macro(data: bytes, item: Item) -> bool:
    match = _STANDALONE.match(data[slice(*item.span)])
    return match is not None and match.group(1) == b"UTEST_MAIN"


def _normal(text: bytes) -> bytes:
    """An item's text for comparison: whitespace runs as one space."""
    return b" ".join(text.split())


def _fresh(name: str, taken: Set[str]) -> str:
    number = 2
    while f"{name}_{number}" in taken:
        number += 1
    fresh = f"{name}_{number}"
    taken.add(fresh)
    return fresh


def _renames(data: bytes, tree, renames: Dict[str, str],
             heads: List[Tuple[int, int]]) -> List[Tuple[int, int, bytes]]:
    """The edits that rename each name of ``renames`` in ``data``: its
    identifiers in code and its words in macro bodies, not those of test
    heads (their masked text is not the file's), strings or comments."""
    if not renames:
        return []
    edits = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        if node.type in _LITERALS:
            continue
        if node.type in ("identifier", "type_identifier"):
            name = _text(node)
            if name in renames and not any(s <= node.start_byte < e for s, e in heads):
                edits.append((node.start_byte, node.end_byte, renames[name].encode()))
            continue
        if node.type == "preproc_arg":
            for match in _WORD.finditer(data, node.start_byte, node.end_byte):
                name = match.group().decode()
                if name in renames:
                    edits.append((match.start(), match.end(), renames[name].encode()))
            continue
        stack.extend(node.children)
    return edits


def _applied(data: bytes, drops: List[Tuple[int, int]],
             edits: List[Tuple[int, int, bytes]]) -> bytes:
    """``data`` without the ``drops`` spans and with the ``edits`` outside them."""
    changes = [(start, end, b"") for start, end in drops]
    changes += [edit for edit in edits
                if not any(s <= edit[0] < e for s, e in drops)]
    out = data
    for start, end, text in sorted(set(changes), reverse=True):
        out = out[:start] + text + out[end:]
    return out
