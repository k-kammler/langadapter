"""Supported project Pythons: below 3.9 (Python 2 included) is refused by the
setup check and every process-backed operation before anything starts, and an
expected, untested or newer-than-the-library's version gets a note."""
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.capabilities import Availability
from langadapter.execution import LocalExecutor
from langadapter.python._profile import SUPPORT, PythonProfile
from langadapter.runs import (
    BenchmarkRequest, CollectionRequest, PerTestEvidenceRequest, SuiteRunRequest,
)

TESTED = "(tested 3.10 to 3.15)"
TODAY = "the interpreter starts, and the project's own imports and tests are not tried yet"


def _refusal(version):
    return (f"Python {version} is not supported: the Python adapter runs project Python "
            f"3.9 or newer {TESTED}, since {SUPPORT.runtime.reason}")


def _metadata(version):
    return json.dumps({"implementation": "CPython", "version": version,
                       "tools": {"pytest": "8.1.1", "coverage": "7.6.8", "cosmic-ray": "8.4.1"}})


@pytest.fixture
def program(tmp_path, monkeypatch):
    """A selected program whose answers to the full probe (``-I``) and to the
    minimal one (``-E``) each test sets."""
    (tmp_path / "subject.py").write_text("value = 1\n", encoding="ascii")
    # The selected interpreter is a file: the stand-in executor answers for it.
    (tmp_path / "runtime").write_text("selected interpreter", encoding="ascii")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(str(tmp_path / "runtime")),
                                environment=lb.Environment({"PATH": "selected"}, inherit=False))
    answers = {"-I": (0, _metadata("3.12.3")), "-E": (0, "CPython 3.12.3")}
    calls = []

    def run(executor, argv, **options):
        calls.append(argv)
        returncode, stdout = answers[argv[1]]
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    monkeypatch.setattr(LocalExecutor, "run", run)
    return context, answers, calls


def _operation(report, name):
    return next(item for item in report.operations if item.name == name)


def _check(context, operations=("tests.run_suite", "mutations.run")):
    report = lb.inspect_project("python", lb.ProjectSetupRequest(context, ("subject.py",), operations))
    return report, next(item for item in report.items if item.key == "runtime")


def test_python_2_is_named_and_unsupported_instead_of_not_python(program):
    context, answers, calls = program
    # Python 2 rejects -I ("Unknown option: -I") and exits with code 2.
    answers["-I"], answers["-E"] = (2, ""), (0, "CPython 2.7.18")
    report, runtime = _check(context)
    assert report.outcome is lb.Outcome.UNSUPPORTED and report.context is None
    assert runtime.outcome is lb.Outcome.UNSUPPORTED and runtime.readiness is Availability.UNSUPPORTED
    assert runtime.reason == report.reason == _refusal("2.7.18")
    assert runtime.action == "select a Python 3.9 or newer interpreter" and runtime.version is None
    assert [argv[1:4] for argv in calls] == [["-I", "-S", "-c"], ["-E", "-s", "-S"]]
    for name in ("tests.run_suite", "mutations.run"):
        operation = _operation(report, name)
        assert operation.readiness is Availability.UNSUPPORTED and operation.reason == _refusal("2.7.18")


def test_python_3_8_is_unsupported_from_the_full_probe(program):
    context, answers, calls = program
    answers["-I"] = (0, _metadata("3.8.20"))
    report, runtime = _check(context)
    assert report.outcome is lb.Outcome.UNSUPPORTED and runtime.reason == _refusal("3.8.20")
    assert len(calls) == 1
    verification = lb.verify_project("python", lb.ProjectSetupRequest(
        context, ("subject.py",), ("tests.run_suite",)))
    assert verification.outcome is lb.Outcome.UNSUPPORTED
    assert _operation(verification, "tests.run_suite").readiness is Availability.UNSUPPORTED


def test_an_unsupported_python_does_not_stop_a_check_of_source_operations_only(program):
    context, answers, _ = program
    answers["-I"] = (0, _metadata("3.8.20"))
    report, runtime = _check(context, ("sources.metrics",))
    assert report.outcome is lb.Outcome.COMPLETED and report.context is context
    assert runtime.outcome is lb.Outcome.UNSUPPORTED and not runtime.required


def test_a_supported_python_that_fails_the_full_probe_is_named(program):
    context, answers, _ = program
    answers["-I"] = (1, "")
    report, runtime = _check(context)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE
    assert runtime.reason == "the selected CPython 3.12.3 exited with code 1 when asked for its installed packages"


def test_a_program_that_fails_both_probes_keeps_its_message(program):
    context, answers, _ = program
    answers["-I"], answers["-E"] = (2, ""), (2, "")
    _, runtime = _check(context)
    assert runtime.reason.endswith("it may not be a Python interpreter")


@pytest.mark.parametrize("version, note", [
    ("3.9.18", "Python 3.9.18 is expected to work but is not among the tested versions (3.10 to 3.15)"),
    ("3.16.0", "Python 3.16.0 is not among the tested versions (3.10 to 3.15)"),
])
def test_an_expected_or_untested_python_completes_with_a_note(program, monkeypatch, version, note):
    context, answers, _ = program
    monkeypatch.setattr(PythonProfile, "library", (3, 16, 0))
    answers["-I"] = (0, _metadata(version))
    report, runtime = _check(context)
    assert report.outcome is lb.Outcome.COMPLETED and runtime.version == f"CPython {version}"
    assert runtime.reason == f"{TODAY}. {note}"


def test_a_tested_python_reads_as_before(program, monkeypatch):
    context, _, _ = program
    monkeypatch.setattr(PythonProfile, "library", (3, 12, 10))
    _, runtime = _check(context)
    assert runtime.reason == TODAY and runtime.version == "CPython 3.12.3"


def test_a_project_python_newer_than_the_librarys_gets_a_note(program, monkeypatch):
    context, _, _ = program
    monkeypatch.setattr(PythonProfile, "library", (3, 10, 4))
    _, runtime = _check(context)
    assert runtime.reason == (
        f"{TODAY}. LangAdapter runs on Python 3.10 and reads this project's Python 3.12 sources "
        "with its own parser; a source using newer syntax is reported as unreadable")


def test_every_operation_refuses_an_unsupported_python_before_anything_starts(tmp_path, monkeypatch):
    def started(*args, **kwargs):
        pytest.fail("a command started for an unsupported Python")

    (tmp_path / "test_calc.py").write_text("def test_one():\n    assert True\n", encoding="ascii")
    (tmp_path / "python").write_text("", encoding="ascii")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(tmp_path / "python")),
                                runtime_version="Python 2.7.18")
    adapter = lb.create("python", context)
    monkeypatch.setattr(subprocess, "Popen", started)
    suite = lb.Suite(str(tmp_path), ("test_calc.py",))
    results = [
        adapter.tests.run_suite(SuiteRunRequest(suite, 60)),
        adapter.tests.collect_suite(CollectionRequest(suite, 60)),
        adapter.tests.run_as_delivered(SuiteRunRequest(suite, 60, session_seed=1)),
        adapter.tests.per_test_evidence(PerTestEvidenceRequest(suite, "test_calc.py", 60)),
        adapter.tests.benchmark(BenchmarkRequest(suite, 1, 60)),
        adapter.mutations.operators("cosmic_ray"),
    ]
    for result in results:
        assert result.outcome is lb.Outcome.UNSUPPORTED and result.reason == _refusal("2.7.18"), result
    with adapter.mutations.open_session("cosmic_ray") as session:
        assert session.outcome is lb.Outcome.UNSUPPORTED and session.reason == _refusal("2.7.18")
    validation = adapter.sources.validate_source("value = 1\n")
    assert validation.status is lb.SourceStatus.UNSUPPORTED and validation.reason == _refusal("2.7.18")
    pruned = adapter.sources.prune_collection_breakers("value = 1\n", timeout_s=60)
    assert pruned.status == "unsupported" and pruned.detail == _refusal("2.7.18")
    # The source operations that run no command still work.
    assert adapter.sources.metrics("def test_one():\n    assert True\n").status is lb.SourceStatus.COMPLETED


def test_the_check_before_an_operation_starts_no_process_of_its_own(tmp_path, monkeypatch):
    """The check reads the version once from the toolchain probe that the
    run's identity reuses, so no --version process starts."""
    calls = []

    def run(executor, argv, **options):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, _metadata("3.12.3"), "")

    (tmp_path / "python").write_text("", encoding="ascii")
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(tmp_path / "python"))))
    monkeypatch.setattr(LocalExecutor, "run", run)
    assert adapter._runtime_refusal() == ""
    assert adapter.profile().runtime.text == "3.12.3"
    assert adapter.toolchain_identity().version == "3.12.3"
    assert len(calls) == 1 and calls[0][1] == "-c" and "--version" not in calls[0]


@pytest.mark.parametrize("disposed", [False, True])
def test_the_version_line_probe_keeps_an_unconfirmed_command(tmp_path, monkeypatch, disposed):
    """Every operation reads the version line first. A probe whose command
    may still run is raised, not read as an unknown version."""
    owner = SimpleNamespace(_closed=True, _langadapter_command_disposed=disposed)
    error = OSError("the probe failed")
    error._langadapter_command = owner

    def fail(executor, argv, **options):
        raise error

    (tmp_path / "python").write_text("", encoding="ascii")
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(tmp_path / "python"))))
    monkeypatch.setattr(LocalExecutor, "run", fail)
    if disposed:
        assert adapter._runtime_refusal() == "" and adapter.runtime_version() is None
    else:
        with pytest.raises(OSError) as caught:
            adapter._runtime_refusal()
        assert caught.value is error


@pytest.mark.parametrize("disposed", [False, True])
def test_the_toolchain_probe_keeps_an_unconfirmed_command_and_caches_no_failure(
        tmp_path, monkeypatch, disposed):
    """The probe that names a run's toolchain raises a command that may
    still run, and a probe that failed is asked again, not kept as an
    unknown toolchain for the adapter's life. A project file read in a
    container raises such a command too."""
    owner = SimpleNamespace(_closed=True, _langadapter_command_disposed=disposed)
    error = OSError("the probe failed")
    error._langadapter_command = owner
    failures = [error]

    def run(executor, argv, **options):
        if failures:
            raise failures.pop()
        return subprocess.CompletedProcess(argv, 0, _metadata("3.12.3"), "")

    (tmp_path / "python").write_text("", encoding="ascii")
    adapter = lb.create("python", lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(tmp_path / "python"))))
    monkeypatch.setattr(LocalExecutor, "run", run)
    if disposed:
        assert adapter.toolchain_identity().version != "3.12.3"
    else:
        with pytest.raises(OSError) as caught:
            adapter.toolchain_identity()
        assert caught.value is error
    assert adapter.toolchain_identity().version == "3.12.3"
    monkeypatch.setattr(adapter, "_container", lambda: True)
    monkeypatch.setattr(adapter, "_executor", lambda: SimpleNamespace(run=lambda argv, **options: (
        _ for _ in ()).throw(error)))
    if disposed:
        assert adapter._read_project_file("/project/pyproject.toml") is None
    else:
        with pytest.raises(OSError) as caught:
            adapter._read_project_file("/project/pyproject.toml")
        assert caught.value is error


def _version_program(directory, line):
    """An executable that answers only ``--version``, like an interpreter."""
    if os.name == "nt":
        path = directory / "python.cmd"
        path.write_text(f"@echo {line}\r\n", encoding="ascii")
    else:
        path = directory / "python"
        path.write_text(f"#!/bin/sh\necho '{line}'\n", encoding="ascii")
        path.chmod(0o755)
    return path


def test_a_run_reads_the_interpreters_version_line_to_refuse_it(tmp_path):
    (tmp_path / "test_calc.py").write_text("def test_one():\n    assert True\n", encoding="ascii")
    context = lb.ProjectContext(str(tmp_path), lb.Interpreter(path=str(_version_program(tmp_path, "Python 3.8.18"))))
    run = lb.create("python", context).tests.run_suite(SuiteRunRequest(lb.Suite(str(tmp_path), ("test_calc.py",)), 60))
    assert run.outcome is lb.Outcome.UNSUPPORTED and run.reason == _refusal("3.8.18")
