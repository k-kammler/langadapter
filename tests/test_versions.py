"""One description of a version for every adapter, one declaration of what a
language supports, and the classification of a found version against it."""
import pytest

from langadapter.errors import InvalidRequest
from langadapter.versions import (
    LanguageSupport, RuntimeSupport, Support, UNDECLARED, Version, VersionProfile,
    VersionSpan, java_version, numbers_of, python_version,
)

PYTHON = RuntimeSupport("project Python", tested=(VersionSpan((3, 10), (3, 13)),),
                        expected=(VersionSpan((3, 9), (3, 9)),),
                        reason="the pinned tools need Python 3.9")
JDK = RuntimeSupport("JDK", tested=tuple(VersionSpan((n,), (n,)) for n in (11, 17, 21, 25)),
                     expected=(VersionSpan((12,), (16,)), VersionSpan((18,), (20,)),
                               VersionSpan((22,), (24,))),
                     reason="PIT 1.30 needs Java 11")


@pytest.mark.parametrize("text, numbers", [
    ("3.12.10", (3, 12, 10)), ("3.13.0rc1", (3, 13, 0)), ("2.7.18", (2, 7, 18)),
    ("3.14", (3, 14)), ("unknown", ()), ("", ())])
def test_a_python_version_keeps_its_text_and_reads_its_numbers(text, numbers):
    version = python_version("CPython", text, "probe")
    assert (version.implementation, version.numbers, version.text, version.source) == (
        "CPython", numbers, text, "probe")


@pytest.mark.parametrize("text, numbers, feature", [
    ("21.0.11", (21, 0, 11), 21), ("1.8.0_412", (8, 0, 412), 8), ("17", (17,), 17),
    ("21.0.12.1", (21, 0, 12, 1), 21), ("11.0.32.1", (11, 0, 32, 1), 11)])
def test_a_jdk_version_reads_the_legacy_scheme_as_its_feature_version(text, numbers, feature):
    version = java_version(text, "release file")
    assert version.numbers == numbers and version.feature == feature and version.text == text


@pytest.mark.parametrize("text", ["1.8.0_412", "1.8", "11-ea", "17", " 21.0.11", "25.0.4.1"])
def test_the_jdk_feature_reader_is_the_shared_parser(text):
    from langadapter.java._env import feature_of

    assert feature_of(text) == java_version(text, "release file").feature


def test_a_jdk_text_without_numbers_has_no_feature_to_read():
    from langadapter.java._env import feature_of

    with pytest.raises(ValueError):
        feature_of("${java.version}")


def test_a_version_without_numbers_has_no_feature():
    assert python_version("CPython", "unknown", "probe").feature is None
    assert numbers_of("rc1") == ()


@pytest.mark.parametrize("field, value", [
    ("implementation", ""), ("numbers", (3, -1)), ("source", "guess")])
def test_a_version_refuses_what_it_cannot_describe(field, value):
    values = dict(implementation="CPython", numbers=(3, 12), text="3.12", source="probe")
    values[field] = value
    with pytest.raises(InvalidRequest, match=field):
        Version(**values)


@pytest.mark.parametrize("text, support", [
    ("3.12.10", Support.TESTED), ("3.10.0", Support.TESTED), ("3.13.9", Support.TESTED),
    ("3.9.18", Support.EXPECTED), ("3.8.20", Support.UNSUPPORTED), ("2.7.18", Support.UNSUPPORTED),
    ("3.14.0", Support.UNTESTED), ("4.0", Support.UNTESTED), ("unknown", Support.UNTESTED),
    # Fewer numbers than the oldest bound: "3" may be 3.9 or newer, "2" and "3.8" are below.
    ("3", Support.UNTESTED), ("2", Support.UNSUPPORTED), ("3.8", Support.UNSUPPORTED)])
def test_a_python_version_is_classified_against_the_declaration(text, support):
    assert PYTHON.classify(python_version("CPython", text, "probe")) is support


@pytest.mark.parametrize("text, support", [
    ("21.0.11", Support.TESTED), ("11.0.32.1", Support.TESTED), ("25", Support.TESTED),
    ("19.0.2", Support.EXPECTED), ("1.8.0_412", Support.UNSUPPORTED), ("10.0.2", Support.UNSUPPORTED),
    ("26", Support.UNTESTED)])
def test_a_jdk_version_is_classified_against_the_declaration(text, support):
    assert JDK.classify(java_version(text, "release file")) is support


def test_a_range_is_described_in_words():
    assert PYTHON.describe() == "3.9 or newer (tested 3.10 to 3.13)"
    assert JDK.describe() == "11 or newer (tested 11, 17, 21, 25)"
    assert VersionSpan((26,), None).text() == "26 or newer"


def test_a_span_needs_ordered_bounds():
    with pytest.raises(InvalidRequest):
        VersionSpan((3, 13), (3, 10))
    with pytest.raises(InvalidRequest):
        VersionSpan((), None)


def test_an_undeclared_language():
    assert UNDECLARED.classify(python_version("CPython", "3.12", "probe")) is Support.UNTESTED


def test_of_keeps_the_identitys_texts():
    from langadapter.outcomes import ToolchainIdentity

    identity = ToolchainIdentity.of(java_version("21.0.11", "release file"), (("junit-platform", "1.14.4"),))
    assert identity == ToolchainIdentity("java", "21.0.11", (("junit-platform", "1.14.4"),))
    assert ToolchainIdentity.of(python_version("CPython", "3.12.10", "probe")).to_dict() == {
        "implementation": "CPython", "version": "3.12.10", "tools": {}, "structural_parser": None}


def test_the_report_records_keep_todays_texts():
    from langadapter.versions import capability_runtime, facts_runtime, python_line, setup_runtime

    jdk, cpython = java_version("21.0.11", "release file"), python_version("CPython", "3.12.10", "probe")
    assert capability_runtime(jdk) == "Java 21.0.11" and capability_runtime(cpython) == "CPython 3.12.10"
    assert setup_runtime(jdk) == "21.0.11" and setup_runtime(cpython) == "CPython 3.12.10"
    assert facts_runtime(jdk) == "21.0.11"
    assert facts_runtime(python_line("Python 3.12.3", "probe")) == "Python 3.12.3"
    assert python_line("Python 3.12.3", "probe").numbers == (3, 12, 3)


def test_a_record_refuses_a_text_its_version_does_not_give():
    import langadapter as lb
    from langadapter.capabilities import CapabilityReport, LanguageFacts

    jdk = java_version("21.0.11", "release file")
    report = dict(language="java", interpreter="java", outcome=lb.Outcome.COMPLETED, reason="",
                  capabilities=(), tools=(), branch_coverage="counters", mutation_engines=())
    assert CapabilityReport(**report, runtime="Java 21.0.11", version_record=jdk).runtime == "Java 21.0.11"
    assert CapabilityReport(**report, runtime="anything").version_record is None
    with pytest.raises(InvalidRequest, match="runtime"):
        CapabilityReport(**report, runtime="Java 21", version_record=jdk)
    facts = dict(language="Java", test_framework="JUnit 5", code_fence="java",
                 test_file_patterns=("*Test.java",), test_function_pattern="@Test",
                 test_class_pattern="*Test", timeout_marker=None)
    assert LanguageFacts(**facts, runtime_version="21.0.11", version_record=jdk).runtime_version == "21.0.11"
    with pytest.raises(InvalidRequest, match="runtime_version"):
        LanguageFacts(**facts, runtime_version="Java 21.0.11", version_record=jdk)
    item = dict(key="runtime", required=True, readiness=lb.Availability.AVAILABLE,
                outcome=lb.Outcome.COMPLETED, selected="/jdk")
    cpython = python_version("CPython", "3.12.10", "probe")
    assert lb.ProjectSetupItem(**item, version="CPython 3.12.10", version_record=cpython).version
    with pytest.raises(InvalidRequest, match="version"):
        lb.ProjectSetupItem(**item, version="3.12.10", version_record=cpython)


def test_the_java_profile_refuses_a_jdk_with_the_warmed_setups_texts():
    from langadapter.java._profile import JavaProfile

    assert JavaProfile.preparation_refusal(8) == "Java preparation requires JDK 11 or newer, not 8"
    assert JavaProfile.preparation_refusal(11) is None
    assert JavaProfile.run_refusal(11, 17, "1.14.4") == (
        "the project's test release and JUnit Platform need JDK 17 or newer, not 11")
    assert JavaProfile.run_refusal(11, 8, "6.0.1") == (
        "the project's test release and JUnit Platform need JDK 17 or newer, not 11")
    assert JavaProfile.run_refusal(21, 17, "6.0.1") is None and JavaProfile.run_refusal(11, 8, "1.14.4") is None


def test_a_jar_is_found_by_its_whole_artifact_name():
    """hamcrest-junit holds no JUnit: the artifact's name is the whole file
    name before its version, in both lookups."""
    from langadapter.java import _tools as _junit
    from langadapter.java._profile import _jar_version

    classpath = ("/repo/org/hamcrest/hamcrest-junit/2.0.0.0/hamcrest-junit-2.0.0.0.jar",
                 "/repo/junit/junit/4.13.2/junit-4.13.2.jar")
    for lookup in (_jar_version, _junit._version):
        assert lookup(classpath[:1], "junit") is None
        assert lookup(classpath, "junit") == "4.13.2"
        assert lookup(classpath, "hamcrest-junit") == "2.0.0.0"
        assert lookup(("/lib/junit-jupiter-api-5.14.4.jar",), "junit") is None
        assert lookup(("/lib/junit-jupiter-api-5.14.4.jar",), "junit-jupiter-api") == "5.14.4"


def test_the_java_profile_answers_as_the_rules_it_replaced():
    from langadapter.java._profile import JavaProfile
    from langadapter.java._profile import JUNIT4_FLOOR

    assert JavaProfile.runtime_for(8) == 11 and JavaProfile.runtime_for(17) == 17
    assert JavaProfile.minimum_runtime(8, "1.14.4") == 11 and JavaProfile.minimum_runtime(8, "6.0.1") == 17
    assert JavaProfile.minimum_runtime(21, "6.0.1") == 21
    assert JavaProfile.platform_of_jupiter("5.14.4") == "1.14.4" and JavaProfile.platform_of_jupiter("6.0.1") == "6.0.1"
    assert JavaProfile.family("1.14.4") == "5.14.4" and JavaProfile.family("6.0.1") == "6.0.1"
    assert JavaProfile.junit4_substitute("4.11") == JUNIT4_FLOOR
    assert JavaProfile.junit4_substitute("4.13.2") is None and JavaProfile.junit4_substitute(None) is None
    assert JavaProfile.framework_name("5.14.4", True) == "JUnit 5"
    assert JavaProfile.framework_name("6.0.1", False) == "JUnit 6"
    assert JavaProfile.framework_name(None, True) == "JUnit 4" and JavaProfile.framework_name(None, False) == "JUnit 5"
    assert JavaProfile.timeout_marker("JUnit 4") == "@Test(timeout = {seconds}000)"
    assert JavaProfile.timeout_marker("JUnit 5") == "@Timeout({seconds})"
    assert JavaProfile.helper_release == 11 and JavaProfile.minimum_jdk == 11
    profile = JavaProfile(java_version("1.8.0_412", "release file"), LanguageSupport(JDK))
    assert profile.support is Support.UNSUPPORTED


def test_a_profile_is_chosen_once_with_its_classification():
    profile = VersionProfile(java_version("21.0.11", "release file"), LanguageSupport(JDK))
    assert profile.support is Support.TESTED and profile.runtime.feature == 21
    assert VersionProfile(None).support is Support.UNTESTED
