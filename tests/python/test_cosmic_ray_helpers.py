"""The Cosmic Ray engine's helpers without a complete mutation run: worker
plans, port allocation and readiness, cloning, session paths, the run
environment, the catalogue cache and the operator sets and catalogue."""
from __future__ import annotations

import os
import socket
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from langadapter.python.cosmic_ray import CosmicRaySettings
import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.python import _cosmic_ray as cre
from langadapter.python.adapter import PythonAdapter


def _engine(tmp_path, config=None):
    """An engine for a local project at ``tmp_path`` in this interpreter."""
    context = lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable))
    return cre.CosmicRayEngine({**CosmicRaySettings()._engine_config("local"), **(config or {})},
                               adapter=PythonAdapter(context))


def _engine_with(executor, python="/project/python"):
    """An engine whose project commands go through ``executor``."""
    project = SimpleNamespace(_executor=lambda: executor,
                              interpreter=python, _runtime_flags=lambda: [])
    return cre.CosmicRayEngine(CosmicRaySettings()._engine_config("local"), adapter=project)


@pytest.mark.parametrize(
    ("configured", "use_http", "pending", "expected"),
    [
        (15, True, 11, (11, True)),
        (15, True, 1, (1, True)),
        (15, True, 25, (15, True)),
        (15, False, 11, (1, False)),
    ],
)
def test_auxiliary_worker_plan_never_allocates_idle_workers(
    configured, use_http, pending, expected,
):
    assert cre._auxiliary_worker_plan(
        configured, use_http, pending,
    ) == expected


# _wait_for_worker

def test_wait_for_worker_returns_false_on_timeout():
    """With no listener on the port, the wait times out with False."""
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    assert cre.CosmicRayEngine._wait_for_worker(port, timeout=0.5) is False


def test_wait_for_worker_returns_true_when_listener_accepts():
    """A live listener on the port makes the wait return True."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def accept_once():
        try:
            conn, _ = server.accept()
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=accept_once, daemon=True)
    t.start()
    try:
        out = cre.CosmicRayEngine._wait_for_worker(port, timeout=5.0)
    finally:
        server.close()
    assert out is True


# _clone_one

def test_clone_one_mirrors_files_and_subdirs(tmp_path):
    """``_clone_one`` mirrors the project root's files and directories."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("A", encoding="utf-8")
    (src / "pkg").mkdir()
    (src / "pkg" / "b.py").write_text("B", encoding="utf-8")
    dst = tmp_path / "clone"
    dst.mkdir()
    cre.CosmicRayEngine._clone_one(src, dst)
    assert (dst / "a.py").read_text() == "A"
    assert (dst / "pkg" / "b.py").read_text() == "B"


# _resolve_paths

def test_resolve_paths_default_uses_tempdir(tmp_path):
    """Without ``cosmic_ray_session_dir`` the session files live in a
    per-engine temporary directory on local disk, clear of SQLite locking
    failures on a network filesystem."""
    engine = _engine(tmp_path)
    sqlite_p, toml_p = engine._resolve_paths()
    assert sqlite_p.parent == toml_p.parent
    assert sqlite_p.parent != tmp_path
    assert sqlite_p.parent == engine._default_session_dir
    assert sqlite_p.name == "cr-session.sqlite"
    assert toml_p.name == "cosmic-ray.toml"
    # The directory is cached, so both files stay together across calls.
    sqlite_p2, _ = engine._resolve_paths()
    assert sqlite_p2.parent == sqlite_p.parent


def test_resolve_paths_uses_session_dir_override(tmp_path):
    """With ``cosmic_ray_session_dir`` set, the session files live there."""
    session = tmp_path / "elsewhere"
    engine = _engine(tmp_path, {"cosmic_ray_session_dir": str(session)})
    sqlite_p, toml_p = engine._resolve_paths()
    assert sqlite_p.parent == session
    assert toml_p.parent == session
    assert session.exists()


# _build_env

def test_build_env_prepends_project_root_to_existing_pythonpath(tmp_path, monkeypatch):
    """A PYTHONPATH of the run's environment gets the project root prepended."""
    existing = "/existing/path"
    monkeypatch.setenv("PYTHONPATH", existing)
    engine = _engine(tmp_path)
    project_root = tmp_path / "project"
    out = engine._build_env(project_root)
    assert out["PYTHONPATH"] == f"{project_root}{os.pathsep}{existing}"


def test_build_env_creates_pythonpath_from_scratch(tmp_path, monkeypatch):
    """Without a PYTHONPATH it is the project root alone."""
    monkeypatch.delenv("PYTHONPATH", raising=False)
    engine = _engine(tmp_path)
    project_root = tmp_path / "project"
    out = engine._build_env(project_root)
    assert out["PYTHONPATH"] == str(project_root)


# _persist_cache

def test_persist_cache_writes_atomically_via_rename(tmp_path):
    """The cache file is written through a temporary file and a rename. The
    source holds the session tables: a malformed file is refused so that a
    corrupt cache cannot poison later runs."""
    src = tmp_path / "session.sqlite"
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE work_results "
            "(job_id TEXT, test_outcome TEXT, worker_outcome TEXT, diff TEXT)"
        )
        conn.execute("INSERT INTO work_items VALUES ('j0')")
        conn.commit()
    finally:
        conn.close()
    expected_bytes = src.read_bytes()
    cache = tmp_path / "cache" / "key_abc.sqlite"
    cre.CosmicRayEngine._persist_cache(src, cache)
    assert cache.read_bytes() == expected_bytes


def test_concurrent_cache_writers_copy_to_their_own_temporary_files(tmp_path, monkeypatch):
    """While one store of a catalogue is between its copy and its move, a
    second store completes. Each copies to its own temporary file, so both
    succeed and neither moves the other's file."""
    src = tmp_path / "session.sqlite"
    conn = sqlite3.connect(str(src))
    try:
        conn.execute("CREATE TABLE work_items (job_id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE work_results "
                     "(job_id TEXT, test_outcome TEXT, worker_outcome TEXT, diff TEXT)")
        conn.commit()
    finally:
        conn.close()
    cache = tmp_path / "cache" / "key_abc.sqlite"
    copy = cre.shutil.copy2
    targets, logs = [], []

    def copy_then_another_store(source, target):
        targets.append(target)
        copy(source, target)
        if len(targets) == 1:
            cre.CosmicRayEngine._persist_cache(src, cache, log_fn=lambda m, level=0: logs.append(m))

    monkeypatch.setattr(cre.shutil, "copy2", copy_then_another_store)
    cre.CosmicRayEngine._persist_cache(src, cache, log_fn=lambda m, level=0: logs.append(m))
    assert len(targets) == 2 and targets[0] != targets[1]
    assert not [m for m in logs if "write failed" in m], logs
    assert cache.read_bytes() == src.read_bytes()
    assert [p.name for p in cache.parent.iterdir()] == [cache.name]


# _gc_cache_dir

def test_gc_cache_dir_caps_entry_count(tmp_path):
    """Beyond ``max_entries`` cache files the oldest go."""
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(5):
        (cache / f"key_{i:016x}.sqlite").write_bytes(b"x")
        time.sleep(0.01)  # stable mtime order
    engine = _engine(tmp_path, {"cosmic_ray_cache_max_entries": 3})
    engine._gc_cache_dir(cache)
    remaining = sorted(p.name for p in cache.glob("*.sqlite"))
    assert len(remaining) == 3
    assert (cache / f"key_{4:016x}.sqlite").exists()
    assert (cache / f"key_{3:016x}.sqlite").exists()


def test_gc_cache_dir_no_op_when_under_limit(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for i in range(2):
        (cache / f"k_{i:016x}.sqlite").write_bytes(b"x")
    engine = _engine(tmp_path, {"cosmic_ray_cache_max_entries": 10})
    engine._gc_cache_dir(cache)
    assert len(list(cache.glob("*.sqlite"))) == 2


def test_gc_cache_dir_removes_only_the_caches_entries(tmp_path, monkeypatch):
    """The cleanup removes only files named as the cache names its entries
    (``_resolve_cache_path``): another SQLite file in the directory stays."""
    cache = tmp_path / "cache"
    cache.mkdir()
    foreign = cache / "results.sqlite"
    foreign.write_bytes(b"x")
    engine = _engine(tmp_path, {"cosmic_ray_catalogue_cache_dir": str(cache),
                                "cosmic_ray_cache_max_entries": 1})
    monkeypatch.setattr(engine, "_cosmic_ray_metadata", lambda: ("8.4.1", None, "py3.12"))
    # A stem with a letter outside ASCII, which the entry's name keeps.
    target, entries = tmp_path / "módulo.py", []
    for body in ("def f(): return 1\n", "def f(): return 2\n"):
        time.sleep(0.01)  # stable mtime order
        target.write_text(body, encoding="utf-8")
        entries.append(engine._resolve_cache_path(target, ["core/X"]))
        entries[-1].write_bytes(b"x")
    engine._gc_cache_dir(cache)
    assert foreign.exists() and entries[1].exists() and not entries[0].exists()


def test_gc_cache_dir_no_op_on_missing_dir(tmp_path):
    engine = _engine(tmp_path, {"cosmic_ray_cache_max_entries": 5})
    engine._gc_cache_dir(tmp_path / "nope")


# _wong_mathur_selective_operators

def test_wong_mathur_returns_74_operators():
    """The Wong-Mathur 1995 selective set is 74 operators
    (42 AOR + 30 ROR + 2 LCR), without duplicates."""
    ops = cre._wong_mathur_selective_operators()
    assert len(ops) == 74
    assert len(set(ops)) == 74


def test_wong_mathur_operators_are_real_cosmic_ray_names():
    """Every operator of the set is registered in Cosmic Ray."""
    try:
        import cosmic_ray.plugins
    except ImportError:
        pytest.skip("cosmic-ray not installed")
    all_names = set(cosmic_ray.plugins.operator_names())
    missing = [n for n in cre._wong_mathur_selective_operators()
               if n not in all_names]
    assert missing == [], (
        f"Wong-Mathur entries not registered with cosmic-ray: {missing}")


# project Cosmic Ray metadata / _compute_excluded_operators

def test_project_cosmic_ray_metadata_uses_selected_interpreter():
    class Executor:
        def run(self, argv, timeout):
            assert argv[0] == "/project/python"
            assert argv[1] == "-c"
            assert timeout == 60
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "unrelated warning\n"
                    f"{cre._PROJECT_METADATA_PREFIX}"
                    '{"operators":["core/A","core/B"],"python":"py3.12",'
                    '"version":"8.4.1"}\n'
                ),
            )

    assert cre._project_cosmic_ray_metadata(Executor(), "/project/python", flags=()) == (
        "8.4.1", ["core/A", "core/B"], "py3.12",
    )
    assert _engine_with(Executor()).full_operators() == ["core/A", "core/B"]


def test_project_cosmic_ray_metadata_fails_closed():
    class Executor:
        def run(self, argv, timeout):
            return SimpleNamespace(returncode=1, stdout="")

    assert cre._project_cosmic_ray_metadata(Executor(), "/project/python", flags=()) == (
        "unknown", None, None)
    assert _engine_with(Executor()).full_operators() is None


def test_compute_excluded_complements_full_catalogue():
    """The exclude list is the catalogue minus the allow-list."""
    catalogue = ["core/A", "core/B", "core/C"]
    assert cre._compute_excluded_operators(
        ["core/A", "core/B"], catalogue) == ["core/C"]


def test_compute_excluded_drops_unknown_operator_names():
    """A name outside the catalogue drops out of the allow-list, and the
    complement still holds every real operator."""
    catalogue = ["core/A", "core/B", "core/C"]
    assert cre._compute_excluded_operators(
        ["core/A", "core/DoesNotExist"], catalogue) == ["core/B", "core/C"]


def test_compute_excluded_fails_when_project_catalogue_is_unavailable():
    with pytest.raises(RuntimeError, match="selected project interpreter"):
        cre._compute_excluded_operators(["core/X"], None)


# Cosmic Ray engine helpers: the module path of a target, the written TOML
# settings and the catalogue cache path.
# --- _target_to_module_path ---

def test_target_to_module_path_refuses_a_target_outside_the_project(tmp_path):
    """A target outside the project root is refused, as the session refuses
    it before (``project_file``), not mutated in place by its absolute path."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    other = tmp_path / "elsewhere"
    other.mkdir()
    target = other / "mod.py"
    target.write_text("")
    engine = _engine(tmp_path)
    with pytest.raises(InvalidRequest) as refused:
        engine._target_to_module_path(target, project_root)
    assert refused.value.field == "target"
    assert "does not lie under the project root" in refused.value.message


def _cosmic_ray_settings(text: str) -> dict:
    """The ``[cosmic-ray]`` table of a written config."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib
    return tomllib.loads(text)["cosmic-ray"]


# --- _write_config_toml ---

def test_write_config_toml_operators_restricted_via_filter(tmp_path):
    """A restricted operator list becomes an exclude-operators filter naming
    every other operator, since Cosmic Ray has no allow-list."""
    pytest.importorskip("cosmic_ray")
    engine = _engine(tmp_path)
    cfg = tmp_path / "cosmic-ray.toml"
    keep = [
        "core/ReplaceBinaryOperator_Add_Sub",
        "core/ReplaceComparisonOperator_Eq_NotEq",
    ]
    engine._write_config_toml(
        cfg,
        module_path="m.py",
        test_command="pytest",
        per_mutant_timeout=60,
        operators=keep,
    )
    exclude = _cosmic_ray_settings(cfg.read_text(encoding="utf-8"))[
        "filters"]["operators-filter"]["exclude-operators"]
    # The anchored form avoids matching sibling operator names.
    assert "^core/ReplaceBinaryOperator_Add_Sub$" not in exclude
    assert "^core/ReplaceComparisonOperator_Eq_NotEq$" not in exclude
    assert "^core/ReplaceBinaryOperator_Mul_Add$" in exclude


def test_write_config_toml_no_operators_emits_no_filter_block(tmp_path):
    """The full catalogue writes no filter block."""
    engine = _engine(tmp_path)
    cfg = tmp_path / "cosmic-ray.toml"
    engine._write_config_toml(
        cfg,
        module_path="m.py",
        test_command="pytest",
        per_mutant_timeout=60,
        operators=None,
    )
    assert "filters" not in _cosmic_ray_settings(cfg.read_text(encoding="utf-8"))


# --- _resolve_cache_path ---

def test_resolve_cache_path_includes_source_hash(tmp_path):
    """The cache path is derived deterministically from the source, the
    operators, the Python version and the cache format."""
    target = tmp_path / "foo.py"
    target.write_text("def f(): return 1", encoding="utf-8")
    engine = _engine(tmp_path, {
        "cosmic_ray_catalogue_cache_dir": str(tmp_path / "artifacts"),
    })
    out = engine._resolve_cache_path(target, operators=["a", "b"])
    assert out is not None
    assert out.suffix == ".sqlite"
    assert "foo" in out.name
    out2 = engine._resolve_cache_path(target, operators=["a", "b"])
    assert out == out2
    out3 = engine._resolve_cache_path(target, operators=["a"])
    assert out3 != out


def test_resolve_cache_path_returns_none_when_target_unreadable(tmp_path):
    """An unreadable target has no cache path."""
    engine = _engine(tmp_path, {
        "cosmic_ray_catalogue_cache_dir": str(tmp_path / "artifacts"),
    })
    out = engine._resolve_cache_path(
        tmp_path / "missing.py", operators=None,
    )
    assert out is None
