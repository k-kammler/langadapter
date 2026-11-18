"""A Cosmic Ray session's distributor follows its worker count: ``auto``,
the default, is ``local`` for one worker and ``http`` for more. An explicit
``http`` runs as chosen, and an explicit ``local`` with more workers is
refused. ``auto`` with 15 workers opens the same session as ``http``."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from langadapter.context import LocalExecution
from langadapter.errors import InvalidRequest
from langadapter.python import _cosmic_ray as engine_module
from langadapter.python import _cosmic_ray as native
from langadapter.python.cosmic_ray import (
    _DISTRIBUTORS, CosmicRaySettings, _resolve_distributor, _validate_session_options,
)


def test_auto_follows_the_worker_count_and_an_explicit_choice_stays():
    assert _DISTRIBUTORS == ("auto", "local", "http")
    assert [_resolve_distributor("auto", n) for n in (1, 2, 15)] == ["local", "http", "http"]
    assert [_resolve_distributor("http", n) for n in (1, 15)] == ["http", "http"]
    assert _resolve_distributor("local", 1) == "local"
    _validate_session_options(workers=4)
    _validate_session_options(workers=1, distributor="http")


def test_an_explicit_local_distributor_refuses_more_workers_naming_both():
    with pytest.raises(InvalidRequest) as refused:
        _validate_session_options(workers=4, distributor="local")
    assert refused.value.field == "workers"
    assert "distributor local" in str(refused.value) and "workers 4" in str(refused.value)


class _Handle:
    def __init__(self, workers, use_http):
        self.use_http = use_http
        self.worker_urls = [f"worker:{i}" for i in range(workers)] if use_http else []
        self._worker_procs = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


@pytest.fixture
def opened(tmp_path, monkeypatch):
    """Opens sessions on an engine that records its settings and the
    requested session, starting nothing."""
    adapter = SimpleNamespace(
        context=SimpleNamespace(root=str(tmp_path), execution=LocalExecution()))
    seen = []

    class Engine:
        def __init__(self, config, adapter):
            self.config = config

        def session(self, *, project_root, workers, distributor=None):
            real = engine_module.CosmicRayEngineSession.__new__(
                engine_module.CosmicRayEngineSession)
            engine_module.CosmicRayEngineSession.__init__(
                real, SimpleNamespace(config=self.config, _exec_mode=lambda: "local"),
                project_root=Path(project_root), workers=workers, distributor=distributor)
            seen.append((dict(self.config), workers, distributor, real.use_http))
            return _Handle(workers, real.use_http)

    monkeypatch.setattr(native, "CosmicRayEngine", Engine)

    def open_(**options):
        seen.clear()
        session = native.MutationSession(adapter, **options)
        (config, workers, distributor, use_http), = seen
        return session, config, workers, distributor, use_http
    return open_


@pytest.mark.parametrize("options, distributor, use_http, started", [
    ({"workers": 1}, "local", False, 1),
    ({"workers": 4}, "http", True, 4),
    ({"workers": 1, "distributor": "http"}, "http", True, 1),
    ({"workers": 4, "distributor": "http"}, "http", True, 4),
])
def test_a_session_runs_the_distributor_its_worker_count_and_choice_give(
        opened, options, distributor, use_http, started):
    session, config, workers, asked, http = opened(**options)
    assert session.distributor == asked == config["cosmic_ray_distributor"] == distributor
    assert (workers, http, session.workers_started) == (options["workers"], use_http, started)


def test_auto_with_15_workers_sets_up_the_explicit_http_session(opened):
    """``auto`` and an explicit ``http`` with 15 workers set up the same
    session: the http engine settings, 15 workers and the http distributor."""
    settings = CosmicRaySettings(catalogue_cache_dir="/cache", local_session_dir="/dev/shm/x")
    explicit = opened(workers=15, distributor="http", allow_fewer_workers=True, settings=settings)
    _, config, workers, distributor, use_http = explicit
    assert config == settings._engine_config("http")
    assert (workers, distributor, use_http) == (15, "http", True)
    auto = opened(workers=15, allow_fewer_workers=True, settings=settings)
    assert auto[1:] == explicit[1:]


def test_the_configured_distributor_keeps_one_worker_sequential():
    """Without a session's own choice, a configured http distributor runs
    one worker sequentially."""
    def use_http(config, workers, distributor=None):
        engine = SimpleNamespace(config={**CosmicRaySettings()._engine_config("local"), **config}, _exec_mode=lambda: "local")
        return engine_module.CosmicRayEngineSession(
            engine, project_root=Path("."), workers=workers, distributor=distributor).use_http
    assert use_http({"cosmic_ray_distributor": "http"}, 1) is False
    assert use_http({"cosmic_ray_distributor": "http"}, 4) is True
    assert use_http({}, 4) is False
    assert use_http({"cosmic_ray_distributor": "http"}, 1, "http") is True
    assert use_http({}, 4, "auto") is True and use_http({}, 1, "auto") is False
