"""Configuration of the Python adapter's Cosmic Ray mutation sessions, and the
attribution of named pytest killers to measured tests in their owning suite."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, Set
from langadapter.errors import InvalidRequest


#: The distributors. ``auto``, the default, follows the worker count.
_DISTRIBUTORS = ("auto", "local", "http")


def _resolve_distributor(distributor: str, workers: int) -> str:
    """The distributor a session of ``workers`` runs: ``auto`` is ``local``
    for one worker and ``http`` for more. An explicit choice stays."""
    if distributor == "auto":
        return "http" if workers > 1 else "local"
    return distributor


class SessionFolderRefused(OSError):
    """The configured node-local session directory cannot hold the session. The
    run is refused rather than moved, since a session on a shared network
    file system slows every mutant's run."""


def check_local_session_dir(configured: Optional[str]) -> None:
    """Check that ``configured`` (``CosmicRaySettings.local_session_dir``)
    can hold a local run's live session: a missing directory is made, and one that
    cannot be made, or in which no directory can be made, raises
    ``SessionFolderRefused`` with the cause. An empty value passes. A local run
    applies the same rule when its session starts, so a caller can refuse early
    with the run's own text. A directory inside a container only the run's
    launcher can check."""
    from pathlib import Path
    import os
    import uuid

    configured = str(configured or "").strip()
    if not configured:
        return
    try:
        Path(configured).mkdir(parents=True, exist_ok=True)
    except OSError as error:
        reason = f"{type(error).__name__}: {error}"
    else:
        # Make and remove a directory, as the run will: a directory with the write
        # bit but no search bit passes a write-bit test. One attempt, since
        # tempfile.mkdtemp retries a refused name practically forever on
        # Windows. The reason leaves out the probe's random name.
        probe = os.path.join(configured, f"cr_session_check_{uuid.uuid4().hex}")
        try:
            os.mkdir(probe)
        except OSError as error:
            reason = (f"no directory can be made in it "
                      f"({type(error).__name__}: {error.strerror or 'refused'})")
        else:
            try:
                os.rmdir(probe)
            except OSError:
                pass
            return
    raise SessionFolderRefused(
        f"the configured node-local session directory {configured} "
        f"(cosmic_ray_local_session_dir, CosmicRaySettings.local_session_dir) "
        f"cannot be used: {reason}. "
        f"The run is refused rather than moved to another directory")


def _validate_session_options(*, workers=1, distributor="auto",
                              allow_fewer_workers=False, settings=None) -> None:
    """Validate the session options without starting an engine or worker,
    raising ``InvalidRequest``. ``local`` with more than one worker is refused,
    never silently run with one."""
    if type(workers) is not int or workers < 1:
        raise InvalidRequest("workers", "must be a positive integer")
    if type(allow_fewer_workers) is not bool:
        raise InvalidRequest("allow_fewer_workers", "must be true or false")
    if distributor not in _DISTRIBUTORS:
        raise InvalidRequest("distributor", f"must be one of {', '.join(_DISTRIBUTORS)}")
    if distributor == "local" and workers != 1:
        raise InvalidRequest(
            "workers", f"distributor local runs one mutant at a time, so it cannot run "
                       f"workers {workers}: choose distributor http or auto, or workers 1")
    if settings is not None and not isinstance(settings, CosmicRaySettings):
        raise InvalidRequest("settings", "must be CosmicRaySettings or None")


@dataclass(frozen=True)
class CosmicRaySettings:
    """Cosmic Ray's own settings for a session. Every value has a default.
    ``catalogue_cache_dir`` (an absolute path) enables the content-hashed
    catalogue cache. ``keep_output`` keeps each mutant's test output in its
    kill-matrix row. ``log`` receives the engine's log lines
    (``log(message, level)``). ``session_dir`` holds a local run's session
    database. ``local_session_dir`` is the node-local directory of the live
    session database: a container run's launcher keeps it there (unset:
    memory-backed storage, then the system's temporary directory), and a local
    run without ``session_dir`` keeps its own directory below it (unset: the
    system's temporary directory). A configured directory that cannot be made or
    written refuses the run. Every run logs where its session ran.
    ``verbose`` turns on Cosmic Ray's informational log (host runs only)."""

    init_timeout_s: int = 600
    worker_ready_timeout_s: int = 60
    catalogue_cache_dir: Optional[str] = None
    cache_max_entries: int = 200
    cache_max_age_days: int = 0
    clone_dir: Optional[str] = None
    clone_sweep_hours: float = 12
    session_dir: Optional[str] = None
    local_session_dir: Optional[str] = None
    progress_interval_s: int = 0
    per_file_adjudication: bool = True
    keep_output: bool = False
    verbose: bool = False
    log: Optional[Callable[..., None]] = None

    def _engine_config(self, distributor: str) -> Dict[str, Any]:
        """The engine's configuration for these settings."""
        config: Dict[str, Any] = {
            "cosmic_ray_distributor": distributor,
            "cosmic_ray_init_timeout_s": self.init_timeout_s,
            "cosmic_ray_worker_ready_timeout_s": self.worker_ready_timeout_s,
            "cosmic_ray_catalogue_cache_dir": self.catalogue_cache_dir or "",
            "cosmic_ray_cache_max_entries": self.cache_max_entries,
            "cosmic_ray_cache_max_age_days": self.cache_max_age_days,
            "cosmic_ray_clone_dir": self.clone_dir or "",
            "cosmic_ray_clone_sweep_hours": self.clone_sweep_hours,
            "cosmic_ray_progress_interval_s": self.progress_interval_s,
            "cosmic_ray_per_file_adjudication": (
                "true" if self.per_file_adjudication else "false"),
            "cosmic_ray_keep_output": self.keep_output,
            "log_fn": self.log,
        }
        if self.session_dir:
            config["cosmic_ray_session_dir"] = self.session_dir
        if self.local_session_dir:
            config["cosmic_ray_local_session_dir"] = self.local_session_dir
        if self.verbose:
            config["cosmic_ray_verbose"] = True
        return config


def operator_set_names() -> Tuple[str, ...]:
    """The names of the operator sets the Python adapter defines for Cosmic
    Ray, such as ``wong_mathur_selective``."""
    from langadapter.python._cosmic_ray import _OPERATOR_SET_SHORTCUTS
    return tuple(sorted(_OPERATOR_SET_SHORTCUTS))


def operator_set(name: str) -> Tuple[str, ...]:
    """The operators of the set ``name`` as Cosmic Ray names them, fixed by its
    definition without a project environment. A request takes a set as
    ``Catalogue(name=...)``. An unknown name raises ``InvalidRequest``."""
    from langadapter.python._cosmic_ray import _OPERATOR_SET_SHORTCUTS
    _refuse_unknown_set("operator_set", name)
    return tuple(_OPERATOR_SET_SHORTCUTS[name]())


def _refuse_unknown_set(field: str, name: str) -> None:
    """Refuse ``name`` in ``field`` unless it names an operator set."""
    if name not in operator_set_names():
        raise InvalidRequest(
            field, f"unknown operator set {name!r}; known: {', '.join(operator_set_names())}")


def _match_test_context(context: str, test_ids: Set[str]) -> Optional[str]:
    """The test ID equal to the native dotted ``context``, else the only one
    that ends in it after a dot, else None."""
    if context in test_ids:
        return context
    matches = [test for test in test_ids if test.endswith("." + context)]
    return matches[0] if len(matches) == 1 else None
