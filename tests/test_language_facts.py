"""The facts a consumer names a language's files and test ids by: their
defaults, and Python's and Java's values."""
import dataclasses

import langadapter as lb
from langadapter import hooks
from langadapter.adapter import LangAdapter, ProjectResolution
from langadapter.java.adapter import JavaAdapter
from langadapter.outcomes import Outcome
from langadapter.python.adapter import PythonAdapter

JAVA = ("package demo.calc;\n\nimport org.junit.jupiter.api.Test;\n\n"
        "class Helper {}\n\npublic class CalcTest {\n    @Test\n    void adds() {}\n}\n")


class Plain(LangAdapter):
    language = "plain"


def _adapter(cls, tmp_path):
    return cls(lb.ProjectContext(str(tmp_path)))


def test_the_defaults_name_no_rule_and_resolve(tmp_path):
    plain = _adapter(Plain, tmp_path)
    assert plain.file_rule("anything") is None
    assert plain.test_id_prefix("anything", "test_calc") == "test_calc"
    assert plain.scratch_stem == "candidate"
    assert plain.project() == ProjectResolution(Outcome.COMPLETED, "")
    assert plain.reuses_bytecode is False and plain.copies_external_suite is False
    assert plain.provides_timeout_journal is True
    assert {"node_modules", ".venv", "target", "tests"} <= set(plain.ignored_dirs)
    for name in ("file_rule", "test_id_prefix", "scratch_stem", "project", "reuses_bytecode",
                 "copies_external_suite", "provides_timeout_journal", "ignored_dirs"):
        assert not hooks._present(Plain, hooks.hook(name)), name

    class Own(LangAdapter):
        language = "own"
        ignored_dirs = ("_build",)

    assert hooks._present(Own, hooks.hook("ignored_dirs"))


def test_the_tests_and_the_target_are_the_adapter_s_own_language_by_default():
    facts = lb.LanguageFacts(language="Plain", test_framework="plain", code_fence="plain",
                             runtime_version=None, test_file_patterns=("test_*",),
                             test_function_pattern="test", test_class_pattern=None,
                             timeout_marker=None)
    assert (facts.test_language, facts.target_code_fence) == (None, None)


def test_only_python_names_a_non_source_file():
    """A search for sources leaves out the names a language declares as no
    sources (non_source_files): Python's build script, nothing by default."""
    from langadapter.java.adapter import JavaAdapter

    assert LangAdapter.non_source_files == () and JavaAdapter.non_source_files == ()
    assert PythonAdapter.non_source_files == ("setup.py",)
    assert hooks.hook("non_source_files").works_by_default
    assert not hooks._present(Plain, hooks.hook("non_source_files"))


def test_python_keeps_the_defaults_with_its_file_names(tmp_path):
    python = _adapter(PythonAdapter, tmp_path)
    assert python.file_rule("class TestCalc:\n    pass\n") is None
    assert python.test_id_prefix("", "test_calc") == "test_calc"
    assert PythonAdapter.test_file_name.format(stem=PythonAdapter.scratch_stem) == (
        "test_candidate.py")
    assert PythonAdapter.test_file_name.format(stem="calc") == "test_calc.py"
    # A measurement copy reuses earlier copies' bytecode and takes an
    # external suite with it.
    assert python.reuses_bytecode is True and python.copies_external_suite is True
    for name in ("reuses_bytecode", "copies_external_suite"):
        assert hooks._present(PythonAdapter, hooks.hook(name)), name


def test_java_names_a_file_for_its_class_and_ids_by_its_package(tmp_path):
    java = _adapter(JavaAdapter, tmp_path)
    assert java.file_rule(JAVA) == "CalcTest"
    assert java.file_rule("class Only {}\n") == "Only"
    assert java.file_rule("int x;\n") is None and java.file_rule("class {") is None
    assert java.test_id_prefix(JAVA, "CalcTest") == "demo.calc.CalcTest"
    assert java.test_id_prefix("class CalcTest {}\n", "CalcTest") == "CalcTest"
    assert JavaAdapter.test_file_name.format(stem=JavaAdapter.scratch_stem) == (
        "CandidateTest.java")
    assert JavaAdapter.test_file_name.format(stem="calc") == "calcTest.java"
    for name in ("file_rule", "test_id_prefix", "scratch_stem", "project"):
        assert hooks._present(JavaAdapter, hooks.hook(name)), name
    # A copy compiles its own classes, and an external suite stays where it is.
    assert java.reuses_bytecode is False and java.copies_external_suite is False
    for name in ("reuses_bytecode", "copies_external_suite"):
        assert not hooks._present(JavaAdapter, hooks.hook(name)), name


def test_python_and_java_take_the_timeout_journal_by_the_default(tmp_path):
    """Both inherit the default, so their suite runs take the journal."""
    for cls in (PythonAdapter, JavaAdapter):
        assert _adapter(cls, tmp_path).provides_timeout_journal is True
        assert not hooks._present(cls, hooks.hook("provides_timeout_journal")), cls


def test_only_python_seeds_its_runs_as_delivered(tmp_path):
    """Python's run as delivered takes a session seed, so a consumer may
    make it the normal run. Java's refuses one, and the base states no
    seed."""
    assert LangAdapter.seeds_delivered_runs is False
    assert _adapter(PythonAdapter, tmp_path).seeds_delivered_runs is True
    assert hooks._present(PythonAdapter, hooks.hook("seeds_delivered_runs"))
    for cls in (JavaAdapter, Plain):
        assert _adapter(cls, tmp_path).seeds_delivered_runs is False
        assert not hooks._present(cls, hooks.hook("seeds_delivered_runs")), cls
    assert hooks.hook("seeds_delivered_runs").kind == "fact"


def test_a_source_is_renamed_to_fit_its_file_s_stem(tmp_path):
    from langadapter.java.source_analysis import rename_class
    from langadapter.sources import SourceStatus

    java = _adapter(JavaAdapter, tmp_path)
    source = ("public class JSONCompareTest {\n    JSONCompareTest() {}\n"
              "    static JSONCompareTest make() { return new JSONCompareTest(); }\n}\n")
    renamed = java.rename_file_unit(source, "JSONCompareResultTest")
    assert renamed == rename_class(source, "JSONCompareTest", "JSONCompareResultTest")
    assert renamed.completed and "JSONCompareTest" not in renamed.value
    assert renamed.value.count("JSONCompareResultTest") == 4
    unchanged = java.rename_file_unit(source, "JSONCompareTest")
    assert unchanged.completed and unchanged.value == source
    for nothing in ("int x;\n", "class {"):
        assert java.rename_file_unit(nothing, "CalcTest").value == nothing
    clash = "public class OldTest {\n    void OldTest2() {}\n    int OldTest;\n}\n"
    refused = java.rename_file_unit(clash, "NewTest")
    assert refused.status is SourceStatus.UNSUPPORTED and refused.value is None
    assert refused.reason and refused == rename_class(clash, "OldTest", "NewTest")
    for cls in (Plain, PythonAdapter):
        kept = _adapter(cls, tmp_path).rename_file_unit("class TestCalc:\n    pass\n", "x")
        assert kept.completed and kept.value == "class TestCalc:\n    pass\n"
    assert not hooks._present(Plain, hooks.hook("file_rule"))


def test_test_ids_and_kills_follow_each_language(tmp_path):
    from langadapter.suites import TestId

    test = TestId("tests/test_calc.py", "TestCalc.test_adds")
    matrix = {"mutants": [
        {"id": "m1", "killers": ["tests/test_calc.py::TestCalc::test_adds"],
         "killing_tests": [dataclasses.asdict(test)]},
        {"id": "m2", "killers": ["tests/test_other.py::test_x"], "killing_tests": [],
         "unmatched_killers": ["tests/test_other.py::test_x"]}]}
    python = _adapter(PythonAdapter, tmp_path)
    dotted = python.test_id_text(test)
    assert dotted == "tests.test_calc.TestCalc.test_adds"
    assert python.kill_tokens(matrix, {dotted}) == (
        {dotted: frozenset({"K:m1"})}, ("tests/test_other.py::test_x",))
    assert python.pool_kills(matrix, {"test_calc.py": 0}, {0: {"TestCalc.test_adds"}}) == {
        0: {"TestCalc.test_adds": {"K:m1"}}}
    java = _adapter(JavaAdapter, tmp_path)
    java_test = TestId("demo/CalcTest.java", "CalcTest.adds", text="demo.CalcTest#adds()")
    assert java.test_id_text(java_test) == "demo.CalcTest.adds"
    assert java.kill_tokens({"mutants": [{"id": 3, "killers": ["demo.CalcTest#adds()"],
                                          "killing_tests": [dataclasses.asdict(java_test)]}]},
                            {"demo.CalcTest.adds"}) == ({"demo.CalcTest.adds": frozenset({"K:3"})}, ())
    plain = _adapter(Plain, tmp_path)
    assert plain.test_id_text(test) == str(test)
    assert plain.kill_tokens(matrix, {str(test)}) == (
        {str(test): frozenset({"K:m1"})}, ("tests/test_other.py::test_x",))
    assert plain.pool_kills(matrix, {}, {}) == {}
    assert hooks._present(PythonAdapter, hooks.hook("kill_tokens"))
    assert not hooks._present(Plain, hooks.hook("kill_tokens"))


def test_a_hook_a_consumer_cannot_call_is_refused_when_its_class_is_made():
    import pytest

    with pytest.raises(TypeError) as refused:
        class Wrong(LangAdapter):
            language = "wrong"

            def prune_tests(self, source):
                return source

            def run_suite(self, request, extra):
                return None

    message = str(refused.value)
    assert "hook test_units: Wrong.prune_tests(self, source) cannot be called as " \
           "prune_tests(source, tests)." in message
    assert "hook run_suite: Wrong.run_suite(self, request, extra) cannot be called as " \
           "run_suite(request)." in message

    class Fine(LangAdapter):
        language = "fine"

        def prune_collection_breakers(self, source, *, timeout_s, report=None):
            return source

        @staticmethod
        def file_rule(source):
            return None

    assert hooks._signature_problems(Fine) == ()
    assert hooks._signature_problems(PythonAdapter) == hooks._signature_problems(JavaAdapter) == ()


def test_every_hook_method_langadapter_defines_is_checked():
    """A hook method LangAdapter defines, such as mutation_session_attempts,
    is checked by its own signature, so a wrong override is refused when its
    class is made, not when a mutation run calls it."""
    import pytest

    with pytest.raises(TypeError) as refused:
        class Wrong(LangAdapter):
            language = "wrong"

            def mutation_session_attempts(self, config):
                return ()

    assert ("hook open_mutation_session: Wrong.mutation_session_attempts(self, config) cannot be "
            "called as mutation_session_attempts(config, configured_workers=...") in str(refused.value)
    base = {name for name in {m for h in hooks.table() for m in h.methods}
            if callable(getattr(LangAdapter, name, None))}
    assert base <= set(hooks._CALLS)


def test_the_calls_with_keywords_are_checked_when_the_class_is_made():
    """run_as_delivered takes sources, request_options a run's wishes and
    prepare_suite its two switches, as the consumer passes them."""
    import pytest

    with pytest.raises(TypeError) as refused:
        class Short(LangAdapter):
            language = "short"

            def run_as_delivered(self, request):
                return None

            def request_options(self, purpose):
                return {}

            def prepare_suite(self, suite):
                return None

    message = str(refused.value)
    for call in ("run_as_delivered(request, sources=...)",
                 "request_options(purpose, target=..., traceback=..., stop_at_first_failure=..., "
                 "record_leaks=..., session_seed=..., reverse_order=..., "
                 "leave_out_timing_plugins=...)",
                 "prepare_suite(suite_root, isolation_config=...)"):
        assert f"cannot be called as {call}." in message


def test_the_collection_unit_is_the_adapter_s_declaration():
    from langadapter.java._profile import COLLECTION_UNIT as JAVA_UNIT
    from langadapter.python._profile import COLLECTION_UNIT as PYTHON_UNIT

    assert Plain.collection_unit == ""
    assert PythonAdapter.collection_unit == PYTHON_UNIT and "replaced by pass" in PYTHON_UNIT
    assert JavaAdapter.collection_unit == JAVA_UNIT and "javac" in JAVA_UNIT
    assert hooks.hook("collection_unit").family == "load repair"
    assert hooks._present(JavaAdapter, hooks.hook("collection_unit"))
    assert not hooks._present(Plain, hooks.hook("collection_unit"))


def test_the_java_profile_adds_to_the_jvm_profile_and_redefines_nothing():
    from langadapter.java._profile import JavaProfile
    from langadapter.java._profile import JvmProfile

    def names(cls):
        return {name for name in vars(cls) if not name.startswith("__")}

    assert names(JavaProfile) & names(JvmProfile) == set()
    assert names(JavaProfile) == {"refusal", "note", "timeout_marker"}


def test_the_facts_callers_read_are_declared_with_defaults():
    """A caller reads the merge separator and the validity policy version of
    any adapter: the base declares both (none), Python and Java their own,
    and Python's evidence fact is the one its LanguageFacts report."""
    from langadapter.java._validity import POLICY_VERSION as JAVA
    from langadapter.python._validity import POLICY_VERSION as PYTHON

    class Plain(LangAdapter):
        language = "plain"

    assert (Plain.source_merge_separator, Plain.validity_policy_version) == (None, None)
    assert (PythonAdapter.source_merge_separator, JavaAdapter.source_merge_separator) == ("#", "//")
    assert (PythonAdapter.validity_policy_version, JavaAdapter.validity_policy_version) == (
        PYTHON, JAVA)
    assert (PythonAdapter.unmatched_evidence_kept, JavaAdapter.unmatched_evidence_kept) == (
        True, False)


def test_declaring_javas_validity_version_loads_no_parser():
    """Importing LangAdapter, which registers the Java adapter, loads neither
    Java's validity policy nor tree-sitter: the adapter reads the policy's
    version from a module of its own."""
    import subprocess
    import sys

    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, langadapter; from langadapter.java.adapter import "
         "JavaAdapter; print(sorted(m for m in ('langadapter.java._validity', 'tree_sitter') "
         "if m in sys.modules))"], capture_output=True, text=True, check=True).stdout.strip()
    assert loaded == "[]"
    from langadapter.java._validity import POLICY_VERSION

    assert JavaAdapter.validity_policy_version == POLICY_VERSION


def test_importing_the_python_adapter_loads_no_tree_sitter():
    """The Python adapter's source operations are its base class, imported
    with it, and tree-sitter loads at the first parse, not before."""
    import subprocess
    import sys

    loaded = subprocess.run(
        [sys.executable, "-c", "import sys, langadapter; from langadapter.python.adapter import "
         "PythonAdapter; print(sorted(m for m in ('tree_sitter', 'tree_sitter_python') "
         "if m in sys.modules))"], capture_output=True, text=True, check=True).stdout.strip()
    assert loaded == "[]"
