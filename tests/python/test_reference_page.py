"""PythonAdapter's source operations live in its base class PythonSources,
whose module the reference does not render: the Python reference page lists
them as PythonAdapter's inherited members, and the list must name every
public member of PythonSources, so none drops off the page unnoticed."""
from __future__ import annotations

import re
from pathlib import Path

from langadapter.python._sources import PythonSources

_PAGE = Path(__file__).resolve().parents[2] / "docs" / "reference" / "python.md"


def _inherited_members() -> list[str]:
    lines = _PAGE.read_text(encoding="utf-8").splitlines()
    start = lines.index("::: langadapter.python.adapter")
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
    assert set(names) == {name for name in vars(PythonSources) if not name.startswith("_")}
