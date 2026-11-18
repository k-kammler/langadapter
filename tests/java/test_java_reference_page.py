"""JavaAdapter's source operations live in its base class JavaSources,
whose module the reference does not render: the Java reference page lists
them as JavaAdapter's inherited members, and the list must name every
public member of JavaSources, so none drops off the page unnoticed."""
from __future__ import annotations

import re
from pathlib import Path

from langadapter.java._sources import JavaSources

_PAGE = Path(__file__).resolve().parents[2] / "docs" / "reference" / "java.md"


def _inherited_members() -> list[str]:
    lines = _PAGE.read_text(encoding="utf-8").splitlines()
    start = lines.index("::: langadapter.java.adapter")
    names = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        found = re.fullmatch(r"\s+- (\w+)", line)
        if found:
            names.append(found.group(1))
    return names


def test_the_page_lists_every_public_source_operation_of_the_base_class():
    names = _inherited_members()
    assert len(names) == len(set(names))
    assert set(names) == {name for name in vars(JavaSources) if not name.startswith("_")}
