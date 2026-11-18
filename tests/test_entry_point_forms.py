"""Every entry point LangAdapter declares has the form pip's
strict entry-point reader accepts (pkg_resources, used on Python 3.10),
since pip there refuses to install any other form."""
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
#: pkg_resources.EntryPoint.pattern, the strict reader.
STRICT = re.compile(r"(?P<name>.+?)\s*=\s*(?P<module>[\w.]+)\s*(:\s*(?P<attr>[\w.]+))?"
                    r"\s*(?P<extras>\[.*\])?\s*$")
LINE = re.compile(r'^\s*(\w[\w.-]*)\s*=\s*"([^"]+)"\s*$')


def _entry_points(text):
    """The ``name = value`` lines of every entry-point table in ``text``."""
    found, inside = [], False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[project.entry-points.") or stripped == "[project.scripts]":
            inside = True
            continue
        match = LINE.match(line) if inside else None
        if match:
            found.append(f"{match[1]} = {match[2]}")
        else:
            inside = False
    return found


def _file(*parts):
    path = ROOT.joinpath(*parts)
    if not path.is_file():
        pytest.skip(f"{'/'.join(parts)} is in the source tree only")
    return path.read_text(encoding="utf-8")


def test_every_entry_point_langadapter_declares_passes_the_strict_reader():
    found = _entry_points(_file("pyproject.toml"))
    assert found
    for line in found:
        assert STRICT.match(line), line
