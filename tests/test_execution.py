"""Where project commands run: the local and container executors, the
environment that crosses into a container, and the work directory."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import langadapter as lb
from langadapter import execution
from langadapter.python import _runs


def _recording(monkeypatch, seen, stdout=b""):
    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return subprocess.CompletedProcess(argv, 0, stdout, b"")

    monkeypatch.setattr(execution._process_tree, "run", fake_run)


def test_local_executor_runs_directly():
    completed = execution.LocalExecutor().run(
        [sys.executable, "-c", "print('ok')"], timeout=30)
    assert completed.returncode == 0
    assert "ok" in completed.stdout


def test_local_executor_passes_input():
    completed = execution.LocalExecutor().run(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"],
        input="piped", timeout=30)
    assert "piped" in completed.stdout


def test_capture_keeps_raw_stdin_and_normalizes_output_newlines():
    completed = execution.run_capture(
        [sys.executable, "-c",
         "import sys; data = sys.stdin.buffer.read(); print(data.hex()); "
         "sys.stderr.buffer.write(b'first\\r\\nsecond\\rthird\\n')"],
        input="a\nb\n", timeout=30)

    assert completed.returncode == 0
    assert completed.stdout == "610a620a\n"
    assert completed.stderr == "first\nsecond\nthird\n"


def test_stdin_is_passed_as_bytes(monkeypatch):
    """Newlines are not rewritten to the OS separator, which would corrupt a
    patch piped to git apply on Windows."""
    seen = {}
    _recording(monkeypatch, seen, stdout=b"out")
    execution.LocalExecutor().run(["x"], input="a\nb")
    assert seen["kw"]["input"] == b"a\nb"


def test_container_executor_builds_docker_exec(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen, stdout=b"out")
    env = {"LANGADAPTER_X": "1", "PYTHONPATH": "/p", "PYTHONHASHSEED": "0",
           "OTHER_TOOL_Y": "2", "HOME": "/home/host", "PATH": "/usr/bin"}
    execution.ContainerExecutor("c1", runner="docker").run(
        ["py", "-m", "pytest"], cwd="/testbed", env=env, timeout=99)
    argv = seen["argv"]
    assert argv[:2] == ["docker", "exec"]
    assert "-w" in argv and "/testbed" in argv
    assert "c1" in argv
    assert argv[-3:] == ["py", "-m", "pytest"]
    joined = " ".join(argv)
    # Only LangAdapter's variables, the import path and the hash seed cross.
    assert "LANGADAPTER_X=1" in joined
    assert "PYTHONPATH=/p" in joined
    assert "PYTHONHASHSEED=0" in joined
    assert "OTHER_TOOL_Y" not in joined
    assert "HOME=/home/host" not in joined
    assert "PATH=/usr/bin" not in joined
    assert seen["kw"].get("timeout") == 99


def test_a_caller_names_what_else_crosses(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen)
    execution.ContainerExecutor(
        "c1", forward_prefixes=("LANGADAPTER_", "OTHER_TOOL_"),
        forward_keys=("EXTRA",),
    ).run(["py"], env={"OTHER_TOOL_Y": "2", "EXTRA": "3", "NOT": "4"})
    joined = " ".join(seen["argv"])
    assert "OTHER_TOOL_Y=2" in joined and "EXTRA=3" in joined
    assert "NOT=4" not in joined


def test_container_executor_input_adds_dash_i(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen)
    execution.ContainerExecutor("c1").run(["py", "-c", "x"], input="a\nb")
    assert "-i" in seen["argv"]
    assert seen["kw"].get("input") == b"a\nb"


def test_container_work_dir_is_the_host_path_on_posix():
    assert execution.container_work_dir_for("/home/user/wz/run") == \
        "/home/user/wz/run"


def test_container_work_dir_is_fixed_for_a_windows_path():
    assert execution.container_work_dir_for(r"C:\Users\k\wz\run") == \
        "/workzone"


def test_translation_is_identity_on_a_posix_host():
    ex = execution.ContainerExecutor("c", work_dir="/home/user/wz/run")
    assert ex._to_container("/home/user/wz/run/cov.json") == \
        "/home/user/wz/run/cov.json"


def test_translation_rewrites_windows_paths_including_embedded():
    wz = r"C:\Users\k\wz\run"
    ex = execution.ContainerExecutor("c", work_dir=wz)
    assert ex._to_container(wz + r"\d\cov.json") == "/workzone/d/cov.json"
    assert ex._to_container(r"--rcfile=" + wz + r"\cov.rc") == \
        "--rcfile=/workzone/cov.rc"
    assert ex._to_container("json:" + wz + r"\c.json") == \
        "json:/workzone/c.json"
    # Container paths (the repository, the interpreter) are untouched.
    assert ex._to_container("/home/swe/repo/pkg/m.py") == \
        "/home/swe/repo/pkg/m.py"


@pytest.mark.parametrize("value", [
    "C:/workzone-archive/results.json",
    "prefixC:/workzone/file.py",
    "C:/other/C:/workzone/file.py",
])
def test_translation_does_not_rewrite_similar_unmounted_paths(value):
    executor = execution.ContainerExecutor("c", work_dir="C:/workzone")
    assert executor._to_container(value) == value


def test_posix_translation_is_case_sensitive():
    executor = execution.ContainerExecutor(
        "c", work_dir="/host/Zone", container_work_dir="/mapped")
    assert executor._to_container("/host/zone/file") == "/host/zone/file"
    assert executor._to_container("/host/Zone/file") == "/mapped/file"


def test_windows_translation_keeps_case_insensitive_path_matching():
    executor = execution.ContainerExecutor("c", work_dir="C:/WORKZONE")
    assert executor._to_container("c:/workzone/file") == "/workzone/file"


def test_translation_handles_exact_work_dir_entries_in_path_lists():
    executor = execution.ContainerExecutor("c", work_dir="C:/zone")
    assert executor._to_container("C:/zone;C:/zone/lib;C:/zone-backup") == (
        "/workzone;/workzone/lib;C:/zone-backup")


def test_a_path_written_into_a_file_is_rebased_as_the_arguments_are():
    """``container_path`` rebases a work-dir path written into a file onto
    the container work directory the executor uses for arguments: the context's
    explicit one, else the default."""
    cases = [
        (lb.ContainerExecution(container="c", work_dir="/home/k/wz"),
         "/home/k/wz/cr/s.sqlite", "/home/k/wz/cr/s.sqlite"),
        (lb.ContainerExecution(container="c", work_dir=r"C:\Users\k\wz"),
         r"C:\Users\k\wz\cr\s.sqlite", "/workzone/cr/s.sqlite"),
        (lb.ContainerExecution(container="c", work_dir="/home/k/wz",
                               container_work_dir="/mnt/wz"),
         "/home/k/wz/cr/s.sqlite", "/mnt/wz/cr/s.sqlite"),
        (lb.ContainerExecution(container="c", work_dir=r"C:\Users\k\wz",
                               container_work_dir="/mnt/wz"),
         "C:/Users/k/wz/t/conftest.py", "/mnt/wz/t/conftest.py"),
    ]
    for context, path, expected in cases:
        assert execution.container_path(context, path) == expected
        executor = execution.ContainerExecutor(
            "c", work_dir=context.work_dir,
            container_work_dir=context.container_work_dir)
        assert executor._to_container(path) == expected
    # The container's repository lies outside the work directory and stays.
    assert execution.container_path(
        lb.ContainerExecution(container="c", work_dir=r"C:\Users\k\wz"),
        "/home/swe/repo/pkg/m.py") == "/home/swe/repo/pkg/m.py"


def test_rebase_path_without_a_work_dir_or_a_move_writes_posix():
    assert execution.rebase_path("/x/y", None, None) == "/x/y"
    assert execution.rebase_path("/data/wz/cr/s", "/data/wz",
                                 "/data/wz") == "/data/wz/cr/s"
    assert execution.rebase_path(r"C:\other\f.py", r"C:\Users\k\wz",
                                 "/workzone") == "C:/other/f.py"


def test_a_windows_run_translates_argv_and_env(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen)
    wz = r"C:\Users\k\wz\run"
    execution.ContainerExecutor("c1", work_dir=wz).run(
        [r"--data-file=" + wz + r"\d"], cwd="/home/swe/repo",
        env={"LANGADAPTER_WORK_DIR": wz})
    joined = " ".join(seen["argv"])
    assert "--data-file=/workzone/d" in joined
    assert "LANGADAPTER_WORK_DIR=/workzone" in joined
    assert "-w" in seen["argv"] and "/home/swe/repo" in seen["argv"]


def test_apptainer_uses_its_own_exec_flags(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen)
    execution.ContainerExecutor("instance://c", runner="apptainer").run(
        ["py", "-c", "x"], cwd="/repo", env={"LANGADAPTER_X": "1"},
        input="src")
    argv = seen["argv"]
    assert argv[:2] == ["apptainer", "exec"]
    assert "--pwd" in argv and "/repo" in argv
    assert "--env" in argv and "LANGADAPTER_X=1" in argv
    assert "-i" not in argv


def test_apptainer_exec_uses_cleanenv_and_docker_does_not(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen)
    execution.ContainerExecutor("c", runner="apptainer").run(["py"])
    assert "--cleanenv" in seen["argv"]
    execution.ContainerExecutor("c", runner="docker").run(["py"])
    assert "--cleanenv" not in seen["argv"]


def test_apptainer_prefixes_a_bare_instance_name_once(monkeypatch):
    seen = {}
    _recording(monkeypatch, seen)
    execution.ContainerExecutor("run1", runner="apptainer").run(["py"])
    assert "instance://run1" in seen["argv"]
    execution.ContainerExecutor("instance://c", runner="apptainer").run(["py"])
    assert "instance://c" in seen["argv"]
    assert "instance://instance://c" not in seen["argv"]
    execution.ContainerExecutor("run1", runner="docker").run(["py"])
    assert "run1" in seen["argv"]
    assert not any(str(a).startswith("instance://") for a in seen["argv"])


def _local(tmp_path, **changes):
    return lb.ProjectContext(root=str(tmp_path),
                             interpreter=lb.Interpreter(path=sys.executable),
                             **changes)


def _container(tmp_path, **changes):
    return lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(
            container="c1", work_dir=str(tmp_path), runner="podman"),
        **changes)


def test_the_context_chooses_the_executor(tmp_path, monkeypatch):
    assert isinstance(execution.executor_for(_local(tmp_path)),
                      execution.LocalExecutor)
    seen = {}
    _recording(monkeypatch, seen)
    executor = execution.executor_for(
        _container(tmp_path, environment={"PROJECT_FLAG": "1"}),
        forward_keys=("EXTRA",))
    assert isinstance(executor, execution.ContainerExecutor)
    executor.run(["py"], env={"PROJECT_FLAG": "1", "EXTRA": "2",
                              "LANGADAPTER_A": "3", "NOT": "4"})
    joined = " ".join(seen["argv"])
    assert seen["argv"][:2] == ["podman", "exec"]
    assert "PROJECT_FLAG=1" in joined and "EXTRA=2" in joined
    assert "LANGADAPTER_A=3" in joined and "NOT=4" not in joined


def test_path_lists_use_the_executing_side_separator(tmp_path):
    assert execution.path_separator(_container(tmp_path)) == ":"
    assert execution.path_separator(_local(tmp_path)) == os.pathsep
    env = {"PYTHONPATH": "/a"}
    execution.prepend_path(env, "PYTHONPATH", "/root", ":")
    assert env["PYTHONPATH"] == "/root:/a"
    empty = {}
    execution.prepend_path(empty, "PYTHONPATH", "/root", ":")
    assert empty["PYTHONPATH"] == "/root"


def test_utf8_environment_keeps_values_the_caller_set():
    env = {"PYTHONIOENCODING": "latin-1"}
    assert execution.utf8_environment(env) is env
    assert env == {"PYTHONIOENCODING": "latin-1", "PYTHONUTF8": "1"}


def test_a_work_dir_directory_lies_in_the_work_dir_and_is_removed(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    context = _local(tmp_path, execution=lb.LocalExecution(work_dir=str(zone)))
    with execution.work_dir_tempdir(
            context, execution.LocalExecutor()) as directory:
        assert Path(directory).parent == zone
        (Path(directory) / "data").write_text("x", encoding="utf-8")
    assert not Path(directory).exists()


def test_a_container_work_dir_directory_is_removed_inside_first(tmp_path):
    calls = []

    class Recording(execution.Executor):
        def run(self, argv, **kw):
            calls.append(list(argv))
            output = Path(argv[1]).read_text(encoding="ascii") if argv[0] == "cat" else ""
            return subprocess.CompletedProcess(argv, 0, output, "")

    with execution.work_dir_tempdir(_container(tmp_path), Recording()) as d:
        assert Path(d).parent == tmp_path
    marker = str(Path(d) / ".langadapter-owner")
    assert calls == [["cat", marker], ["cat", marker], ["rm", "-rf", "--", d]]
    assert not Path(d).exists()


def test_scratch_cleanup_keeps_the_original_unresolved_command(tmp_path, monkeypatch):
    from types import SimpleNamespace

    owners = []
    create = execution.OwnedDirectory.create
    error = OSError("container cleanup ownership unresolved")
    error._langadapter_command = SimpleNamespace(_closed=True)

    def allocate(**options):
        owner = create(**options, receipt_parent=str(tmp_path))
        owners.append(owner)
        return owner

    class Recording(execution.Executor):
        def run(self, argv, **kw):
            if argv[0] == "rm":
                raise error
            return subprocess.CompletedProcess(argv, 0, Path(argv[1]).read_text(encoding="ascii"), "")

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)
    with pytest.raises(OSError) as caught:
        with execution.work_dir_tempdir(_container(tmp_path), Recording()) as root:
            (Path(root) / "diagnostic").write_text("retained", encoding="ascii")
    assert caught.value is error
    assert owners[0].root.is_dir() and owners[0].receipt.is_file()
    owners[0].verify()
    assert (owners[0].root / "diagnostic").read_text(encoding="ascii") == "retained"


def test_replaced_scratch_root_is_not_removed(tmp_path):
    context = _local(tmp_path, execution=lb.LocalExecution(work_dir=str(tmp_path)))
    with pytest.warns(RuntimeWarning, match="creation receipt does not match"):
        with execution.work_dir_tempdir(context, execution.LocalExecutor()) as directory:
            root = Path(directory)
            root.rename(tmp_path / "interrupted-run")
            root.mkdir()
            foreign = root / "user-evidence.txt"
            foreign.write_text("foreign", encoding="utf-8")
    assert foreign.read_text(encoding="utf-8") == "foreign"


def test_mismatched_container_mount_is_never_cleaned(tmp_path):
    calls = []

    class Unmounted(execution.Executor):
        def run(self, argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, "another-workspace", "")

    with pytest.warns(RuntimeWarning, match="work directory does not match"):
        with pytest.raises(execution.OwnershipRefused, match="work directory does not match"):
            with execution.work_dir_tempdir(_container(tmp_path), Unmounted()):
                pytest.fail("must not enter an unmounted execution workspace")
    assert all(argv[0] == "cat" for argv in calls)
    assert not any(argv[0] == "rm" for argv in calls)


def test_scratch_interruption_preserves_durable_results(tmp_path):
    context = _local(tmp_path, execution=lb.LocalExecution(work_dir=str(tmp_path)))
    durable = tmp_path / "results.json"
    durable.write_text("accepted independent result", encoding="utf-8")
    with pytest.warns(RuntimeWarning, match="retained after interrupted operation"):
        with pytest.raises(KeyboardInterrupt):
            with execution.work_dir_tempdir(context, execution.LocalExecutor()) as directory:
                (Path(directory) / "scratch.txt").write_text("temporary", encoding="utf-8")
                raise KeyboardInterrupt()
    assert (Path(directory) / "scratch.txt").read_text(encoding="utf-8") == "temporary"
    assert durable.read_text(encoding="utf-8") == "accepted independent result"


@pytest.mark.parametrize("container", [False, True])
@pytest.mark.parametrize("error", [
    RuntimeError("interrupted operation"),
    subprocess.TimeoutExpired("tool", 1),
    KeyboardInterrupt(), SystemExit(7),
], ids=["failure", "timeout", "interrupt", "exit"])
def test_aborted_scratch_retains_receipt_and_never_starts_disposal(
        tmp_path, monkeypatch, recwarn, container, error):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owned = execution.OwnedDirectory.create(
        parent=str(tmp_path), receipt_parent=str(receipts))
    monkeypatch.setattr(execution.OwnedDirectory, "create", lambda **kw: owned)
    calls = []

    class Recording(execution.Executor):
        def run(self, argv, **kw):
            calls.append(list(argv))
            assert argv == ["cat", str(owned.marker)]
            return subprocess.CompletedProcess(argv, 0, owned.token, "")

    context = (_container(tmp_path) if container else _local(
        tmp_path, execution=lb.LocalExecution(work_dir=str(tmp_path))))
    durable = tmp_path / "results.json"
    durable.write_text("accepted result", encoding="utf-8")
    with pytest.raises(type(error)) as raised:
        with execution.work_dir_tempdir(context, Recording()) as directory:
            (Path(directory) / "partial.txt").write_text("partial", encoding="utf-8")
            raise error
    assert raised.value is error
    owned.verify()
    assert calls == ([["cat", str(owned.marker)]] if container else [])
    assert (owned.root / "partial.txt").read_text(encoding="utf-8") == "partial"
    (owned.root / "late-output.txt").write_text("retained workspace", encoding="utf-8")
    assert durable.read_text(encoding="utf-8") == "accepted result"
    assert any("retained after interrupted operation" in str(w.message)
               for w in recwarn.list)


def test_retention_warning_cannot_replace_the_original_interruption(tmp_path, monkeypatch):
    context = _local(tmp_path, execution=lb.LocalExecution(work_dir=str(tmp_path)))

    def warning(*args, **kwargs):
        raise RuntimeWarning("warnings configured as errors")

    monkeypatch.setattr(execution.warnings, "warn", warning)
    error = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as raised:
        with execution.work_dir_tempdir(context, execution.LocalExecutor()) as directory:
            raise error
    assert raised.value is error and Path(directory).is_dir()


def test_public_collection_failure_retains_its_execution_scratch(tmp_path, monkeypatch):
    import sys

    from langadapter.python.adapter import PythonAdapter
    from langadapter.runs import CollectionRequest

    source = tmp_path / "project"
    source.mkdir()
    test = source / "test_value.py"
    test.write_text("def test_value():\n    assert True\n", encoding="utf-8")
    zone = tmp_path / "zone"
    zone.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owners, commands = [], []
    create = execution.OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    class FailedCollection(execution.LocalExecutor):
        def run(self, argv, **options):
            commands.append(list(argv))
            raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)
    context = lb.ProjectContext(
        root=str(source), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.LocalExecution(work_dir=str(zone)))
    adapter = PythonAdapter(context, executor=FailedCollection())
    with pytest.warns(RuntimeWarning, match="retained after interrupted operation"):
        result = adapter.collect_suite(CollectionRequest(lb.Suite(str(source), (test.name,)), 1))
    assert result.outcome is lb.Outcome.TIMED_OUT
    assert result.returncode is None and result.wall_time_s is None
    assert result.diagnostic == "collection timed out"
    assert len(owners) == 1
    owners[0].verify()
    assert owners[0].root.parent == zone
    assert any("--collect-only" in argv for argv in commands)
    assert test.read_text(encoding="utf-8") == "def test_value():\n    assert True\n"


@pytest.mark.parametrize("operation", ["suite", "coverage", "evidence", "warmup", "benchmark", "delivered"])
@pytest.mark.parametrize("failure", ["timeout", "start", "subprocess"])
def test_handled_public_failure_retains_scratch_and_keeps_native_outcome(
        tmp_path, monkeypatch, recwarn, operation, failure):
    import sys

    from langadapter.python import _runs as _benchmark
    from langadapter.python.adapter import PythonAdapter
    from langadapter.runs import BenchmarkRequest, PerTestEvidenceRequest, SuiteRunRequest

    source = tmp_path / "project"
    source.mkdir()
    (source / "value.py").write_text("value = 1\n", encoding="utf-8")
    (source / "test_value.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    zone = tmp_path / "zone"
    zone.mkdir()
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owners, commands = [], []
    create = execution.OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    def failed_command(argv):
        commands.append(list(argv))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if failure == "start":
            raise OSError("runner failed to start")
        raise subprocess.SubprocessError("command supervision failed")

    class FailedExecution(execution.LocalExecutor):
        def run(self, argv, **options):
            return failed_command(argv)

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(source), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.LocalExecution(work_dir=str(zone))), executor=FailedExecution())
    suite = lb.Suite(str(source), ("test_value.py",))
    if operation == "benchmark":
        monkeypatch.setattr(adapter, "_allowed_cpus", lambda *_: None)
        monkeypatch.setattr(_benchmark._BenchmarkRunner, "_repetition",
                            lambda self, command, env: failed_command(command))
    if operation in ("suite", "coverage"):
        result = adapter.run_suite(SuiteRunRequest(
            suite, 1, coverage_target="value.py" if operation == "coverage" else None))
        assert result.counts.passed is None and result.coverage is None
    elif operation in ("evidence", "warmup"):
        result = adapter.per_test_evidence(PerTestEvidenceRequest(
            suite, "value.py", 1, warmup=operation == "warmup"))
        assert result.tests == () and result.coverage is None
    elif operation == "delivered":
        result = adapter.run_as_delivered(SuiteRunRequest(suite, 1, session_seed=1))
        assert result.counts.passed is None and result.coverage is None
    else:
        result = adapter.benchmark(BenchmarkRequest(suite, 2, 1, warmup_runs=0))
        assert result.times_ms == () and result.mean_ms is None and result.returncode is None
    assert result.outcome is (lb.Outcome.TIMED_OUT if failure == "timeout"
                              else lb.Outcome.INFRASTRUCTURE_FAILURE)
    executions = [argv for argv in commands if "-m" in argv]
    probes = [argv for argv in commands if "-c" in argv
              and "platform.python_implementation()" in argv[argv.index("-c") + 1]]
    assert len(executions) == 1 and len(owners) == (2 if operation == "delivered" else 1)
    assert "pytest" in executions[0] or "coverage" in executions[0]
    assert len(probes) == 1 and len(commands) == len(executions) + len(probes)
    assert "platform.python_implementation()" in probes[0][-1]
    assert "platform.python_version()" in probes[0][-1]
    for owned in owners:
        owned.verify()
        assert owned.root.parent == zone
    assert any("retained after interrupted operation" in str(w.message)
               for w in recwarn.list)
    if operation == "delivered":
        assert (owners[0].root / "delivered_suite" / "test_value.py").read_text(
            encoding="utf-8") == "def test_value():\n    assert True\n"
    assert (source / "value.py").read_text(encoding="utf-8") == "value = 1\n"


@pytest.mark.parametrize("failure", ["timeout", "start", "subprocess"])
def test_failed_benchmark_retains_completed_repetitions_and_scratch(
        tmp_path, monkeypatch, recwarn, failure):
    import sys

    from langadapter.python import _runs as _benchmark
    from langadapter.python.adapter import PythonAdapter
    from langadapter.runs import BenchmarkRequest

    source = tmp_path / "project"
    source.mkdir()
    (source / "test_value.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owners, repetitions = [], []
    create = execution.OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    def repeat(self, command, env):
        repetitions.append(list(command))
        if len(repetitions) == 1:
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 1)
        if failure == "start":
            raise OSError("runner failed to start")
        raise subprocess.SubprocessError("runner communication failed")

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)
    monkeypatch.setattr(_benchmark._BenchmarkRunner, "_repetition", repeat)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(source), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.LocalExecution(work_dir=str(tmp_path))))
    monkeypatch.setattr(adapter, "_allowed_cpus", lambda *_: None)
    monkeypatch.setattr(adapter, "toolchain_identity", lambda: lb.ToolchainIdentity("CPython", "fixture"))
    result = adapter.benchmark(BenchmarkRequest(
        lb.Suite(str(source), ("test_value.py",)), 3, 1, warmup_runs=0))
    assert result.outcome is (lb.Outcome.TIMED_OUT if failure == "timeout"
                              else lb.Outcome.INFRASTRUCTURE_FAILURE)
    assert len(result.times_ms) == 1 and result.times_ms[0] >= 0
    assert len(repetitions) == 2 and repetitions[0] == repetitions[1]
    assert result.returncode is None
    assert result.mean_ms is None and result.median_ms is None
    assert result.min_ms is None and result.max_ms is None
    assert len(owners) == 1
    owners[0].verify()
    assert any("retained after interrupted operation" in str(w.message)
               for w in recwarn.list)


def test_retention_is_per_factory_scope_without_adopting_foreign_paths(tmp_path):
    context = _local(tmp_path, execution=lb.LocalExecution(work_dir=str(tmp_path)))
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "evidence.txt").write_text("foreign", encoding="utf-8")
    with pytest.raises(execution.OwnershipRefused, match="factory scope handle"):
        execution._retain_scratch(str(foreign), "do not dispose")
    with pytest.warns(RuntimeWarning, match="handled failure"):
        with execution.work_dir_tempdir(context, execution.LocalExecutor()) as retained:
            execution._retain_scratch(retained, "handled failure")
            with execution.work_dir_tempdir(context, execution.LocalExecutor()) as successful:
                (Path(successful) / "accepted.txt").write_text("scratch", encoding="utf-8")
            assert not Path(successful).exists()
    assert Path(retained).is_dir()
    assert (foreign / "evidence.txt").read_text(encoding="utf-8") == "foreign"


@pytest.mark.parametrize("operation,mode", [
    ("collect", "infrastructure"), ("collect", "complete"), ("collect", "red"),
    ("suite", "infrastructure"), ("suite", "complete"), ("suite", "red"),
    ("coverage", "missing"), ("coverage", "complete"),
    ("evidence", "infrastructure"), ("evidence", "missing"),
    ("evidence", "complete"), ("evidence", "red"),
    ("recovery", "infrastructure"), ("recovery", "timeout"),
    ("recovery", "complete"), ("recovery", "red"),
])
def test_python_post_launch_failures_preserve_owned_observations(
        tmp_path, monkeypatch, recwarn, operation, mode):
    from langadapter.python import _runs as _per_test_evidence
    from langadapter.python import _sources as _source_ops
    from langadapter.python import _runs as _suite_run
    from langadapter.python.adapter import PythonAdapter
    from langadapter.runs import CollectionRequest, CoverageObservation, PerTestEvidenceRequest, SuiteRunRequest

    source, zone, receipts = (tmp_path / name for name in ("project", "zone", "receipts"))
    for directory in (source, zone, receipts):
        directory.mkdir()
    (source / "value.py").write_text("value = 1\n", encoding="utf-8")
    (source / "test_value.py").write_text("def test_value():\n    assert True\n", encoding="utf-8")
    owners, commands = [], []
    create = execution.OwnedDirectory.create

    def allocate(**options):
        owned = create(**options, receipt_parent=str(receipts))
        owners.append(owned)
        return owned

    class Observations(execution.LocalExecutor):
        def run(self, argv, **options):
            commands.append(list(argv))
            assert "-m" in argv and ("pytest" in argv or "coverage" in argv)
            if mode == "timeout":
                raise subprocess.TimeoutExpired(argv, options["timeout"])
            for arg in argv:
                if arg.startswith("--junit-xml="):
                    failure = '<failure message="controlled assertion">assert False</failure>' if mode == "red" else ""
                    Path(arg.split("=", 1)[1]).write_text(
                        '<testsuites><testsuite tests="1" errors="0" skipped="0" '
                        f'failures="{int(mode == "red")}"><testcase classname="test_value" '
                        f'name="test_value" file="test_value.py" time="0.01">{failure}'
                        '</testcase></testsuite></testsuites>', encoding="utf-8")
            collect = "--collect-only" in argv
            code = 3 if mode == "infrastructure" else (2 if collect else 1) if mode == "red" else 0
            output = "ERROR collecting candidate.py" if collect and mode == "red" else "controlled observation"
            return subprocess.CompletedProcess(argv, code, output, "")

    monkeypatch.setattr(execution.OwnedDirectory, "create", allocate)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(source), interpreter=lb.Interpreter(path=sys.executable),
        execution=lb.LocalExecution(work_dir=str(zone))), executor=Observations())
    monkeypatch.setattr(adapter, "toolchain_identity", lambda: lb.ToolchainIdentity("CPython", "fixture"))
    monkeypatch.setattr(_source_ops.PythonSources, "_refusal", lambda *args: None)

    def coverage(runner, *args):
        return (None if mode == "missing" else CoverageObservation(
            target=runner._target_identity(), statements=(1,), covered=(1,), branches=(), taken=()))

    monkeypatch.setattr(_suite_run._SuiteRunner, "_coverage", lambda runner, *args: (
        coverage(runner), "controlled coverage report failure" if mode == "missing" else ""))
    monkeypatch.setattr(_per_test_evidence._EvidenceRunner, "_suite_coverage", coverage)
    monkeypatch.setattr(_runs, "_eviction_tokens_from_coverage", lambda *args: {})
    monkeypatch.setattr(_runs, "_eviction_times", lambda *args: {"test_value.test_value": 10.0})
    suite = lb.Suite(str(source), ("test_value.py",))
    if operation == "recovery":
        result = adapter.sources.prune_collection_breakers("def test_value():\n    assert True\n", timeout_s=1)
        assert result.status == {"infrastructure": "infrastructure_failure", "timeout": "infrastructure_failure",
                                 "complete": "unchanged", "red": "not_repaired"}[mode]
    elif operation == "collect":
        result = adapter.tests.collect_suite(CollectionRequest(suite, 1))
        assert result.outcome is {"infrastructure": lb.Outcome.INFRASTRUCTURE_FAILURE,
                                  "complete": lb.Outcome.COMPLETED, "red": lb.Outcome.COLLECTION_FAILED}[mode]
    elif operation == "evidence":
        result = adapter.tests.per_test_evidence(PerTestEvidenceRequest(suite, "value.py", 1, warmup=False))
        assert result.outcome is {"infrastructure": lb.Outcome.INFRASTRUCTURE_FAILURE,
                                  "red": lb.Outcome.TEST_FAILURES}.get(mode, lb.Outcome.COMPLETED)
        if mode == "missing":
            assert len(result.tests) == 1 and result.coverage is None
    else:
        result = adapter.tests.run_suite(SuiteRunRequest(
            suite, 1, coverage_target="value.py" if operation == "coverage" else None))
        assert result.outcome is {"infrastructure": lb.Outcome.INFRASTRUCTURE_FAILURE,
                                  "missing": lb.Outcome.INFRASTRUCTURE_FAILURE,
                                  "complete": lb.Outcome.COMPLETED, "red": lb.Outcome.TEST_FAILURES}[mode]
    assert len(commands) == len(owners) == 1
    if mode in ("infrastructure", "timeout", "missing"):
        owners[0].verify()
        assert any("retained after interrupted operation" in str(w.message) for w in recwarn.list)
    else:
        assert not owners[0].root.exists() and not owners[0].receipt.exists()
        assert not recwarn.list
    assert (source / "value.py").read_text(encoding="utf-8") == "value = 1\n"


def test_the_evidence_times_file_is_named_as_the_container_sees_it(tmp_path, monkeypatch, recwarn):
    """In a container that sees the work directory at another path, the
    evidence run's times plugin names the times file at that path."""
    from langadapter.python.adapter import PythonAdapter
    from langadapter.runs import PerTestEvidenceRequest

    zone, receipts = tmp_path / "zone", tmp_path / "receipts"
    (zone / "tests").mkdir(parents=True)
    receipts.mkdir()
    (zone / "tests" / "test_value.py").write_text("def test_value():\n    pass\n", encoding="utf-8")
    create = execution.OwnedDirectory.create
    monkeypatch.setattr(execution.OwnedDirectory, "create",
                        lambda **options: create(**options, receipt_parent=str(receipts)))
    written = []

    class Stop(Exception):
        pass

    def coverage_run(adapter, target, data, rc_file, args):
        scratch = Path(data).parent
        (plugin,) = (scratch / "shield").glob("*_times.py")
        written.append((scratch.name, plugin.read_text(encoding="utf-8")))
        raise Stop

    class Answering(execution.LocalExecutor):
        def run(self, argv, **options):
            # The scratch directory's marker reads as the container sees it.
            shown = Path(argv[1]).read_text(encoding="utf-8") if argv[0] == "cat" else ""
            return subprocess.CompletedProcess(argv, 0, shown, "")

    adapter = PythonAdapter(lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c1", work_dir=str(zone),
                                        container_work_dir="/mapped")),
        executor=Answering())
    monkeypatch.setattr(adapter, "toolchain_identity", lambda: lb.ToolchainIdentity("CPython", "fixture"))
    monkeypatch.setattr(_runs, "_coverage_run_command", coverage_run)
    with pytest.raises(Stop):
        adapter.per_test_evidence(PerTestEvidenceRequest(
            lb.Suite(str(zone / "tests"), ("test_value.py",)), "value.py", 60, warmup=False))
    ((name, text),) = written
    assert repr(f"/mapped/{name}/times.json") in text


@pytest.mark.parametrize("container", [False, True])
def test_already_removed_scratch_clears_only_its_receipt(tmp_path, monkeypatch,
                                                        recwarn, container):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    owned = execution.OwnedDirectory.create(
        parent=str(tmp_path), receipt_parent=str(receipts))
    monkeypatch.setattr(execution.OwnedDirectory, "create", lambda **kw: owned)
    calls = []

    class Recording(execution.Executor):
        def run(self, argv, **kw):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 0, owned.token, "")

    context = (_container(tmp_path) if container else _local(
        tmp_path, execution=lb.LocalExecution(work_dir=str(tmp_path))))
    durable = tmp_path / "results.json"
    durable.write_text("accepted result", encoding="utf-8")
    with execution.work_dir_tempdir(context, Recording()) as directory:
        shutil.rmtree(directory)
    assert not owned.receipt.exists()
    assert not recwarn.list
    assert not any(argv[0] == "rm" for argv in calls)
    assert durable.read_text(encoding="utf-8") == "accepted result"


@pytest.mark.parametrize("value", ["relative/zone", "C:relative"])
def test_a_container_work_dir_inside_must_be_absolute(tmp_path, value):
    context = lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(
            container="c1", work_dir=str(tmp_path), container_work_dir=value))
    with pytest.raises(lb.InvalidRequest) as refused:
        context.validate()
    assert refused.value.field == "execution.container_work_dir"


@pytest.mark.skipif(os.name == "nt", reason="the nested work directories here are POSIX host paths")
@pytest.mark.parametrize("host, container", [("/data/wz", "/data/wz/inner"), ("/data/wz/inner", "/data/wz")])
def test_a_container_work_dir_nested_in_the_host_path_is_refused(host, container):
    """Some arguments pass the host-to-container mapping twice, so with one
    work directory under the other a mapped path would be mapped again."""
    context = lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c1", work_dir=host, container_work_dir=container))
    with pytest.raises(lb.InvalidRequest) as refused:
        context.validate()
    assert refused.value.field == "execution.container_work_dir"
