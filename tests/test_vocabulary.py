"""The test vocabulary: its entries, Python's and Java's words, and the
default of a language that names none."""
import hashlib
import json

import pytest

import langadapter as lb
from langadapter import hooks, vocabulary
from langadapter.adapter import LangAdapter
from langadapter.java._profile import JavaProfile
from langadapter.java.adapter import JavaAdapter, _vocabulary as java_vocabulary
from langadapter.python.adapter import VOCABULARY as PYTHON, PythonAdapter


@pytest.mark.parametrize("framework, digest", [
    ("JUnit 4", "57cf4c3f93c5d59046bbc1627277b142583540eecd49f022dbbfd15cf66164a0"),
    ("JUnit 5", "1330aef51615ecc45a8376a3530e6b8b33ca29882248e5cf43165cf6cc1de34f"),
    ("JUnit 6", "df536d8897fa489fe782f3739d7df8f7a9db3c7d524537f8c4e9f27612babe47"),
])
def test_java_words_for_each_junit_version_are_these_bytes(framework, digest):
    """The SHA-256 of Java's words in key order, for each JUnit version."""
    words = java_vocabulary(framework, JavaProfile.timeout_marker(framework))
    assert hashlib.sha256(json.dumps(words, sort_keys=True).encode("utf-8")).hexdigest() == digest


#: The entries Python and Java leave out: those on coverage counts that
#: cannot be read, which their coverage never reports, and the test file's
#: form, which their other entries say.
LEFT_OUT = {"coverage_counts_inconsistent_cause", "coverage_counts_inconsistent_fix",
            "test_file_form"}


def test_every_entry_says_what_its_text_is():
    assert len(set(vocabulary.NAMES)) == len(vocabulary.NAMES) == 22
    assert all(text.endswith(".") for _, text in vocabulary.ENTRIES)


def test_problems_accept_the_entries_on_coverage_counts_that_cannot_be_read():
    assert vocabulary.problems({"coverage_counts_inconsistent_cause": "Counts mean a fault.",
                                "coverage_counts_inconsistent_fix": "Change the test."}) == ()


def test_problems_name_an_unknown_entry_and_a_text_that_is_not_text():
    assert vocabulary.problems({"load_error": "load error"}) == ()
    assert vocabulary.problems({"lang_name": "Toy", "fixture_term": 3}) == (
        "'lang_name' is not a test vocabulary entry",
        "the entry fixture_term is int, not text")


def test_a_language_without_words_has_an_empty_vocabulary(tmp_path):
    class Plain(LangAdapter):
        language = "plain"

    assert Plain(lb.ProjectContext(str(tmp_path))).test_vocabulary() == {}
    entry = hooks.hook("test_vocabulary")
    assert not hooks._present(Plain, entry)
    assert hooks._present(PythonAdapter, entry) and hooks._present(JavaAdapter, entry)


def test_python_names_every_entry_but_those_it_leaves_out():
    assert set(PYTHON) == set(vocabulary.NAMES) - LEFT_OUT
    assert vocabulary.problems(PYTHON) == ()
    assert PYTHON["timeout_marker"].replace("{seconds}", "120") == (
        "``@pytest.mark.timeout(120)``")
    assert PYTHON["load_error"] == "collection error"


def _timeout_plugin(directory):
    """A stand-in pytest-timeout: the pytest11 entry point named timeout,
    found on the path of the project interpreter."""
    info = directory / "fake_timeout-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: fake-timeout\nVersion: 1.0\n",
                                   encoding="utf-8")
    (info / "entry_points.txt").write_text("[pytest11]\ntimeout = fake_timeout\n",
                                           encoding="utf-8")
    return str(directory)


def test_python_s_advice_and_load_errors_are_the_messages_text():
    """Python's advice for tests that stand alone and its load-error
    patterns, line for line."""
    assert PYTHON["stands_alone_advice"] == (
        "  1. A test must leave the module under test as it found it. Change\n     it only through monkeypatch, which restores what it set, or\n     restore it in a fixture; reloading the module in place replaces\n     every function and class the later tests use. To run import-time\n     code (an ``if TYPE_CHECKING:`` block, an ``except ImportError:``\n     fallback), reload it in a fixture that first saves\n     ``dict(module.__dict__)`` as ``saved`` and afterwards restores it\n     with ``module.__dict__.clear()`` and ``module.__dict__.update(saved)``.\n  2. A test must restore a library setting it changes: use the\n     library's context manager (np.errstate, np.printoptions,\n     xr.set_options) or save and restore the setting in a fixture.\n  3. A test must not rely on the measurement's fixed random seed or on\n     dask's default scheduler: seed or configure what it uses.")
    assert PYTHON["load_error_patterns"] == (
        "  - ImportError -> verify the name actually exists in the\n    target module / fix the import path.\n  - SyntaxError / IndentationError -> check the working file for\n    unbalanced brackets, bad indentation, stray characters from\n    a previous edit.\n  - NameError at module top-level -> ensure the name is\n    imported / defined before it's used.")


def test_python_s_validity_rule_is_the_prompts_text():
    """Python's rule for valid generated tests, word for word."""
    assert PYTHON["validity_rule"] == (
        "Allowed: the target's runtime behaviour and documented metadata (attributes, __dict__ and vars(), __name__, __doc__, annotations, inspect.signature). Forbidden: reading or analysing its source text (inspect.getsource, its file) or bytecode (__code__, dis), executing copied source, or attributing synthetic code to its source locations.")


def test_python_names_the_timeout_mark_only_where_pytest_has_it(tmp_path, monkeypatch):
    """Python names @pytest.mark.timeout only where pytest-timeout is
    installed: without it the mark does nothing, so a test that relies on
    it is not bounded."""
    import sys

    def adapter():
        return PythonAdapter(lb.ProjectContext(
            str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))

    with monkeypatch.context() as patched:
        patched.setenv("PYTHONPATH", _timeout_plugin(tmp_path / "site"))
        with_plugin = adapter()
        words = with_plugin.test_vocabulary()
        facts = with_plugin.language_facts()
    assert words == PYTHON and words is not PYTHON
    assert facts.timeout_marker == "@pytest.mark.timeout({seconds})"
    from importlib import metadata
    if any(point.name == "timeout" for point in metadata.entry_points(group="pytest11")):
        return
    without = adapter()
    words = without.test_vocabulary()
    assert words["timeout_marker"] == "" and "timeout" not in words["test_dependencies"]
    assert words["test_dependencies"].startswith(
        "Test toolkit (always available):\n  - pytest (the runner; @pytest.fixture, "
        "@pytest.mark.parametrize, pytest.raises)\n")
    assert {k: v for k, v in words.items() if k not in ("timeout_marker", "test_dependencies")} == {
        k: v for k, v in PYTHON.items() if k not in ("timeout_marker", "test_dependencies")}
    assert without.language_facts().timeout_marker is None


@pytest.mark.parametrize("framework, test_import, fixtures, failure", [
    ("JUnit 4", "org.junit.Test", "lifecycle methods (``@Before``, ``@After``)",
     "``@Test(expected = ...)``"),
    ("JUnit 5", "org.junit.jupiter.api.Test",
     "lifecycle methods (``@BeforeEach``, ``@AfterEach``)", "``assertThrows``"),
    ("JUnit 6", "org.junit.jupiter.api.Test",
     "lifecycle methods (``@BeforeEach``, ``@AfterEach``)", "``assertThrows``"),
])
def test_java_words_follow_the_junit_generation(framework, test_import, fixtures, failure):
    words = java_vocabulary(framework, JavaProfile.timeout_marker(framework))
    assert set(words) == set(vocabulary.NAMES) - LEFT_OUT
    assert vocabulary.problems(words) == ()
    assert words["test_declaration_full"] == f"``@Test`` method (``{test_import}``)"
    assert words["collection_rule"] == (
        f"{framework} runs the methods annotated with ``@Test`` (``{test_import}``) of the "
        "test class.")
    assert (words["fixture_term"], words["exception_assertion"]) == (fixtures, failure)
    assert words["timeout_marker"] == JavaProfile.timeout_marker(framework)
    assert words["fake_builtin_rule"] == ""
    # The validity rule names the Java policy's rules, not Python's.
    assert words["validity_rule"] == (
        "Allowed: the target's runtime behaviour and its API, including reflection on its classes (getDeclaredMethods, getAnnotations). Forbidden: reading its source file (the .java file through Files, a reader or stream, a Scanner or getResource) or its compiled class file (the .class bytes through getResource or a file), or declaring a type named like its class, which would test a copy in its place.")
    assert "__dict__" not in words["validity_rule"]
    # Java's own advice and load errors, with this JUnit version's annotations.
    after = "@After" if framework == "JUnit 4" else "@AfterEach"
    assert f"``{after}``: static fields and singletons" in words["stands_alone_advice"]
    assert words["stands_alone_advice"].startswith(
        "  1. A test file must compile and run on its own: declare in it\n")
    assert words["load_error_patterns"].startswith(
        "  - cannot find symbol -> import the class from its package, or fix\n")
    for entry in ("stands_alone_advice", "load_error_patterns"):
        assert not any(word in words[entry] for word in (
            "monkeypatch", "__dict__", "ImportError", "dask", "pytest")), entry
