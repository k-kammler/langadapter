"""Public settings of Cosmic Ray sessions without consumer imports."""
from dataclasses import FrozenInstanceError
import sys

import pytest

import langadapter as lb
from langadapter.python import _cosmic_ray as engine_module
from langadapter.python.adapter import PythonAdapter
from langadapter.python.cosmic_ray import CosmicRaySettings


def test_default_settings_keep_the_engine_configuration():
    settings = CosmicRaySettings()
    assert type(settings).__module__ == "langadapter.python.cosmic_ray"
    assert settings._engine_config("local") == {
        "cosmic_ray_distributor": "local",
        "cosmic_ray_init_timeout_s": 600,
        "cosmic_ray_worker_ready_timeout_s": 60,
        "cosmic_ray_catalogue_cache_dir": "",
        "cosmic_ray_cache_max_entries": 200,
        "cosmic_ray_cache_max_age_days": 0,
        "cosmic_ray_clone_dir": "",
        "cosmic_ray_clone_sweep_hours": 12,
        "cosmic_ray_progress_interval_s": 0,
        "cosmic_ray_per_file_adjudication": "true",
        "cosmic_ray_keep_output": False,
        "log_fn": None,
    }


def test_explicit_settings_keep_all_values(tmp_path):
    def log(message, level):
        pass

    settings = CosmicRaySettings(
        init_timeout_s=31, worker_ready_timeout_s=33,
        catalogue_cache_dir=str(tmp_path / "catalogues"),
        cache_max_entries=34, cache_max_age_days=35,
        clone_dir=str(tmp_path / "clones"), clone_sweep_hours=1.25,
        session_dir=str(tmp_path / "sessions"), progress_interval_s=36,
        per_file_adjudication=False, keep_output=True, log=log,
    )
    assert settings._engine_config("http") == {
        "cosmic_ray_distributor": "http",
        "cosmic_ray_init_timeout_s": 31,
        "cosmic_ray_worker_ready_timeout_s": 33,
        "cosmic_ray_catalogue_cache_dir": str(tmp_path / "catalogues"),
        "cosmic_ray_cache_max_entries": 34,
        "cosmic_ray_cache_max_age_days": 35,
        "cosmic_ray_clone_dir": str(tmp_path / "clones"),
        "cosmic_ray_clone_sweep_hours": 1.25,
        "cosmic_ray_session_dir": str(tmp_path / "sessions"),
        "cosmic_ray_progress_interval_s": 36,
        "cosmic_ray_per_file_adjudication": "false",
        "cosmic_ray_keep_output": True,
        "log_fn": log,
    }
    assert not list(tmp_path.iterdir())


def test_settings_cannot_change_after_construction():
    settings = CosmicRaySettings()
    with pytest.raises(FrozenInstanceError):
        settings.keep_output = True


def test_public_session_uses_the_supplied_settings(tmp_path, monkeypatch):
    configs = []

    class Session:
        use_http = False

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

    def open_session(engine, **kwargs):
        configs.append(dict(engine.config))
        return Session()

    monkeypatch.setattr(engine_module.CosmicRayEngine, "session", open_session)
    adapter = PythonAdapter(lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable)))
    settings = CosmicRaySettings(init_timeout_s=31, keep_output=True)
    with adapter.open_mutation_session(settings=settings) as session:
        assert session.settings is settings
    assert configs == [settings._engine_config("local")]
