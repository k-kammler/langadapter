"""The per-mutant run wrappers of the Cosmic Ray engine.

POSIX hosts use the shell wrapper, Windows a plain Python wrapper with the
same behavior, tested on every platform. Each run starts where the normal
run starts, in its root (a worker's clone, or the project in place), first
without the files earlier runs created there (the session's inventory of
the root), and with a temporary directory of its own.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from langadapter.python import (
    _cosmic_ray as engine_module, _cr_session_runner as runner,
)
from langadapter.python._plugins import render
from langadapter.python._run_definition import (
    remove_unlisted, run_inventory, write_run_inventory,
)

_PROBE_MODULE = '''\
import json, os, sys, tempfile
print(json.dumps({
    "cwd": os.getcwd(),
    "argv": sys.argv[1:],
    "path0": sys.path[0],
    "pid": os.getpid(),
    "tmpdir": os.environ.get("TMPDIR"),
    "tempdir": tempfile.gettempdir(),
    "tmp_entries": sorted(os.listdir(tempfile.gettempdir())),
    "threads": {k: os.environ.get(k) for k in (
        "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMBA_NUM_THREADS")},
    "journal": {k: os.environ.get(k) for k in (
        "LANGADAPTER_TIMEOUT_EVIDENCE_DIR", "LANGADAPTER_JOURNAL_MODULE")},
}))
raise SystemExit(int(os.environ.get("PROBE_EXIT", "0")))
'''


def _run_wrapper(tmp_path: Path, command, *, extra_env=None, text=None):
    wrapper = tmp_path / "wrapper" / "_cr_run.py"
    wrapper.parent.mkdir(exist_ok=True)
    wrapper.write_text(engine_module._per_run_wrapper_text(False) if text is None
                       else text, encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir(exist_ok=True)
    (modules / "wrapper_probe.py").write_text(_PROBE_MODULE, encoding="utf-8")
    scratch_base = tmp_path / "scratch"
    scratch_base.mkdir(exist_ok=True)
    start_dir = tmp_path / "start"
    start_dir.mkdir(exist_ok=True)
    env = dict(os.environ)
    env.update({"PYTHONPATH": str(modules), "TMPDIR": str(scratch_base),
                "OMP_NUM_THREADS": "8"})
    env.update(extra_env or {})
    result = subprocess.run(
        [sys.executable, str(wrapper), *command],
        cwd=start_dir, env=env, capture_output=True, text=True, timeout=60,
    )
    return result, start_dir, scratch_base


def test_wrapper_runs_the_module_in_its_root_with_a_temporary_dir_of_its_own(tmp_path):
    result, start_dir, scratch_base = _run_wrapper(
        tmp_path,
        [sys.executable, "-m", "wrapper_probe",
         "__CR_WORKER_CWD__/tests", "--rootdir=__CR_WORKER_CWD__/tests", "-q"],
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    # The run's root, as ``python -m pytest`` there puts it first.
    assert Path(seen["cwd"]).resolve() == start_dir.resolve()
    assert Path(seen["path0"]).resolve() == start_dir.resolve()
    run_tmp = Path(seen["tmpdir"]).resolve()
    assert run_tmp.parent == scratch_base.resolve()
    assert run_tmp.name.startswith("cr_runtmp_")
    assert Path(seen["tempdir"]).resolve() == run_tmp and seen["tmp_entries"] == []
    original = str(start_dir.resolve()).replace(os.sep, "/")
    assert [a.replace(os.sep, "/") for a in seen["argv"]] == [
        f"{original}/tests", f"--rootdir={original}/tests", "-q",
    ]
    # Every numeric pool is pinned, overriding an inherited value.
    assert set(seen["threads"].values()) == {"1"}
    # The run removes its temporary directory and owner marker at the end.
    assert not run_tmp.exists()
    assert not Path(str(run_tmp) + engine_module.OWNER_MARKER_SUFFIX).exists()


def test_a_run_never_sees_or_touches_another_runs_temporary_dir(tmp_path):
    scratch_base = tmp_path / "scratch"
    for name in ("cr_runtmp", "cr_runtmp_other"):
        leftover = scratch_base / name / "nested"
        leftover.mkdir(parents=True)
        (leftover / "written_by_a_mutant.txt").write_text("x", encoding="utf-8")

    result, _start, _base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "wrapper_probe"],
    )

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    assert seen["tmp_entries"] == []
    assert Path(seen["tmpdir"]).name not in ("cr_runtmp", "cr_runtmp_other")
    for name in ("cr_runtmp", "cr_runtmp_other"):
        assert (scratch_base / name / "nested" / "written_by_a_mutant.txt").exists()


def test_wrapper_propagates_the_exit_code_of_the_test_command(tmp_path):
    result, _start, scratch_base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "wrapper_probe"],
        extra_env={"PROBE_EXIT": "1"},
    )

    # Cosmic Ray reads a nonzero exit as a kill, so it passes through
    # unchanged, and the temporary directory is still removed.
    assert result.returncode == 1
    assert list(scratch_base.glob("cr_runtmp_*")) == []


def test_wrapper_refuses_a_command_it_cannot_run_in_process(tmp_path):
    result, _start, scratch_base = _run_wrapper(
        tmp_path, [sys.executable, "some_script.py"],
    )

    assert result.returncode == 70
    assert "expects" in result.stderr
    assert list(scratch_base.glob("cr_runtmp*")) == []


def _root_with_leftovers(tmp_path: Path) -> Path:
    """A root whose inventory was taken before a run left files: one in the
    root, one in a new and one in an old directory, one in a directory the
    session keeps, one in the git directory."""
    root = tmp_path / "start"
    (root / "pkg").mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "session").mkdir()
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (root / "data.txt").write_text("data", encoding="utf-8")
    write_run_inventory(root, tmp_path / "start.inventory", ("./session/",))
    (root / "left.txt").write_text("left", encoding="utf-8")
    (root / "new").mkdir()
    (root / "new" / "left.txt").write_text("left", encoding="utf-8")
    (root / "pkg" / "left.txt").write_text("left", encoding="utf-8")
    (root / "session" / "cr-session.sqlite-journal").write_text("kept", encoding="utf-8")
    (root / ".git" / "index").write_text("kept", encoding="utf-8")
    return root


def _left_and_kept(root: Path):
    return (sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()))


KEPT = [".git/index", "data.txt", "pkg/mod.py", "session/cr-session.sqlite-journal"]


def test_a_run_removes_the_files_earlier_runs_left_in_its_root(tmp_path):
    """The Python wrapper: every file below the root that its inventory does
    not list goes before the run, except in the git directory and the directories
    the session keeps. Directories stay."""
    root = _root_with_leftovers(tmp_path)
    result, _start, _base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "wrapper_probe"],
        extra_env={"LANGADAPTER_RUN_INVENTORY": str(tmp_path / "start.inventory"),
                   "LANGADAPTER_RUN_KEEP": "./session/"})
    assert result.returncode == 0, result.stderr
    assert _left_and_kept(root) == KEPT
    assert (root / "new").is_dir()


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="the shell wrapper runs on POSIX hosts")
def test_the_shell_wrapper_removes_the_files_earlier_runs_left_in_its_root(tmp_path):
    root = _root_with_leftovers(tmp_path)
    wrapper = tmp_path / "_cr_fdlimit_run.sh"
    wrapper.write_text(engine_module._per_run_wrapper_text(True), encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "wrapper_probe.py").write_text(_PROBE_MODULE, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ, PYTHONPATH=str(modules), TMPDIR=str(scratch),
               LANGADAPTER_RUN_INVENTORY=str(tmp_path / "start.inventory"),
               LANGADAPTER_RUN_KEEP="./session/")
    result = subprocess.run(
        ["sh", str(wrapper), sys.executable, "-m", "wrapper_probe"],
        cwd=root, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert _left_and_kept(root) == KEPT


def test_the_inventory_is_one_listing_for_the_engine_the_runner_and_the_wrapper(tmp_path):
    """The engine's run_inventory, the container runner's copy and the Python
    wrapper's: the same entries, links not followed, the git directory and the
    kept directories left out, and remove_unlisted removes what the wrapper would."""
    root = _root_with_leftovers(tmp_path)
    keep = ("./session/",)
    namespace: dict = {}
    exec(compile(engine_module._per_run_wrapper_text(False), "wrapper", "exec"), namespace)
    listed = run_inventory(root, keep)
    assert listed == runner._entries(root, keep) == namespace["_entries"](str(root), list(keep))
    assert "./left.txt" in listed and "./data.txt" in listed
    assert not any(entry.startswith(("./.git/", "./session/")) for entry in listed)
    removed = remove_unlisted(root, tmp_path / "start.inventory", keep)
    assert removed == ["./left.txt", "./new/left.txt", "./pkg/left.txt"]
    assert _left_and_kept(root) == KEPT


@pytest.mark.skipif(os.name == "nt" or shutil.which("find") is None,
                    reason="the shell wrapper's listing runs on POSIX hosts")
def test_the_shell_wrappers_listing_is_the_engines(tmp_path):
    root = _root_with_leftovers(tmp_path)
    (root / "link").symlink_to(root / "pkg")
    found = subprocess.run(
        ["sh", "-c", "find . -path ./.git -prune -o \\( -type f -o -type l \\) -print"
         " | LC_ALL=C sort"], cwd=root, capture_output=True, text=True, check=True)
    assert found.stdout.splitlines() == run_inventory(root)


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="the shell wrapper runs on POSIX hosts")
def test_the_shell_wrappers_temporary_dir_is_one_per_worker(tmp_path):
    """The shell wrapper runs in its root and empties one temporary directory
    per worker (its parent process) before each run."""
    wrapper = tmp_path / "_cr_fdlimit_run.sh"
    wrapper.write_text(engine_module._per_run_wrapper_text(True), encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "wrapper_probe.py").write_text(_PROBE_MODULE, encoding="utf-8")
    scratch = tmp_path / "scratch"
    (scratch / f"cr_runtmp_{os.getpid()}").mkdir(parents=True)
    (scratch / f"cr_runtmp_{os.getpid()}" / "left.txt").write_text("x", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(modules), TMPDIR=str(scratch))
    result = subprocess.run(
        ["sh", str(wrapper), sys.executable, "-m", "wrapper_probe"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["cwd"] == str(tmp_path)
    assert seen["tmpdir"] == f"{scratch}/cr_runtmp_{os.getpid()}" and seen["tmp_entries"] == []
    assert set(seen["threads"].values()) == {"1"}
    # The directory stays for the session's cleanup or the engine's sweep.
    assert Path(seen["tmpdir"] + engine_module.OWNER_MARKER_SUFFIX).is_file()


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="the shell wrapper runs on POSIX hosts")
def test_the_shell_wrapper_runs_as_the_caller(tmp_path):
    """No user namespace: a run's tests have the caller's user id, as in the
    normal run, not the namespace's uid 0."""
    wrapper = tmp_path / "_cr_fdlimit_run.sh"
    wrapper.write_text(engine_module._per_run_wrapper_text(True), encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "uid_probe.py").write_text("import os\nprint(os.getuid())\n", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    result = subprocess.run(
        ["sh", str(wrapper), sys.executable, "-m", "uid_probe"],
        cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(modules), TMPDIR=str(scratch)),
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip().splitlines()[-1]) == os.getuid()


def test_written_wrapper_matches_the_host_platform(tmp_path):
    executable = sys.executable.replace(os.sep, "/")

    prefix = engine_module._write_per_run_wrapper(tmp_path / "session",
                                                  executable)

    if os.name == "nt":
        script = tmp_path / "session" / "_cr_run.py"
        assert prefix == [executable, script.as_posix()]
        assert script.read_text(encoding="utf-8") == \
            render(engine_module._PER_RUN_WRAPPER_PY)
    else:
        script = tmp_path / "session" / "_cr_fdlimit_run.sh"
        assert prefix == ["sh", script.as_posix()]
        assert script.read_text(encoding="utf-8") == render(
            engine_module._PER_RUN_WRAPPER_SH)

    journal = (tmp_path / "evidence", "pkg/mod.py")
    engine_module._write_per_run_wrapper(tmp_path / "journaled", executable,
                                         journal)
    (written,) = (tmp_path / "journaled").iterdir()
    assert written.name == script.name
    assert written.read_text(encoding="utf-8") == \
        engine_module._per_run_wrapper_text(os.name != "nt", journal)


def test_a_journal_adds_its_variables_and_nothing_else(tmp_path):
    """Both wrappers: a journal adds its two prefixed variables before the
    working directory is read, never the per-test module restore variable.
    Without a journal nothing is added."""
    root = tmp_path / "evidence dir"
    for posix, plain in (
            (True, render(engine_module._PER_RUN_WRAPPER_SH)),
            (False, render(engine_module._PER_RUN_WRAPPER_PY))):
        assert engine_module._per_run_wrapper_text(posix) == plain
        text = engine_module._per_run_wrapper_text(
            posix, journal=(root, "pkg/mod.py"))
        added = [line for line in text.splitlines()
                 if line not in plain.splitlines()]
        if posix:
            assert added == [
                f"export LANGADAPTER_TIMEOUT_EVIDENCE_DIR='{root.as_posix()}'"
                "/r$$ LANGADAPTER_JOURNAL_MODULE=pkg/mod.py"]
            assert text.index(added[0]) < text.index("_origcwd=$PWD")
        else:
            assert added == [
                "    os.environ['LANGADAPTER_TIMEOUT_EVIDENCE_DIR'] = "
                f"os.path.join({str(root)!r}, 'r%d' % os.getpid())",
                "    os.environ['LANGADAPTER_JOURNAL_MODULE'] = 'pkg/mod.py'"]
            assert text.index(added[-1]) < text.index("original_cwd = ")


def test_the_journal_plugin_adds_its_path_and_a_source_root(tmp_path):
    """Both wrappers: the plugin directory goes on the import path, and the
    start directory becomes the source root unless the process names one."""
    plugin = tmp_path / "plugin dir"
    for posix, plain in (
            (True, render(engine_module._PER_RUN_WRAPPER_SH)),
            (False, render(engine_module._PER_RUN_WRAPPER_PY))):
        text = engine_module._per_run_wrapper_text(
            posix, plugin_dir=plugin)
        added = [line for line in text.splitlines()
                 if line not in plain.splitlines()]
        if posix:
            assert added == [
                'export LANGADAPTER_MUTATION_SOURCE_ROOT='
                '"${LANGADAPTER_MUTATION_SOURCE_ROOT:-$PWD}" '
                f"PYTHONPATH='{plugin.as_posix()}'"
                '"${PYTHONPATH:+:$PYTHONPATH}"']
            assert text.index(added[0]) < text.index("_origcwd=$PWD")
        else:
            assert added == [
                "    os.environ.setdefault('LANGADAPTER_MUTATION_SOURCE_ROOT', "
                "os.getcwd())",
                f"    sys.path.insert(1, {str(plugin)!r})"]
            assert text.index(added[0]) < text.index("original_cwd = ")
            assert text.index(added[1]) > text.index("sys.path[0] = root")


def test_a_runs_import_roots_go_first_below_its_source_root(tmp_path):
    """Both wrappers: the target package's directories, below the source root
    (named or the start directory), go first on the import path."""
    for posix, plain in (
            (True, render(engine_module._PER_RUN_WRAPPER_SH)),
            (False, render(engine_module._PER_RUN_WRAPPER_PY))):
        text = engine_module._per_run_wrapper_text(
            posix, import_roots=("pysrc",))
        added = [line for line in text.splitlines()
                 if line not in plain.splitlines()]
        if posix:
            assert added == [
                'export PYTHONPATH="${LANGADAPTER_MUTATION_SOURCE_ROOT:-$PWD}"/pysrc'
                '"${PYTHONPATH:+:$PYTHONPATH}"']
            assert text.index(added[0]) < text.index("_origcwd=$PWD")
        else:
            assert added[-1] == "    sys.path[1:1] = roots"
            assert text.index(added[-1]) > text.index("sys.path[0] = root")


def test_a_run_imports_its_target_package_from_its_import_roots(tmp_path):
    """The Python wrapper: a package of the import root shadows an
    installed one of the same name."""
    (tmp_path / "pysrc" / "pkg").mkdir(parents=True)
    (tmp_path / "pysrc" / "pkg" / "__init__.py").write_text("WHERE = 'root'\n",
                                                           encoding="utf-8")
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "pkg_probe.py").write_text(
        "import pkg\nprint(pkg.WHERE)\n", encoding="utf-8")
    text = engine_module._per_run_wrapper_text(False, import_roots=("pysrc",))
    result, _start, _base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "pkg_probe"], text=text,
        extra_env={"LANGADAPTER_MUTATION_SOURCE_ROOT": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "root"


def test_a_run_imports_the_journal_plugin_and_names_its_source_root(
        tmp_path):
    """The Python wrapper: the test command imports a module of the plugin
    directory and sees its start directory as the source root, unless the
    process names one."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "journal_probe.py").write_text("NAME = 'plugin'\n",
                                             encoding="utf-8")
    probe = ("import json, os\nimport journal_probe\n"
             "print(json.dumps({'plugin': journal_probe.NAME, 'root': "
             "os.environ.get('LANGADAPTER_MUTATION_SOURCE_ROOT')}))\n")
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "plugin_probe.py").write_text(probe,
                                                          encoding="utf-8")
    text = engine_module._per_run_wrapper_text(False, plugin_dir=plugin)
    result, start_dir, _base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "plugin_probe"], text=text)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    assert seen["plugin"] == "plugin"
    assert Path(seen["root"]).resolve() == start_dir.resolve()
    named, _start, _base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "plugin_probe"], text=text,
        extra_env={"LANGADAPTER_MUTATION_SOURCE_ROOT": "clone"})
    assert json.loads(named.stdout)["root"] == "clone"


def test_each_run_gets_a_journal_of_its_own(tmp_path):
    """The Python wrapper: the test command sees a journal directory named
    by its own process id, and the module."""
    evidence = tmp_path / "evidence"
    result, _start, _base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "wrapper_probe"],
        text=engine_module._per_run_wrapper_text(
            False, journal=(evidence, "pkg/mod.py")))

    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout)
    assert seen["journal"] == {
        "LANGADAPTER_TIMEOUT_EVIDENCE_DIR": os.path.join(
            str(evidence), f"r{seen['pid']}"),
        "LANGADAPTER_JOURNAL_MODULE": "pkg/mod.py"}


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="the shell wrapper runs on POSIX hosts")
def test_the_shell_wrapper_gives_each_run_a_journal_of_its_own(tmp_path):
    """As the Python wrapper, with the shell's process id, which the test
    command keeps because the wrapper execs it."""
    evidence = tmp_path / "evidence"
    wrapper = tmp_path / "_cr_fdlimit_run.sh"
    wrapper.write_text(engine_module._per_run_wrapper_text(
        True, journal=(evidence, "pkg/mod.py")), encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "wrapper_probe.py").write_text(_PROBE_MODULE, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ, PYTHONPATH=str(modules), TMPDIR=str(scratch))
    result = subprocess.run(
        ["sh", str(wrapper), sys.executable, "-m", "wrapper_probe"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    seen = json.loads(result.stdout.strip().splitlines()[-1])
    assert seen["journal"] == {
        "LANGADAPTER_TIMEOUT_EVIDENCE_DIR":
            f"{evidence.as_posix()}/r{seen['pid']}",
        "LANGADAPTER_JOURNAL_MODULE": "pkg/mod.py"}


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="the shell wrapper runs on POSIX hosts")
def test_a_container_sessions_tmpdir_holds_each_runs_dir(tmp_path, monkeypatch):
    """Under the container launcher's TMPDIR a run's temporary directory lies
    inside it, as under a host job's TMPDIR."""
    monkeypatch.setattr(tempfile, "tempdir", "/tmp")
    container_dir, container_tmp = runner._session_tmpdir()
    host_tmp = tmp_path / "job" / "tmp"
    host_tmp.mkdir(parents=True)
    wrapper = tmp_path / "wrapper.sh"
    wrapper.write_text(engine_module._per_run_wrapper_text(True), encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "wrapper_probe.py").write_text(_PROBE_MODULE, encoding="utf-8")
    try:
        for run_tmp in (container_tmp, host_tmp):
            result = subprocess.run(
                ["sh", str(wrapper), sys.executable, "-m", "wrapper_probe"],
                cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(modules),
                                       TMPDIR=str(run_tmp)),
                capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, result.stderr
            seen = json.loads(result.stdout.strip().splitlines()[-1])
            assert Path(seen["tmpdir"]).parent == Path(run_tmp)
    finally:
        shutil.rmtree(container_dir, ignore_errors=True)


def test_the_session_sweeps_dirs_killed_runs_left(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    old, fresh = tmp_path / "cr_runtmp_old", tmp_path / "cr_runtmp_fresh"
    unmarked = tmp_path / "cr_runtmp_someone_elses"
    for directory in (old, fresh):
        engine_module._mark_owned(directory)
    for directory in (old, fresh, unmarked):
        directory.mkdir()
    os.utime(old, (0, 0))
    os.utime(unmarked, (0, 0))
    assert engine_module._sweep_stale_run_directories(12) == 1
    assert not old.exists() and not engine_module._owner_marker(old).exists()
    assert fresh.exists() and unmarked.exists()
    assert engine_module._sweep_stale_run_directories(0) == 0


def test_a_sweep_counts_only_dirs_it_removed(tmp_path, monkeypatch):
    # A directory that survives removal is reported as kept, not swept.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    for name in ("cr_runtmp_kept", "cr_runtmp_gone"):
        engine_module._mark_owned(tmp_path / name).mkdir()
        os.utime(tmp_path / name, (0, 0))
    rmtree = engine_module.shutil.rmtree

    def partial(path, ignore_errors=False):
        if Path(path).name != "cr_runtmp_kept":
            rmtree(path, ignore_errors=ignore_errors)

    logs = []
    monkeypatch.setattr(engine_module.shutil, "rmtree", partial)
    assert engine_module._sweep_stale_run_directories(
        12, log_fn=lambda message, level=0: logs.append(message)) == 1
    assert (tmp_path / "cr_runtmp_kept").exists() and not (tmp_path / "cr_runtmp_gone").exists()
    assert len(logs) == 1 and "swept 1 " in logs[0] and "1 could not be removed" in logs[0]


def test_a_swept_clone_takes_its_inventory_along(tmp_path):
    clone = engine_module._mark_owned(tmp_path / "cr_worker_0_x")
    clone.mkdir()
    write_run_inventory(clone, engine_module._inventory_of(clone))
    os.utime(clone, (0, 0))
    assert engine_module._sweep_stale_clones(tmp_path, 12) == 1
    assert not clone.exists() and not engine_module._inventory_of(clone).exists()


_MARKER_PROBE = (
    "import os\n"
    "print(open(os.environ['TMPDIR'] + '.langadapter-cosmic-ray', encoding='utf-8').read())\n")


def test_a_sessions_runs_name_it_in_their_owner_markers(tmp_path):
    # The session's cleanup removes the directories its runs left by it.
    text = engine_module._per_run_wrapper_text(False, owner="session-token")
    (tmp_path / "modules").mkdir()
    (tmp_path / "modules" / "marker_probe.py").write_text(_MARKER_PROBE, encoding="utf-8")
    result, _start, scratch_base = _run_wrapper(
        tmp_path, [sys.executable, "-m", "marker_probe"], text=text)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "session-token"
    assert list(scratch_base.glob("cr_runtmp*")) == []


@pytest.mark.skipif(os.name == "nt" or shutil.which("sh") is None,
                    reason="the shell wrapper runs on POSIX hosts")
def test_the_shell_wrapper_names_its_session_in_the_owner_marker(tmp_path):
    wrapper = tmp_path / "_cr_fdlimit_run.sh"
    wrapper.write_text(engine_module._per_run_wrapper_text(True, owner="session-token"),
                       encoding="utf-8")
    modules = tmp_path / "modules"
    modules.mkdir()
    (modules / "marker_probe.py").write_text(_MARKER_PROBE, encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env = dict(os.environ, PYTHONPATH=str(modules), TMPDIR=str(scratch))
    result = subprocess.run(
        ["sh", str(wrapper), sys.executable, "-m", "marker_probe"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "session-token"
    assert engine_module._remove_run_directories([scratch], "session-token") == 1
    assert list(scratch.iterdir()) == []


def test_a_session_removes_only_the_run_dirs_it_marked(tmp_path):
    def marked(name, owner):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "left_by_a_test.txt").write_text("x", encoding="utf-8")
        engine_module._owner_marker(directory).write_text(owner, encoding="utf-8")
        return directory

    mine, theirs, swept = (marked("cr_runtmp_mine", "token"),
                           marked("cr_runtmp_theirs", "other"),
                           marked("cr_runtmp_unnamed", ""))
    unmarked = tmp_path / "cr_runtmp_unmarked"
    unmarked.mkdir()
    gone = tmp_path / "cr_runtmp_gone"
    engine_module._owner_marker(gone).write_text("token", encoding="utf-8")

    assert engine_module._remove_run_directories([tmp_path, tmp_path / "absent"], "token") == 2
    assert not mine.exists() and not engine_module._owner_marker(mine).exists()
    assert not engine_module._owner_marker(gone).exists()
    for kept in (theirs, swept):
        assert kept.exists() and engine_module._owner_marker(kept).is_file()
    assert unmarked.exists()


def test_wrapper_runs_a_command_with_interpreter_flags(tmp_path):
    """The flags before -m are the wrapper process's own (the engine passes
    them to the interpreter that runs it): the wrapper runs the module."""
    result, _start, _base = _run_wrapper(
        tmp_path, [sys.executable, "-X", "dev", "-m", "wrapper_probe"])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["argv"] == []


def test_the_python_wrappers_interpreter_takes_the_flags(tmp_path):
    prefix = engine_module._write_per_run_wrapper(tmp_path / "session", "python",
                                                  flags=("-X", "dev"))
    if os.name == "nt":
        assert prefix == ["python", "-X", "dev", (tmp_path / "session" / "_cr_run.py").as_posix()]
    else:
        assert prefix == ["sh", (tmp_path / "session" / "_cr_fdlimit_run.sh").as_posix()]
