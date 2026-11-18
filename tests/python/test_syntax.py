"""A source with a NUL byte is a syntax error on every supported Python:
3.12 raises SyntaxError, 3.10 and 3.11 a ValueError from ast.parse and
compile. The fixture gives the library's process the older parser."""
import ast
import builtins
import sys

import pytest

import langadapter as lb
from langadapter.errors import SourceReadError
from langadapter.python import _syntax, source_analysis, _sources as _treesitter
from langadapter.python._sources import prune_cases
from langadapter.python._validity import screen_python_generated_test_source
from langadapter.python.adapter import PythonAdapter

NUL = chr(0)
SOURCE = "def test_a():\n    assert True\n# a NUL byte: " + NUL + "\n"
OTHER = "def test_b():\n    assert True\n"


@pytest.fixture
def older_parser(monkeypatch):
    """``ast.parse`` and ``compile`` as Python 3.10 and 3.11 have them."""
    def refusing_nul(function):
        def call(source, *args, **kwargs):
            if isinstance(source, str) and NUL in source:
                raise ValueError("source code string cannot contain null bytes")
            return function(source, *args, **kwargs)
        return call

    monkeypatch.setattr(ast, "parse", refusing_nul(ast.parse))
    monkeypatch.setattr(builtins, "compile", refusing_nul(builtins.compile))


def test_the_parse_helper_raises_a_syntax_error(older_parser):
    with pytest.raises(SyntaxError, match="null bytes"):
        _syntax.parse(SOURCE)
    with pytest.raises(SyntaxError, match="null bytes"):
        _syntax.compile_module(SOURCE, "<test>")
    assert isinstance(_syntax.parse(OTHER), ast.Module)


def test_static_reads_report_the_source_as_unparsable(older_parser):
    removal = source_analysis.strip_module_level_skips(SOURCE)
    assert (removal.count, removal.parsed) == (0, False) and "null bytes" in removal.error
    normalization = source_analysis.normalize_shadowed_definitions(SOURCE)
    assert normalization.parse_ok is False
    resolution = source_analysis.resolve_collisions([SOURCE, OTHER])
    assert resolution.ejected == (0,)
    with pytest.raises(SyntaxError):
        prune_cases(SOURCE, {"test_a": {"1"}})
    flags = screen_python_generated_test_source(SOURCE, None)
    assert [flag.rule_id for flag in flags] == ["unparseable_source"]


def test_the_merge_and_the_advisory_read_report_the_source_as_unparsable(
        older_parser, tmp_path):
    assert _treesitter.merge_python_modules(OTHER, SOURCE, sentinel="# merged") is None
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    with pytest.raises(SourceReadError):
        adapter._order_dependent_mock_warnings(SOURCE)


def test_the_target_parser_reports_the_source_as_a_syntax_error(tmp_path):
    """The project's interpreter parses the source in its own process,
    whichever supported Python it is."""
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    validation = adapter._validate_test_source(SOURCE, str(tmp_path))
    assert validation.status == "syntax_error" and "null bytes" in validation.message
