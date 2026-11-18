"""The guide's table texts and the hook reference, from the hook table, and
the command reference, from the command line.

docs/extending/add-a-language.md holds, for each step, a region between
``<!-- table:<anchor> -->`` and ``<!-- /table:<anchor> -->`` under a heading
``<title> {#<anchor>}``. This tool fills each region from the records of
``langadapter.hooks`` whose ``guide`` field names that anchor, checks that the
heading carries the table's title, and renders docs/reference/hooks.md from
every record. The check messages, the scaffold and the tutorials read the
same table, so the texts cannot drift apart. It renders
docs/reference/cli.md from ``langadapter.cli.parser``, the parser of the
``langadapter`` command. After changing the table or the command line,
rewrite the pages with

    python tools/guide_pages.py

``--check`` writes nothing and exits 1 when a page differs.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, Iterable, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
GUIDE_PAGE = ROOT / "docs" / "extending" / "add-a-language.md"
REFERENCE_PAGE = ROOT / "docs" / "reference" / "hooks.md"
COMMANDS_PAGE = ROOT / "docs" / "reference" / "cli.md"
GUIDE_NAME = "add-a-language.md"

_REGION = re.compile(r"(<!-- table:(?P<anchor>[a-z0-9-]+) -->\n)(?P<body>.*?)"
                     r"(<!-- /table:(?P=anchor) -->)", re.S)
_HEADING = re.compile(r"^#{2,3} (?P<title>.+?) \{#(?P<anchor>[a-z0-9-]+)\}$", re.M)


def _field(record, name: str) -> str:
    value = getattr(record, name, None)
    if value is None:
        return ""
    if isinstance(value, (tuple, list)):
        return ", ".join(str(item) for item in value)
    return str(value).strip()


def _anchor(record) -> str:
    """The guide anchor a record names, or "" when it names another page."""
    page, _, anchor = _field(record, "guide").partition("#")
    if page and page != GUIDE_NAME:
        return ""
    return anchor


def _by_anchor(records: Iterable) -> Dict[str, List]:
    grouped: Dict[str, List] = {}
    for record in records:
        anchor = _anchor(record)
        if anchor:
            grouped.setdefault(anchor, []).append(record)
    return grouped


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _written(record) -> str:
    """What a writer writes for a hook: its skeleton in the scaffold
    (``langadapter.cli._SKELETONS``) without docstrings and
    bodies, so the method names and the facts it sets show as the scaffold
    writes them. Empty for a record without a skeleton."""
    from langadapter.cli import _SKELETONS

    skeleton = _SKELETONS.get(_field(record, "name"))
    if not skeleton:
        return ""
    lines: List[str] = []
    in_docstring = False
    for line in skeleton.splitlines():
        stripped = line.strip()
        if in_docstring:
            in_docstring = not stripped.endswith('"""')
            continue
        if stripped.startswith('"""'):
            in_docstring = not (len(stripped) > 3 and stripped.endswith('"""'))
            continue
        if stripped:
            lines.append(line)
    return "\n".join(lines)


def _families(records: Sequence) -> List:
    return [record for record in records if _field(record, "kind") == "family"]


def _members(records: Sequence) -> List:
    return [record for record in records if _field(record, "kind") != "family"]


def render_region(records: Sequence) -> str:
    """The table texts of one step: the family's text first, then each fact
    or hook. Several facts form one table, a single fact or hook plain
    paragraphs."""
    lines: List[str] = []
    for family in _families(records):
        for label, name in (("It makes possible", "enables"), ("Useful for", "useful_for")):
            if _field(family, name):
                lines += [f"**{label}:** {_field(family, name)}", ""]
    members = _members(records)
    if len(members) > 1 and all(_field(record, "kind") == "fact" for record in members):
        return "\n".join(lines + _fact_table(members))
    for record in members:
        lines += _record_block(record)
    return "\n".join(lines)


def _fact_table(members: Sequence) -> List[str]:
    """Several facts of one step as one table, then the names the check
    counts as theirs and the step of the Toy that shows them."""
    lines = ["| Fact | What | Default |", "| --- | --- | --- |"]
    for record in members:
        lines.append(f"| `{_field(record, 'name')}` | {_cell(_field(record, 'what'))} "
                     f"| {_cell(_field(record, 'default'))} |")
    lines.append("")
    counted = [f"- `{_field(record, 'name')}`: {_counted(record)}"
               for record in members if check_names(record)]
    if counted:
        lines += [f"**{CHECK_NAMES} facts':**", "", *counted, ""]
    steps = sorted({_field(record, "step") for record in members if _field(record, "step")})
    if steps:
        lines += [f"**In the Toy:** {_toy_step(steps[0])}", ""]
    return lines


def _record_block(record) -> List[str]:
    """One fact's or hook's text: its fields, its Write block, the names the
    check counts as its own and the step of the Toy that shows it."""
    kind = _field(record, "kind") or "hook"
    lines = [f"`{_field(record, 'name')}` ({kind})", ""]
    for label, name in (("What", "what"), ("It makes possible", "enables"),
                        ("Default", "default"), ("Versions", "versions"),
                        ("Test it", "test")):
        if _field(record, name):
            lines += [f"**{label}:** {_field(record, name)}", ""]
        if name == "what" and _written(record):
            lines += ["**Write:**", "", "```python", _written(record), "```", ""]
        if name == "what" and check_names(record):
            lines += [f"**{CHECK_NAMES} {kind}'s:** {_counted(record)}", ""]
    if _field(record, "step"):
        lines += [f"**In the Toy:** {_toy_step(_field(record, 'step'))}", ""]
    return lines


def _toy_step(step: str) -> str:
    """Where the Toy example shows a record: its step of the tutorial."""
    number = step.split("_", 1)[0].lstrip("0") or "0"
    return (f"step {number} of `langadapter tutorial`, after which `toy.py` in the "
            "tutorial's directory holds it.")


def _expected_title(records: Sequence) -> str:
    """The heading a region's records name: its one fact or hook's title, or
    the family's when the region holds several or none."""
    members = _members(records)
    if len(members) == 1:
        return _field(members[0], "title")
    families = _families(records)
    return _field(families[0], "title") if families else ""


def rewrite_guide(text: str, records: Iterable) -> str:
    """``text`` with every table region filled. Raises ValueError when a
    record names an anchor the guide has no region for, a region has no
    record, or the heading above a region does not carry the table's title,
    so none of them can be forgotten."""
    grouped = _by_anchor(records)
    regions = {match.group("anchor") for match in _REGION.finditer(text)}
    unplaced = sorted(set(grouped) - regions)
    if unplaced:
        raise ValueError("the hook table names guide sections the guide has no region "
                         f"for: {', '.join(unplaced)}")
    empty = sorted(regions - set(grouped))
    if empty:
        raise ValueError("guide regions without a record in the hook table: "
                         f"{', '.join(empty)}")
    for match in _REGION.finditer(text):
        anchor = match.group("anchor")
        headings = list(_HEADING.finditer(text, 0, match.start()))
        heading = headings[-1] if headings else None
        expected = _expected_title(grouped[anchor])
        if heading is None or heading.group("anchor") != anchor or (
                expected and heading.group("title") != expected):
            raise ValueError(f"the heading above the region {anchor} must read "
                             f"'{expected} {{#{anchor}}}'")

    def fill(match: "re.Match[str]") -> str:
        body = render_region(grouped[match.group("anchor")])
        return match.group(1) + (body + "\n" if body else "") + match.group(4)

    return _REGION.sub(fill, text)


def written_names(record) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The methods and the attributes a hook's skeleton defines, in its
    order: what a writer writes for it. Empty for a record without one."""
    import ast

    from langadapter.cli import _SKELETONS

    skeleton = _SKELETONS.get(_field(record, "name"))
    if not skeleton:
        return (), ()
    methods: List[str] = []
    attributes: List[str] = []
    for node in ast.parse(skeleton).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(node.name)
        elif isinstance(node, ast.Assign):
            attributes += [target.id for target in node.targets if isinstance(target, ast.Name)]
    return tuple(methods), tuple(attributes)


def _listed(names: Sequence[str]) -> str:
    """``names`` as code, listed as a sentence lists them."""
    from langadapter._words import listed

    return listed([f"`{name}`" for name in names])


def writes_lines(record) -> List[str]:
    """What a hook's reference entry names for a writer: its methods and its
    attributes, each line empty when there are none."""
    methods, attributes = written_names(record)
    lines = []
    if methods:
        lines.append(f"Methods to write: {_listed(methods)}")
    if attributes:
        lines.append(f"Attributes to write: {_listed(attributes)}")
    if check_names(record):
        lines.append(f"{CHECK_NAMES} {_field(record, 'kind') or 'hook'}'s: {_counted(record)}")
    return lines


#: The label of the names the hook table lists for a record beyond its own
#: name and its Write block.
CHECK_NAMES = "The check counts these names as the"


def check_names(record) -> Tuple[str, ...]:
    """Every name the hook table lists for a fact or hook, when one of them
    is neither its own name nor defined in its Write block, else none. The
    check counts each as the record's: for a fact or a hook without
    operations, it reads the record present when a class defines one of them.
    A writer need not write them."""
    listed = tuple(getattr(record, "methods", None) or ())
    written = {name for part in written_names(record) for name in part}
    return listed if set(listed) - {_field(record, "name"), *written} else ()


#: Where the reference describes LangAdapter's own members, by the site's
#: address, since the copy of the pages installed with the package holds no
#: Core page.
CORE_PAGE = "https://k-kammler.github.io/langadapter/reference/core/"


def _counted(record) -> str:
    """The names the check counts as the record's, each that LangAdapter's
    base class defines linked to its reference entry."""
    from langadapter._words import listed
    from langadapter.adapter import LangAdapter

    return listed([f"[`{name}`]({CORE_PAGE}#langadapter.LangAdapter.{name})"
                   if hasattr(LangAdapter, name) else f"`{name}`" for name in check_names(record)])


def other_names(records: Sequence) -> List[str]:
    """Each fact or hook whose writer writes it under other names than its
    own, as "`hook` is written as the method `name`", in table order."""
    found = []
    for record in _members(records):
        methods, attributes = written_names(record)
        if not (methods or attributes):
            methods, attributes = _listed_names(record)
        if (methods or attributes) and _field(record, "name") not in (*methods, *attributes):
            parts = []
            if methods:
                parts.append(("the method " if len(methods) == 1 else "the methods ")
                             + _listed(methods))
            if attributes:
                parts.append(("the attribute " if len(attributes) == 1 else "the attributes ")
                             + _listed(attributes))
            found.append(f"`{_field(record, 'name')}` is written as {' with '.join(parts)}")
    return found


def _listed_names(record) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """The methods and the attributes a record names in its ``methods``
    field: an attribute where LangAdapter's base class holds a value under
    that name, else a method, as ``is_test_file`` is."""
    from langadapter.adapter import LangAdapter

    names = tuple(getattr(record, "methods", None) or ())
    attributes = tuple(name for name in names if hasattr(LangAdapter, name)
                       and not callable(getattr(LangAdapter, name)))
    return tuple(name for name in names if name not in attributes), attributes


_COUNTS = {1: "One", 2: "Two", 3: "Three", 4: "Four", 5: "Five", 6: "Six", 7: "Seven",
           8: "Eight", 9: "Nine", 10: "Ten"}


def _reference_opening(records: Sequence) -> List[str]:
    """The lines hooks.md opens with: where it comes from and the facts and
    hooks written under other names than their own."""
    others = other_names(records)
    return [
        "# Hooks and facts",
        "",
        "This page is generated from LangAdapter's hook table (`langadapter.hooks`).",
        "[Add a language](../extending/add-a-language.md) explains each step.",
        "",
        "A fact's or a hook's name is its label in the table, the check and the guides.",
        "Your adapter defines the methods or attributes its entry's Write block shows,",
        "and they usually carry the label as their name. "
        + _COUNTS.get(len(others), str(len(others)))
        + " carry another name:",
        "",
        *(f"- {item}" for item in others),
        "",
        "An entry's line \"The check counts these names\" also lists methods and",
        "attributes you need not write: for a fact or a hook without operations,",
        "the check reads it present when your class defines one of them.",
        "",
    ]


def render_reference(records: Sequence) -> str:
    """docs/reference/hooks.md: every family and its facts and hooks, in
    build-up order."""
    lines = _reference_opening(records)
    families = sorted(_families(records), key=lambda item: getattr(item, "order", 0) or 0)
    members = _members(records)
    for family in families:
        name = _field(family, "name")
        lines += [f"## {_field(family, 'title') or name}", "",
                  f"Name in a profile: `{name}`", ""]
        for label, field_name in (("It makes possible", "enables"),
                                  ("Useful for", "useful_for")):
            if _field(family, field_name):
                lines += [f"{label}: {_field(family, field_name)}", ""]
        for record in members:
            if _field(record, "family") != name:
                continue
            lines += [f"### `{_field(record, 'name')}`", ""]
            if _field(record, "title"):
                lines += [_field(record, "title"), ""]
            for label, field_name in (("Kind", "kind"), ("What", "what"),
                                      ("It makes possible", "enables"),
                                      ("Operations built on it", "operations"),
                                      ("Default", "default"), ("Versions", "versions"),
                                      ("Test it", "test")):
                if _field(record, field_name):
                    lines.append(f"- {label}: {_field(record, field_name)}")
            lines += [f"- {line}" for line in writes_lines(record)]
            anchor = _anchor(record)
            if anchor:
                lines.append(f"- Guide: [{_field(record, 'title') or anchor}]"
                             f"(../extending/{GUIDE_NAME}#{anchor})")
            if _written(record):
                lines += ["", "Write:", "", "```python", _written(record), "```"]
            lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _argument(action) -> str:
    """An argument as the command line takes it, such as ``--for ITEM``."""
    name = action.metavar or (action.dest if not action.option_strings else
                              action.dest.upper())
    if not action.option_strings:
        return {"?": f"[{name}]", "+": f"{name} [{name} ...]"}.get(action.nargs, name)
    return action.option_strings[-1] + ("" if action.nargs == 0 else f" {name}")


def _plain(text: str) -> str:
    """``text`` shown as written in a Markdown table cell or paragraph."""
    return _cell(re.sub(r"([\\`*_])", r"\\\1", text).replace("<", "&lt;"))


def render_commands(command_line) -> str:
    """docs/reference/cli.md: every command of ``command_line``, an
    argparse parser with subcommands, with its description, arguments and
    options, as ``--help`` prints them."""
    import argparse

    prog = command_line.prog
    commands = next(action for action in command_line._actions
                    if isinstance(action, argparse._SubParsersAction))
    lines = ["# Commands", "",
             f"This page is generated from `langadapter.cli.parser`, the parser of the `{prog}`",
             f"command. `{prog} <command> --help` prints the same texts.", "",
             "| Command | What |", "| --- | --- |"]
    lines += [f"| [`{prog} {choice.dest}`](#{choice.dest}) | {_plain(choice.help)} |"
              for choice in commands._choices_actions]
    for name, command in commands.choices.items():
        lines += ["", f"## `{prog} {name}` {{#{name}}}", "", _plain(command.description), "",
                  "| Argument | What |", "| --- | --- |"]
        for action in command._actions:
            if isinstance(action, argparse._HelpAction):
                continue
            lines.append(f"| `{_argument(action)}` | {_plain(action.help)} |")
        if command.epilog:
            lines += ["", _plain(command.epilog)]
    lines += ["", "## `python -m langadapter.hooks --json` {#hooks-json}", "",
              "This command belongs to the module `langadapter.hooks`, not to the "
              f"`{prog}` command above, and this page alone describes it. It "
              "prints the hook table as JSON: its schema version, every family with its "
              "title, order, what it makes possible, what it is useful for and its guide, "
              "and every fact and hook with its fields, the table "
              "[Hooks and facts](hooks.md) renders. Other arguments, `--help` among them, "
              "print the usage and exit with status 2."]
    return "\n".join(lines) + "\n"


def table_records() -> List:
    """Every family and every fact and hook of ``langadapter.hooks``, as the
    renderers read them: a family record has the kind ``family``."""
    from dataclasses import fields

    from langadapter import hooks

    families = [SimpleNamespace(kind="family", **{field.name: getattr(family, field.name)
                                                  for field in fields(family)})
                for family in hooks.families()]
    return families + list(hooks.table())


def main(argv: Sequence[str]) -> int:
    from langadapter.cli import parser

    records = table_records()
    pages = {GUIDE_PAGE: rewrite_guide(GUIDE_PAGE.read_text(encoding="utf-8"), records),
             REFERENCE_PAGE: render_reference(records),
             COMMANDS_PAGE: render_commands(parser())}
    if "--check" in argv:
        stale = [path.relative_to(ROOT).as_posix() for path, text in pages.items()
                 if not path.is_file() or path.read_text(encoding="utf-8") != text]
        for name in stale:
            print(f"{name} differs from its source: run python tools/guide_pages.py")
        return 1 if stale else 0
    for path, text in pages.items():
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
