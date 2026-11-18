"""Public static operations retain native observations without project execution."""
from __future__ import annotations

import pytest

from langadapter.errors import InvalidRequest
from langadapter.python import source_analysis
from langadapter.sources import CollisionResolution, RemovedBinding, SuiteNormalization
from langadapter.python.source_analysis import salvage_syntax
from langadapter.sources import SourceValidation


def _sources():
    return [
        f"def helper():\n    return {i}\n"
        "class TestGroup:\n"
        "    def test_value(self, request):\n"
        f"        assert request.getfixturevalue('helper') == {i}\n"
        for i in range(2)
    ]


def test_lexical_code_lines_do_not_require_a_parsed_source():
    assert source_analysis.count_code_lines("# note\ndef test_value(:\n    invalid\n\n") == 2


def test_module_skip_removal_keeps_guards_inside_tests():
    source = ("import pytest\npytest.importorskip('missing')\n"
              "pytest.skip('reason', allow_module_level=True)\n"
              "def test_a():\n    pytest.importorskip('optional')\n")
    result = source_analysis.strip_module_level_skips(source)
    assert result.parsed and result.count == 2
    assert "missing" not in result.source and "reason" not in result.source
    assert "optional" in result.source
    assert source_analysis.strip_module_level_skips(result.source).count == 0


def test_module_skip_removal_reports_unparseable_input():
    source = "def test_value(:\n"
    result = source_analysis.strip_module_level_skips(source)
    assert not result.parsed and result.error
    assert result.source == source and result.count == 0


def test_normalization_retains_removal_spans_and_rule():
    source = ("class TestGroup:\n"
              "    @pytest.mark.slow\n"
              "    def test_value(self):\n"
              "        assert False\n"
              "    def test_value(self):\n"
              "        assert True\n")
    result = source_analysis.normalize_shadowed_definitions(source)
    assert isinstance(result, SuiteNormalization)
    assert result.parse_ok and result.changed
    assert result.removed_bindings == (
        RemovedBinding(("TestGroup",), "test_value", "function", 2, 4, 5, 6),)
    assert result.rule_version and result.before_sha256 != result.after_sha256
    assert "assert False" not in result.source


@pytest.mark.parametrize("source,valid", [("def test_value():\n    pass\n", True),
                                         ("def test_value(:\n", False)])
def test_normalization_noop_or_error_never_loses_input(source, valid):
    result = source_analysis.normalize_shadowed_definitions(source)
    assert result.parse_ok is valid
    assert not result.changed
    assert result.source == source
    assert result.removed_bindings == ()
    assert result.before_sha256 == result.after_sha256
    assert bool(result.parse_error) is not valid


def test_no_preference_leaves_unrenamable_conflicts_undecided():
    sources = _sources()
    result = source_analysis.resolve_collisions(sources)
    assert isinstance(result, CollisionResolution)
    assert result.conflicts == (("helper", (0, 1)),)
    assert result.choices == ()
    assert all("helper" not in dropped for dropped in result.dropped_defs)
    assert sources == _sources()


@pytest.mark.parametrize("owner", [0, 1])
def test_explicit_preference_retains_native_choice_and_binding_drops(owner):
    calls = []

    def preference(name, owners):
        calls.append((name, owners))
        return owner

    result = source_analysis.resolve_collisions(_sources(), preference=preference)
    assert calls == [("helper", (0, 1))]
    assert not result.conflicts and not result.ejected
    assert result.choices == (("helper", owner, (1 - owner,)),)
    assert "helper" in result.dropped_defs[1 - owner]
    assert "helper" not in result.dropped_defs[owner]


def test_invalid_preference_owner_is_a_request_error():
    with pytest.raises(InvalidRequest, match="not an owner"):
        source_analysis.resolve_collisions(_sources(), preference=lambda *_: 2)


@pytest.mark.parametrize(("body", "scope"), [
    ("@pytest.mark.slow\nclass TestGroup:\n"
     "    def test_value(self, request):\n"
     "        assert request.getfixturevalue('helper')\n", "TestGroup"),
    ("class TestGroup:\n"
     "    class TestInner:\n"
     "        def test_value(self, request):\n"
     "            assert request.getfixturevalue('helper')\n", "TestGroup.TestInner"),
    ("class TestGroup:\n"
     "    if True:\n"
     "        @pytest.mark.slow\n"
     "        class TestInner:\n"
     "            def test_value(self, request):\n"
     "                assert request.getfixturevalue('helper')\n", "TestGroup.TestInner"),
    ("class Base:\n"
     "    def test_value(self):\n"
     "        assert True\n"
     "class TestGroup(Base):\n"
     "    def dependency(self, request):\n"
     "        return request.getfixturevalue('helper')\n", "TestGroup"),
])
def test_dependents_include_only_supplied_class_method_ids(body, scope):
    source = "import pytest\ndef helper():\n    return 1\n" + body
    dependent = f"package.test_candidate.{scope}.test_value"
    names = [dependent, "package.test_candidate.TestOther.test_value",
             f"package.test_candidate.{scope}Extra.test_value"]
    assert source_analysis.collision_dependents(source, "helper", names) == (dependent,)
    assert source_analysis.collision_dependents(source, "absent", names) == ()
    assert source_analysis.collision_dependents(source, "helper", ()) == ()


def test_invalid_source_is_not_a_successful_empty_dependency_result():
    with pytest.raises(InvalidRequest, match="syntax error"):
        source_analysis.collision_dependents("def test_value(:\n", "helper", ["test_a"])


def test_static_operations_start_no_tools(monkeypatch):
    import subprocess

    def refuse(*args, **kwargs):
        pytest.fail("static source analysis started a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    source_analysis.normalize_shadowed_definitions(_sources()[0])
    source_analysis.collision_dependents(_sources()[0], "helper", ["TestGroup.test_value"])
    source_analysis.resolve_collisions(_sources(), localize_autouse=True,
                                       preference=lambda _name, owners: owners[0])
    assert source_analysis.strip_module_level_skips(
        "pytest.importorskip('missing')\n").count == 1
    result = source_analysis.merge_sources(
        "import os\ndef test_a():\n    assert True\n",
        "from __future__ import annotations\nimport os\ndef test_b():\n    assert True\n",
        separator="# merged")
    assert result.applied
    assert result.source.startswith("from __future__ import annotations\nimport os\n")
    assert result.source.count("import os") == 1
    assert "# merged" in result.source and "def test_b" in result.source


def test_failed_static_merge_retains_the_first_source():
    source = "def test_a():\n    pass\n"
    result = source_analysis.merge_sources(source, "def test_b(:\n", separator="# merged")
    assert not result.applied
    assert result.status == "parse_error" and result.source == source


def test_static_merge_reports_a_rejected_merged_module_as_refused(monkeypatch):
    """A merged module the calling process's compiler rejects is refused, as
    the adapter's merge reports it, not a parse error of the inputs."""
    source = "def test_a():\n    pass\n"
    monkeypatch.setattr(source_analysis._treesitter, "merge_python_modules",
                        lambda first, second, sentinel: None)
    result = source_analysis.merge_sources(source, "def test_b():\n    pass\n",
                                           separator="# merged")
    assert result.status == "refused" and result.source == source
    assert "merged module" in result.detail


def test_static_merge_refuses_an_executable_separator():
    with pytest.raises(InvalidRequest, match="comment line"):
        source_analysis.merge_sources("pass\n", "pass\n", separator="print('extra')")


#: A suite and the agent's edit of it share every module-level statement.
SHARED = (
    "import copy\nfrom typing import TYPE_CHECKING\n\n"
    "if TYPE_CHECKING:\n    from decimal import Decimal\n\n"
    "try:\n    import numpy as np\nexcept ImportError:\n    np = None\n\n"
    "def test_a():\n    assert True\n"
)


def test_a_statement_both_sources_hold_identically_is_merged_once():
    """A module-level statement that only binds names, held identically by
    both sources, appears once, as an import does."""
    second = SHARED.replace("def test_a", "def test_b")
    result = source_analysis.merge_sources(SHARED, second, separator="# merged")
    assert result.applied
    assert result.source.count("if TYPE_CHECKING:") == 1
    assert result.source.count("except ImportError:") == 1
    assert "def test_a" in result.source and "def test_b" in result.source
    compile(result.source, "<merged>", "exec")


@pytest.mark.parametrize("between,guards,fallbacks", [
    ("Decimal = int\n", 2, 1),                      # rebinds what a copy binds
    ("np = None\n", 1, 2),
    ("TYPE_CHECKING = True\n", 2, 1),               # rebinds what a copy reads
    ("np.seterr = None\n", 1, 2),                   # changes what a copy binds
    ("copy.copy(np)\n", 1, 2),                      # passes it to a call
    ("print('between')\n", 2, 2),                   # a builtin runs code
    ("def setup():\n    pass\n\nsetup()\n", 2, 2),  # the module's own code runs
])
def test_a_shared_statement_stays_twice_when_one_between_can_change_it(
        between, guards, fallbacks):
    """Where a statement between the two copies rebinds or changes what the
    copy binds or reads, or can, both copies stay."""
    second = SHARED.replace("def test_a", "def test_b")
    result = source_analysis.merge_sources(SHARED + "\n" + between, second,
                                           separator="# merged")
    assert result.applied
    assert result.source.count("if TYPE_CHECKING:") == guards
    assert result.source.count("except ImportError:") == fallbacks


@pytest.mark.parametrize("statement", [
    "sys.path.insert(0, '.')",   # a call adds an entry again
    "CACHE = {}",                # a new object each time: a statement between
    "PARTS = sys.argv[:]",       # may hold the first one
    "if sys.flags.debug:\n    LEVEL = sys.maxsize + 1",
])
def test_a_shared_statement_that_can_differ_when_run_again_stays_twice(statement):
    """A statement whose second run can change state or bind a new object
    stays twice."""
    first = f"import sys\n\n{statement}\n\ndef test_a():\n    assert True\n"
    second = first.replace("def test_a", "def test_b")
    result = source_analysis.merge_sources(first, second, separator="# merged")
    assert result.source.count(statement) == 2


def test_a_shared_definition_collision_resolution_drops_leaves_no_blank_line_run():
    """A definition both suites hold is kept once: the copy dropped from the
    second suite goes with the blank lines after it, so the merged suite
    keeps one gap between statements, not the dropped ones' too."""
    first = ("import pytest\n\nLIMIT = 3\n\n\ndef test_a():\n    assert True\n\n\n"
             "def test_b():\n    assert LIMIT\n")
    second = first + "\n\ndef test_c():\n    assert True\n"
    resolution = source_analysis.resolve_collisions([first, second])
    assert resolution.deduped[1]
    kept = resolution.sources[1]
    assert "\n\n\n\n" not in kept, kept
    merged = source_analysis.merge_sources(*resolution.sources, separator="# merged").source
    assert "\n\n\n\n" not in merged, merged
    assert merged.count("def test_c") == 1 and merged.count("LIMIT = 3") == 1


def test_a_removed_copy_takes_its_line_comment_and_adds_no_blank_lines():
    first = ("import sys\n\nif sys.platform == 'win32':  # Windows\n    SEP = '\\\\'\n\n\n"
             "def test_a():\n    assert True\n")
    second = first.replace("def test_a", "def test_b")
    merged = source_analysis.merge_sources(first, second, separator="# merged").source
    assert merged.count("# Windows") == 1
    assert "# merged\n\n\ndef test_b" in merged



def test_normalization_consumes_the_hidden_definition_comment():
    source = (
        "# module header\n\n"
        "# rationale for the hidden definition\n"
        "def test_value():\n    assert False\n\n"
        "# rationale for the retained definition\n"
        "def test_value():\n    assert True\n"
    )

    normalized = source_analysis.normalize_shadowed_definitions(source)

    assert normalized.changed
    assert "module header" in normalized.source
    assert "hidden definition" not in normalized.source
    assert "retained definition" in normalized.source
    assert "assert False" not in normalized.source
    assert "assert True" in normalized.source


SHADOWED = ("x = 1  #{c}\n\n\n"
            "def test_value():\n    assert False  # {c}\n\n\n"
            "def test_other():\n    assert '{c}'\n\n\n"
            "def test_value():\n    assert True\n")


@pytest.mark.parametrize("char", ["\x0b", "\x0c", "\x1c", "\x85", "\u2028"])
def test_normalization_numbers_lines_as_python_does(char):
    # str.splitlines ends a line at these characters, Python source does not.
    plain = source_analysis.normalize_shadowed_definitions(SHADOWED.format(c=""))
    normalized = source_analysis.normalize_shadowed_definitions(
        SHADOWED.format(c=char))

    assert normalized.removed_bindings == plain.removed_bindings
    assert normalized.source == (
        "x = 1  #{c}\n\n\n\n\n"
        "def test_other():\n    assert '{c}'\n\n\n"
        "def test_value():\n    assert True\n").format(c=char)


def test_normalization_keeps_a_form_feed_line_as_written():
    source = "\x0c\n" + SHADOWED.format(c="")
    normalized = source_analysis.normalize_shadowed_definitions(source)
    plain = source_analysis.normalize_shadowed_definitions("\n" + SHADOWED.format(c=""))
    assert normalized.source == "\x0c" + plain.source


def test_module_skip_removal_after_a_form_feed_removes_the_guard():
    source = ("\x0c\nimport pytest\npytest.importorskip('missing')\n\n\n"
              "def test_a():\n    assert True\n")
    result = source_analysis.strip_module_level_skips(source)
    assert result.count == 1
    assert result.source == ("\x0c\nimport pytest\n\n\n"
                             "def test_a():\n    assert True\n")


# Repair uses the supplied validator and keeps unresolved execution distinct.
def verdict(status, message="", line=0):
    return SourceValidation(status, message, line, 0, "target compiler")


def test_repair_uses_only_the_supplied_validator(monkeypatch):
    import subprocess

    def refuse(*args, **kwargs):
        pytest.fail("repair started an unrequested process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    calls = []

    def validate(source):
        calls.append(source)
        return verdict("valid") if len(calls) == 2 else verdict(
            "syntax_error", "expected an indented block after function on line 1", 3)

    result = salvage_syntax("def helper():\n\ndef test_a():\n    pass\n", validate)
    assert result.source == calls[-1]
    assert "def helper():\n    pass\n" in result.source
    assert result.status == "repaired" and result.repairs == 1
    assert result.validation.valid


@pytest.mark.parametrize("status", ["timeout", "infrastructure_failure", "validator_error"])
def test_validator_execution_failure_stops_without_blanking_source(status):
    calls = []

    def validate(source):
        calls.append(source)
        return verdict(status, "compiler could not run", 1)

    source = "def test_a():\n    pass\n"
    result = salvage_syntax(source, validate)
    assert calls == [source]
    assert result.source is None and result.status == "validator_failed"
    assert result.repairs == 0 and result.validation.status == status


def test_repair_stops_at_the_existing_round_limit():
    calls = []

    def validate(source):
        calls.append(source)
        return verdict("syntax_error", "unmatched brace", len(calls))

    result = salvage_syntax("}\n" * 60, validate)
    assert len(calls) == 25
    assert result.source is None and result.status == "beyond_repair"
    assert result.repairs == 24


def test_valid_source_is_returned_byte_for_byte():
    source = "# comment\r\ndef test_a():\r\n    pass\r\n"
    result = salvage_syntax(source, lambda _: verdict("valid"))
    assert result.source == source and result.status == "valid"


def test_repair_blanks_the_line_the_compiler_numbered():
    # A form feed in a comment is no line end for the compiler, so line 3 is
    # the closing parenthesis.
    calls = []

    def validate(source):
        calls.append(source)
        return verdict("valid") if len(calls) == 2 else verdict(
            "syntax_error", "unmatched ')'", 3)

    result = salvage_syntax("# page\x0cbreak\nx = 1\n)\n", validate)
    assert result.status == "repaired"
    assert result.source == "# page\x0cbreak\nx = 1\n\n"
