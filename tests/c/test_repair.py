"""Load repair and import pruning of generated tests, both test languages."""
from __future__ import annotations

import sys

import pytest

import langadapter as la
from langadapter.runs import SuiteRunRequest

from langadapter.c.adapter import CAdapter
from conftest import needs_gcc, needs_python_headers

pytestmark = needs_gcc

GOOD = """\
UTEST(clamp, below_the_range) {
    ASSERT_EQ(0L, clamp(-5, 0, 10));
}
"""


def _source(*tests: str, includes: str = '#include "utest.h"\n#include "calc.h"\n') -> str:
    return includes + "\n" + "\n".join(tests) + "\nUTEST_MAIN()\n"


def test_a_test_that_does_not_compile_is_removed_and_the_others_kept(project, adapter):
    root = project("calc_make")
    broken = """\
UTEST(clamp, too_few_arguments) {
    ASSERT_EQ(0L, clamp(-5, 0));
}
"""
    undeclared = """\
UTEST(clamp, calls_what_the_target_lacks) {
    ASSERT_EQ(1, no_such_function(2));
}
"""
    result = adapter(root, c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD, broken, undeclared), timeout_s=120)
    assert result.status == "applied", result.detail
    assert sorted(result.removed) == ["clamp.calls_what_the_target_lacks",
                                      "clamp.too_few_arguments"]
    assert "below_the_range" in result.source
    assert "too_few_arguments" not in result.source


def test_every_call_of_an_undeclared_function_goes_in_one_round(project, adapter):
    """GCC reports an implicitly declared function, such as a utest.h macro
    that does not exist, at its first call only: every test that calls it
    goes in one round, so six such tests need no six rounds."""
    callers = [f"UTEST(clamp, near_{n}) {{\n    ASSERT_NEAR_ULP(clamp({n}, 0, 10), {n}, 1);\n}}\n"
               for n in range(6)]
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD, *callers), timeout_s=120)
    assert result.status == "applied", result.detail
    assert sorted(result.removed) == [f"clamp.near_{n}" for n in range(6)]
    assert "below_the_range" in result.source


def test_an_assertion_in_a_helper_outside_a_test_goes(project, adapter):
    """utest.h's assertion outside a test fails inside utest.h, and only its
    last "in expansion of macro" note names the test file's line: the helper
    goes, then the test that calls it."""
    helper = """\
static void check_clamp(int x) {
    ASSERT_EQ(0L, clamp(x, 0, 10));
}

UTEST(clamp, through_a_helper) {
    check_clamp(-5);
}
"""
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD, helper), timeout_s=120)
    assert result.status == "applied", result.detail
    assert sorted(result.removed) == ["check_clamp", "clamp.through_a_helper"]
    assert "below_the_range" in result.source and "check_clamp" not in result.source


def test_a_test_that_does_not_link_goes_as_in_a_run(project, adapter):
    """The repair links as a run does: a test that calls a function it
    declares but nothing defines compiles, and ld names its line."""
    unlinked = """\
int defined_nowhere(int x);

UTEST(clamp, calls_what_nothing_defines) {
    ASSERT_EQ(1, defined_nowhere(2));
}
"""
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD, unlinked), timeout_s=120)
    assert result.status == "applied", result.detail
    assert result.removed == ("clamp.calls_what_nothing_defines",)
    assert "below_the_range" in result.source


def test_every_call_of_a_function_ld_cannot_resolve_goes_in_one_round(project, adapter):
    """ld names five references to a function nothing defines: every test
    that calls it goes in one round, so thirty such tests need no thirty."""
    callers = [f"UTEST(m, t{n}) {{\n    ASSERT_EQ({n}L, missing({n}));\n}}\n" for n in range(30)]
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD, "long missing(long x);\n", *callers), timeout_s=300)
    assert result.status == "applied", result.detail
    assert sorted(result.removed) == sorted(f"m.t{n}" for n in range(30))
    assert "below_the_range" in result.source


@pytest.mark.parametrize("tests, removed", [
    # A helper defined after its first use: GCC reports the use before it.
    ("UTEST(a, uses_early) {\n    ASSERT_EQ(2, twice(1));\n}\n\n"
     "int twice(int x) {\n    return 2 * x;\n}\n\n"
     "UTEST(a, uses_late) {\n    ASSERT_EQ(4, twice(2));\n}\n", ("a.uses_early",)),
    # A local function pointer of the name another test calls undeclared.
    ("UTEST(a, undeclared) {\n    ASSERT_EQ(1, pick(1));\n}\n\n"
     "UTEST(a, local_pointer) {\n    int (*pick)(long) = sign;\n    ASSERT_EQ(1, pick(4));\n}\n",
     ("a.undeclared",)),
], ids=["helper_defined_later", "local_pointer_same_name"])
def test_a_name_the_file_declares_goes_only_where_gcc_reports_it(project, adapter, tests,
                                                                  removed):
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD, tests), timeout_s=120)
    assert result.status == "applied", result.detail
    assert result.removed == removed


def test_a_function_defined_again_names_its_cause_and_goes(project, adapter, suite):
    """A test file that defines a function of the target again does not
    link: the shown lines hold ld's cause, and the repair removes the second
    definition, whose line ld names where the first one stands."""
    own = "int sign(long x) {\n    return x > 0;\n}\n\nUTEST(a, uses_own_sign) {\n" \
          "    ASSERT_EQ(1, sign(4));\n}\n"
    c = adapter(project("calc_make"), c_target="src/calc.c")
    run = c.run_suite(SuiteRunRequest(suite(_source(GOOD, own), "test_calc.c"), timeout_s=120))
    assert run.outcome is la.Outcome.COLLECTION_FAILED, run.reason
    assert any("multiple definition of `sign'" in line for line in run.reason_detail), \
        run.reason_detail
    result = c.sources.prune_collection_breakers(_source(GOOD, own), timeout_s=120)
    assert result.status == "applied", result.detail
    assert result.removed == ("sign",)


def test_a_file_without_main_does_not_load(project, adapter):
    """Without ``UTEST_MAIN()`` the program does not link: ld finds no main
    and no utest_state, which ``UTEST_MAIN()`` defines, so no test can go."""
    source = _source(GOOD).replace("UTEST_MAIN()\n", "")
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        source, timeout_s=120)
    assert result.status == "not_repaired", result.detail
    assert result.source == source
    assert "the test file does not load" in result.detail


#: A gcc that logs the locale variables it was given and its arguments to
#: GCC_ENV_LOG, then runs the real gcc.
GCC_LOGGING = """\
#!/bin/sh
printf '%s|%s|%s\\n' "${{LC_ALL-unset}}" "${{LC_MESSAGES-unset}}" "$*" >> "$GCC_ENV_LOG"
exec {gcc} "$@"
"""


def test_the_repair_reads_english_messages_under_another_locale(project, tmp_path):
    """The compiles whose text the repair reads run with LC_MESSAGES=C and
    without LC_ALL, so a German locale in the context leaves GCC's messages
    English, and the repair still finds the line."""
    import shutil

    from langadapter.c import _toolchain

    real = shutil.which("gcc")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    wrapper = bin_dir / "gcc"
    wrapper.write_text(GCC_LOGGING.format(gcc=real), encoding="utf-8")
    wrapper.chmod(0o755)
    (bin_dir / "gcov").symlink_to(_toolchain.gcov_beside(real))
    log = tmp_path / "gcc-env.log"
    environment = {**CAdapter.context_environment({"c_target": "src/calc.c"}),
                   "LC_ALL": "de_DE.UTF-8", "LANG": "de_DE.UTF-8", "GCC_ENV_LOG": str(log)}
    adapter = CAdapter(la.ProjectContext(root=str(project("calc_make")), environment=environment,
                                         interpreter=la.Interpreter(path=str(wrapper))))
    broken = "UTEST(clamp, too_few_arguments) {\n    ASSERT_EQ(0L, clamp(-5, 0));\n}\n"
    result = adapter.sources.prune_collection_breakers(_source(GOOD, broken), timeout_s=120)
    assert result.status == "applied", result.detail
    assert result.removed == ("clamp.too_few_arguments",)
    compiles = [line.split("|")[:2] for line in log.read_text(encoding="utf-8").splitlines()
                if "test_calc.c" in line]
    assert compiles and all(found == ["unset", "C"] for found in compiles), compiles


def test_load_repair_never_removes_the_files_main(project, adapter):
    from langadapter.c import _repair

    source = _source(GOOD).replace("UTEST_MAIN()", "UTEST_MAIN(argc, argv)")
    main = source.splitlines().index("UTEST_MAIN(argc, argv)") + 1
    assert not [unit for unit in _repair.removable_units(source) if unit[1] <= main <= unit[2]]
    result = adapter(project("calc_make"), c_target="src/calc.c").sources.prune_collection_breakers(
        source, timeout_s=120)
    assert "UTEST_MAIN(argc, argv)" not in result.removed
    assert result.status != "applied" or "UTEST_MAIN(argc, argv)" in result.source


def test_a_source_that_compiles_is_unchanged(project, adapter):
    root = project("calc_make")
    result = adapter(root, c_target="src/calc.c").sources.prune_collection_breakers(
        _source(GOOD), timeout_s=120)
    assert result.status == "unchanged"


def test_an_include_of_a_missing_file_is_removed(project, adapter):
    root = project("calc_make")
    source = _source(GOOD, includes='#include "utest.h"\n#include "calc.h"\n'
                                   '#include "nowhere.h"\n#include <also_nowhere.h>\n')
    result = adapter(root, c_target="src/calc.c").sources.prune_unresolvable_imports(source)
    assert result.status == "applied", result.detail
    assert result.removed == ("nowhere.h", "also_nowhere.h")
    assert "nowhere" not in result.source and '#include "calc.h"' in result.source


def test_the_target_is_the_source_of_the_header_the_test_includes(project, adapter):
    root = project("calc_make")
    result = adapter(root).sources.prune_collection_breakers(_source(GOOD), timeout_s=120)
    assert result.status == "unchanged", result.detail


def test_without_a_target_the_repair_says_so(project, adapter):
    root = project("calc_make")
    result = adapter(root).sources.prune_collection_breakers(
        _source(GOOD, includes='#include "utest.h"\n'), timeout_s=60)
    assert result.status == "unsupported"
    assert "c_target" in result.detail


@needs_python_headers
def test_python_tests_of_an_extension_are_pruned_as_python_with_the_target_staged(project):
    root = project("calc_ext")
    context = la.ProjectContext(
        root=str(root), interpreter=la.Interpreter(path=sys.executable),
        environment=CAdapter.context_environment({"c_target": "calcext/_calc.c"}))
    c = CAdapter(context)
    source = ("import nosuchmodule\n"
              "from calcext import _calc\n\n\n"
              "def test_clamp():\n"
              "    assert _calc.clamp(-5, 0, 10) == 0\n")
    result = c.sources.prune_unresolvable_imports(source)
    # The extension is not built in the project, but staged: its import stays.
    assert result.status == "applied", result.detail
    assert "nosuchmodule" in " ".join(result.removed)
    assert "from calcext import _calc" in result.source
    assert c.collection_unit == c._python().collection_unit
    (root / "conftest.py").write_text("")
    listed = [str(root / "test__calc.py")]
    assert c.setup_files(str(root), listed) == c._python().setup_files(str(root), listed)
    assert any(name.endswith("conftest.py") for name in c.setup_files(str(root), listed))


@needs_python_headers
def test_python_tests_name_their_target_by_the_extension_they_import(project):
    root = project("calc_ext")
    c = CAdapter(la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=sys.executable)))
    source = ("import nosuchmodule\n"
              "from calcext import _calc\n\n\n"
              "def test_clamp():\n"
              "    assert _calc.clamp(-5, 0, 10) == 0\n")
    result = c.sources.prune_unresolvable_imports(source)
    assert result.status == "applied", result.detail
    assert "from calcext import _calc" in result.source
    # A test file that imports no extension of the project's names no target,
    # for an adapter that has built none.
    fresh = CAdapter(la.ProjectContext(root=str(root), interpreter=la.Interpreter(path=sys.executable)))
    unnamed = fresh.sources.prune_unresolvable_imports("import nosuchmodule\n\n\ndef test_x():\n"
                                                   "    assert True\n")
    assert unnamed.status == "unsupported" and "c_target" in unnamed.detail
