"""Source operations of the Python adapter say whether they applied, and tell
a source that matched nothing from one that does not parse, one the project
interpreter rejects, and a validator that could not run."""
import sys
import hashlib
import base64
from subprocess import CompletedProcess

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.python.adapter import PythonAdapter
from langadapter.sources import (
    APPLIED, INFRASTRUCTURE_FAILURE, NOT_REPAIRED, PARSE_ERROR, REFUSED,
    UNCHANGED,
)
from langadapter.suites import TestId
from langadapter.sources import PoolMember, StagedPool
from langadapter.python._profile import PythonProfile, library_parser

SUITE = ("import pytest\n\n\n"
         "def test_a():\n    assert 1\n\n\n"
         "@pytest.mark.parametrize('x', [1, 2])\n"
         "def test_b(x):\n    assert x\n\n\n"
         "class TestC:\n"
         "    def test_m(self):\n        assert True\n\n"
         "    def test_n(self):\n        assert True\n")
BROKEN = "def test_a(:\n    pass\n"
# tree-sitter parses it, but CPython's compiler does not.
REJECTED = "return 1\n\n\ndef test_a():\n    assert 1\n"


@pytest.fixture
def adapter(tmp_path):
    return PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))


def test_read_only_analysis_starts_no_project_tools(adapter, monkeypatch):
    import subprocess

    def refuse(*args, **kwargs):
        pytest.fail("read-only analysis started a process")

    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert adapter.is_test_file("conftest.py")
    assert not adapter.is_test_file("module.py")
    assert "TestC.test_m" in adapter.skeleton(SUITE)
    assert adapter.same_skeleton(SUITE, SUITE) == (True, "")
    assert adapter.function_fingerprints(SUITE)
    assert adapter.optimization_metrics(SUITE)["cyclomatic_sum"] > 0
    assert adapter.imports(SUITE) == ("pytest",)
    assert adapter.import_path_candidates("package.module") == (
        "package/module.py", "package/module/__init__.py")
    # A relative name has no path without its importer's package.
    assert [adapter.import_path_candidates(name) for name in (".", "..pkg", ".submod")] == [
        (), (), ()]
    assert adapter.duplicate_test_names(SUITE + SUITE)
    assert adapter.merge_collisions(SUITE, SUITE) == ("TestC", "test_a", "test_b")
    assert adapter.order_dependent_mock_warnings(SUITE) == []


def test_invalid_source_keeps_structural_counts_unknown(adapter):
    result = adapter.test_metrics(BROKEN)
    assert not result.parsed and result.detail
    assert result.code_lines is None
    assert result.tests is None and result.assertions is None
    assert result.fixtures is None and result.parametrized_cases is None


def test_pruning_reports_what_matched_and_what_did_not(adapter):
    result = adapter.prune_tests(SUITE, [
        TestId("test_s.py", "test_b", "1"), "TestC.test_m", "test_gone"])
    assert result.status == APPLIED and result.applied
    assert result.removed == ("TestC.test_m", "test_b[1]")
    assert result.unmatched == ("test_gone",)
    assert "@pytest.mark.parametrize('x', [2])" in result.source
    assert "def test_m" not in result.source and "def test_n" in result.source
    assert result.parser.startswith("tree-sitter")


CASES = ("import pytest\n\n\n"
         "@pytest.mark.parametrize('x', [1, 2, 'a b', None])\n"
         "def test_values(x):\n    assert x != 2\n\n\n"
         "@pytest.mark.parametrize('x, y', [(1, 2), (3, 4), pytest.param(5, 6, id='five')])\n"
         "def test_pairs(x, y):\n    assert x < y\n\n\n"
         "@pytest.mark.parametrize('x', [[1], [2]], ids=['one', 'two'])\n"
         "def test_named(x):\n    assert x\n\n\n"
         "@pytest.mark.parametrize('x', [{'k': 1}, {'k': 2}])\n"
         "def test_dicts(x):\n    assert x\n")


def _collected(tmp_path, source):
    """The ids pytest collects from ``source``, by test."""
    import subprocess

    module = tmp_path / "test_cases.py"
    module.write_text(source, encoding="utf-8")
    done = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q",
                           "-p", "no:cacheprovider", str(module)],
                          capture_output=True, text=True, cwd=tmp_path)
    found = {}
    for line in done.stdout.splitlines():
        if "::" in line:
            name = line.split("::", 1)[1]
            function, _, rest = name.partition("[")
            found.setdefault(function, []).append(rest[:-1])
    return found


def test_a_parametrized_test_loses_only_its_named_cases(adapter, tmp_path):
    """Removing cases of a test with a literal parameter list removes only
    those, with the ids pytest's own collection gives them."""
    assert _collected(tmp_path, CASES) == {
        "test_values": ["1", "2", "a b", "None"], "test_pairs": ["1-2", "3-4", "five"],
        "test_named": ["one", "two"], "test_dicts": ["x0", "x1"]}
    result = adapter.prune_tests(CASES, [
        TestId("test_cases.py", "test_values", "2"), "test_values[a b]",
        TestId("test_cases.py", "test_pairs", "five"), "test_cases.test_named[one]",
        TestId("test_cases.py", "test_dicts", "x1")])
    assert result.status == APPLIED, result.detail
    assert result.removed == ("test_values[2]", "test_values[a b]", "test_pairs[five]",
                              "test_named[one]", "test_dicts[x1]")
    assert result.unmatched == () and result.detail == ""
    assert _collected(tmp_path, result.source) == {
        "test_values": ["1", "None"], "test_pairs": ["1-2", "3-4"],
        "test_named": ["two"], "test_dicts": ["x0"]}


@pytest.mark.parametrize(("source", "reason"), [
    ("import pytest\n\nVALUES = [1, 2]\n\n\n"
     "@pytest.mark.parametrize('x', VALUES)\ndef test_x(x):\n    assert x\n",
     "its parameter list is not literal"),
    ("import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2])\n"
     "@pytest.mark.parametrize('y', [3, 4])\ndef test_x(x, y):\n    assert x\n",
     "its cases combine several parameter lists"),
    ("import pytest\n\n\n@pytest.mark.parametrize('x', ['a\\\\b', 'c'])\n"
     "def test_x(x):\n    assert x\n",
     "a case's id cannot be read from its values"),
    ("import pytest\n\n\n@pytest.mark.parametrize('x', [1, 2])\n"
     "def test_x(x):\n    assert x\n",
     "every case is named"),
    ("import pytest\n\n\n@pytest.mark.parametrize('x', [{'k': 1}, {'k': 2}])\n"
     "def test_x(x):\n    assert x\n",
     "removing a case would change the ids of the cases after it"),
], ids=["not-literal", "stacked", "escaped", "every-case", "positional"])
def test_a_case_that_cannot_go_alone_removes_its_test_and_says_why(adapter, source, reason):
    names = ["test_x[1]", "test_x[2]"] if reason == "every case is named" else ["test_x[1]"]
    if "VALUES" not in source and "'c'" in source:
        names = ["test_x[c]"]
    if "parametrize('y'" in source:
        names = ["test_x[3-1]"]
    if "'k'" in source:
        names = ["test_x[x0]"]
    result = adapter.prune_tests(source, names)
    assert result.status == APPLIED and "def test_x" not in result.source
    assert result.removed == ("test_x",)
    assert result.detail == f"test_x is removed whole: {reason}"


#: A kept test whose string holds three blank lines in a row.
KEPT_BLANKS = 'def test_kept():\n    assert """a\n\n\n\nb""".count("\\n") == 4\n'


@pytest.mark.parametrize("end", ["\n", "\r\n"])
def test_pruning_collapses_only_the_blank_lines_at_its_cut(adapter, end):
    """At most two blank lines stay where a test was cut, in a CRLF file too,
    and a kept test's string keeps its three."""
    kept = "def test_a():\n    assert True\n\n\n" + KEPT_BLANKS
    source = kept.replace(KEPT_BLANKS, "def test_gone():\n    assert True\n\n\n" + KEPT_BLANKS)
    result = adapter.prune_tests(source.replace("\n", end), ["test_gone"])
    assert result.removed == ("test_gone",)
    assert result.source == kept.replace("\n", end)


def test_pruning_an_identifier_that_matches_nothing(adapter):
    result = adapter.prune_tests(SUITE, ["test_gone"])
    assert result.status == UNCHANGED and not result.applied
    assert result.source == SUITE and result.unmatched == ("test_gone",)


def test_pruning_an_unparsable_source_reports_the_parse_failure(adapter):
    result = adapter.prune_tests(BROKEN, ["test_a"])
    assert result.status == PARSE_ERROR
    assert result.source == BROKEN and "line 1" in result.detail


def test_pruning_resolves_dotted_ids_to_the_longest_definition_name(adapter):
    source = ("def test_same():\n    assert True\n\n"
              "class TestC:\n"
              "    def test_same(self):\n        assert False\n\n"
              "    def test_kept(self):\n        assert True\n")
    result = adapter.prune_tests(source, [
        "pkg.test_s.TestC.test_same", "test_s.TestC.test_same", "not_test_same"])
    assert result.status == APPLIED
    assert result.removed == ("TestC.test_same",)
    assert result.unmatched == ("not_test_same",)
    assert "def test_same():" in result.source
    assert "def test_same(self):" not in result.source
    assert "def test_kept(self):" in result.source


def test_pruning_matches_only_definitions_it_actually_removes(adapter):
    result = adapter.prune_tests(SUITE, ["TestC", "pkg.test_s.test_b"])
    assert result.removed == ("test_b",)
    assert result.unmatched == ("TestC",)
    assert "class TestC:" in result.source
    assert "def test_b" not in result.source


def test_pruning_after_a_form_feed_removes_the_definition_comment(adapter):
    # A form feed is no line end in Python source: the comment stays test_x's.
    source = ("\x0c\nimport os\n# about test_x\ndef test_x():\n    assert os\n\n\n"
              "def test_y():\n    assert True\n")
    result = adapter.prune_tests(source, ["test_x"])
    assert result.status == APPLIED
    assert "about test_x" not in result.source
    assert result.source == "\x0c" + adapter.prune_tests(source[1:], ["test_x"]).source


def test_merging_names_the_source_that_does_not_parse(adapter):
    result = adapter.merge_sources(SUITE, BROKEN, separator="# merged")
    assert result.status == PARSE_ERROR
    assert result.detail.startswith("the second source does not parse")
    merged = adapter.merge_sources(
        "import os\n\n\ndef test_x():\n    assert os\n",
        "import os\nimport sys\n\n\ndef test_y():\n    assert sys\n",
        separator="# from the second suite")
    assert merged.status == APPLIED
    assert merged.source.startswith("import os\nimport sys\n")
    assert "# from the second suite" in merged.source
    with pytest.raises(InvalidRequest) as refused:
        adapter.merge_sources(SUITE, SUITE, separator="not a comment")
    assert refused.value.field == "separator"
    # A carriage return ends a line in Python source: what follows it would
    # be a statement of the merged module.
    with pytest.raises(InvalidRequest) as refused:
        adapter.merge_sources(SUITE, SUITE, separator="# merged\rimport os")
    assert refused.value.field == "separator"


def test_rewriting_refuses_what_the_project_interpreter_rejects(adapter):
    merged = adapter.merge_sources(SUITE, REJECTED, separator="# merged")
    assert merged.status == REFUSED
    assert "the second source" in merged.detail
    normalized = adapter.normalize_shadowed(REJECTED)
    assert normalized.status == REFUSED and normalized.source == REJECTED


def test_normalization_keeps_the_last_definition(adapter):
    shadowed = ("def test_a():\n    assert 0\n\n\n"
                "def test_a():\n    assert 1\n")
    result = adapter.normalize_shadowed(shadowed)
    assert result.status == APPLIED and result.removed == ("test_a",)
    assert result.source.count("def test_a") == 1
    assert "assert 1" in result.source
    assert adapter.normalize_shadowed(SUITE).status == UNCHANGED


def test_metrics_of_an_unparsable_source_have_no_counts(adapter):
    metrics = adapter.test_metrics(BROKEN)
    assert not metrics.parsed and metrics.tests is None
    assert metrics.code_lines is None and metrics.detail
    counted = adapter.test_metrics(SUITE)
    assert (counted.tests, counted.parametrized_cases) == (4, 2)


COUNTED = ("import pytest\nfrom pytest import mark\n\nVALUES = [1, 2, 3]\n\n\n"
           "@pytest.mark.parametrize('x', VALUES)\ndef test_named(x):\n    assert x\n\n\n"
           "@pytest.mark.parametrize('x', argvalues=[1, 2])\ndef test_keyword(x):\n    assert x\n\n\n"
           "@mark.parametrize('x', [1, 2])\ndef test_mark(x):\n    assert x\n\n\n"
           "@pytest.mark.parametrize('x', [1, 2])\n@pytest.mark.parametrize('y', [3, 4, 5])\n"
           "def test_stacked(x, y):\n    assert x\n")


def test_the_parametrized_cases_are_the_cases_pytest_runs(adapter, tmp_path):
    """The cases counted are those pytest collects: a list given by name, as
    argvalues or under mark.parametrize counts its cases, and stacked lists
    their product (3 + 2 + 2 + 6)."""
    assert len(_collected(tmp_path, COUNTED)["test_stacked"]) == 6
    assert sum(len(ids) for ids in _collected(tmp_path, COUNTED).values()) == 13
    assert adapter.test_metrics(COUNTED).parametrized_cases == 13


def test_validation_and_a_validator_that_cannot_run(adapter, tmp_path):
    verdict = adapter.validate_source(SUITE)
    assert verdict.valid
    assert verdict.source_sha256 == hashlib.sha256(SUITE.encode()).hexdigest()
    rejected = adapter.validate_source(REJECTED)
    assert rejected.status == "syntax_error" and rejected.line == 1
    not_python = tmp_path / "not_python.txt"
    not_python.write_text("text\n", encoding="utf-8")
    broken = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=str(not_python))))
    verdict = broken.validate_source(SUITE)
    assert verdict.status == INFRASTRUCTURE_FAILURE and not verdict.valid
    refused = broken.normalize_shadowed(SUITE)
    assert refused.status == INFRASTRUCTURE_FAILURE


def test_finalizing_removes_detached_comment_blocks_only(adapter):
    commented = ("def test_a():\n    assert 1\n\n\n"
                 "# a block left behind by a removed test\n"
                 "# second line\n\n\n"
                 "def test_b():\n    assert 1\n")
    result = adapter.finalize_source(commented)
    assert result.status == APPLIED
    assert "left behind" not in result.source
    assert adapter.finalize_source(SUITE).status == UNCHANGED
    assert adapter.finalize_source(BROKEN).status == PARSE_ERROR


def test_the_structural_check_names_the_first_error(adapter):
    check = adapter.check_structure(BROKEN)
    assert not check.ok and check.line == 1
    # The snippet is the error's text for the consumer to cut: none here.
    assert check.message == f"missing token at line 1:{check.column}" and check.snippet == ""
    broken = adapter.check_structure("x = (1,\n     2,\n")
    assert broken.message == "syntax error at line 1:1" and broken.snippet == "x = (1,\n     2,"
    assert adapter.check_structure(SUITE).ok
    assert adapter.check_structure(SUITE).snippet == ""


# A fixture named by a string cannot be renamed: when two sources bind it
# differently, only a preference decides which one is dropped.
FIXTURE_A = ("import pytest\n\n\n"
             "@pytest.fixture\n"
             "def db():\n    return 1\n\n\n"
             "@pytest.mark.usefixtures('db')\n"
             "def test_a():\n    assert True\n")
FIXTURE_B = FIXTURE_A.replace("return 1", "return 2").replace(
    "test_a", "test_b")


def test_an_unrenamable_conflict_without_a_preference_drops_nothing(
        adapter):
    result = adapter.resolve_collisions([FIXTURE_A, FIXTURE_B]).value
    assert result.conflicts == (("db", (0, 1)),)
    assert result.sources == (FIXTURE_A, FIXTURE_B)
    assert result.dropped_defs == ((), ()) and result.choices == ()
    assert result.ejected == ()


def test_a_preference_decides_and_every_drop_is_reported(adapter):
    asked = []

    def keep_the_second(name, owners):
        asked.append((name, owners))
        return owners[-1]

    result = adapter.resolve_collisions([FIXTURE_A, FIXTURE_B],
                                        preference=keep_the_second).value
    assert asked == [("db", (0, 1))]
    assert result.choices == (("db", 1, (0,)),)
    assert {"db", "test_a"} <= set(result.dropped_defs[0])
    assert "def db" not in result.sources[0]
    assert "def test_a" not in result.sources[0]
    assert result.sources[1] == FIXTURE_B and result.ejected == result.conflicts == ()
    with pytest.raises(InvalidRequest) as refused:
        adapter.resolve_collisions([FIXTURE_A, FIXTURE_B],
                                   preference=lambda name, owners: 7)
    assert refused.value.field == "preference"


def test_renamable_definitions_are_renamed_or_shared(adapter):
    first = ("HELPER = 1\n\n\ndef shared():\n    return 0\n\n\n"
             "def test_a():\n    assert HELPER == 1 and shared() == 0\n")
    second = ("HELPER = 2\n\n\ndef shared():\n    return 0\n\n\n"
              "def test_b():\n    assert HELPER == 2 and shared() == 0\n")
    result = adapter.resolve_collisions([first, second]).value
    assert result.renames == ({}, {"HELPER": "HELPER_2"})
    assert result.deduped == ((), ("shared",))
    assert result.conflicts == () and result.ejected == ()
    assert "HELPER_2 == 2" in result.sources[1]


def test_a_source_the_project_interpreter_rejects_is_left_out(adapter):
    result = adapter.resolve_collisions([SUITE, REJECTED]).value
    ((index, reason),) = zip(result.ejected, result.eject_reasons)
    assert index == 1 and "rejects" in reason
    assert result.sources == (SUITE, REJECTED)


def _not_python(tmp_path):
    not_python = tmp_path / "not_python.txt"
    not_python.write_text("text\n", encoding="utf-8")
    return PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=str(not_python))))


def test_salvage_tells_a_repair_from_a_failure_to_repair(adapter, tmp_path):
    assert adapter.salvage_syntax(SUITE).status == UNCHANGED
    glitched = "def test_a():\n    assert 1\n\nx = = 1\n"
    repaired = adapter.salvage_syntax(glitched)
    assert repaired.status == APPLIED and repaired.detail == "1 repair(s)"
    assert "x = = 1" not in repaired.source
    hopeless = "x = = 1\n" * 30
    beyond = adapter.salvage_syntax(hopeless)
    assert beyond.status == NOT_REPAIRED and beyond.source == hopeless
    # The last validation sees 24 repairs. The loop discards its 25th,
    # which it never validates.
    assert beyond.detail.startswith("still does not compile after 24")
    # A validator that crashes is no verdict on the source.
    crashed = _not_python(tmp_path).salvage_syntax(glitched)
    assert crashed.status == INFRASTRUCTURE_FAILURE


def test_a_failed_import_probe_is_reported(adapter, tmp_path):
    imports = ("import os\nimport no_such_module_xyz\n"
               "from os import path, no_such_name\n\n\n"
               "def test_a():\n    assert path and os\n")
    pruned = adapter.prune_unresolvable_imports(imports)
    assert pruned.status == APPLIED
    assert "no_such_module_xyz" not in pruned.source
    assert "no_such_name" not in pruned.source and "from os import path" \
        in pruned.source
    assert adapter.prune_unresolvable_imports(
        "def test_a():\n    assert 1\n").status == UNCHANGED
    # The probe runs with the interpreter's flags, the validation without.
    failing_probe = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable),
        interpreter_flags=("-c", "import sys; sys.exit(3)")))
    result = failing_probe.prune_unresolvable_imports(imports)
    assert result.status == INFRASTRUCTURE_FAILURE
    assert result.source == imports and result.removed == ()


def test_collection_breakers_are_pruned_or_the_failure_is_named(adapter):
    breaks = ("import pytest\n\n"
              "BROKEN = 1 / 0\n\n\n"
              "def test_a():\n    assert 1\n")
    pruned = adapter.prune_collection_breakers(breaks, timeout_s=120)
    assert pruned.status == APPLIED and pruned.removed == ("line 3-3",)
    assert "1 / 0" not in pruned.source
    assert adapter.prune_collection_breakers(
        SUITE, timeout_s=120).status == UNCHANGED
    slow = "import time\n\ntime.sleep(30)\n\n\ndef test_a():\n    pass\n"
    timed_out = adapter.prune_collection_breakers(slow, timeout_s=3)
    assert timed_out.status == INFRASTRUCTURE_FAILURE
    assert "timeout of 3 s" in timed_out.detail


def test_a_fractional_timeout_reaches_the_collection_as_given(adapter, monkeypatch):
    """The collection round runs in the request's timeout as given, its
    fraction of a second included."""
    executor = adapter._executor()
    timeouts = []

    class Recorded:
        def __getattr__(self, name):
            return getattr(executor, name)

        def run(self, command, **kwargs):
            if "--collect-only" in command:
                timeouts.append(kwargs["timeout"])
            return executor.run(command, **kwargs)

    monkeypatch.setattr(adapter, "_executor", lambda: Recorded())
    adapter.prune_collection_breakers(SUITE, timeout_s=30.5)
    assert timeouts == [30.5]


def test_two_same_size_rounds_in_one_second_each_collect_their_own_text(adapter, monkeypatch):
    """Two collection breakers of four bytes: each blanking to pass keeps the
    candidate's size, and its time is held, so a rewrite pytest cached in an
    earlier round would collect that round's code again. Each round collects
    its own text, and both breakers go."""
    import os
    from pathlib import Path

    write = Path.write_text

    def held(path, text, *args, **kwargs):
        written = write(path, text, *args, **kwargs)
        if path.name == "candidate.py":
            os.utime(path, (1_700_000_000, 1_700_000_000))
        return written

    monkeypatch.setattr(Path, "write_text", held)
    pruned = adapter.prune_collection_breakers("f(0)\ng(0)\n\n\ndef test_a():\n    assert 1\n",
                                               timeout_s=120)
    assert pruned.status == APPLIED, pruned.detail
    assert pruned.removed == ("line 1-1", "line 2-2")


def test_the_import_probe_writes_no_cache_into_the_project(adapter, tmp_path):
    (tmp_path / "helpers.py").write_text("VALUE = 1\n", encoding="utf-8")
    pruned = adapter.prune_unresolvable_imports(
        "from helpers import VALUE, MISSING\n\n\ndef test_a():\n    assert VALUE\n")
    assert pruned.status == APPLIED and "MISSING" not in pruned.source
    assert not list(tmp_path.rglob("__pycache__"))


def test_collection_pruning_that_cannot_start_keeps_the_input(adapter, monkeypatch):
    def cannot_start(*args, **kwargs):
        raise FileNotFoundError("pytest interpreter absent")

    monkeypatch.setattr(adapter, "_prune_collection_breakers", cannot_start)
    source = "BROKEN = 1 / 0\ndef test_a(): pass\n"
    result = adapter.prune_collection_breakers(source, timeout_s=60)
    assert result.status == INFRASTRUCTURE_FAILURE
    assert result.source == source and not result.removed
    assert "could not execute" in result.detail


def test_collection_pruning_without_a_verdict_keeps_the_source(adapter):
    source = "import os\nos._exit(3)\ndef test_a(): pass\n"
    result = adapter.prune_collection_breakers(source, timeout_s=60)
    assert result.status == INFRASTRUCTURE_FAILURE
    assert result.source == source and not result.removed
    assert "exited with 3" in result.detail


def test_collection_crash_trace_is_not_a_rewrite_location(adapter, monkeypatch):
    from subprocess import CompletedProcess

    original = adapter._executor()
    collections = []

    class Executor:
        def run(self, command, **kwargs):
            if "--collect-only" not in command:
                return original.run(command, **kwargs)
            collections.append(command)
            return CompletedProcess(command, -11 if len(collections) == 1 else 0,
                                    "candidate.py:1: process crashed", "")

    monkeypatch.setattr(adapter, "_executor", lambda: Executor())
    source = "VALUE = 1\ndef test_a(): assert VALUE\n"
    result = adapter.prune_collection_breakers(source, timeout_s=60)
    assert result.status == INFRASTRUCTURE_FAILURE
    assert result.source == source and not result.removed
    assert len(collections) == 1


def test_module_level_skips_are_removed(adapter):
    guarded = ("import pytest\n\n"
               "pytest.importorskip('no_such_module_xyz')\n\n\n"
               "def test_a():\n    assert 1\n")
    stripped = adapter.strip_module_level_skips(guarded)
    assert stripped.status == APPLIED and stripped.detail == "1 guard(s) removed"
    assert "importorskip" not in stripped.source
    assert adapter.strip_module_level_skips(SUITE).status == UNCHANGED
    assert adapter.strip_module_level_skips(REJECTED).status == REFUSED


def test_the_validity_policy_tells_nothing_found_from_no_verdict(
        adapter, tmp_path):
    (tmp_path / "sign.py").write_text(
        "def sign(x):\n    return 1 if x > 0 else -1\n", encoding="utf-8")
    target = adapter.target_identity("sign.py")
    assert (target.module_name, target.language) == ("sign", "python")
    clean = "from sign import sign\n\n\ndef test_a():\n    assert sign(1) == 1\n"
    analysis = adapter.analyze_validity(clean, target)
    assert (analysis.status, analysis.items) == ("completed", ())
    assert analysis.policy_version == target.policy_version
    assert analysis.policy_version == adapter.validity_policy_version
    reads_the_path = ("import sign\n\n\n"
                      "def test_path():\n    assert sign.__file__\n")
    (finding,) = adapter.analyze_validity(reads_the_path, target).items
    assert finding.rule_id == "target_source_path"
    broken = adapter.analyze_validity(BROKEN, target)
    assert (broken.status, broken.items) == (PARSE_ERROR, ())
    assert adapter.screen_source(clean, target).status == "completed"
    screened = adapter.screen_source(BROKEN, target)
    assert (screened.status, screened.items) == (PARSE_ERROR, ())
    for missing, field in (("absent.py", "target"),
                           (str(tmp_path.parent / "outside.py"), "target")):
        with pytest.raises(InvalidRequest) as refused:
            adapter.target_identity(missing)
        assert refused.value.field == field


def test_the_target_s_module_is_its_path_below_the_root(tmp_path):
    """A package named like its project directory keeps its name, and a path
    with no importable name is refused."""
    project = tmp_path / "proj"
    (project / "proj").mkdir(parents=True)
    (project / "proj" / "core.py").write_text("x = 1\n", encoding="utf-8")
    (project / "my-tools").mkdir()
    (project / "my-tools" / "run.py").write_text("x = 1\n", encoding="utf-8")
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(project), interpreter=lb.Interpreter(path=sys.executable)))
    assert adapter.target_identity("proj/core.py").module_name == "proj.core"
    with pytest.raises(InvalidRequest, match="cannot derive an importable module name") as refused:
        adapter.target_identity("my-tools/run.py")
    assert refused.value.field == "target"


def test_validity_identifies_the_target_where_the_container_runs(tmp_path):
    source = "def sign(x):\n    return x > 0\n"

    class Executor:
        def __init__(self):
            self.paths = []

        def run(self, command, **kwargs):
            self.paths.append(command[-1])
            if command[-1] == "/testbed/sign.py":
                output = (base64.b64encode(source.encode()).decode()
                          if "b64encode" in command[-2] else source)
                return CompletedProcess(command, 0, output, "")
            return CompletedProcess(command, 1, "", "not found")

    executor = Executor()
    adapter = PythonAdapter(lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/opt/env/bin/python"),
        execution=lb.ContainerExecution(container="project", work_dir=str(tmp_path))),
        executor=executor)
    identity = adapter.target_identity("sign.py")
    assert identity.source_path == "/testbed/sign.py"
    assert identity.project_root == "/testbed"
    assert identity.module_name == "sign"
    assert identity.target_source == source
    assert identity.target_source_sha256 == hashlib.sha256(source.encode()).hexdigest()
    assert executor.paths and set(executor.paths) == {"/testbed/sign.py"}
    for outside in ("../outside.py", "/outside.py"):
        with pytest.raises(InvalidRequest):
            adapter.target_identity(outside)


def test_a_target_reads_the_same_locally_and_in_a_container(tmp_path):
    """Both modes decode the target's bytes as UTF-8, an undecodable byte
    replaced, with universal newlines, so its text and recorded hash agree."""
    raw = b"def sign(x):\r\n    return x > 0  # \xff\r\n"
    local_root = tmp_path / "local"
    local_root.mkdir()
    (local_root / "sign.py").write_bytes(raw)
    local = PythonAdapter(lb.ProjectContext(
        root=str(local_root), interpreter=lb.Interpreter(path=sys.executable)))

    class Executor:
        def run(self, command, **kwargs):
            if command[-1] == "/testbed/sign.py":
                return CompletedProcess(command, 0, (
                    base64.b64encode(raw).decode() if "b64encode" in command[-2] else ""), "")
            return CompletedProcess(command, 1, "", "not found")

    container = PythonAdapter(lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/opt/env/bin/python"),
        execution=lb.ContainerExecution(container="project", work_dir=str(tmp_path))),
        executor=Executor())
    text = "def sign(x):\n    return x > 0  # �\n"
    for adapter in (local, container):
        identity = adapter.target_identity("sign.py")
        assert identity.target_source == text
        assert identity.target_source_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()



def test_a_structure_check_fails_loudly_without_the_grammar(adapter, monkeypatch):
    """A missing grammar is not mislabeled as valid source."""
    from langadapter import _treesitter

    def unavailable(_grammar, _source):
        raise ModuleNotFoundError("tree_sitter_python")

    monkeypatch.setattr(_treesitter, "parse", unavailable)
    with pytest.raises(ModuleNotFoundError, match="tree_sitter_python"):
        adapter.check_structure("def test_a():\n    assert True\n")


def test_no_scaled_generator_comparison_is_exposed(adapter):
    from langadapter import _treesitter as substrate
    from langadapter.python import _sources as _treesitter
    assert not hasattr(adapter, "structurally_equivalent")
    assert not hasattr(_treesitter, "structurally_equivalent")
    assert not hasattr(substrate, "structurally_equivalent")


def test_function_fingerprints_change_with_an_operator(adapter):
    # Operators count, formatting and comments do not.
    plus = adapter.function_fingerprints("def f(a, b):\n    return a + b\n")
    minus = adapter.function_fingerprints("def f(a, b):\n    return a - b\n")
    spaced = adapter.function_fingerprints("def f(a,b):\n    return a+b  # a sum\n")
    assert plus["f"] != minus["f"]
    assert plus == spaced


def test_a_test_defined_twice_in_one_scope_is_a_duplicate(adapter):
    src = ("def test_x():\n    assert 1\n\n"
           "def test_y():\n    assert 1\n\n"
           "def test_x():\n    assert 2\n")
    assert adapter.duplicate_test_names(src) == ["test_x"]
    # The same method name in two classes is no duplicate, but twice in one is.
    assert adapter.duplicate_test_names(
        "class TestA:\n    def test_x(self):\n        assert 1\n\n"
        "class TestB:\n    def test_x(self):\n        assert 2\n") == []
    assert adapter.duplicate_test_names(
        "class TestA:\n    def test_x(self):\n        assert 1\n"
        "    def test_x(self):\n        assert 2\n") == ["test_x"]


def test_a_test_class_defined_twice_is_a_duplicate_and_a_helper_is_not(adapter):
    """A module-level Test class defined twice loses every test of the
    earlier body, so it is named as a duplicated test is. Helpers defined
    twice, functions or classes, collect no tests and are not named."""
    assert adapter.duplicate_test_names(
        "class TestX:\n    def test_a(self):\n        assert True\n"
        "class TestX:\n    def test_b(self):\n        assert True\n") == ["TestX"]
    assert adapter.duplicate_test_names(
        "class TestX:\n    def test_a(self):\n        assert True\n"
        "class TestY:\n    def test_b(self):\n        assert True\n") == []
    assert adapter.duplicate_test_names("class Helper:\n    pass\nclass Helper:\n    pass\n") == []
    assert adapter.duplicate_test_names("def helper():\n    pass\ndef helper():\n    pass\n") == []


#: Tests as pytest's default collection finds them: testfoo, TestCalc.test_z
#: and CalcTests.testAdd. A nested function, a fixture and the methods of
#: Helper and of TestBuilt, which has an __init__, are none.
COLLECTED = ("import unittest\n\nimport pytest\n\n\n"
             "def testfoo():\n    def test_inner():\n        return None\n"
             "    assert test_inner() is None\n\n\n"
             "@pytest.fixture\ndef test_data():\n    return 1\n\n\n"
             "class Helper:\n    def test_y(self):\n        assert 1\n\n\n"
             "class TestCalc:\n    def test_z(self):\n        assert 1\n\n\n"
             "class TestBuilt:\n    def __init__(self):\n        pass\n\n"
             "    def test_w(self):\n        assert 1\n\n\n"
             "class CalcTests(unittest.TestCase):\n    def testAdd(self):\n"
             "        self.assertEqual(1, 1)\n")
#: A mock's side_effect list whose named element a test asserts on.
ORDERED = ("        m = mock.Mock()\n        a, b = object(), object()\n"
           "        m.side_effect = [a, b]\n        assert m() is a\n")


def test_a_python_test_is_one_pytest_collects(adapter):
    assert adapter.test_metrics(COLLECTED).tests == 3
    # Only collected tests are named twice or flagged.
    helper = "class Helper:\n    def test_y(self):\n        pass\n    def test_y(self):\n        pass\n"
    fixture = ("import pytest\n\n@pytest.fixture\ndef test_data():\n    return 1\n\n"
               "@pytest.fixture\ndef test_data():\n    return 2\n")
    case = ("import unittest\n\nclass CalcTests(unittest.TestCase):\n"
            "    def testAdd(self):\n        pass\n    def testAdd(self):\n        pass\n")
    assert [adapter.duplicate_test_names(source) for source in (helper, fixture, case)] == [
        [], [], ["testAdd"]]
    ordered = ("import unittest\nfrom unittest import mock\n\n"
               "class Helper:\n    def test_h(self):\n" + ORDERED +
               "\nclass CalcTests(unittest.TestCase):\n    def testOrder(self):\n" + ORDERED)
    assert adapter.order_dependent_mock_warnings(ordered) == ["CalcTests.testOrder"]


def test_a_class_pytest_skips_holds_no_tests_and_an_unread_module_no_counts(adapter):
    """A Test* class with __new__ or __test__ = False holds no tests. A
    module the library's Python parser rejects, though tree-sitter reads
    it, has no counts, never a zero."""
    for body in ("    def __new__(cls):\n        return super().__new__(cls)\n",
                 "    __test__ = False\n"):
        assert adapter.test_metrics(
            f"class TestData:\n{body}\n    def test_x(self):\n        pass\n").tests == 0
    source = "print 'old'\n\n\ndef test_a():\n    assert 1\n"
    assert adapter.check_structure(source).ok
    metrics = adapter.test_metrics(source)
    assert (metrics.parsed, metrics.tests) == (False, None)
    assert "cannot read the source" in metrics.detail


def test_an_autouse_fixture_is_requested_by_a_test_case_class(adapter):
    """Localized, an autouse fixture is requested by a unittest class whose
    name pytest's Test* pattern does not match."""
    source = ("import unittest\n\nimport pytest\n\n\n"
              "@pytest.fixture(autouse=True)\ndef setup():\n    yield\n\n\n"
              "class CalcTests(unittest.TestCase):\n    def testAdd(self):\n"
              "        self.assertEqual(1, 1)\n")
    localized = adapter.resolve_collisions([source], localize_autouse=True).value.sources[0]
    assert ("@pytest.mark.usefixtures('setup')\nclass CalcTests" in localized
            and "autouse=False" in localized)


def test_same_skeleton_names_the_definitions_a_candidate_drops(adapter):
    src = ("class T:\n    def test_a(self):\n        assert f(1) == 2\n"
           "    def test_b(self):\n        pass\n"
           "def helper():\n    return 9\n")
    assert adapter.same_skeleton(src, src) == (True, "")
    assert adapter.same_skeleton(src, "def helper():\n    return 9\n") == (
        False, "definition(s) removed: T, T.test_a, T.test_b")


def test_the_public_skeleton_comparison_gives_the_structure_checks_reasons():
    """langadapter.treesitter.diff_skeletons, which another language's
    structure check uses, gives the reasons Python's check reports: the
    removed definitions, else the added ones, else the first reordering."""
    from langadapter.treesitter import diff_skeletons

    skeleton = ("T", "T.test_a", "helper")
    assert diff_skeletons(skeleton, skeleton) == (True, "")
    assert diff_skeletons(skeleton, ("helper",)) == (
        False, "definition(s) removed: T, T.test_a")
    assert diff_skeletons(("helper",), ("helper", "extra")) == (
        False, "definition(s) added: extra")
    assert diff_skeletons(("a", "b"), ("b", "a")) == (
        False, "definition order changed at position 0: 'a' vs 'b'")


# A source the library's parser cannot read is a reading error, never an
# empty result: the flat methods raise ``SourceReadError``, the typed read
# reports a parse error, and a pool member whose imports cannot be read is
# refused. When the library's Python is older than the project's, the error
# names both.
# tree-sitter reads it, and Python's own parser rejects it.
UNREADABLE = "def test_a():\n    del f()\n"


@pytest.fixture
def library(monkeypatch):
    monkeypatch.setattr(PythonProfile, "library", (3, 12, 10))
    return ("the parser of Python 3.12 cannot read the source: "
            "cannot delete function call (line 2, column 9)")


def _adapter(tmp_path, runtime_version=None):
    if runtime_version is None:
        return lb.create("python", lb.ProjectContext(str(tmp_path)))
    (tmp_path / "python").write_text("", encoding="ascii")
    return lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(tmp_path / "python")),
                                                 runtime_version=runtime_version))


def test_duplicate_test_names_of_an_unreadable_source_are_a_reading_error(tmp_path, library):
    adapter = _adapter(tmp_path)
    with pytest.raises(lb.SourceReadError) as raised:
        adapter.duplicate_test_names(UNREADABLE)
    assert str(raised.value) == library and raised.value.parser == library_parser()
    typed = adapter.sources.duplicate_test_names(UNREADABLE)
    assert typed.status is lb.SourceStatus.PARSE_ERROR and typed.value is None
    assert typed.reason == library and typed.parser == library_parser()


def test_mock_warnings_of_an_unreadable_source_are_a_reading_error(tmp_path, library):
    with pytest.raises(lb.SourceReadError) as raised:
        _adapter(tmp_path).order_dependent_mock_warnings(UNREADABLE)
    assert str(raised.value) == library


def test_readable_sources_read_as_before(tmp_path):
    adapter = _adapter(tmp_path)
    source = "def test_a():\n    pass\n\n\ndef test_a():\n    assert True\n"
    assert adapter.duplicate_test_names(source) == ["test_a"]
    assert adapter.sources.duplicate_test_names(source).value == ("test_a",)
    assert adapter.order_dependent_mock_warnings(source) == []


def test_a_pool_member_whose_imports_cannot_be_read_is_refused(tmp_path, library):
    members = (PoolMember("s1", "test_a.py", UNREADABLE), PoolMember("s2", "test_b.py", "def test_b():\n    pass\n"))
    staged = _adapter(tmp_path).sources.stage_pool(members)
    assert staged.status is lb.SourceStatus.PARSE_ERROR and staged.parser == library_parser()
    assert staged.reason == ("the imports of test_a.py of suite s1 cannot be read, and the pool checks "
                             f"that it imports no file of its own suite: {library}")
    readable = _adapter(tmp_path).sources.stage_pool(members[1:])
    assert readable.status is lb.SourceStatus.COMPLETED and isinstance(readable.value, StagedPool)


def test_a_reading_error_names_both_pythons_when_the_librarys_is_older(tmp_path, monkeypatch):
    monkeypatch.setattr(PythonProfile, "library", (3, 10, 4))
    both = ("the parser of Python 3.10 cannot read the source: cannot delete function call (line 2, column 9). "
            "LangAdapter runs on Python 3.10, older than this project's Python 3.12, and its parser does "
            "not know the newer syntax: run the program that uses LangAdapter on Python 3.12 or newer")
    adapter = _adapter(tmp_path, "Python 3.12.3")
    with pytest.raises(lb.SourceReadError) as raised:
        adapter.duplicate_test_names(UNREADABLE)
    assert str(raised.value) == both
    assert adapter.sources.duplicate_test_names(UNREADABLE).reason == both
    with pytest.raises(lb.SourceReadError, match="older than this project's Python 3.12"):
        adapter.order_dependent_mock_warnings(UNREADABLE)
    staged = adapter.sources.stage_pool((PoolMember("s1", "test_a.py", UNREADABLE),))
    assert staged.reason.endswith(both)
    # Without an interpreter the project's Python is unknown: the library's is named alone.
    with pytest.raises(lb.SourceReadError) as alone:
        _adapter(tmp_path).duplicate_test_names(UNREADABLE)
    assert "older than" not in str(alone.value)


def test_no_operation_gives_up_quietly_on_a_source_the_older_library_cannot_read(tmp_path, monkeypatch):
    """With a project Python newer than the library's, every source operation
    that reads Python's syntax tree reports the reading error and its one
    action where it gave up before: a refused merge, a parse error, sources
    returned unrenamed, and the validity gate's copied-definition check,
    which read nothing of a target it could not parse."""
    from langadapter.sources import TargetIdentity

    monkeypatch.setattr(PythonProfile, "library", (3, 10, 4))
    adapter = _adapter(tmp_path, "Python 3.12.3")
    readable = "def test_b():\n    assert True\n"

    def identity(target_source):
        return TargetIdentity(
            source_path=str(tmp_path / "pkg" / "target.py"), project_root=str(tmp_path),
            module_name="pkg.target", language="python", policy_version="v",
            target_source_sha256=hashlib.sha256(target_source.encode()).hexdigest(),
            target_source=target_source)

    calls = {
        "merge_sources": lambda: adapter.merge_sources(readable, UNREADABLE, separator="# merged"),
        "prune_tests of one case": lambda: adapter.prune_tests(UNREADABLE, ["test_a[1]"]),
        "normalize_shadowed": lambda: adapter.normalize_shadowed(UNREADABLE),
        "normalize_bindings": lambda: adapter.normalize_bindings(UNREADABLE),
        "resolve_collisions": lambda: adapter.resolve_collisions([readable, UNREADABLE]),
        "strip_module_level_skips": lambda: adapter.strip_module_level_skips(UNREADABLE),
        "analyze_validity": lambda: adapter.analyze_validity(UNREADABLE, identity("def add():\n    pass\n")),
        "analyze_validity of an unreadable target": lambda: adapter.analyze_validity(readable, identity(UNREADABLE)),
        "screen_source": lambda: adapter.screen_source(UNREADABLE, identity("def add():\n    pass\n")),
    }
    for name, call in calls.items():
        with pytest.raises(lb.SourceReadError, match="run the program that uses LangAdapter on Python 3.12 "
                                                     "or newer") as raised:
            call()
        assert "older than this project's Python 3.12" in str(raised.value), name
    merged = adapter.sources.merge_sources(readable, UNREADABLE, separator="# merged")
    assert merged.status == "infrastructure_failure" and "on Python 3.12 or newer" in merged.detail
    # Sources the library reads are not refused, nor whole tests pruned by
    # the structural parser.
    assert adapter.normalize_bindings(readable).parse_ok
    assert adapter.prune_tests(UNREADABLE, ["test_a"]).status == "applied"
    assert adapter.analyze_validity(readable, identity("def add():\n    pass\n")).status == "completed"
