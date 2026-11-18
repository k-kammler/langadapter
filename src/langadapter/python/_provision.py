"""Python's provisioning (``PythonAdapter.provision_runtime``): an environment
uv makes for a requested Python version, with the project's requirements
file and the tools LangAdapter pins for that Python, kept under the
caller's directory by name and a hash of those inputs and reused while they
stay the same."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, List, Mapping, Optional, Sequence

from langadapter import _process_tree
from langadapter.errors import InvalidRequest

#: The file in a provisioned environment that records its inputs.
MARKER = ".langadapter_provision.json"
#: How long a provisioning's uv commands may take together. A command still
#: running then is stopped with its children.
PROVISION_SECONDS = 540
#: How long a provisioning waits for another process making the same one,
#: longer than that process's commands may take.
LOCK_WAIT_SECONDS = 600


def tool_pins(version: Optional[str], names: Optional[Sequence[str]] = None) -> List[str]:
    """The install requirements of the tools LangAdapter pins for a project
    Python of ``version``, of ``names`` only when given (a name without a pin
    as it is). Provisioning and the setup's install advice use them."""
    from langadapter.python import _pinned_tools

    pins = _pinned_tools(version)
    return [f"{name}=={pins[name]}" if name in pins else name for name in (pins if names is None else names)]


def provision(version: str, requirements: Optional[str], *, store: Path, name: str,
              environment: Optional[Mapping[str, str]] = None,
              log: Callable[[str], Any] = print) -> str:
    """The interpreter of the environment for Python ``version`` under
    ``store``, in the directory ``name`` plus a hash of its inputs (the version,
    the ``requirements`` file's resolved path and content, and the pinned
    tools), made once: uv creates it (downloading the interpreter when none
    matches) and installs the ``requirements`` file and the pinned tools.
    Both uv commands run in the requirements file's directory, so a relative
    entry in it, such as ``-e .``, names a path beside the file. A marker
    records those inputs. Equal inputs reuse the environment, and
    changed ones make another beside it, so no caller removes an environment
    another one runs. An atomic lock directory per environment serializes two
    processes making it. The commands run in ``environment``, within
    ``PROVISION_SECONDS`` together. A missing requirements file or uv is an
    InvalidRequest naming its setting, as is a ``name`` that is not one plain
    directory name, so no path outside ``store`` is made or removed. A uv command
    that fails or does not finish in time, which removes the half-made
    environment, and a lock left by another process are RuntimeErrors."""
    # Windows path rules read both separators and a drive on every system.
    if name in ("", "..") or PureWindowsPath(name).name != name:
        raise InvalidRequest("name", f"must be one plain directory name, not {name!r}")
    req_path = Path(requirements).resolve() if requirements else None
    req_file = req_hash = ""
    if req_path is not None:
        if not req_path.exists():
            raise InvalidRequest("requirements", f"points at {req_path}, which does not exist")
        req_file, req_hash = str(req_path), hashlib.sha256(req_path.read_bytes()).hexdigest()
    wanted = {"python_version": version, "requirements": req_file,
              "requirements_hash": req_hash, "tool_pins": tool_pins(version)}
    # Twelve hex digits keep the directory's paths within Windows' limit.
    digest = hashlib.sha256(json.dumps(wanted, sort_keys=True, separators=(",", ":"))
                            .encode("utf-8")).hexdigest()[:12]
    # Absolute, since uv runs in the requirements file's directory.
    venv_dir = Path(store).absolute() / f"{name}-{digest}"
    venv_python = (venv_dir / "Scripts" / "python.exe" if os.name == "nt"
                   else venv_dir / "bin" / "python")
    marker = venv_dir / MARKER
    if _made(marker, venv_python, wanted):
        return str(venv_python)
    uv = shutil.which("uv")
    if uv is None:
        raise InvalidRequest("python_version", "provisioning an environment requires uv, which "
                             "is not on PATH: install it with `pip install uv`, or leave "
                             "python_version empty to use the project's own environment")
    lock = venv_dir.parent / f".{venv_dir.name}.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + LOCK_WAIT_SECONDS
    while True:
        try:
            os.mkdir(lock)
            break
        except FileExistsError:
            if time.time() > deadline:
                raise RuntimeError(
                    f"timed out waiting for another process to finish provisioning "
                    f"{venv_dir}; remove the stale lock {lock} if no provisioning is running")
            time.sleep(2)
    try:
        # Another process may have finished while this one waited.
        if _made(marker, venv_python, wanted):
            return str(venv_python)
        # A directory of these inputs without a complete marker is half made.
        if venv_dir.exists():
            shutil.rmtree(venv_dir)
        log(f"provisioning Python {version} at {venv_dir} (first run for this project; "
            f"later runs reuse it)")
        env = dict(environment) if environment is not None else None
        install = [uv, "pip", "install", "--python", str(venv_python)]
        cwd = None
        if req_path is not None:
            install += ["-r", str(req_path)]
            cwd = str(req_path.parent)
        install += wanted["tool_pins"]
        deadline = time.monotonic() + PROVISION_SECONDS
        for step, args in (("the environment's creation",
                            [uv, "venv", str(venv_dir), "--python", version]),
                           ("the dependency install into the environment", install)):
            cause = None
            try:
                done = _process_tree.run(args, timeout=deadline - time.monotonic(),
                                         text=True, env=env, cwd=cwd)
                failure = f"failed (uv exit {done.returncode})" if done.returncode else None
            except subprocess.TimeoutExpired as error:
                # It carries the output read until the command was stopped.
                done = cause = error
                failure = (f"did not finish within the {PROVISION_SECONDS} s that a "
                           f"provisioning's commands may take together")
            if failure is not None:
                shutil.rmtree(venv_dir, ignore_errors=True)
                raise RuntimeError(f"{step} {failure}. Last output:\n"
                                   f"{(done.stderr or done.stdout or '')[-2000:]}") from cause
        marker.write_text(json.dumps(wanted, indent=2), encoding="utf-8")
        log(f"environment ready: {venv_python}")
        return str(venv_python)
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def _made(marker: Path, python: Path, wanted: dict) -> bool:
    """Whether the environment holds an interpreter and records ``wanted``."""
    if not (marker.exists() and python.exists()):
        return False
    try:
        return json.loads(marker.read_text(encoding="utf-8")) == wanted
    except (OSError, ValueError):
        return False
