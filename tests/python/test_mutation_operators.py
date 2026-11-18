"""The mutation operators a consumer can ask for (the project's catalogue and
the named operator sets), session settings and the environment of a
session's runs."""
import os
import subprocess
import sys
import venv

import pytest

import langadapter as lb
from langadapter.errors import InvalidRequest
from langadapter.execution import Executor
from langadapter.mutation import OperatorCatalogue
from langadapter.python import _cosmic_ray as engine_module, _project_files
from langadapter.python._cosmic_ray import MutationSession
from langadapter.python.adapter import PythonAdapter
from langadapter.python.cosmic_ray import (
    CosmicRaySettings, operator_set, operator_set_names,
)


def _cosmic_ray_runs() -> bool:
    executable = engine_module._project_bin("cosmic-ray", sys.executable)
    try:
        return subprocess.run([executable, "--help"], capture_output=True,
                              timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _local(tmp_path, **changes):
    root = tmp_path / "proj"
    root.mkdir(exist_ok=True)
    return lb.ProjectContext(
        root=str(root), interpreter=lb.Interpreter(path=sys.executable),
        **changes)


def test_the_named_operator_sets_and_their_members():
    assert operator_set_names() == ("wong_mathur_selective",)
    assert operator_set("wong_mathur_selective") == tuple(
        engine_module._wong_mathur_selective_operators())


def test_an_unknown_operator_set_names_the_known_ones():
    with pytest.raises(InvalidRequest) as raised:
        operator_set("mostly_everything")
    assert raised.value.field == "operator_set"
    assert "wong_mathur_selective" in str(raised.value)


@pytest.mark.skipif(not _cosmic_ray_runs(),
                    reason="needs cosmic-ray beside the interpreter")
def test_the_catalogue_is_the_engines_full_operator_list(tmp_path):
    """The catalogue, read through the project interpreter, is the engine's
    full operator list."""
    adapter = PythonAdapter(_local(tmp_path))
    catalogue = adapter.mutation_operators()
    assert isinstance(catalogue, OperatorCatalogue)
    assert catalogue.outcome is lb.Outcome.COMPLETED
    assert catalogue.engine == "cosmic_ray"
    assert catalogue.tool_version
    engine = engine_module.CosmicRayEngine(
        CosmicRaySettings()._engine_config("local"),
        adapter=adapter)
    assert list(catalogue.operators) == engine.full_operators()
    assert len(catalogue.operators) > 100
    assert set(operator_set("wong_mathur_selective")) <= set(
        catalogue.operators)


def test_a_project_without_cosmic_ray_has_no_catalogue(tmp_path):
    env_dir = tmp_path / "bare"
    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(
        env_dir)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt"
                        else "bin/python")
    (tmp_path / "proj").mkdir()
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path / "proj"),
        interpreter=lb.Interpreter(path=str(python))))
    catalogue = adapter.mutation_operators()
    assert catalogue.operators is None
    assert catalogue.outcome is lb.Outcome.MISSING_SETUP
    assert str(python) in catalogue.reason


def test_the_catalogue_of_an_unknown_engine_is_refused(tmp_path):
    with pytest.raises(InvalidRequest) as raised:
        PythonAdapter(_local(tmp_path)).mutation_operators("pit")
    assert raised.value.field == "engine"


def test_the_local_session_directory_setting_reaches_the_settings_and_its_refusal(tmp_path):
    """A run's mutation settings name the node-local session directory as
    cosmic_ray_local_session_dir, and its refusal names that setting."""
    from langadapter.python._mutation import session_dir_refusal, session_settings

    settings = session_settings({"cosmic_ray_local_session_dir": "/scratch/cr"},
                                lambda *args: None, str(tmp_path))
    assert settings.local_session_dir == "/scratch/cr"
    occupied = tmp_path / "a file"
    occupied.write_text("", encoding="utf-8")
    refusal = session_dir_refusal({"cosmic_ray_local_session_dir": str(occupied)},
                                  create=True)
    assert "(cosmic_ray_local_session_dir, CosmicRaySettings.local_session_dir)" in refusal


def test_settings_for_the_container_launcher_and_verbose_output(tmp_path):
    default = CosmicRaySettings()._engine_config("http")
    assert "cosmic_ray_local_session_dir" not in default
    assert "cosmic_ray_verbose" not in default
    config = CosmicRaySettings(local_session_dir="/scratch/cr",
                               verbose=True)._engine_config("http")
    assert config["cosmic_ray_local_session_dir"] == "/scratch/cr"
    assert config["cosmic_ray_verbose"] is True


class _NoProcess(Executor):
    """A container executor stand-in: opening a container session starts
    nothing but the source-dir probe, answered for a flat layout, and the
    check of pytest-benchmark's storage, answered as absent."""

    def run(self, argv, **kw):
        if engine_module._SHADOWED_ROOTS in argv:
            return subprocess.CompletedProcess(list(argv), 0,
                                               '{"inside": [], "outside": []}\n', "")
        if any(part in (_project_files._EXISTS, _project_files._REMOVE_EMPTY_DIR)
               for part in argv):
            return subprocess.CompletedProcess(list(argv), 1, "", "")
        raise AssertionError(f"unexpected command {argv}")


def test_a_container_sessions_runs_import_only_the_container_root(
    tmp_path, monkeypatch,
):
    """The executor forwards PYTHONPATH into the container, so the host's
    import path is left out: the container's project root is the whole
    import path, as for suite runs, and the context's environment is added."""
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(["/host/site", "/host/b"]))
    context = lb.ProjectContext(
        root="/testbed", interpreter=lb.Interpreter(path="/venv/bin/python"),
        execution=lb.ContainerExecution(container="c1",
                                        work_dir=str(tmp_path)),
        environment={"PROJECT_FLAG": "1"})
    adapter = PythonAdapter(context, executor=_NoProcess())
    session = MutationSession(adapter)
    try:
        env = session._session.env
        assert env["PYTHONPATH"] == "/testbed"
        assert env["PROJECT_FLAG"] == "1"
        assert env["PYTHONUTF8"] == "1"
        assert adapter._build_env("/testbed")["PYTHONPATH"] == \
            "/testbed"
    finally:
        session.close()


def test_a_local_sessions_runs_keep_the_inherited_import_path(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "site"))
    context = _local(tmp_path, environment={"PROJECT_FLAG": "2"})
    adapter = PythonAdapter(context)
    session = MutationSession(adapter)
    try:
        env = session._session.env
        assert env["PYTHONPATH"].split(os.pathsep) == [
            context.root, str(tmp_path / "site")]
        assert env["PROJECT_FLAG"] == "2"
    finally:
        session.close()


def test_a_contexts_environment_stays_with_its_own_runs(
    tmp_path, monkeypatch,
):
    """The variables a context adds reach its own runs only: neither the
    process environment nor a session of another context sees them."""
    monkeypatch.delenv("PROJECT_FLAG", raising=False)
    first = MutationSession(PythonAdapter(
        _local(tmp_path, environment={"PROJECT_FLAG": "4"})))
    try:
        assert first._session.env["PROJECT_FLAG"] == "4"
    finally:
        first.close()
    assert "PROJECT_FLAG" not in os.environ
    second = MutationSession(PythonAdapter(_local(tmp_path)))
    try:
        assert "PROJECT_FLAG" not in second._session.env
    finally:
        second.close()


def test_suite_runs_add_the_contexts_environment(tmp_path):
    """The suite runs' environment builder adds the context's environment."""
    context = _local(tmp_path, environment={"PROJECT_FLAG": "3"})
    env = PythonAdapter(context)._build_env(context.root)
    assert env["PROJECT_FLAG"] == "3"
    assert env["PYTHONHASHSEED"] == "0"


def test_the_mutation_settings_map_each_key_and_take_each_default(tmp_path):
    """session_settings maps each [mutation] key of a run into
    CosmicRaySettings, an absent key takes the engine's default, and an
    empty directory, as a Testuary run passes it, names no directory."""
    from langadapter.python._mutation import session_settings

    log = object()
    default = session_settings({}, log, str(tmp_path))
    assert (default.init_timeout_s, default.worker_ready_timeout_s) == (600, 60)
    assert default.catalogue_cache_dir is None
    assert (default.cache_max_entries, default.cache_max_age_days) == (200, 0)
    assert (default.clone_dir, default.clone_sweep_hours) == (None, 12)
    assert (default.session_dir, default.local_session_dir) == (None, None)
    assert default.progress_interval_s == 0
    assert default.per_file_adjudication and not default.keep_output and not default.verbose
    assert default.log is log
    chosen = session_settings({
        "cosmic_ray_init_timeout_s": "1800", "cosmic_ray_worker_ready_timeout_s": "90",
        "cosmic_ray_catalogue_cache_dir": "cr_cache", "cosmic_ray_cache_max_entries": "3",
        "cosmic_ray_cache_max_age_days": "7", "cosmic_ray_clone_dir": "/scratch/clones",
        "cosmic_ray_clone_sweep_hours": "2.5", "cosmic_ray_session_dir": "/scratch/sessions",
        "cosmic_ray_local_session_dir": " /local/cr ", "cosmic_ray_progress_interval_s": "30",
        "cosmic_ray_per_file_adjudication": "false", "cosmic_ray_keep_output": True,
        "cosmic_ray_verbose": "true"}, log, str(tmp_path))
    assert (chosen.init_timeout_s, chosen.worker_ready_timeout_s) == (1800, 90)
    assert chosen.catalogue_cache_dir == str((tmp_path / "cr_cache").resolve())
    assert (chosen.cache_max_entries, chosen.cache_max_age_days) == (3, 7)
    assert (chosen.clone_dir, chosen.clone_sweep_hours) == ("/scratch/clones", 2.5)
    assert (chosen.session_dir, chosen.local_session_dir) == ("/scratch/sessions", "/local/cr")
    assert chosen.progress_interval_s == 30
    assert not chosen.per_file_adjudication and chosen.keep_output and chosen.verbose
    auto = session_settings({"cosmic_ray_catalogue_cache_dir": "auto",
                             "cosmic_ray_clone_dir": "auto"}, log, str(tmp_path))
    assert auto.catalogue_cache_dir == str(
        (tmp_path / "cache" / "mutation" / "cosmic_ray").resolve())
    assert auto.clone_dir is None
    empty = session_settings({"cosmic_ray_catalogue_cache_dir": "",
                              "cosmic_ray_local_session_dir": "",
                              "cosmic_ray_session_dir": ""}, log, str(tmp_path))
    assert empty.catalogue_cache_dir is None
    assert empty.local_session_dir is None
    assert empty.session_dir is None
