"""Public Java verification with real prepared copies and controlled version probes."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import shutil
import stat
import subprocess
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter import _execution_scope, preparation
from langadapter.execution import LocalExecutor
from langadapter.java import _setup as _verification


def _tool_cache(directory, monkeypatch, *, omit=()):
    """A host Java tool cache holding the measurement jars' placeholder
    files, each pinned as the jar it stands for."""
    from langadapter.java import _jacoco
    from langadapter.java import _pit
    from langadapter.java import _tools

    placeholder = b"cached jar placeholder\n"
    for coordinates in (_jacoco.AGENT, *_jacoco.CORE, *_pit.JARS):
        monkeypatch.setitem(_tools.TOOL_SHA256, coordinates, hashlib.sha256(placeholder).hexdigest())
        if coordinates not in omit:
            path = directory / _tools._relative(coordinates)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(placeholder)
    return directory


def _without_action(report, operations):
    """Required items and operations that are not confirmed yet name no next step."""
    return ([item.key for item in report.items if item.required and item.action is None
             and not (item.readiness is lb.Availability.AVAILABLE and item.outcome is lb.Outcome.COMPLETED)]
            + [item.name for item in report.operations if item.name in operations
               and item.action is None and item.readiness is not lb.Availability.AVAILABLE])


@pytest.fixture
def selected(tmp_path, monkeypatch):
    root = tmp_path / "source"
    root.mkdir()
    (root / "Subject.java").write_bytes(b"class Subject {}\n")
    (root / "pom.xml").write_bytes(b"<project/>\n")
    home = tmp_path / "jdk"
    (home / "bin").mkdir(parents=True)
    (home / "release").write_text('JAVA_VERSION="21.0.11"\n', encoding="ascii")
    for tool in ("java", "javac"):
        (home / "bin" / (tool + (".exe" if os.name == "nt" else ""))).write_bytes(b"selected tool\n")
    cache = _tool_cache(tmp_path / "tools", monkeypatch)
    maven = tmp_path / "maven" / "mvn"
    maven.parent.mkdir()
    maven.write_bytes(b"selected launcher")
    context = lb.ProjectContext(str(root), lb.Interpreter(str(home)),
        execution=lb.LocalExecution(str(tmp_path)),
        environment=lb.Environment({"VALUE": "selected", "API_KEY": "private",
                                    "LANGADAPTER_JAVA_TOOLS": str(cache),
                                    "LANGADAPTER_MAVEN": str(maven)}, inherit=False))
    request = lb.ProjectSetupRequest(context, ("Subject.java",), ("tests.run_suite", "mutations.run"))
    state = SimpleNamespace(calls=[], owners=[], code=0, error=None, output=None, after_probe=None)
    native_prepare = _verification.prepare_project

    def prepare(context, **options):
        owner = native_prepare(context, **options)
        state.owners.append((owner, options))
        return owner

    def run(executor, argv, **options):
        if argv[-1] != "-version":
            # The build, after the version commands: the first run tries it.
            raise subprocess.TimeoutExpired(argv, options.get("timeout"))
        context = executor._context
        context._require_execution()
        assert context.root != request.context.root and options["cwd"] == context.root
        assert "env" not in options and context.environment is request.context.environment
        assert Path(context.root, "Subject.java").read_bytes() == b"class Subject {}\n"
        assert argv[-1] == "-version"
        state.calls.append((context, argv, options))
        if state.error is not None:
            raise state.error
        if state.after_probe is not None:
            state.after_probe()
        compiler = Path(argv[0]).stem == "javac"
        output = state.output if state.output is not None else (
            "javac 21.0.11\n" if compiler else 'openjdk version "21.0.11" 2026-04-21\n')
        return subprocess.CompletedProcess(argv, state.code, "" if not compiler else output,
                                           output if not compiler else "")

    monkeypatch.setattr(_verification, "prepare_project", prepare)
    monkeypatch.setattr(LocalExecutor, "run", run)
    monkeypatch.setenv("JAVA_HOME", "later ambient home")
    return request, state, home


@pytest.mark.parametrize("acquisition", [False, True])
def test_uncached_measurement_tools_are_missing_where_runs_may_not_download(selected, tmp_path, acquisition):
    request, state, _ = selected
    cache = tmp_path / "fresh"
    environment = lb.Environment(dict(request.context.environment, LANGADAPTER_JAVA_TOOLS=str(cache),
                                      LANGADAPTER_JAVA_TOOLS_OFFLINE="1"), inherit=False)
    request = replace(request, context=replace(request.context, environment=environment),
                      allow_acquisition=acquisition)
    for scope in (lb.inspect_project, lb.verify_project):
        report = scope("java", request)
        items = {item.key: item for item in report.items}
        assert items["pitest"].required and items["pitest"].readiness is lb.Availability.MISSING_SETUP
        assert "LANGADAPTER_JAVA_TOOLS_OFFLINE" in items["pitest"].action
        assert str(cache) not in items["pitest"].reason and items["pitest"].tried_by is None
        # Coverage was not requested of the suite run, so JaCoCo is observed, not required.
        assert not items["jacoco"].required
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert state.calls == [] and state.owners == [] and not cache.exists()


@pytest.mark.parametrize("acquisition", [False, True])
def test_uncached_measurement_tools_are_tried_by_a_run_that_may_download(selected, tmp_path, monkeypatch, acquisition):
    """On a fresh online install the jars are left to the first run, and
    verification downloads nothing."""
    from langadapter.java import _tools

    monkeypatch.setattr(_tools.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("verification downloaded"))
    request, state, _ = selected
    # The fixture's own cache, emptied: a fresh install with the same values.
    cache = Path(request.context.environment["LANGADAPTER_JAVA_TOOLS"])
    shutil.rmtree(cache)
    request = replace(request, allow_acquisition=acquisition)
    report = lb.verify_project("java", request)
    assert report.outcome is lb.Outcome.COMPLETED
    items = {item.key: item for item in report.items}
    assert items["pitest"].required and items["pitest"].readiness is None and items["pitest"].tried_by == "run"
    assert _not_run_only(report, request.required_operations) == [] and not cache.exists()


def test_a_run_that_may_not_download_reports_the_jar_unavailable(tmp_path, monkeypatch):
    from langadapter.java import _pit
    from langadapter.java import _tools

    monkeypatch.setattr(_tools.urllib.request, "urlopen", lambda *a, **kw: pytest.fail("offline run downloaded"))
    environment = lb.Environment({"LANGADAPTER_JAVA_TOOLS": str(tmp_path / "fresh"),
                                  "LANGADAPTER_JAVA_TOOLS_OFFLINE": "true"}, inherit=False)
    with pytest.raises(_tools.ToolUnavailable, match="downloads are off"):
        _tools.jar(_pit.JARS[0], environment)


@pytest.mark.parametrize("failure", ["cut body", "empty checksum"])
def test_a_failed_download_reports_the_jar_unavailable(tmp_path, monkeypatch, failure):
    """A body cut short (http.client.IncompleteRead, which is no OSError)
    and an empty published checksum leave the jar unavailable, as any
    other failed fetch does, and cache nothing."""
    import http.client
    import io

    from langadapter.java import _tools

    def fetch(url, **options):
        if url.endswith(".sha1"):
            return io.BytesIO(b"" if failure == "empty checksum" else b"0" * 40)
        if failure == "cut body":
            raise http.client.IncompleteRead(b"partial")
        return io.BytesIO(b"fixture jar")

    monkeypatch.setattr(_tools.urllib.request, "urlopen", fetch)
    cache = tmp_path / "cache"
    with pytest.raises(_tools.ToolUnavailable):
        _tools.jar("fixture:artifact:1", {"LANGADAPTER_JAVA_TOOLS": str(cache)})
    assert not list(cache.rglob("*.jar"))


def test_every_fixed_tool_jar_is_pinned():
    from langadapter.java import _jacoco, _pit, _profile, _runs, _tools

    fixed = {_jacoco.AGENT, *_jacoco.CORE, *_pit.JARS, *_runs.API, _tools.HAMCREST, _profile.JUNIT4_FLOOR}
    assert set(_tools.TOOL_SHA256) == fixed
    assert all(len(pin) == 64 and int(pin, 16) >= 0 for pin in _tools.TOOL_SHA256.values())


def test_a_pinned_jar_runs_only_with_its_pinned_sha256(tmp_path, monkeypatch):
    """A cached jar that is not the pinned one is refused without a fetch,
    a fetched one is checked against the pin, not against the server's
    checksum, and the pinned one is cached and read."""
    import io

    from langadapter.java import _jacoco, _tools

    data, requested = b"fixture jar", []

    def fetch(url, **options):
        requested.append(url)
        return io.BytesIO(hashlib.sha1(data).hexdigest().encode() if url.endswith(".sha1") else data)

    monkeypatch.setattr(_tools.urllib.request, "urlopen", fetch)
    cache = tmp_path / "cache"
    environment = {"LANGADAPTER_JAVA_TOOLS": str(cache)}
    cached = cache / _tools._relative(_jacoco.AGENT)
    cached.parent.mkdir(parents=True)
    cached.write_bytes(data)
    with pytest.raises(_tools.ToolUnavailable, match="in the tool cache .* does not match its pinned SHA-256"):
        _tools.jar(_jacoco.AGENT, environment)
    cached.unlink()
    with pytest.raises(_tools.ToolUnavailable, match="from Maven Central does not match its pinned SHA-256"):
        _tools.jar(_jacoco.AGENT, environment)
    assert not cached.exists() and len(requested) == 1 and not requested[0].endswith(".sha1")
    monkeypatch.setitem(_tools.TOOL_SHA256, _jacoco.AGENT, hashlib.sha256(data).hexdigest())
    assert _tools.jar(_jacoco.AGENT, environment).read_bytes() == data
    assert _tools.jar(_jacoco.AGENT, environment) == cached and len(requested) == 2


def _not_run_only(report, operations):
    return ([item.key for item in report.items if item.required and item.tried_by != "run"
             and not (item.readiness is lb.Availability.AVAILABLE and item.outcome is lb.Outcome.COMPLETED)]
            + [item.name for item in report.operations if item.name in operations
               and item.tried_by != "run" and item.readiness is not lb.Availability.AVAILABLE])


def test_uncached_tools_stay_unverified_where_they_can_still_be_downloaded(selected, tmp_path, monkeypatch):
    from langadapter.java import _pit

    request, state, _ = selected
    cache = _tool_cache(tmp_path / "partial", monkeypatch, omit=(_pit.JARS[0],))
    environment = lb.Environment(dict(request.context.environment, LANGADAPTER_JAVA_TOOLS=str(cache)),
                                 inherit=False)
    request = replace(request, context=replace(request.context, environment=environment))
    # Inspection leaves them to the first run, which may download them.
    report = lb.inspect_project("java", request)
    items = {item.key: item for item in report.items}
    assert items["pitest"].required and items["pitest"].readiness is None
    assert items["pitest"].outcome is None and "the first run downloads them" in items["pitest"].reason
    assert "offline" in items["pitest"].action
    operations = {item.name: item for item in report.operations}
    assert operations["mutations.open_session"].readiness is not lb.Availability.MISSING_SETUP
    assert report.outcome is lb.Outcome.COMPLETED and state.calls == []


@pytest.mark.parametrize("acquisition", [False, True])
def test_public_version_scope_preserves_selection_unknown_project_and_owned_cleanup(selected, acquisition):
    request, state, home = selected
    report = lb.verify_project("java", replace(request, allow_acquisition=acquisition))
    assert report.scope == "verification" and report.outcome is lb.Outcome.COMPLETED
    assert report.context is request.context and report.inputs.required == ("Subject.java", "pom.xml")
    assert len(state.calls) == 2 and len(state.owners) == 1
    assert len({context.root for context, _, _ in state.calls}) == 1
    assert [Path(argv[0]).stem for _, argv, _ in state.calls] == ["java", "javac"]
    assert all(Path(argv[0]).parent == home / "bin" and 0 < options["timeout"] <= 10
               for _, argv, options in state.calls)
    owner, options = state.owners[0]
    assert options["inputs"] == report.inputs and 0 < options["timeout_s"] <= 120
    assert not Path(owner.root).exists() and not owner._prepared_copy.baseline.root.exists()
    assert Path(request.context.root, "Subject.java").read_bytes() == b"class Subject {}\n"
    assert not Path(request.context.root, "target").exists()
    items = {item.key: item for item in report.items}
    assert all(items[key].version == "21.0.11" and items[key].readiness is lb.Availability.AVAILABLE
               for key in ("jvm", "compiler"))
    assert items["native_execution"].required and items["native_execution"].outcome is None
    assert all(item.readiness is not lb.Availability.AVAILABLE for item in report.operations)
    assert "private" not in repr(report.safe_export())
    # The version commands ran, and the build and operations are left to a run.
    run = "start a run, which tries it for the first time"
    assert items["native_execution"].action == run
    assert {item.action for item in report.operations if item.name in request.required_operations} == {run}
    assert not _without_action(report, request.required_operations)
    # What verification leaves unconfirmed (build, tool jars, operations) is
    # left to a run.
    unconfirmed = [item for item in report.items if item.required and item.readiness is not lb.Availability.AVAILABLE]
    assert {item.key for item in unconfirmed} >= {"build_descriptor", "native_execution"}
    assert {item.tried_by for item in unconfirmed} == {"run"}
    assert {item.tried_by for item in report.operations if item.name in request.required_operations} == {"run"}


def test_a_work_dir_disappearing_before_preparation_is_missing_setup(selected, tmp_path, monkeypatch):
    request, state, _ = selected
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    request = replace(request, context=replace(request.context, execution=lb.LocalExecution(str(work_dir))))
    selected_prepare = _verification.prepare_project

    def missing(context, **options):
        work_dir.rmdir()
        return selected_prepare(context, **options)

    monkeypatch.setattr(_verification, "prepare_project", missing)
    report = lb.verify_project("java", request)
    assert report.outcome is lb.Outcome.MISSING_SETUP and report.context is None
    assert next(item for item in report.items if item.key == "preparation_input").required
    assert not state.calls and not state.owners


def test_a_build_without_its_launcher_fails_verification_before_any_probe(selected):
    request, state, _ = selected
    environment = lb.Environment({key: value for key, value in request.context.environment.items()
                                  if key != "LANGADAPTER_MAVEN"}, inherit=False)
    report = lb.verify_project("java", replace(request, context=replace(request.context, environment=environment)))
    assert report.outcome is lb.Outcome.MISSING_SETUP and not state.calls and not state.owners
    launcher = next(item for item in report.items if item.key == "build_launcher")
    assert launcher.readiness is lb.Availability.MISSING_SETUP and "LANGADAPTER_MAVEN" in launcher.action
    assert not _without_action(report, request.required_operations)


@pytest.mark.parametrize("output", ["", "unexpected", 'openjdk version "private"', "javac nonsense", "javac 21 fake"])
def test_failed_version_judgment_never_becomes_missing_setup(selected, output):
    request, state, _ = selected
    state.output = output
    report = lb.verify_project("java", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and report.context is None
    assert len(state.calls) == 1 and not Path(state.owners[0][0].root).exists()
    assert next(item for item in report.items if item.key == "jvm").readiness is None
    assert all(item.readiness is None for item in report.operations if item.supported)
    items = {item.key: item for item in report.items}
    assert "-version and fix what it reports" in items["jvm"].action and items["jvm"].tried_by is None
    assert items["compiler"].tried_by == "verification"
    assert items["compiler"].action.startswith("fix ") and not _without_action(report, request.required_operations)


def test_nonzero_version_cannot_credit_success_looking_output(selected):
    request, state, _ = selected
    state.code = 3
    report = lb.verify_project("java", request)
    assert report.outcome is lb.Outcome.INFRASTRUCTURE_FAILURE and report.context is None
    assert next(item for item in report.items if item.key == "jvm").version is None


@pytest.mark.parametrize("error,outcome", [(FileNotFoundError("private"), lb.Outcome.MISSING_SETUP),
    (OSError("private"), lb.Outcome.INFRASTRUCTURE_FAILURE),
    (subprocess.TimeoutExpired("private", 1), lb.Outcome.TIMED_OUT)])
def test_confirmed_probe_failure_is_typed_after_owned_cleanup(selected, error, outcome):
    request, state, _ = selected
    state.error = error
    report = lb.verify_project("java", request)
    assert report.outcome is outcome and report.context is None
    assert not Path(state.owners[0][0].root).exists()
    assert next(item for item in report.items if item.key == "jvm").readiness is (
        lb.Availability.MISSING_SETUP if outcome is lb.Outcome.MISSING_SETUP else None)
    assert "private" not in repr(report.safe_export())
    assert not _without_action(report, request.required_operations)


@pytest.mark.parametrize("kind", ["pure", "absent_runtime", "missing_metadata", "ambiguous_build", "missing_build"])
def test_unjudged_or_pure_selection_never_allocates_or_substitutes_tools(selected, kind):
    request, state, home = selected
    if kind == "pure":
        request = replace(request, required_operations=("sources.metrics",))
    elif kind == "absent_runtime":
        request = replace(request, context=replace(request.context, interpreter=None))
    elif kind == "missing_metadata":
        (home / "release").unlink()
    elif kind == "ambiguous_build":
        Path(request.context.root, "build.gradle").write_bytes(b"do not execute\n")
    else:
        Path(request.context.root, "pom.xml").unlink()
    report = lb.verify_project("java", request)
    assert not state.calls and not state.owners
    assert report.outcome is ({"pure": lb.Outcome.COMPLETED, "absent_runtime": lb.Outcome.MISSING_SETUP,
        "missing_metadata": lb.Outcome.MISSING_SETUP, "ambiguous_build": lb.Outcome.COMPLETED,
        "missing_build": lb.Outcome.MISSING_SETUP}[kind])
    assert next(item for item in report.items if item.key == "native_execution").outcome is None
    assert not _without_action(report, request.required_operations)
    # A verification report never tells the reader to run the verification.
    assert not [entry for entry in (*report.items, *report.operations)
                if entry.action == "run the tool check to try it"]


def test_readonly_java_inputs_stay_readonly_through_use_and_close(selected):
    request, state, _ = selected
    original = Path(request.context.root, "Subject.java")
    original.chmod(stat.S_IRUSR)
    mode = original.stat().st_mode
    try:
        report = lb.verify_project("java", request)
        assert report.outcome is lb.Outcome.COMPLETED
        assert original.stat().st_mode == mode and original.read_bytes() == b"class Subject {}\n"
        assert not Path(state.owners[0][0].root).exists()
    finally:
        original.chmod(mode | stat.S_IWUSR)


def test_unresolved_probe_retains_actual_owner_until_same_owner_recovery(selected, monkeypatch):
    request, state, _ = selected
    command = SimpleNamespace(_langadapter_command_disposed=False)
    primary = OSError("same unresolved command")
    primary._langadapter_command = command
    run = LocalExecutor.run

    def unresolved(executor, argv, **options):
        _execution_scope.track(command, "Java version command")
        state.error = primary
        return run(executor, argv, **options)

    monkeypatch.setattr(LocalExecutor, "run", unresolved)
    with pytest.raises(OSError) as caught:
        lb.verify_project("java", request)
    owner, _ = state.owners[0]
    assert caught.value is primary and primary._langadapter_prepared_project is owner
    assert primary._langadapter_command is command
    if hasattr(primary, "add_note"):
        assert any("Java verification copy cleanup failed" in note for note in primary.__notes__)
    assert Path(owner.root).exists() and Path(owner._prepared_copy.owned.receipt).is_file()
    _execution_scope.completed(command)
    command._langadapter_command_disposed = True
    owner.close()
    owner.close()
    assert not Path(owner.root).exists()


def test_postallocation_fault_preserves_original_factory_owner(selected, monkeypatch):
    request, _, _ = selected
    primary = OSError("same factory fault")
    verify = preparation._verify_independent

    def fail(prepared_copy, parent):
        verify(prepared_copy, parent)
        raise primary

    monkeypatch.setattr(preparation, "_verify_independent", fail)
    with pytest.raises(OSError) as caught:
        lb.verify_project("java", request)
    assert caught.value is primary
    owner = primary._langadapter_prepared_project
    assert Path(owner.root).exists() and Path(owner._prepared_copy.owned.receipt).is_file()
    owner.close()


def test_cancellation_preserves_same_exception_and_closes_idle_copy(selected):
    request, state, _ = selected
    state.error = KeyboardInterrupt("same cancellation")
    with pytest.raises(KeyboardInterrupt) as caught:
        lb.verify_project("java", request)
    assert caught.value is state.error and not Path(state.owners[0][0].root).exists()
