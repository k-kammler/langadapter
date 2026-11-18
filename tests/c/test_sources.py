"""Test units, syntax checks and repair, merging, the validity policy, facts and
settings, without a compiler."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as la
from langadapter.errors import InvalidRequest

from langadapter.c import _build, _syntax
from langadapter.c.adapter import CAdapter

from conftest import needs_linux


def test_c_is_registered_when_the_package_is_imported_without_its_extra():
    probe = ("import sys, langadapter; print(sorted(langadapter.names()), "
             "langadapter.language_for('src/calc.c'), sorted(name for name in sys.modules "
             "if name.split('.')[0] in ('tree_sitter_c', 'gcovr', 'setuptools')))")
    source = str(Path(la.__file__).parents[1])
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          env={**os.environ, "PYTHONPATH": source}, check=True)
    assert done.stdout.splitlines()[0] == "['c', 'java', 'python'] c []"


def test_a_project_s_sources_are_its_c_files(project):
    listing = CAdapter(la.ProjectContext(str(project("calc_make")))).sources.project_source_files()
    assert listing.value == ("src/calc.c", "src/main.c", "src/util.c")


TESTS = """\
#include "utest.h"
#include "calc.h"

struct buffer { int *data; };

UTEST_F_SETUP(buffer) {
    utest_fixture->data = 0;
}

UTEST_F_TEARDOWN(buffer) {
    (void)utest_fixture;
}

UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST_F(buffer, starts_empty) {
    EXPECT_TRUE(utest_fixture->data == 0);
}

UTEST_I(buffer, indexed, 3) {
    EXPECT_LT(utest_index, 3u);
}

UTEST_MAIN()
"""


def _adapter(tmp_path) -> CAdapter:
    # The source operations run no tool, and their public path wants a
    # runtime named: a compiler name that need not exist here.
    return CAdapter(la.ProjectContext(root=str(tmp_path),
                                      interpreter=la.Interpreter(path="cc")))


def test_a_test_file_with_utest_macros_parses(tmp_path):
    assert _adapter(tmp_path).validate_source(TESTS).valid


def test_a_syntax_error_is_reported_with_its_line(tmp_path):
    broken = TESTS.replace("ASSERT_EQ(0L, clamp(-5, 0, 10));", "ASSERT_EQ(0L, clamp(-5, 0, 10);")
    result = _adapter(tmp_path).validate_source(broken)
    assert result.status == "syntax_error"
    assert (result.line, result.column) == (15, 35)
    # The parser reads a utest.h macro head masked to its own length: an
    # error after the head on its line keeps its column.
    joined = TESTS.replace("UTEST(clamp, below_the_range) {\n    ASSERT_EQ(0L, clamp(-5, 0, 10));",
                           "UTEST(clamp, below_the_range) { ASSERT_EQ(0L, clamp(-5, 0, 10);")
    result = _adapter(tmp_path).validate_source(joined)
    assert (result.status, result.line, result.column) == ("syntax_error", 14, 63)


def test_the_test_units_are_the_tests_and_not_the_fixture_blocks(tmp_path):
    units = _adapter(tmp_path).test_units(TESTS)
    assert [unit.name for unit in units] == [
        "clamp.below_the_range", "buffer.starts_empty", "buffer.indexed"]
    first = units[0]
    assert TESTS[first.start:first.end].startswith("UTEST(clamp, below_the_range) {")
    assert TESTS[first.start:first.end].endswith("}\n")


def test_removing_a_test_keeps_the_others(tmp_path):
    adapter = _adapter(tmp_path)
    result = adapter.sources.prune_tests(TESTS, ("clamp.below_the_range",))
    assert "below_the_range" not in result.source
    assert [unit.name for unit in adapter.test_units(result.source)] == [
        "buffer.starts_empty", "buffer.indexed"]


def test_tests_are_removed_by_the_ids_a_run_names(tmp_path):
    """A consumer removes the tests a run named by their id text
    (``test_id_text``). An indexed test's id removes its whole ``UTEST_I``
    block, every index of it."""
    adapter = _adapter(tmp_path)
    ids = [adapter.test_id_text(la.TestId("test_calc.c", "clamp.below_the_range")),
           adapter.test_id_text(la.TestId("test_calc.c", "buffer.indexed", params="1")),
           adapter.test_id_text(la.TestId("test_calc.c", "clamp.missing"))]
    assert ids == ["test_calc.c::clamp.below_the_range", "test_calc.c::buffer.indexed/1",
                   "test_calc.c::clamp.missing"]
    result = adapter.sources.prune_tests(TESTS, ids)
    assert result.status == "applied"
    assert result.removed == ("clamp.below_the_range", "buffer.indexed")
    assert result.unmatched == ("test_calc.c::clamp.missing",)
    assert [unit.name for unit in adapter.test_units(result.source)] == ["buffer.starts_empty"]
    assert result.detail == ("buffer.indexed is removed whole: one index of a UTEST_I test "
                             "is no unit of its own")


def test_an_indexed_case_id_removes_its_block_and_says_so(tmp_path):
    """A consumer's case id, ``test_id_text`` with the case's parameters in
    brackets, names its unit as the id does, and the detail names the unit
    once however many of its indices are named. A TestId is named by its id
    text, and one that matches nothing is reported by its qualname, as
    Python and Java report it."""
    adapter = _adapter(tmp_path)
    missing = la.TestId("test_calc.c", "clamp.missing")
    result = adapter.sources.prune_tests(TESTS, ["test_calc.c::buffer.indexed/1[1]",
                                                 "test_calc.c::buffer.indexed/2[2]", missing])
    assert result.removed == ("buffer.indexed",)
    assert result.unmatched == ("clamp.missing",)
    assert result.detail == ("buffer.indexed is removed whole: one index of a UTEST_I test "
                             "is no unit of its own")


def test_a_file_that_does_not_parse_has_no_units(tmp_path):
    assert _adapter(tmp_path).test_units("UTEST(a, b) { int x = ; }\nUTEST_MAIN()\n") is None


def test_the_settings_reach_the_context_as_variables():
    assert CAdapter.context_environment({"c_flags": "-I include", "c_build_kind": "flags",
                                         "c_link": "", "c_build": " "}) == {
        "LANGADAPTER_C_FLAGS": "-I include", "LANGADAPTER_C_BUILD_KIND": "flags"}


def test_c_declares_pythons_settings_but_its_test_environment():
    """c_test_environment gives an extension's Python tests their variables,
    so C declares Python's other settings but not python_test_environment."""
    names = [declared.name for declared in CAdapter.project_settings]
    assert "c_test_environment" in names and "python_import_root" in names
    assert "python_executable" in names and "pytest_config" in names
    assert "python_test_environment" not in names


@pytest.mark.parametrize("value", ["/abs/src", "../src", "src/../.."])
def test_an_import_root_outside_the_project_is_refused_as_python_refuses_it(value):
    """C reads python_import_root by Python's rule: a directory below the
    project, else the setting is refused."""
    with pytest.raises(InvalidRequest) as refused:
        CAdapter.context_environment({"python_import_root": value})
    assert refused.value.field == "python_import_root"
    assert CAdapter.context_environment({"python_import_root": "src/"}) == {
        "LANGADAPTER_C_PYTHON_IMPORT_ROOT": "src"}


@pytest.mark.parametrize("value", [".", "none"])
def test_an_import_root_of_the_project_itself_stays_a_choice(tmp_path, value):
    """python_import_root . or none names the project itself, which a project
    with a package below src can choose over the src that discovery finds."""
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    variables = CAdapter.context_environment({"python_import_root": value})
    assert variables == {"LANGADAPTER_C_PYTHON_IMPORT_ROOT": "."}
    assert CAdapter(la.ProjectContext(root=str(tmp_path), environment=variables)
                    )._import_root() == ""
    assert CAdapter(la.ProjectContext(root=str(tmp_path)))._import_root() == "src"


def test_the_import_line_names_the_targets_header(tmp_path):
    (tmp_path / "calc.c").write_text("int add(int a, int b) { return a + b; }\n")
    assert CAdapter.import_statement("calc.c", str(tmp_path)) is None
    (tmp_path / "calc.h").write_text("int add(int a, int b);\n")
    assert CAdapter.import_statement("calc.c", str(tmp_path)) == '#include "calc.h"'


def test_c_tests_are_the_adapter_s_own_language(tmp_path):
    facts = _adapter(tmp_path).language_facts()
    assert (facts.language, facts.code_fence, facts.test_language, facts.target_code_fence) == (
        "C", "c", None, None)


def test_c_provides_the_timeout_journal(tmp_path):
    """C provides the timeout journal: the run's output names the test a
    hang ended."""
    assert _adapter(tmp_path).provides_timeout_journal is True


def test_the_vocabulary_says_why_coverage_counts_cannot_be_read(tmp_path):
    """gcov's counters can hold counts gcovr cannot read (the diagnostic
    ``coverage_counts_inconsistent``): the cause and the fix, word for word."""
    from langadapter import vocabulary

    words = _adapter(tmp_path).test_vocabulary()
    assert words["coverage_counts_inconsistent_cause"] == (
        "Counts that cannot be read usually mean that a test corrupts memory or relies on "
        "undefined behavior.")
    assert words["coverage_counts_inconsistent_fix"] == (
        "Change the test so that it uses only valid values and memory, or remove it.")
    assert vocabulary.problems(words) == ()


#: C's texts the owner approved, by their bytes and SHA-256.
APPROVED_TEXTS = {
    "test_file_form": (652, "a037d367d5fa4d9f2bb7d9c4a297fe2abcb8de2b0185cf691bd5606d91d6fe23"),
    "test_dependencies": (376, "a4168938cd53a308cb4b715b5f072539f671bcad3a5630056aa061ffd34b8804"),
    "load_error_patterns": (588,
                            "8dca49a9fd39bcd6a5fcecc2eb77d013723064989ff7308324387f4e9cb6c145"),
}


def test_the_approved_texts_are_these_bytes_and_list_each_check_of_utest_h(tmp_path):
    import hashlib
    import re

    from langadapter.c.adapter import _CHECKS

    words = _adapter(tmp_path).test_vocabulary()
    for name, (size, digest) in APPROVED_TEXTS.items():
        data = words[name].encode("utf-8")
        assert (len(data), hashlib.sha256(data).hexdigest()) == (size, digest), name
    assert len(_CHECKS) == 14
    for check in _CHECKS:
        assert f"``{check}``" in words["test_file_form"], check
        assert re.search(rf"\b{check}\b", words["test_dependencies"]), check


def test_the_vocabulary_says_what_a_test_restores_to_pass_beside_the_others(tmp_path):
    """A suite run runs a test file whole, so a test must leave the process
    as it found it: C's advice, byte for byte as the vocabulary states it."""
    import hashlib

    text = _adapter(tmp_path).test_vocabulary()["stands_alone_advice"]
    data = text.encode("ascii")
    assert len(data) == 632
    assert hashlib.sha256(data).hexdigest() == (
        "13cbf7f6b247902792d2b0e5eeeea6acb216656df84a774cc9476de881ad9291")
    assert text.startswith("  1. A test must leave the target's global and static variables")


#: A reply cut off after its last test, before ``UTEST_MAIN()``.
CUT_OFF = ('#include "utest.h"\n#include "calc.h"\n\n'
           "UTEST(clamp, below_the_range) {\n    ASSERT_EQ(0L, clamp(-5, 0, 10));\n}\n")


def test_finalize_source_ends_a_test_file_that_has_tests_and_no_main(tmp_path):
    """A test file with tests and no main gets ``UTEST_MAIN()`` appended, as
    a reply cut off after its last test lacks it. A file with a main of its
    own, or without a test, stays as it is."""
    sources = _adapter(tmp_path).sources
    result = sources.finalize_source(CUT_OFF)
    assert result.status == "applied", result.detail
    assert result.source == CUT_OFF + "\nUTEST_MAIN()\n"
    assert sources.finalize_source(CUT_OFF.rstrip("\n")).source == CUT_OFF + "\nUTEST_MAIN()\n"
    for whole in (result.source, CUT_OFF + "\nint main(void) {\n    return 0;\n}\n",
                  '#include "utest.h"\n#include "calc.h"\n'):
        assert sources.finalize_source(whole).status == "unchanged", whole


def test_finalize_source_finds_a_main_inside_a_conditional(tmp_path):
    """``UTEST_MAIN()`` anywhere in the file is its main, also inside a
    conditional, so no second one is appended."""
    guarded = CUT_OFF + "\n#ifndef NO_MAIN\nUTEST_MAIN()\n#endif\n"
    result = _adapter(tmp_path).sources.finalize_source(guarded)
    assert result.status == "unchanged", result.detail
    assert result.source.count("UTEST_MAIN") == 1


def test_the_vocabulary_names_no_python(tmp_path):
    words = " ".join(_adapter(tmp_path).test_vocabulary().values()).lower()
    for term in ("pytest", "monkeypatch", "importerror", "python", "fixture(", "def test"):
        assert term not in words


def test_main_is_found_as_a_top_level_function():
    assert _syntax.defines_function(b"int main(void) { return 0; }\n", "main")
    assert not _syntax.defines_function(b"static int helper(void) { return 0; }\n", "main")


SECOND = """\
#include "utest.h"
#include <string.h>
#include "calc.h"

struct buffer { int *data; };

UTEST_F_SETUP(buffer) {
    utest_fixture->data = 0;
}

UTEST_F_TEARDOWN(buffer) {
    (void)utest_fixture;
}

static long twice(long x) { return 2 * x; }

UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(twice(-5), 0, 10));
}

UTEST_F(buffer, holds_nothing) {
    EXPECT_EQ(0, (int)strlen(""));
}

UTEST_MAIN()
"""


@needs_linux
def test_two_test_files_merge_with_shared_and_renamed_tests(tmp_path):
    sources = _adapter(tmp_path).sources
    assert sources.supports("merge_sources") and sources.supports("salvage_syntax")
    result = sources.merge_sources(TESTS, SECOND, separator="// second")
    assert result.status == "applied", result.detail
    merged = result.source
    assert merged.startswith(TESTS.replace("UTEST_MAIN()\n", "").rstrip())
    assert merged.count('#include "utest.h"') == merged.count('#include "calc.h"') == 1
    assert "#include <string.h>" in merged.split("// second\n")[1]
    # The fixture is the same in both: its struct and blocks come once.
    assert merged.count("struct buffer {") == merged.count("UTEST_F_SETUP(buffer)") == 1
    assert result.detail == "shared buffer"
    assert [unit.name for unit in _adapter(tmp_path).test_units(merged)] == [
        "clamp.below_the_range", "buffer.starts_empty", "buffer.indexed",
        "clamp.below_the_range_2", "buffer.holds_nothing"]
    assert merged.endswith("}\n\nUTEST_MAIN()\n") and merged.count("UTEST_MAIN()") == 1


@needs_linux
def test_a_name_both_declare_differently_is_renamed_in_the_second(tmp_path):
    first = """\
#include "utest.h"
#define LIMIT 10
static int seen = 0;

UTEST(count, first) {
    seen += LIMIT;
    ASSERT_EQ(10, seen);
}

UTEST_MAIN()
"""
    second = """\
#include "utest.h"
#define LIMIT 3
#define TWICE_LIMIT (2 * LIMIT)
static int seen = 1;
struct buffer { int size; };

UTEST_F_SETUP(buffer) { utest_fixture->size = TWICE_LIMIT + seen; }
UTEST_F_TEARDOWN(buffer) { (void)utest_fixture; }

UTEST_F(buffer, sized) {
    const char *label = "seen";
    ASSERT_EQ(7, utest_fixture->size);
    ASSERT_EQ('s', label[0]);
}

UTEST_MAIN()
"""
    adapter = _adapter(tmp_path)
    result = adapter.sources.merge_sources(
        first.replace("UTEST_MAIN()", "struct buffer {};\nUTEST_MAIN()"), second,
        separator="// second")
    assert result.status == "applied", result.detail
    assert result.detail == "renamed LIMIT -> LIMIT_2, buffer -> buffer_2, seen -> seen_2"
    rest = result.source.split("// second\n")[1]
    assert "#define LIMIT_2 3\n#define TWICE_LIMIT (2 * LIMIT_2)\n" in rest
    assert "static int seen_2 = 1;" in rest and 'label = "seen";' in rest
    assert "UTEST_F_SETUP(buffer_2) { utest_fixture->size = TWICE_LIMIT + seen_2; }" in rest
    assert "UTEST_F(buffer_2, sized)" in rest
    assert "seen += LIMIT;" in result.source.split("// second\n")[0]
    assert adapter.validate_source(result.source).valid


def test_only_declarations_are_shared_without_identical_text(tmp_path):
    """A prototype or extern declaration the first file has in other words
    is shared. A function the first file defines, and a function pointer
    variable, are renamed in the second."""
    first = """\
#include "utest.h"
int add(int, int);
extern long total;
static int twice(int x) { return 2 * x; }
int (*pick)(int);

UTEST(sum, first) { ASSERT_EQ(4, add(twice(1), 2) + (int)total * 0); }

UTEST_MAIN()
"""
    second = """\
#include "utest.h"
int add(int a, int b);
extern long int total;
int twice(int x);
int (*pick)(int value);

UTEST(sum, second) { ASSERT_EQ(5, add(twice(1), 3)); }

UTEST_MAIN()
"""
    result = _adapter(tmp_path).merge_sources(first, second, separator="// second")
    assert result.status == "applied", result.detail
    assert result.detail == "renamed pick -> pick_2, twice -> twice_2; shared add, total"
    rest = result.source.split("// second\n")[1]
    assert "int add(int a, int b);" in rest and "extern long int total;" in rest
    assert "add(twice_2(1), 3)" in rest and "int (*pick_2)(int value);" in rest


@needs_linux
def test_a_test_file_with_its_own_main_does_not_merge(tmp_path):
    own = ('#include "utest.h"\nUTEST(a, b) { ASSERT_TRUE(1); }\nUTEST_STATE();\n'
           'int main(int argc, const char *const argv[]) { return utest_main(argc, argv); }\n')
    sources = _adapter(tmp_path).sources
    result = sources.merge_sources(TESTS, own, separator="// second")
    assert result.status == "refused" and "own main" in result.detail
    broken = sources.merge_sources(TESTS, "UTEST(a, b) {\n", separator="// second")
    assert broken.status == "parse_error" and broken.source == TESTS


METRICS_SOURCE = """\
#include <stdio.h>

struct pair { int a; int b; };
enum color { RED, GREEN };
struct pair *first(struct pair *pairs);

static int fact(int n) {
    return n <= 1 ? 1 : n * fact(n - 1);
}

int total(const int *values, int count) {
    int sum = 0;
    for (int i = 0; i < count; i++) {
        for (int j = 0; j < i; j++) {
            sum += values[j];
        }
    }
    while (sum > 100 && count > 0) {
        sum /= 2;
    }
    return sum;
}

void save(const char *path) {
    FILE *file = fopen(path, "w");
    if (file) {
        fclose(file);
    }
}
"""


def test_the_structural_metrics_of_a_c_source(tmp_path):
    """lizard's lines of code and complexity sum, as the screening measured
    the targets, and the other counters from tree-sitter-c's tree."""
    import lizard

    adapter = _adapter(tmp_path)
    for operation in ("metrics", "skeleton", "same_skeleton", "function_fingerprints"):
        assert adapter.sources.supports(operation), operation
    info = lizard.analyze_file.analyze_source_code("source.c", METRICS_SOURCE)
    assert adapter.optimization_metrics(METRICS_SOURCE) == {
        "cyclomatic_sum": 9, "nloc": 25, "function_count": 3, "class_count": 2,
        "loop_count": 3,
        "loop_max_nesting": 2, "recursive_function_count": 1, "io_call_count": 1}
    assert [f.cyclomatic_complexity for f in info.function_list] == [2, 5, 2]
    assert set(adapter.optimization_metrics("int f( {").values()) == {None}
    identity = adapter.structural_parser_identity()
    assert "tree-sitter-c" in identity and "lizard" in identity


def test_a_test_files_skeleton_names_its_tests_as_utest_does(tmp_path):
    adapter = _adapter(tmp_path)
    names = ("UTEST_F_SETUP(buffer)", "UTEST_F_TEARDOWN(buffer)", "clamp.below_the_range",
             "buffer.starts_empty", "buffer.indexed")
    assert adapter.skeleton(TESTS) == names
    assert adapter.skeleton(METRICS_SOURCE) == ("fact", "total", "save")
    assert adapter.skeleton("int f( {") == ()
    fingerprints = adapter.function_fingerprints(TESTS)
    assert tuple(fingerprints) == names
    changed = adapter.function_fingerprints(
        TESTS.replace("clamp(-5, 0, 10)", "clamp(-6, 0, 10)").replace(
            "EXPECT_LT(utest_index, 3u);", "EXPECT_LT(utest_index,   3u); // the same"))
    assert [name for name in names if changed[name] != fingerprints[name]] == [
        "clamp.below_the_range"]
    without = TESTS.replace("UTEST(clamp, below_the_range) {\n"
                            "    ASSERT_EQ(0L, clamp(-5, 0, 10));\n}\n", "")
    assert adapter.same_skeleton(TESTS, TESTS) == (True, "")
    assert adapter.same_skeleton(TESTS, without) == (
        False, "definition(s) removed: clamp.below_the_range")
    assert adapter.optimization_metrics(TESTS)["function_count"] == 5


def test_two_suites_pool_under_names_of_their_own(tmp_path):
    """As Python's: each file named for its suite, with the way back. A C
    test file is its own program, so its tests keep their names."""
    from langadapter.sources import PoolMember, StagedPool

    adapter = _adapter(tmp_path)
    assert adapter.sources.supports("stage_pool")
    pool = adapter.stage_pool([PoolMember("s1", "test_calc.c", TESTS),
                               PoolMember("s2", "test_calc.c", TESTS)])
    assert isinstance(pool, StagedPool)
    assert [(f.slot, f.original, f.path, f.self_references) for f in pool.files] == [
        ("s1", "test_calc.c", "test_calc_s1.c", ()), ("s2", "test_calc.c", "test_calc_s2.c", ())]
    pooled = la.TestId("test_calc_s2.c", "clamp.below_the_range",
                       text="test_calc_s2.c::clamp.below_the_range")
    assert pool.original_test(pooled) == ("s2", la.TestId(
        "test_calc.c", "clamp.below_the_range", None, "test_calc.c::clamp.below_the_range"))
    assert pool.slot_of_killer("test_calc_s1.c::buffer.starts_empty") == "s1"


def test_a_pool_refuses_or_notes_what_its_names_would_change(tmp_path):
    from langadapter.sources import PARSE_ERROR, PoolMember, SourceObservation

    adapter = _adapter(tmp_path)
    refused = adapter.stage_pool([PoolMember("s1", "test_calc.py", "def test(): pass\n")])
    assert isinstance(refused, SourceObservation) and refused.status.value == "unsupported"
    assert adapter.stage_pool([PoolMember("s1", "test_calc.c", "UTEST(a, b) {")]).status.value \
        == PARSE_ERROR
    sibling = adapter.stage_pool([
        PoolMember("s1", "test_calc.c", '#include "test_helpers.c"\n' + TESTS),
        PoolMember("s1", "test_helpers.c", "static int helper(void) { return 1; }\n")])
    assert sibling.status.value == "unsupported" and "test_helpers.c" in sibling.reason
    named = adapter.stage_pool([PoolMember(
        "s1", "test_calc.c", TESTS.replace("ASSERT_EQ(0L, clamp(-5, 0, 10));",
                                           'ASSERT_TRUE(__FILE__[0] != 0);'))])
    assert named.files[0].self_references == ("reads its file's name (__FILE__)",)


def test_ctests_tests_do_not_pool_and_the_report_says_so(tmp_path):
    """ctest's tests are its CMake project's: the operation report says the
    pool is unsupported, with the reason a call answers, so a run that pools
    records its fallback."""
    from langadapter.sources import PoolMember

    adapter = CAdapter(la.ProjectContext(
        root=str(tmp_path), environment=CAdapter.context_environment({"c_tests": "ctest"})))
    pool = next(item for item in adapter.operation_capabilities()
                if item.name == "sources.stage_pool")
    assert not pool.supported and "CMake project" in pool.reason
    assert not adapter.sources.supports("stage_pool")
    refused = adapter.sources.stage_pool([PoolMember("s1", "test_calc.c", TESTS)])
    assert refused.status.value == "unsupported" and refused.reason == pool.reason


def test_skip_removal_keeps_a_c_test_file_as_it_is(tmp_path):
    """utest.h has no statement that skips a whole file: UTEST_SKIP stands in
    a test or a fixture's setup, which Python's rule keeps as well."""
    from langadapter.sources import PARSE_ERROR, UNCHANGED

    adapter = _adapter(tmp_path)
    skipping = TESTS.replace("utest_fixture->data = 0;", 'UTEST_SKIP("no buffer");').replace(
        "ASSERT_EQ(0L, clamp(-5, 0, 10));", 'UTEST_SKIP("later");')
    assert adapter.sources.supports("strip_module_level_skips")
    result = adapter.strip_module_level_skips(skipping)
    assert (result.status, result.source, result.removed) == (UNCHANGED, skipping, ())
    assert adapter.strip_module_level_skips("UTEST(a, b) {").status == PARSE_ERROR


@needs_linux
def test_syntax_repair_inserts_a_missing_token_or_blanks_the_line(tmp_path):
    """A missing semicolon is inserted where the parser finds it missing, and
    the line keeps its text. A line the parser cannot complete is blanked,
    the other lines and their numbers kept."""
    adapter = _adapter(tmp_path)
    assert adapter.sources.salvage_syntax(TESTS).status == "unchanged"
    unclosed = TESTS.replace("UTEST_MAIN()\n", "UTEST(open, end) {\n    ASSERT_TRUE(1);\n")
    stray = TESTS.replace("UTEST_MAIN()", "@@ stray text @@\nUTEST_MAIN()")
    for broken, repaired in (
            (TESTS.replace("clamp(-5, 0, 10));", "clamp(-5, 0, 10))"), TESTS),
            (unclosed, unclosed.replace("UTEST(open, end) {\n", "\n")),
            (stray, stray.replace("@@ stray text @@\n", "\n"))):
        assert not adapter.validate_source(broken).valid
        result = adapter.sources.salvage_syntax(broken)
        assert result.status == "applied", (result.detail, broken)
        assert result.source == repaired
        assert adapter.validate_source(result.source).valid


TARGET_SOURCE = """\
#include "calc.h"

static long limit(long x) { return x; }

long clamp(long x, long lo, long hi) {
    if (x < lo) return lo;
    return x > limit(hi) ? hi : x;
}
"""


def _policy(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.c").write_text(TARGET_SOURCE)
    adapter = _adapter(tmp_path)
    return adapter, adapter.target_identity("src/calc.c")


def test_the_policy_finds_tests_that_read_copy_or_redefine_the_target(tmp_path):
    adapter, target = _policy(tmp_path)
    assert (target.source_path, target.module_name, target.language) == (
        "src/calc.c", "src/calc", "c")
    with pytest.raises(InvalidRequest):
        adapter.target_identity("src/none.c")
    assert adapter.sources.supports("analyze_validity")
    observed = adapter.sources.analyze_validity("""\
#include <stdio.h>
#include "utest.h"
#include "../src/calc.c"
#define clamp(x, lo, hi) (x)

static long limit(long x) { return 0; }

UTEST(source, read) {
    FILE *file = fopen("src/calc.c", "r");
    ASSERT_TRUE(file != NULL);
}

UTEST(clamp, below) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}

UTEST_MAIN()
""", target)
    assert observed.status.value == "completed"
    assert [(f.rule_id, f.start_line, f.enclosing_test) for f in observed.value] == [
        ("copied_target", 3, None), ("target_redefined", 4, None), ("copied_target", 6, None),
        ("target_source_read", 9, "source.read")]
    # Each finding and the adapter's report name the policy that read the source.
    assert {f.policy_version for f in observed.value} == {"c-generated-test-validity-v1"}
    clean = adapter.analyze_validity(TESTS, target)
    assert clean.items == () and clean.policy_version == "c-generated-test-validity-v1"


def test_the_policy_screens_for_review_signals(tmp_path):
    adapter, target = _policy(tmp_path)
    report = adapter.screen_source("""\
#define static
#include <dirent.h>
#include "utest.h"

UTEST(look, around) {
    DIR *dir = opendir("src");
    const char *name = "calc.c";
    ASSERT_TRUE(dir != NULL && name != NULL);
}

UTEST_MAIN()
""", target)
    assert report.status == "completed"
    assert [(f.rule_id, f.start_line) for f in report.items] == [
        ("keyword_override", 1), ("source_discovery", 6), ("target_literal", 7)]
    broken = "UTEST(a, b) {\n"
    for read in (adapter.analyze_validity, adapter.screen_source):
        unparsed = read(broken, target)
        assert unparsed.status == "parse_error" and unparsed.error_line == 1


def test_the_extension_modules_of_setup_py(tmp_path):
    from langadapter.c import _build

    (tmp_path / "setup.py").write_text(
        'from setuptools import Extension, setup\n'
        'setup(ext_modules=[Extension("pkg._fast", sources=["pkg/_fast.c"]),\n'
        '                   Extension("pkg._two", ["pkg/a.c", "pkg/b.c"])])\n')
    assert _build.extension_modules(tmp_path) == {"pkg._fast": "pkg/_fast.c"}
    # setuptools' run_setup reads what the code computes, which the file's
    # text does not show.
    computed = tmp_path / "computed"
    computed.mkdir()
    (computed / "setup.py").write_text(
        'from setuptools import Extension, setup\n'
        'names = ["one", "two"]\n'
        'setup(ext_modules=[Extension(f"pkg._{n}", [f"pkg/_{n}.c"]) for n in names])\n')
    assert _build.extension_modules(computed) == {"pkg._one": "pkg/_one.c",
                                                  "pkg._two": "pkg/_two.c"}
    assert _build.declared_module(computed, "pkg/_two.c") == "pkg._two"
    assert _build.builds_extensions(computed)
    # A setup.py that cannot be read counts as building extensions, so the
    # setuptools build reports its own error; it declares no module.
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "setup.py").write_text('import not_installed_here\n')
    assert _build.builds_extensions(broken)
    assert "not_installed_here" in _build.setup_extensions(broken).problem
    assert _build.declared_module(broken, "pkg/_one.c") is None
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "setup.py").write_text('from setuptools import setup\nsetup(name="p")\n')
    assert not _build.builds_extensions(plain)


class _Machine:
    """A setup's probe of a machine without tools."""

    def is_dir(self, path):
        return Path(path).is_dir()

    def is_file(self, path):
        return Path(path).is_file()

    def which(self, name):
        return None

    def environment(self, name):
        return None

    def read_text(self, path):
        return None

    def run(self, argv, timeout):
        return None


def test_c_asks_its_own_settings_and_pythons_for_python_tests(project):
    from langadapter.adapter import SetupQuestion

    def asked(root):
        return [item.setting for item in CAdapter.setup_questions(root, _Machine())
                if isinstance(item, SetupQuestion)]

    make = project("calc_make")
    plain, extension = asked(make), asked(project("calc_ext"))
    # Python's settings, which C declares for Python tests, are Python's to ask.
    assert plain and all(setting.startswith("c_") for setting in plain)
    # c_test_environment is C's own too, though no LANGADAPTER_C_ variable carries it.
    assert "c_test_environment" in plain
    assert set(plain) <= set(extension)
    assert {setting for setting in extension if not setting.startswith("c_")}
    assert CAdapter.setup_problem("c_flags", "-O2", make, _Machine()) is None


def test_c_checks_its_own_settings_by_their_kind(project):
    make = project("calc_make")
    assert CAdapter.setup_problem("c_build_kind", "ninja", make, _Machine()) == (
        f"The value must be one of {', '.join(_build.KINDS)}.")
    assert CAdapter.setup_problem("c_target", "src/missing.c", make, _Machine()) == (
        "No such file or directory.")
    assert CAdapter.setup_problem("c_target", "src/calc.c", make, _Machine()) is None
    assert CAdapter.setup_problem("c_mull", "mull-runner-99", make, _Machine()) == (
        "No such program.")


@pytest.mark.parametrize("kind", ["programs", "ctest"])
def test_generation_for_a_projects_own_suite_is_refused_at_its_start(tmp_path, kind):
    """A project's own programs and ctest's tests are measured, not
    generated: their kinds refuse the words a generation's prompts start
    from, naming c_tests c, while c tests keep theirs."""
    context = la.ProjectContext(root=str(tmp_path), interpreter=la.Interpreter(
        path=sys.executable), environment=CAdapter.context_environment({"c_tests": kind}))
    with pytest.raises(InvalidRequest) as refused:
        CAdapter(context).test_vocabulary()
    assert refused.value.field == "c_tests" and "c_tests c" in refused.value.message
    plain = la.ProjectContext(root=str(tmp_path), interpreter=la.Interpreter(
        path=sys.executable), environment=CAdapter.context_environment({"c_tests": "c"}))
    assert CAdapter(plain).test_vocabulary()
