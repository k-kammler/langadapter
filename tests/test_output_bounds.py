"""The adapter returns the texts a consumer shows whole, bounded only
against memory exhaustion with the shared marker. The consumer decides how
much to show."""
import pytest

from langadapter.sources import SAFETY_CHARS, bounded, whole


def test_a_text_within_the_safety_bound_is_returned_whole():
    text = "x" * 600_000
    assert whole(text, "suite output") == text
    assert SAFETY_CHARS >= 4_000_000


def test_a_text_beyond_the_safety_bound_names_what_it_omits():
    text = "a" * 10 + "b" * 90
    cut = whole(text, "suite output", limit=30)
    assert cut == "a" * 10 + "\n[... 70 chars of suite output omitted ...]\n" + "b" * 20


def test_texts_no_model_reads_keep_their_bound():
    assert len(bounded("y" * 6000)) < 6000


def test_a_parse_rejection_names_the_offending_line_in_full():
    pytest.importorskip("tree_sitter")
    from langadapter import _treesitter
    from langadapter.java.source_analysis import GRAMMAR

    line = "    void f( " + "int a, " * 30 + "}"
    result = _treesitter.check_structure(GRAMMAR, "class X {\n" + line + "\n}\n")
    snippet = _treesitter.structure_message(result).split(": ", 1)[1]
    assert not result.ok and len(snippet) > 80 and "\n" not in snippet
    assert result.message == f"syntax error at line {result.line}:{result.col}"


def test_a_parse_rejection_returns_the_offending_text_whole():
    """The result carries the offending node's text whole, over several
    lines, for the consumer to cut."""
    pytest.importorskip("tree_sitter")
    from langadapter import _treesitter
    GRAMMAR = "tree_sitter_python"

    source = "x = (1,\n     2,\n"
    result = _treesitter.check_structure(GRAMMAR, source)
    assert not result.ok and "\n" in result.snippet
    assert result.snippet == result.snippet.strip() and result.snippet in source
    first = result.snippet.splitlines()[0]
    assert _treesitter.structure_message(result) == f"{result.message}: {first!r}"
    assert _treesitter.structure_message(_treesitter.check_structure(GRAMMAR, "x = 1\n")) == ""
