"""The Python adapter's Cosmic Ray mutation engine and its mutation sessions.

The engine targets Cosmic Ray 8.x: ``cosmic-ray init <config.toml>
<session.sqlite>`` generates the mutant catalogue and ``cosmic-ray exec``
with the same arguments runs it. Results are read from the session SQLite
directly: its schema, not ``cr-report``, is the stable contract.

Properties of Cosmic Ray the engine relies on:

  * A fixed catalogue: mutants are generated from the source, whatever
    tests run, so adding tests never lowers the mutation score.
  * Parallelism through the ``http`` distributor: a fleet of ``cosmic-ray
    http-worker`` processes, each with its own source clone. The ``local``
    distributor is single-threaded (it ignores its worker count).
  * A per-mutant timeout in the config (no pytest-timeout).
  * One fresh session per run. A terminated run's partial results are
    read, never resumed.
  * A content-hashed catalogue cache: a run over the same source, operator
    set and Cosmic Ray version copies the cached session in and skips
    ``cosmic-ray init``. Its path is
    ``<cache_dir>/<safe_target>_<digest>.sqlite`` (the first 16 hex digits
    of a SHA-256), ``cache_dir`` being ``cosmic_ray_catalogue_cache_dir``.
    The library has no artifacts directory, so ``auto`` turns the cache off,
    and a relative path resolves against the current working directory.

A run tests the operators its request names (a named set expands through
the registry), else Cosmic Ray's full catalogue.

In a container the whole fleet (init, operator filter, worker clones,
``cosmic-ray exec``) runs there through one self-contained launcher
(``_cr_session_runner``): the engine writes the config and a spec into the
shared work directory, makes one blocking executor call and reads the session
back. A local run uses the host-managed worker fleet.

The project environment (interpreter, executor, execution mode, work directory,
pytest configuration, import path) comes from the owning Python adapter,
and the names written into child processes from ``_plugins``. Settings
arrive as the configuration mapping, the recovery settings through hooks
that the session's engine overrides.

A ``MutationSession`` opens the engine with its workers and distributor,
runs requests and closes it. A run takes a ``MutationRequest`` and returns
a ``MutationRun`` recording the value and source of every setting, the
run's identity and its outcome. The run is read from the engine's session
before the call returns, so it stays complete after the session closes. In
a container the launcher runs the fleet for each run, with the session and
its catalogue in the shared work directory, and the target is checked and
identified there.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional, Sequence, Tuple

import tomli_w

from langadapter import _process_tree
from langadapter._execution_scope import unresolved_command
from langadapter._linux_job import ControlError
from langadapter.execution import (
    ENV_PREFIX, rebase_path, execution_identity,
)
from langadapter.python.project import _PYTEST_CONFIG_COPY
from langadapter.mutation import (
    _DEFAULT_TIMEOUT_RULE, _chosen, _common_settings,
    NativeRecord, MutationClass, FROM_DEFAULT, FROM_REQUEST, Catalogue, EffectiveSetting, MutationRequest,
    TimeoutRule, MutationRun, FAILED_INITIALIZATION,
    FAILED_INSTRUMENT_CHECK, FAILED_SESSION, FAILED_WORKERS, FAILED_SESSION_READ,
    MutationIdentity, MutationCleanupError, MutationSession as CommonMutationSession,
    _carry_cleanup, _interrupted, _require_engine,
)
from langadapter.suites import TestId
from langadapter.python._plugins import (
    JOURNAL_PLUGIN, RECORD_PLUGIN, SOURCE_DIRS, journal_plugin_text,
    run_record_text, render,
)
from langadapter.python._runs import (
    FILE_ATTRIBUTION, identify_test, carried_instrument,
    loaded_setup_files,
)
from langadapter.python._cr_session_runner import (
    WORKER_SECRET, _clone_one, _clone_pythonpath, _wait_for_worker, http_exec_command,
    target_original_copy, worker_command,
)
from langadapter.python._run_definition import (
    DISABLED_PLUGINS, RUN_VARIABLES, pytest_arguments, pytest_command, remove_unlisted,
    run_environment, write_run_inventory,
)
from langadapter.python._project_files import (
    read_container_bytes, ProjectPath, file_identity, project_file, remove_created_storage,
    storage_exists,
)
from langadapter.python.cosmic_ray import (
    SessionFolderRefused, check_local_session_dir, CosmicRaySettings,
    _resolve_distributor, _validate_session_options,
)
import dataclasses
import sys
from langadapter._environment import run_launch_environment
from langadapter.context import ContainerExecution
from langadapter.errors import InvalidRequest
from langadapter.outcomes import ObservationIdentity, Outcome
from langadapter.python._cr_verify import (
    MutationInstrumentFault, _bounded_output, _killers, _pytest_reported, _without_record,
    _zero_collected,
)
from langadapter.python import _cr_verify

# Part of every cache key: bump it when an engine change would invalidate
# cached catalogues. The key includes the target's module path.
_CACHE_FORMAT_VERSION = 2

#: The failure kind of each step the container's launcher names when it
#: stops (its status's ``failed_stage``).
_STAGE_FAILURES = {"initialization": FAILED_INITIALIZATION, "workers": FAILED_WORKERS,
                   "isolation": FAILED_WORKERS, "exec": FAILED_SESSION}

#: A catalogue cache entry's name (``_resolve_cache_path``): the target's
#: stem in word characters and hyphens, then the key's first 16 hex digits.
#: The cache's cleanup removes only files of this name.
_CACHE_ENTRY = re.compile(r"[\w-]*_[0-9a-f]{16}\.sqlite")

#: Prints the project's top directories (not packages themselves) whose packages
#: or modules the run's interpreter imports from that directory ("inside": a
#: src layout) or from another tree ending in the same path ("outside": an
#: editable install mapping a copy's ``src`` to the original), and the
#: packages of other directories named like installed ones ("elsewhere"). A
#: package in the root, ``src`` or ``lib`` is outside wherever else it
#: resolves, as is an installed package, module or namespace directory in
#: ``src``, ``lib`` or ``python``. Packages directly under the root count as
#: the directory "" (an import hook such as meson-python's maps them). Nothing
#: is imported.
_SHADOWED_ROOTS = r'''
import importlib.util, json, os, sys, sysconfig
def norm(path):
    return os.path.normcase(os.path.realpath(path))
def installed(path):
    directories = [sysconfig.get_paths().get(key) for key in ("purelib", "platlib")]
    try:
        import site
        directories += site.getsitepackages() + [site.getusersitepackages()]
    except Exception:
        pass
    return any(path.startswith(norm(directory) + os.sep) for directory in directories if directory)
root, inside, outside, elsewhere = norm(sys.argv[1]), set(), set(), set()
candidates = []
for top in sorted(os.listdir(root)):
    base = os.path.join(root, top)
    if top.startswith(".") or not os.path.isdir(base):
        continue
    if os.path.exists(os.path.join(base, "__init__.py")):
        candidates.append(("", top, base))
        continue
    for name in sorted(os.listdir(base))[:200]:
        candidates.append((top, name, os.path.join(base, name)))
for top, name, path in candidates:
    stem = name[:-3] if name.endswith(".py") else name
    if not stem.isidentifier() or (stem == name and not os.path.isdir(path)):
        continue
    try:
        spec = importlib.util.find_spec(stem)
    except Exception:
        continue
    if spec is None:
        continue
    # The first existing search directory, else the origin: an import hook may
    # name a search location that is no directory (meson-python 0.16+).
    first = next((location for location in spec.submodule_search_locations or ()
                  if os.path.isdir(location)), "")
    if not first and spec.origin and os.path.exists(spec.origin):
        first = spec.origin
        if os.path.basename(first).startswith("__init__."):
            first = os.path.dirname(first)
    first = norm(first) if first else ""
    package = os.path.isfile(os.path.join(path, "__init__.py"))
    strict = (top in ("", "src", "lib") and package
              or top in @SOURCE_DIRS@ and installed(first))
    if first == norm(path):
        inside.add(top)
    elif first and (strict or first.endswith(norm(path)[len(root):])):
        outside.add(top)
    elif first and top and package and installed(first):
        # A package in another directory named like an installed one: an
        # example may share a library's name, so its directory goes first
        # only for a run whose target it holds.
        elsewhere.add((top, name))
print(json.dumps({"inside": sorted(inside), "outside": sorted(outside),
                  "elsewhere": sorted([top, name] for top, name in elsewhere)}))
'''.replace("@SOURCE_DIRS@", repr(SOURCE_DIRS))


def _auxiliary_worker_plan(
    configured_workers: int,
    use_http: bool,
    pending_mutants: int,
) -> Tuple[int, bool]:
    """Right-size an auxiliary fleet without changing its distributor."""
    if not use_http:
        return 1, False
    workers = max(1, min(int(configured_workers), int(pending_mutants)))
    return workers, True


#: The open-file soft limit of each mutant's test run on a POSIX host,
#: bounded by the hard limit (``_cr_session_runner._OPEN_FILES_TARGET``).
TEST_OPEN_FILES = 65536


def wrapped_open_file_limit(soft: int, hard: int) -> int:
    """The soft limit the per-run wrapper gives a test run, from the soft
    and hard limits it inherits: 65536 bounded by the hard limit, never
    below a higher soft limit (``resource.RLIM_INFINITY`` is unlimited)."""
    try:
        import resource
        unlimited = resource.RLIM_INFINITY
    except ImportError:  # read on a Windows host for a container's limits
        unlimited = -1
    want = TEST_OPEN_FILES if hard == unlimited else min(hard, TEST_OPEN_FILES)
    return soft if soft == unlimited or soft >= want else want


_PER_RUN_WRAPPER_SH = (

            "#!/bin/sh\n"
            'if [ -n "$@PREFIX@FDLIMIT_DEBUG" ]; then\n'
            '  echo "[fdlimit] before soft=$(ulimit -Sn) hard=$(ulimit -Hn)'
            ' fds=$(ls /proc/self/fd 2>/dev/null | wc -l)"\n'
            "fi\n"
            # Up to 65536, not the hard limit: a huge soft limit slows every
            # spawn that closes descriptors. A higher soft limit stays.
            f'_hard=$(ulimit -Hn); _soft=$(ulimit -Sn); _want={TEST_OPEN_FILES}\n'
            'if [ "$_hard" != "unlimited" ] && [ "$_hard" -lt "$_want" ]; then _want=$_hard; fi\n'
            'if [ "$_soft" != "unlimited" ] && [ "$_soft" -lt "$_want" ]; then\n'
            '  ulimit -S -n "$_want" 2>/dev/null || true\n'
            'fi\n'
            'if [ -n "$@PREFIX@FDLIMIT_DEBUG" ]; then\n'
            '  echo "[fdlimit] after soft=$(ulimit -Sn)"\n'
            "fi\n"
            # One thread per numeric pool, overriding an inherited count that
            # _base_env's setdefault keeps: deterministic float reductions,
            # and no pool descriptors (eventfd).
            "export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1"
            " NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1 NUMBA_NUM_THREADS=1\n"
            # Diagnostic descriptor watcher (<prefix>FDLEAK_WATCH), a shell loop
            # since the climb happens in a C-extension import holding the GIL.
            # It polls the pid the exec'd pytest inherits and dumps the
            # descriptor targets once their count passes 3000.
            'if [ -n "$@PREFIX@FDLEAK_WATCH" ]; then\n'
            "  _self=$$\n"
            '  _wdir="${@PREFIX@FDLEAK_DIR:-${@PREFIX@PROJECT_WORKZONE:-.}}"\n'
            "  ( _i=0\n"
            "    while [ $_i -lt 60000 ]; do\n"
            # Stop when pytest exits: orphaned pollers pile up across mutants.
            "      [ -e /proc/$_self ] || break\n"
            '      _n=$(ls /proc/$_self/fd 2>/dev/null | wc -l)\n'
            '      if [ "${_n:-0}" -gt 3000 ]; then\n'
            '        { echo "total=$_n"; ls -l /proc/$_self/fd 2>/dev/null'
            ' | sed -E "s/.* -> //; s/[0-9]+/N/g" | sort | uniq -c | sort -rn'
            ' | head -30; } > "$_wdir/fdleak_$_self.txt" 2>/dev/null\n'
            "        break\n"
            "      fi\n"
            "      _i=$((_i+1)); sleep 0.005\n"
            "    done ) &\n"
            "fi\n"
            # Every test run starts where the normal run starts: in its root,
            # a worker's clone or the project the local distributor mutates in
            # place. Cosmic Ray splits the command without a shell, so the
            # engine plants __CR_WORKER_CWD__ in the synced suite's paths and
            # this wrapper rewrites it to that root. The run is exec'd so a
            # timeout kill hits pytest, not a shell.
            '_origcwd=$PWD\n'
            "_n=$#\n"
            "_i=0\n"
            'while [ "$_i" -lt "$_n" ]; do\n'
            '  _a=$1; shift\n'
            '  case "$_a" in\n'
            "    *__CR_WORKER_CWD__*)\n"
            '      _a=$(printf %s "$_a" | sed "s|__CR_WORKER_CWD__|$_origcwd|g");;\n'
            "  esac\n"
            '  set -- "$@" "$_a"\n'
            "  _i=$((_i+1))\n"
            "done\n"
            # The files earlier runs created in the root go first, as before a
            # normal run: every file or link below it that the session's
            # inventory of it (@PREFIX@RUN_INVENTORY, a file beside the root)
            # does not list. The root's git directory and the directories
            # @PREFIX@RUN_KEEP names (the session's own, one per line, as
            # ``./<path>/``) stay untouched, and so do directories. A root without
            # an inventory (one the session does not own) stays as it is.
            'if [ -n "${@PREFIX@RUN_INVENTORY:-}" ]; then\n'
            '  _now="${@PREFIX@RUN_INVENTORY}.$$"\n'
            "  find . -path ./.git -prune -o \\( -type f -o -type l \\) -print"
            ' | awk -v keep="${@PREFIX@RUN_KEEP:-}"'
            " 'BEGIN { n = split(keep, k, \"\\n\") }"
            ' { for (i = 1; i <= n; i++) if (k[i] != "" && index($0 "/", k[i]) == 1) next;'
            " print }'"
            ' | LC_ALL=C sort > "$_now" || exit 70\n'
            '  LC_ALL=C comm -13 "${@PREFIX@RUN_INVENTORY}" "$_now" |'
            ' while IFS= read -r _f; do\n'
            '    case "$_f" in ./?*) rm -f -- "$_f" || exit 70;; esac\n'
            '  done || exit 70\n'
            '  rm -f -- "$_now"\n'
            'fi\n'
            # A temporary directory of its own (TMPDIR), emptied at each run of
            # this worker (the wrapper's parent), inside the inherited TMPDIR.
            # No user namespace: in one the tests ran as uid 0, which writes a
            # read-only file that the normal run cannot.
            '_runtmp="${TMPDIR:-/tmp}/cr_runtmp_$PPID"\n'
            'rm -rf "$_runtmp" && mkdir -p "$_runtmp"'
            ' && : > "$_runtmp.langadapter-cosmic-ray" || exit 70\n'
            'export TMPDIR="$_runtmp"\n'
            'exec "$@"\n'
)

# The shell wrapper names its variables with ``@PREFIX@``, which
# ``render`` replaces with the variable prefix.
#
# Windows has no POSIX shell, so local runs there use this equivalent: one
# thread per numeric pool, the __CR_WORKER_CWD__ rewrite, the removal of the files earlier runs created
# in the root (from the session's inventory, whose listing ``_entries``
# repeats from ``_run_definition.run_inventory``), a temporary directory made
# for each run and removed after it (a Windows venv interpreter is a
# launcher, so the parent process is no stable worker identity), and the run
# in the root, as the normal run starts it. The descriptor limit has no
# Windows counterpart. The test command runs in this process, so a timeout
# kill reaches pytest, and the interpreter flags before ``-m`` are this
# process's own: the engine passes them to the interpreter that runs the
# wrapper. A failure to prepare the run exits 70.
_PER_RUN_WRAPPER_PY = '''\
import os
import runpy
import shutil
import sys
import tempfile


def _entries(root, keep):
    entries = []
    pending = [""]
    while pending:
        relative = pending.pop()
        with os.scandir(os.path.join(root, relative) if relative else root) as listing:
            for entry in listing:
                name = relative + "/" + entry.name if relative else entry.name
                if not relative and entry.name == ".git":
                    continue
                if any(("./" + name + "/").startswith(prefix) for prefix in keep):
                    continue
                if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                    entries.append("./" + name)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(name)
    return sorted(entries, key=lambda text: text.encode("utf-8", "surrogateescape"))


def main():
    for name in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                 "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
                 "NUMBA_NUM_THREADS"):
        os.environ[name] = "1"
    original_cwd = os.getcwd().replace(os.sep, "/")
    root = os.getcwd()
    command = [arg.replace("__CR_WORKER_CWD__", original_cwd)
               for arg in sys.argv[1:]]
    module = command.index("-m", 1) + 1 if "-m" in command[1:] else 0
    if not module or len(command) <= module:
        sys.stderr.write("per-run wrapper expects: <python> [flags] -m <module> ...\\n")
        return 70
    inventory = os.environ.get("@PREFIX@RUN_INVENTORY")
    keep = [prefix for prefix in os.environ.get("@PREFIX@RUN_KEEP", "").split("\\n") if prefix]
    base = os.environ.get("TMPDIR") or tempfile.gettempdir()
    try:
        if inventory:
            with open(inventory, "rb") as handle:
                listed = set(handle.read().decode("utf-8", "surrogateescape").splitlines())
            for entry in _entries(root, keep):
                if entry not in listed:
                    os.remove(os.path.join(root, *entry[2:].split("/")))
        run_tmp = tempfile.mkdtemp(prefix="cr_runtmp_", dir=base)
        open(run_tmp + ".langadapter-cosmic-ray", "x").close()
    except OSError as exc:
        sys.stderr.write("per-run wrapper cannot prepare the run in %s: "
                         "%s\\n" % (base, exc))
        return 70
    os.environ["TMPDIR"] = run_tmp
    tempfile.tempdir = None
    # ``python -m`` puts the working directory first on the import path.
    sys.path[0] = root
    sys.argv = command[module:]
    try:
        runpy.run_module(command[module], run_name="__main__", alter_sys=True)
    finally:
        # The test command's exit arrives as SystemExit. A run the clock or a
        # cancel kills leaves its directory for its session's cleanup or the
        # engine's sweep.
        shutil.rmtree(run_tmp, ignore_errors=True)
        if not os.path.exists(run_tmp):
            try:
                os.remove(run_tmp + ".langadapter-cosmic-ray")
            except OSError:
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

#: Where the wrappers set a run's journal variables: before they read the
#: working directory the suite's paths are relative to.
_SH_JOURNAL_ANCHOR = "_origcwd=$PWD\n"

_PY_JOURNAL_ANCHOR = '    original_cwd = os.getcwd().replace(os.sep, "/")\n'

#: Where the wrappers write a run's owner marker, which names the session
#: whose cleanup removes the run's directory (``_remove_run_directories``).
_SH_MARKER_ANCHOR = ': > "$_runtmp.langadapter-cosmic-ray"'

_PY_MARKER_ANCHOR = '        open(run_tmp + ".langadapter-cosmic-ray", "x").close()\n'

#: Where the Python wrapper puts the journal plugin on the import path: after
#: the run's root.
_PY_PATH_ANCHOR = "    sys.path[0] = root\n"


def _per_run_wrapper_text(posix: bool,
                          journal: Optional[Tuple[Path, str]] = None,
                          plugin_dir: Optional[Path] = None,
                          target: Optional[str] = None,
                          import_roots: Sequence[str] = (),
                          owner: str = "",
                          record_dir: Optional[str] = None) -> str:
    """The per-run wrapper: on a POSIX host the shell wrapper with the
    variable prefix, else the Python wrapper.

    ``journal`` (the evidence directory, and the module under mutation
    relative to the source root) makes each run write its per-test journal
    (the timeout evidence) into a directory of its own there, named by its
    process id. ``target`` (the module under mutation relative to the
    source root) turns on the instrument's per-test restore of that module,
    as container runs do, so a test that rebinds one of its names does not
    change what later tests see. ``plugin_dir`` holds the journal plugin for
    a test command without the instrument and goes on the import path.
    ``import_roots``, directories of the source root, go first on the import
    path (``CosmicRayEngine._target_roots``). ``owner``, a session's token,
    goes into the owner marker beside each run's temporary directory, so that
    session's cleanup removes it (``_remove_run_directories``).
    ``record_dir`` holds the run record plugin (``RECORD_PLUGIN``): it goes
    on the import path, and each run prints its record after pytest's output.

    The source root is the one the process names (a worker's clone), else
    the directory the run starts in (the local distributor mutates the
    project in place there)."""
    prefix = ENV_PREFIX
    source_root = prefix + "MUTATION_SOURCE_ROOT"
    lines = ""
    if not posix:
        text = render(_PER_RUN_WRAPPER_PY)
        if owner:
            text = text.replace(_PY_MARKER_ANCHOR, (
                '        with open(run_tmp + ".langadapter-cosmic-ray", "x") as marker:\n'
                f"            marker.write({owner!r})\n"))
        if target is not None:
            lines += (
                f"    os.environ[{prefix + 'MUTATION_TARGET'!r}] = {target!r}\n"
                f"    os.environ.setdefault({source_root!r}, os.getcwd())\n")
        if journal is not None:
            root, module = journal
            lines += (
                f"    os.environ[{prefix + 'TIMEOUT_EVIDENCE_DIR'!r}] = "
                f"os.path.join({str(root)!r}, 'r%d' % os.getpid())\n"
                f"    os.environ[{prefix + 'JOURNAL_MODULE'!r}] = {module!r}\n")
        if plugin_dir is not None:
            lines += (f"    os.environ.setdefault({source_root!r}, "
                      f"os.getcwd())\n")
            text = text.replace(_PY_PATH_ANCHOR, _PY_PATH_ANCHOR + (
                f"    sys.path.insert(1, {str(plugin_dir)!r})\n"))
        if record_dir is not None:
            lines += f"    os.environ[{prefix + 'RUN_RECORD'!r}] = '-'\n"
            text = text.replace(_PY_PATH_ANCHOR, _PY_PATH_ANCHOR + (
                f"    sys.path.insert(1, {str(record_dir)!r})\n"))
        if import_roots:
            lines += (
                f"    roots = [os.path.join(os.environ.get({source_root!r}) or os.getcwd(),"
                f" name) for name in {list(import_roots)!r}]\n"
                "    os.environ['PYTHONPATH'] = os.pathsep.join(\n"
                "        roots + [os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH')"
                " else roots)\n")
            text = text.replace(_PY_PATH_ANCHOR, _PY_PATH_ANCHOR + (
                "    sys.path[1:1] = roots\n"))
        return text.replace(_PY_JOURNAL_ANCHOR, lines + _PY_JOURNAL_ANCHOR)
    text = render(_PER_RUN_WRAPPER_SH)
    if owner:
        text = text.replace(_SH_MARKER_ANCHOR, (
            f'printf %s {shlex.quote(owner)} > "$_runtmp.langadapter-cosmic-ray"'))
    if target is not None:
        lines += (
            f"export {prefix}MUTATION_TARGET={shlex.quote(target)} "
            f'{source_root}="${{{source_root}:-$PWD}}"\n')
    if journal is not None:
        root, module = journal
        lines += (
            f"export {prefix}TIMEOUT_EVIDENCE_DIR="
            f"{shlex.quote(Path(root).as_posix())}/r$$ "
            f"{prefix}JOURNAL_MODULE={shlex.quote(module)}\n")
    if plugin_dir is not None:
        lines += (
            f'export {source_root}="${{{source_root}:-$PWD}}" '
            f"PYTHONPATH={shlex.quote(Path(plugin_dir).as_posix())}"
            '"${PYTHONPATH:+:$PYTHONPATH}"\n')
    if record_dir is not None:
        lines += (
            f"export {prefix}RUN_RECORD=- "
            f"PYTHONPATH={shlex.quote(str(record_dir))}"
            '"${PYTHONPATH:+:$PYTHONPATH}"\n')
    if import_roots:
        roots = ":".join(f'"${{{source_root}:-$PWD}}"/{shlex.quote(name)}'
                         for name in import_roots)
        lines += f'export PYTHONPATH={roots}"${{PYTHONPATH:+:$PYTHONPATH}}"\n'
    return text.replace(_SH_JOURNAL_ANCHOR, lines + _SH_JOURNAL_ANCHOR)


def _write_per_run_wrapper(directory: Path, executable: str,
                           journal: Optional[Tuple[Path, str]] = None,
                           plugin_dir: Optional[Path] = None,
                           target: Optional[str] = None,
                           import_roots: Sequence[str] = (),
                           owner: str = "", *,
                           flags: Sequence[str] = (),
                           record_dir: Optional[Path] = None) -> List[str]:
    """Write this host's per-run wrapper (``_per_run_wrapper_text``) beside
    the session and return the arguments that invoke it, which come before
    the test command's own. The Python wrapper runs the test command in its
    own process, so its interpreter takes the context's ``flags``."""
    directory.mkdir(parents=True, exist_ok=True)
    posix = os.name != "nt"
    wrapper_path = directory / ("_cr_fdlimit_run.sh" if posix
                                else "_cr_run.py")
    wrapper_path.write_text(
        _per_run_wrapper_text(posix, journal, plugin_dir, target, import_roots,
                              owner, None if record_dir is None else Path(record_dir).as_posix()),
        encoding="utf-8")
    if posix:
        return ["sh", wrapper_path.as_posix()]
    return [executable, *flags, wrapper_path.as_posix()]


def _suite_fingerprint(test_dir: Path, command: str) -> Optional[str]:
    """The suite a verification measured, keying the session's cache of
    genuine kills: the per-mutant test command and every file of the suite's
    directory (bytecode caches aside). None when a file cannot be read, so
    nothing is cached or taken from the cache."""
    digest = hashlib.sha256(command.encode("utf-8"))
    try:
        files = sorted(
            path for path in Path(test_dir).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts)
        for path in files:
            digest.update(b"\0" + path.relative_to(test_dir).as_posix().encode("utf-8")
                          + b"\0" + path.read_bytes())
    except OSError:
        return None
    return digest.hexdigest()


class OriginRefused(MutationInstrumentFault):
    """The check run before the first mutant refused the measurement: the
    unmutated suite did not pass, did not finish in time, or executed the
    module under test from another file. Nothing was mutated or changed, so
    the session stays usable for its next run. ``stopping_tests`` are the
    tests that did not pass."""

    stopping_tests: Tuple[TestId, ...] = ()


_PROJECT_METADATA_PREFIX = "__LANGADAPTER_COSMIC_RAY_METADATA__="

#: Cosmic Ray's version (``unknown`` when unreadable), its operator
#: catalogue and the project interpreter's version as ``pyMAJOR.MINOR``
#: (each None when unreadable).
CosmicRayMetadata = Tuple[str, Optional[List[str]], Optional[str]]


def _project_cosmic_ray_metadata(
    executor: Any, python: str, *, flags: Sequence[str],
) -> CosmicRayMetadata:
    """The Cosmic Ray metadata, read through the project interpreter
    ``python`` with ``flags`` run by ``executor``. In container mode the engine runs on the
    host and the mutants in the container: reading through the project
    interpreter ties operator filters and cache keys to the Cosmic Ray that
    runs the mutants and to the interpreter that parses the target."""
    script = (
        "import importlib.metadata as m,json,sys\n"
        "try:\n"
        "    version=m.version('cosmic-ray')\n"
        "except Exception:\n"
        "    version='unknown'\n"
        "try:\n"
        "    import cosmic_ray.plugins as p\n"
        "    operators=list(p.operator_names())\n"
        "except Exception:\n"
        "    operators=None\n"
        f"print({_PROJECT_METADATA_PREFIX!r}+json.dumps("
        "{'version':version,'operators':operators,"
        "'python':'py%d.%d'%sys.version_info[:2]},sort_keys=True))\n"
    )
    try:
        result = executor.run(
            [python, *flags, "-c", script],
            timeout=60,
        )
    except Exception:
        return "unknown", None, None
    if result.returncode != 0:
        return "unknown", None, None
    for line in reversed((result.stdout or "").splitlines()):
        if not line.startswith(_PROJECT_METADATA_PREFIX):
            continue
        try:
            payload = json.loads(line[len(_PROJECT_METADATA_PREFIX):])
        except (TypeError, ValueError):
            return "unknown", None, None
        version = payload.get("version")
        operators = payload.get("operators")
        interpreter = payload.get("python")
        if not isinstance(version, str):
            version = "unknown"
        if (
            not isinstance(operators, list)
            or not all(isinstance(name, str) for name in operators)
        ):
            operators = None
        if not (isinstance(interpreter, str)
                and re.fullmatch(r"py\d+\.\d+", interpreter)):
            interpreter = None
        return version, operators, interpreter
    return "unknown", None, None


#: The refusal of a run whose operator filter failed, on the host and in a
#: container.
FILTER_REFUSAL = ("the operator filter failed, so the mutation run stops rather than "
                  "measure every mutant of the catalogue as the requested operators: "
                  "{failure}")


def _project_bin(name: str, python: str) -> str:
    """The executable ``name`` (``cosmic-ray``) beside the project
    interpreter, so the run uses that environment's tool even when it is not
    on ``PATH``, else the bare name for ``PATH`` resolution."""
    bindir = str(Path(python).parent)
    return shutil.which(name, path=bindir) or name


def _resolve_project_tooling(python: str) -> Tuple[str, str]:
    """The interpreter that runs the mutant test command and the
    ``cosmic-ray`` binary, both from the project interpreter ``python``, so
    a caller with a separate environment mutates and tests in the project's.
    The interpreter path uses forward slashes: Cosmic Ray splits the test
    command with ``shlex.split(posix=True)``, which drops backslashes."""
    executable = python.replace(os.sep, "/")
    return executable, _project_bin("cosmic-ray", python)


def _node_local_scratch_candidates() -> Tuple[Path, ...]:
    """Node-local scratch locations for the worker clones, best first.
    /dev/shm only on POSIX: on Windows it is a plain disk directory
    (``C:\\dev\\shm``)."""
    if os.name == "posix":
        return (Path("/dev/shm"), Path(tempfile.gettempdir()))
    return (Path(tempfile.gettempdir()),)


def _clone_base_dir(config, project_root: Path) -> Path:
    """The directory for the per-worker source clones. A clone is a full
    copy of ``project_root``, read hot by every per-mutant run and discarded
    with the session, so clones go on node-local scratch: RAM (``/dev/shm``)
    first, the system temporary directory second, each only when writable
    with at least 1 GiB free (a fleet on a shared filesystem is slow and
    exhausts its inode quota). Without such scratch they go in the launch
    directory, never inside ``project_root`` (cloning would copy them into
    themselves). The stale-clone sweep reclaims what a killed run leaves.
    ``cosmic_ray_clone_dir`` overrides the location."""
    raw = str(config["cosmic_ray_clone_dir"] or "").strip()
    if raw and raw.lower() != "auto":
        base = Path(raw)
    else:
        base = None
        for cand in _node_local_scratch_candidates():
            try:
                if (cand.is_dir() and os.access(cand, os.W_OK)
                        and shutil.disk_usage(cand).free >= 1 << 30):
                    base = cand / "cr_worker_clones"
                    break
            except OSError:
                continue
        if base is None:
            base = Path.cwd() / "cr_worker_clones"
            pr = Path(project_root).resolve()
            if pr == base.resolve() or pr in base.resolve().parents:
                base = pr.parent / "cr_worker_clones"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _local_session_base(configured: Optional[str]) -> Tuple[Optional[str], str]:
    """The directory holding a local run's session directory, and where it was
    chosen: the configured node-local directory (made when missing), else None
    for the system temporary directory. A configured directory that cannot be
    made or written raises ``SessionFolderRefused``, by the rule a caller
    can check first (``cosmic_ray.check_local_session_dir``), as the
    container launcher does (``_cr_session_runner``)."""
    configured = str(configured or "").strip()
    if not configured:
        return None, "system temporary folder"
    check_local_session_dir(configured)
    return configured, "configured cosmic_ray_local_session_dir"


def _launcher_status(path: Path, label: str, log_fn) -> Dict[str, Any]:
    """The container launcher's status (``_cr_session_runner``), with one
    logged line naming where the session ran, the ready workers and the
    open-file limit: the run's record of them. Empty when unreadable."""
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            raise ValueError("not an object")
    except (OSError, ValueError) as exc:
        if log_fn is not None:
            log_fn(f"[CosmicRayEngine] {label}: the launcher wrote no status "
                   f"({type(exc).__name__})", level=1)
        return {}
    if log_fn is not None:
        if status.get("session_refused"):
            place = f"session SQLite refused: {status['session_refused']}"
        else:
            place = (f"session SQLite at {status.get('session_dir')} "
                     f"({status.get('session_origin')})")
        fleet = (f"{status.get('ready_workers')}/{status.get('workers')} workers ready"
                 if status.get("distributor") == "http" else "local distributor")
        limit = status.get("open_file_limit")
        files = (f"open-file limit: workers {limit[0]}, tests "
                 f"{wrapped_open_file_limit(limit[0], limit[1])} (hard {limit[1]})"
                 if isinstance(limit, list) and len(limit) == 2
                 else "open-file limit unchanged")
        log_fn(f"[CosmicRayEngine] {label}: {place}, {fleet}, {files}", level=1)
    return status


#: The suffix of the owner marker beside each directory this engine makes in
#: a shared temporary location: the stale sweeps remove only marked ones.
OWNER_MARKER_SUFFIX = ".langadapter-cosmic-ray"


def _owner_marker(directory: Path) -> Path:
    return directory.with_name(directory.name + OWNER_MARKER_SUFFIX)


def _mark_owned(directory: Path) -> Path:
    _owner_marker(directory).touch(exist_ok=False)
    return directory


def _inventory_of(directory: Path) -> Path:
    """Where a root's run inventory lies: beside it, outside it
    (``_run_definition.write_run_inventory``)."""
    return directory.with_name(directory.name + ".inventory")


def _unmark(directory: Path) -> None:
    try:
        _owner_marker(directory).unlink()
    except FileNotFoundError:
        pass


def _sweep_marked(directory: Path, cutoff: float) -> Optional[bool]:
    """Remove one stale marked directory: True if gone, False if it stays,
    None if it is not this engine's or not stale."""
    marker = _owner_marker(directory)
    if (not directory.is_dir() or directory.is_symlink() or not marker.is_file()
            or directory.stat().st_mtime >= cutoff):
        return None
    shutil.rmtree(directory, ignore_errors=True)
    if directory.exists():
        return False
    _inventory_of(directory).unlink(missing_ok=True)
    marker.unlink()
    return True


def _sweep_stale_clones(base: Optional[Path], max_age_hours: float,
                        log_fn=None) -> int:
    """Remove this engine's marked ``cr_worker_*`` clone directories older
    than ``max_age_hours`` from ``base`` (the system temporary directory
    when None) and return how many. Only a process killed before its cleanup
    leaves one, and an age well past any session spares concurrent live
    clones. Session directories are never swept (a failed session's
    database is kept as evidence). ``max_age_hours <= 0`` disables it."""
    if max_age_hours <= 0:
        return 0
    root = base if base is not None else Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_hours * 3600.0
    removed = kept = 0
    try:
        candidates = list(root.glob("cr_worker_*"))
    except Exception:
        return 0
    for d in candidates:
        try:
            swept = _sweep_marked(d, cutoff)
        except Exception:
            continue
        if swept is True:
            removed += 1
        elif swept is False:
            kept += 1
    if (removed or kept) and log_fn is not None:
        log_fn(
            f"[CosmicRayEngine] swept {removed} stale worker clone(s) "
            f"(> {max_age_hours}h old) from {root}"
            + (f", {kept} could not be removed" if kept else ""),
            level=2,
        )
    return removed


def _run_base(env: Dict[str, str]) -> Path:
    """Where the per-run wrapper of a run with ``env`` makes its temporary
    directory: TMPDIR, else on Windows the TEMP or TMP that Python's
    ``tempfile`` takes there, and on POSIX the shell wrapper's /tmp."""
    if env.get("TMPDIR"):
        return Path(env["TMPDIR"])
    if os.name == "nt":
        for name in ("TEMP", "TMP"):
            if env.get(name):
                return Path(env[name])
        return Path(tempfile.gettempdir())
    return Path("/tmp")


def _remove_run_directories(bases, owner: str) -> int:
    """Remove from ``bases`` the per-run temporary directories, and their
    markers, whose owner marker names ``owner`` (a session's, once its runs
    have ended), and return how many. The Windows wrapper leaves one only
    when the per-mutant clock or a cancel kills a run, the POSIX shell
    wrapper one per worker process. One that cannot be removed stays marked
    for the age sweep (``_sweep_stale_run_directories``)."""
    removed = 0
    for base in bases:
        try:
            markers = list(Path(base).glob("cr_runtmp_*" + OWNER_MARKER_SUFFIX))
        except OSError:
            continue
        for marker in markers:
            directory = marker.with_name(marker.name[:-len(OWNER_MARKER_SUFFIX)])
            try:
                if (not marker.is_file() or directory.is_symlink()
                        or marker.read_text(encoding="utf-8") != owner):
                    continue
                shutil.rmtree(directory, ignore_errors=True)
                if directory.exists():
                    continue
                marker.unlink()
            except OSError:
                continue
            removed += 1
    return removed


def _sweep_stale_run_directories(max_age_hours: float, log_fn=None) -> int:
    """Remove this engine's marked per-run temporary directories
    (``cr_runtmp_*``) older than ``max_age_hours`` from TMPDIR or the system
    temporary directory, and return how many. The Windows wrapper leaves one
    when the per-mutant clock or a cancel kills a run. The POSIX shell
    wrapper ``exec``s the test command and removes nothing: each worker
    process (an HTTP worker, or the local distributor's ``cosmic-ray
    exec``) keeps one, ``cr_runtmp_<worker pid>``, that outlives it, and a
    container's worker keeps it in a directory its launcher removes.
    ``max_age_hours <= 0`` disables the sweep."""
    if max_age_hours <= 0:
        return 0
    base = Path(os.environ.get("TMPDIR") or tempfile.gettempdir())
    cutoff = time.time() - max_age_hours * 3600.0
    removed = kept = 0
    try:
        candidates = list(base.glob("cr_runtmp_*"))
    except OSError:
        return 0
    for directory in candidates:
        if directory.name.endswith(OWNER_MARKER_SUFFIX):
            continue
        try:
            swept = _sweep_marked(directory, cutoff)
        except OSError:
            continue
        if swept is True:
            removed += 1
        elif swept is False:
            kept += 1
    if (removed or kept) and log_fn is not None:
        log_fn(
            f"[CosmicRayEngine] swept {removed} stale per-run directory(ies) "
            f"(> {max_age_hours}h old) from {base}"
            + (f", {kept} could not be removed" if kept else ""),
            level=2,
        )
    return removed


def _mutation_diag_tb() -> str:
    """pytest's ``--tb`` level for the per-mutant run: ``no`` (fastest), or
    ``<prefix>MUTATION_DIAG_TB`` (``short``, ``line``) to keep tracebacks."""
    return os.environ.get(f"{ENV_PREFIX}MUTATION_DIAG_TB", "no")


def _wong_mathur_selective_operators() -> List[str]:
    """The Wong-Mathur (1995) selective operator set in Cosmic Ray 8.x
    operator names, 74 of the 213 operators Cosmic Ray 8.4.1 registers:

      AOR (arithmetic operator replacement): every pairwise replacement of
          the 7 binary arithmetic operators (Add, Sub, Mul, Div, FloorDiv,
          Mod, Pow), 42 operators.
      ROR (relational operator replacement): every pairwise replacement of
          the 6 relational operators (Eq, NotEq, Lt, LtE, Gt, GtE), 30
          operators. The identity comparisons Is and IsNot are left out.
      LCR (logical connector replacement): the and/or swaps, 2 operators.

    Selective mutation follows Wong and Mathur (1995) and Andrews et al.
    (2005)."""
    arith = ("Add", "Sub", "Mul", "Div", "FloorDiv", "Mod", "Pow")
    comp = ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE")
    aor = [
        f"core/ReplaceBinaryOperator_{src}_{dst}"
        for src in arith for dst in arith if src != dst
    ]
    ror = [
        f"core/ReplaceComparisonOperator_{src}_{dst}"
        for src in comp for dst in comp if src != dst
    ]
    lcr = ["core/ReplaceAndWithOr", "core/ReplaceOrWithAnd"]
    return aor + ror + lcr


# Named operator sets, by the name a catalogue gives (``MutationSession``
# resolves them). A set added here needs no other change.
_OPERATOR_SET_SHORTCUTS = {
    "wong_mathur_selective": _wong_mathur_selective_operators,
}


def _compute_excluded_operators(
    allow_list: List[str], operators: Optional[List[str]],
) -> List[str]:
    """The complement of ``allow_list`` in Cosmic Ray's full operator
    catalogue ``operators``, to emit as exclude patterns: ``cosmic-ray
    init`` generates every registered operator and takes no allow-list, and
    the supported restriction, ``exclude-operators`` of
    ``[cosmic-ray.filters.operators-filter]``, marks matching mutants
    SKIPPED between init and exec.

    ``operators`` is read through the project interpreter (in the container
    in container mode). None raises ``RuntimeError``: an empty exclude list
    would run the full catalogue instead of the requested subset."""
    if operators is None:
        raise RuntimeError(
            "Could not read the Cosmic Ray operator catalogue from the "
            "selected project interpreter"
        )
    return sorted(set(operators) - set(allow_list))


def _validate_session_db(session_path: Path) -> None:
    """Raise ``sqlite3.DatabaseError`` unless ``session_path`` is a session
    database Cosmic Ray accepts. Run after each cache restore and each newly
    persisted cache entry, as strict as Cosmic Ray itself: a file whose
    header and ``sqlite_master`` are intact but whose data pages are corrupt
    would otherwise fail later, in the operator filter or ``cosmic-ray
    exec``, as an opaque ``file is not a database``.

    The checks, each reaching deeper: ``PRAGMA quick_check`` (every B-tree
    page), a ``work_items`` table in ``sqlite_master``, ``PRAGMA
    table_info("work_items")`` (the probe SQLAlchemy's ``has_table`` runs
    in Cosmic Ray) and ``SELECT COUNT(*) FROM work_items`` (loads the
    table's data pages)."""
    conn = sqlite3.connect(
        f"file:{session_path}?mode=ro", uri=True, timeout=5,
    )
    try:
        result = conn.execute("PRAGMA quick_check").fetchall()
        if result != [("ok",)]:
            raise sqlite3.DatabaseError(
                f"quick_check failed: {result!r}"
            )
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='work_items'"
        ).fetchone()
        if row is None:
            raise sqlite3.DatabaseError(
                "missing cosmic-ray ``work_items`` table"
            )
        # Cosmic Ray's own probe, byte for byte.
        conn.execute('PRAGMA table_info("work_items")').fetchall()
        conn.execute("SELECT COUNT(*) FROM work_items").fetchone()
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _stopped_workers(procs: Sequence[Any]) -> List[str]:
    """How each of the workers ``procs`` that stopped ended: its exit
    status, or the end its supervisor reports, which a supervised worker
    raises on each poll and the session's cleanup handles."""
    stopped = []
    for proc in procs:
        try:
            code = proc.poll()
        except ControlError as error:
            stopped.append(str(error))
            continue
        if code is not None:
            stopped.append(f"exit {code}")
    return stopped


def _restore_container_target(executor: Any, restore: List[str], session_path: Path,
                              project_root: PurePosixPath, log_fn: Any) -> None:
    """Write the container's target back from the durable copy a launcher
    left when it ended before restoring it (``_cr_session_runner``'s
    ``target_original_copy``), with the launcher's ``restore`` command.
    A copy that stays is a RuntimeError naming it."""
    copy = target_original_copy(session_path)
    if not copy.exists():
        return
    done = executor.run(restore, cwd=project_root.as_posix(), timeout=120)
    if done.returncode != 0 or copy.exists():
        raise RuntimeError(f"the mutation target in the container may hold a mutant: the "
                           f"launcher ended before restoring it, and its original stays at "
                           f"{copy}")
    if log_fn is not None:
        log_fn("[CosmicRayEngine] the launcher ended before restoring the target, "
               "which was restored from its copy", level=1)


class CosmicRayEngine(_cr_verify._Verification):
    """The Cosmic Ray mutation engine of a Python adapter."""

    name = "cosmic_ray"
    language = "python"

    #: The failure of a container run without a work directory.
    _missing_work_dir_message = (
        "container mutation requires a work folder: a host directory "
        "mounted into the container (ContainerExecution.work_dir, seen in "
        "the container at its container_work_dir, by default the same path)."
    )
    #: The failure of a run that fails on unresolved kills. ``{detail}``
    #: says what remained unresolved.
    _unresolved_failure_message = (
        "unresolved kills fail this run: {detail}. Per-file adjudication "
        "and mutation scoring are not admissible."
    )

    #: The recovery settings of the current run: its verification and
    #: adjudication use one worker without the session's fleet, as a
    #: one-worker run does, and a kill without measurement evidence after its
    #: verification fails the run (``MutationSession`` sets both).
    serial_auxiliary = False
    fail_on_unresolved = False

    def __init__(self, config: Dict[str, Any], *, adapter: Any = None):
        """A Cosmic Ray engine configured by ``config`` for the project of
        ``adapter``, a Python adapter with a project context."""
        self.config = config
        self.adapter = adapter
        # Job ids of the last sweep's unresolved kills (verified twice
        # without measurement evidence), which the session's capture marks.
        self._last_unresolved_job_ids: set = set()
        self._last_verify_exec_failed = False
        self._full_operator_cache_loaded = False
        self._full_operator_cache: Optional[List[str]] = None
        self._last_requested_catalogue = "unknown"
        self._last_requested_operators: List[str] = []
        #: The seconds of the last run's origin run (``_origin_check``).
        self._last_origin_run_s: Optional[float] = None
        #: The last run's failure kind and reason when a step of the
        #: mutation tool failed, else None (``MutationSession._finished``).
        self._last_failure: Optional[Tuple[str, str]] = None
        #: The last run's exec exit status, None before exec finished.
        self._last_exec_exit: Optional[int] = None

    # The project environment, from the Python adapter

    def _project_adapter(self) -> Any:
        if self.adapter is None:
            raise RuntimeError(
                "this Cosmic Ray engine has no Python adapter to reach the "
                "project")
        return self.adapter

    def _executor(self) -> Any:
        return self._project_adapter()._executor()

    def _exec_mode(self) -> str:
        return self._project_adapter()._exec_mode()

    def _cosmic_ray_command(self, cosmic_ray: str, python: Optional[str] = None) -> List[str]:
        """The start of a Cosmic Ray command line for the project Python
        (``PythonProfile.cosmic_ray_command``): the console script
        ``cosmic_ray``, or ``python`` (the project interpreter) running it."""
        return self._project_adapter().profile().cosmic_ray_command(
            python or self._project_adapter().interpreter, cosmic_ray)

    def _owns_root(self, project_root) -> bool:
        """Whether ``project_root`` is the current prepared copy, the one
        root the runs may clean (as a normal run's cleanup requires)."""
        from langadapter.preparation import _Binding

        context = self._project_adapter().context
        if not isinstance(context._prepared_copy_binding, _Binding):
            return False
        if self._exec_mode() == "container":
            return PurePosixPath(Path(project_root).as_posix()) == PurePosixPath(context.root)
        return Path(project_root).absolute() == Path(context.root).absolute()

    def _root_inventory(self, project_root: Path, session_dir: Path):
        """``(root, inventory, keep)`` for an owned project root: its run
        inventory, written in the session's directory, and the session's directories
        inside the root, which no run touches. None for a root the session
        does not own: it stays as it is."""
        if not self._owns_root(project_root):
            return None
        root = Path(project_root).resolve()
        keep = tuple(f"./{kept_dir.resolve().relative_to(root).as_posix()}/"
                     for kept_dir in (Path(session_dir),)
                     if root in kept_dir.resolve().parents)
        inventory = Path(session_dir) / "run-inventory.txt"
        write_run_inventory(root, inventory, keep)
        return root, inventory, keep

    def _session_config(
        self,
        sess: "CosmicRayEngineSession",
        project_root: Path,
        suite_dir: Path,
        *,
        execution_suite_root: str,
    ) -> Optional[str]:
        """The normal run's configuration for a suite of ``sess``
        (``_suite_config``). The files it writes beside the suite (the copy
        of the project's pytest configuration, the anchor), which may lie in
        the caller's project, are the session's when they did not exist
        before: the session removes them when it closes."""
        stem = _PYTEST_CONFIG_COPY
        written = [Path(suite_dir) / f"{stem}{suffix}"
                   for suffix in (".ini", ".cfg", ".toml")]
        written.append(Path(suite_dir) / "pytest.ini")
        existing = {path for path in written if path.exists()}
        config = self._project_adapter()._suite_config(
            str(project_root), str(suite_dir),
            execution_suite_root=execution_suite_root)
        sess._config_copies.update(
            path for path in written if path not in existing and path.exists())
        return config

    def _prepend_pythonpath(self, env: Dict[str, str], entry: str) -> None:
        self._project_adapter()._prepend_pythonpath(env, entry)

    def _source_dirs(self, project_root: Path, env: Dict[str, str],
                        ) -> Optional[Tuple[Tuple[str, ...], Tuple[str, ...],
                                            Tuple[Tuple[str, str], ...]]]:
        """``_SHADOWED_ROOTS``'s inside and outside directories for
        ``project_root`` under ``env``, and its other directories' packages
        named like installed ones, None when the probe gives none."""
        root = (Path(project_root).as_posix() if self._exec_mode() == "container"
                else str(project_root))
        from langadapter._execution_scope import reraise_unresolved

        adapter = self._project_adapter()
        try:
            done = self._executor().run(
                [adapter.interpreter, *adapter._runtime_flags(), "-c", _SHADOWED_ROOTS, root],
                cwd=root, env=env, timeout=60)
            found = json.loads((done.stdout or "").strip().splitlines()[-1])
        except Exception as error:
            reraise_unresolved(error)
            return None
        if (done.returncode != 0 or not isinstance(found, dict)
                or set(found) != {"inside", "outside", "elsewhere"}
                or not all(isinstance(found[key], list)
                           and all(isinstance(name, str) for name in found[key])
                           for key in ("inside", "outside"))
                or not isinstance(found["elsewhere"], list)
                or not all(isinstance(pair, list) and len(pair) == 2
                           and all(isinstance(part, str) for part in pair)
                           for pair in found["elsewhere"])):
            return None
        return (tuple(found["inside"]), tuple(found["outside"]),
                tuple((top, name) for top, name in found["elsewhere"]))

    def _import_from_the_copy(self, project_root: Path, env: Dict[str, str],
                              ) -> Tuple[Tuple[str, ...], Tuple[Tuple[str, str], ...]]:
        """Make a session's runs import the project's own source, and return
        the directories holding it and the packages of other directories that go
        first only for a run whose target they hold (``_target_roots``). An
        editable install maps the packages to the original tree, so with a
        src layout the tests in a prepared copy or a worker clone would
        import the original and every mutant survive. The copy's directory goes
        first in ``env`` when another tree provides it, and each worker puts
        its clone's directory first. An import hook ahead of the path (as
        meson-python installs, even for a root layout) still wins, and the
        origin run (``_origin_check``) refuses it. A container without a
        work directory gets nothing (its run reports the missing work directory)."""
        container = self._exec_mode() == "container"
        if container and self._container_session_path() is None:
            return (), ()
        inside, outside, elsewhere = self._source_dirs(project_root, env) or ((), (), ())
        for name in reversed([name for name in outside if name]):
            source_dir = Path(project_root) / name
            self._prepend_pythonpath(env, source_dir.as_posix() if container else str(source_dir))
        # python_import_root's directory, first in env already (_build_env),
        # goes first in each worker's clone too.
        configured = self._project_adapter()._import_root_name()
        return tuple(sorted(name for name in {*inside, *outside, configured} if name)), elsewhere

    @staticmethod
    def _target_roots(sess: Any, module_path: str) -> Tuple[str, ...]:
        """The project's directory that holds the run's target package when
        another tree provides a package of that name and the session does
        not put the directory first (``_import_from_the_copy``'s other
        directories): it goes first for this run's tests only. A package of
        another directory, named like a library the tests import, stays out."""
        parts = PurePosixPath(module_path).parts
        if len(parts) < 3:
            return ()
        top, name = parts[0], parts[1]
        if ((top, name) in {tuple(pair) for pair in getattr(sess, "target_roots", ())}
                and top not in getattr(sess, "import_roots", ())):
            return (top,)
        return ()

    def _origin_check(self, test_command: str, *, target: str, root: str, cwd: str,
                      env: Dict[str, str], scratch: Path, timeout: float,
                      files: Sequence[str] = ()) -> None:
        """Run the session's per-mutant test command once on the unmutated
        project with the origin plugin (``_plugins.ORIGIN_PLUGIN``), and
        raise ``OriginRefused`` when its tests did not pass or finish in time
        (a failing suite would kill every mutant), when they executed the
        module under test from a file other than ``target`` (every mutant
        would survive), or when pytest ended without the record. A command
        that cannot start returns (the sweep reports it). A suite that keeps
        state between runs sees this extra run. ``target`` and ``root`` are
        the paths the run sees, ``scratch`` a fresh directory of this host the
        run can write. The run's JUnit report names the tests that did not
        pass (``OriginRefused.stopping_tests``), ``files`` being the suite's
        test files relative to its directory."""
        from langadapter._execution_scope import reraise_unresolved
        from langadapter.python._plugins import (
            ORIGIN_PLUGIN, origin_environment, origin_plugin_text, origin_refusal, read_origin,
        )
        from langadapter.runs import with_detail

        container = self._exec_mode() == "container"
        project = self._project_adapter()
        seen = (lambda path: rebase_path(str(path), project._work_dir(),
                                         project._container_work_dir())) if container else str
        plugin_dir, record = scratch / "origin_plugin", scratch / "origin.json"
        plugin_dir.mkdir(parents=True, exist_ok=True)
        (plugin_dir / f"{ORIGIN_PLUGIN}.py").write_text(
            origin_plugin_text(), encoding="utf-8")
        run_env = dict(env)
        # As Cosmic Ray runs a test command.
        run_env["PYTHONDONTWRITEBYTECODE"] = "1"
        self._prepend_pythonpath(run_env, seen(plugin_dir))
        run_env.update(origin_environment(target, root, seen(record)))
        report = scratch / "origin.xml"
        # The command's own arguments as Cosmic Ray splits them, then the check's.
        argv = [*shlex.split(test_command), "-p", ORIGIN_PLUGIN,
                f"--junit-xml={seen(report) if container else report.as_posix()}",
                "-o", "junit_family=xunit1"]
        started = time.perf_counter()
        try:
            done = self._executor().run(argv, cwd=cwd, env=run_env, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            reraise_unresolved(error)
            raise OriginRefused(
                f"the unmutated suite did not finish within {timeout:g} s in the check "
                f"run before the first mutant") from error
        except (OSError, subprocess.SubprocessError) as error:
            # A command that cannot start records nothing, and the sweep
            # reports its own failure.
            reraise_unresolved(error)
            return
        finally:
            # Reported as MutationRun.origin_run_s, apart from the mutants.
            self._last_origin_run_s = time.perf_counter() - started
        found = read_origin(record)
        refusal = origin_refusal(found, collected=done.returncode in (0, 1))
        if refusal is None and done.returncode != 0:
            refusal = (f"the unmutated suite did not pass in the check run before the "
                       f"first mutant (pytest exit status {done.returncode}), so every "
                       f"mutant would be killed by its failure")
        if refusal is not None:
            if found is None or done.returncode != 0:
                # The run's own account of its end.
                output = ((done.stdout or "") + "\n" + (done.stderr or "")).splitlines()
                refusal = with_detail(refusal, tuple(line for line in output if line.strip()), 5)
            # A run in a prepared copy names the copy's paths: written as the
            # original project's, as a suite run's text is.
            from langadapter.preparation import original_text
            from langadapter.python._runs import failing_tests

            error = OriginRefused(original_text(self._project_adapter().context, refusal))
            if done.returncode != 0:
                error.stopping_tests = failing_tests(report, files)
            raise error

    def _cosmic_ray_metadata(self) -> CosmicRayMetadata:
        """The project's Cosmic Ray version and operator catalogue, and the
        project interpreter's version."""
        adapter = self._project_adapter()
        return _project_cosmic_ray_metadata(self._executor(), adapter.interpreter,
                                            flags=adapter._runtime_flags())

    def _excluded_operators(self, allow_list: List[str]) -> List[str]:
        return _compute_excluded_operators(
            allow_list, self._cosmic_ray_metadata()[1])

    # Public API

    def session(
        self,
        *,
        project_root: Path,
        workers: int = 1,
        distributor: Optional[str] = None,
    ) -> "CosmicRayEngineSession":
        """Open a session whose HTTP workers and source clones serve every
        ``.run(...)`` in its ``with`` block, so cloning, spawning and the
        ready-wait happen once. ``distributor`` is the session's own choice,
        kept as given (``CosmicRayEngineSession``)."""
        return CosmicRayEngineSession(
            self,
            project_root=project_root,
            distributor=distributor,
            workers=workers,
        )

    def full_operators(self) -> Optional[List[str]]:
        """Every operator Cosmic Ray registers (the full catalogue): a caller
        can run it once and derive an operator subset's score by filtering.
        None when the project interpreter cannot read it."""
        if not self._full_operator_cache_loaded:
            operators = self._cosmic_ray_metadata()[1]
            self._full_operator_cache = (
                sorted(set(operators)) if operators is not None else None
            )
            self._full_operator_cache_loaded = True
        if self._full_operator_cache is None:
            return None
        return list(self._full_operator_cache)

    def _stop(self, failure: str, reason: str) -> bool:
        """End the run on a failed step of the mutation tool, not truncated,
        with its failure kind and reason for the run (``_last_failure``,
        which ``MutationSession._finished`` reads). Worker cleanup is the
        session's ``__exit__``'s."""
        self._last_failure = (failure, reason)
        return False

    def _remember_requested_catalogue(
        self, operators: Optional[List[str]],
    ) -> None:
        """Bind the next captured matrix to the operator request just run."""
        full = self.full_operators()
        requested = full if operators is None else operators
        self._last_requested_operators = sorted(set(requested or []))
        if operators is None or (full is not None and set(operators) == set(full)):
            self._last_requested_catalogue = "full"
        else:
            self._last_requested_catalogue = "filtered"

    def _run_in_session(
        self,
        *,
        sess: "CosmicRayEngineSession",
        target: Path,
        test_dir: Path,
        per_mutant_timeout: int,
        wall_clock_timeout: int,
        operators: Optional[List[str]],
        test_files: Optional[Sequence[str]] = None,
    ) -> bool:
        """Run ``target``'s mutants with the workers of ``sess``
        (``CosmicRayEngineSession.run``). ``test_files`` names the suite's
        files relative to ``test_dir``, else the run tests the directory.
        Returns whether the wall clock ended the run. The session database
        holds the results."""
        # Cleared at the single entry point, so a run that aborts before the
        # verification pass leaves no stale ids for a later capture.
        self._last_unresolved_job_ids = set()
        self._last_verify_exec_failed = False
        self._last_verification = {}
        self._last_adjudication = {}
        self._last_requested_catalogue = "unknown"
        self._last_requested_operators = []
        self._last_origin_run_s = None
        self._last_failure = None
        self._last_exec_exit = None
        if sess.container_mode:
            return self._run_in_session_container(
                sess=sess,
                target=target,
                test_dir=test_dir,
                per_mutant_timeout=per_mutant_timeout,
                wall_clock_timeout=wall_clock_timeout,
                operators=operators,
                test_files=test_files,
            )
        project_root = sess.project_root
        env = sess.env
        sess._run_bases.add(_run_base(env))
        session_path, config_path = self._resolve_paths()
        sess._session_dir = getattr(self, "_default_session_dir", None)
        _log = self.config["log_fn"]
        if _log is not None:
            # Where the session ran and the open-file limits: the workers
            # inherit this process's, each mutant's wrapper sets its run's.
            try:
                import resource
                soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
                files = (f", open-file limit: workers {soft}, tests "
                         f"{wrapped_open_file_limit(soft, hard)} (hard {hard})")
            except (ImportError, OSError, ValueError):
                files = ""
            _log(f"[CosmicRayEngine] session SQLite at {session_path.parent} "
                 f"({getattr(self, '_session_origin', 'origin not recorded')}){files}",
                 level=1)

        self._remember_requested_catalogue(operators)

        # A .py path relative to project_root: Cosmic Ray loads it through
        # SourceFileLoader, so the target need not be a top-level package.
        module_path = self._target_to_module_path(target, project_root)

        # The distributor is fixed when the session is made.
        use_http = sess.use_http

        executable, cosmic_ray_exe = _resolve_project_tooling(self._project_adapter().interpreter)
        cosmic_ray = self._cosmic_ray_command(cosmic_ray_exe)
        # ``-x`` records one killer per mutant. ``_omit_fail_fast`` (attribution
        # ``all``) drops it to record every killing test.
        stop_at_first_failure = not self.config.get("_omit_fail_fast")
        _diag_tb = _mutation_diag_tb()
        # Every run writes a per-test journal, which the capture pairs with
        # the session's verdicts: the sweep starts from an empty directory
        # with the baseline module beside it, each later execution (a
        # verification batch, an adjudication) keeps the earlier journals in
        # a numbered directory, and the capture reads them in order, so a
        # verdict pairs with the mutant's last run. The baseline is the
        # module as Cosmic Ray reads it
        # (the text its diffs apply to), best effort. ``_timeout_journal``
        # turns the journal off. A suite whose setup files carry no
        # instrument journals through the adapter's plugin (``_journal_plugin``).
        baseline_module: Optional[str] = None
        if self.config.get("_timeout_journal", True):
            try:
                from langadapter.python._mutation import (
                    read_as_cosmic_ray,
                )
                baseline_module = read_as_cosmic_ray(
                    Path(project_root) / module_path)
            except (OSError, SyntaxError, ValueError) as exc:
                baseline_module = None
                if _log is not None:
                    _log(f"[CosmicRayEngine] timeout-evidence baseline "
                         f"snapshot failed: {exc}", level=2)
        serial_auxiliary = self.serial_auxiliary
        fail_on_unresolved = self.fail_on_unresolved

        # The local distributor runs in project_root and takes test_dir as
        # is. HTTP workers address it in their own clone.
        rel_test_dir: Optional[str] = None
        if use_http:
            try:
                rel_test_dir = test_dir.resolve().relative_to(
                    project_root.resolve()).as_posix()
                # Runs start in the clone's root, which the token names.
                command_dir = "__CR_WORKER_CWD__/" + rel_test_dir
            except ValueError:
                # Not under project_root: the absolute path.
                command_dir = test_dir.as_posix()
        else:
            command_dir = test_dir.as_posix()
        evidence_dir = session_path.parent / "evidence"
        plugin_dir: Optional[Path] = None
        if baseline_module is not None and self.config.get("_journal_plugin"):
            plugin_dir = session_path.parent / "journal_plugin"
            plugin_dir.mkdir(parents=True, exist_ok=True)
            (plugin_dir / f"{JOURNAL_PLUGIN}.py").write_text(
                journal_plugin_text(), encoding="utf-8")
        # Every run of a mutant prints its run record, which its verdict
        # and its killers come from.
        record_dir = session_path.parent / "record_plugin"
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / f"{RECORD_PLUGIN}.py").write_text(
            run_record_text(), encoding="utf-8")
        flags = self._project_adapter()._runtime_flags()
        wrapper_prefix = _write_per_run_wrapper(
            session_path.parent, executable,
            journal=(None if baseline_module is None
                     else (evidence_dir, module_path)),
            plugin_dir=plugin_dir, target=module_path,
            import_roots=self._target_roots(sess, module_path),
            owner=sess._run_owner, flags=flags, record_dir=record_dir)
        plugins = ["-p", RECORD_PLUGIN] + (
            ["-p", JOURNAL_PLUGIN] if plugin_dir is not None else [])
        config = self._session_config(
            sess, project_root, test_dir,
            execution_suite_root=command_dir,
        )

        def suite_command(targets: Sequence[str], *,
                          stop_at_first_failure: bool = stop_at_first_failure) -> str:
            # The normal run's command (``_run_definition``): the interpreter
            # flags, the suite's configuration, and the suite's directory as
            # rootdir and confcutdir. Unanchored, pytest walks up from the work
            # directory and can read a configuration or scan a host path whose
            # failures would count as kills. The per-file adjudication passes
            # a single test file as the target. Cosmic Ray splits the command
            # with ``shlex.split``, so each argument is quoted.
            return shlex.join([*wrapper_prefix, *pytest_command(
                executable, flags, targets, [*pytest_arguments(
                    config, command_dir, traceback=_diag_tb,
                    stop_at_first_failure=stop_at_first_failure,
                    disabled_plugins=DISABLED_PLUGINS), *plugins])])

        def fresh_journals(keep: bool = False) -> None:
            # The sweep starts empty. A verification batch or an
            # adjudication keeps the journals before it (``keep``).
            from langadapter.python._mutation import next_execution
            next_execution(evidence_dir, baseline_module, keep=keep)

        suite_files = tuple(test_files or ())
        suite_targets = ([f"{command_dir}/{name}" for name in suite_files]
                         if suite_files else [command_dir])

        test_command = suite_command(suite_targets)
        # An owned project root (the current prepared copy) loses, before each
        # run that starts in it, what earlier runs created there, as before a
        # normal run: the origin check, the local distributor's mutants and
        # the auxiliary runs. The session's run ends with the same cleanup.
        sess._root_inventory = self._root_inventory(project_root, session_path.parent)
        if sess._root_inventory is not None:
            _root, inventory, keep = sess._root_inventory
            env = {**env, f"{ENV_PREFIX}RUN_INVENTORY": str(inventory),
                   f"{ENV_PREFIX}RUN_KEEP": "\n".join(keep)}
        # A worker that stopped since the session started fails every request
        # sent to it, so the run stops before its first mutant.
        stopped = _stopped_workers(sess._worker_procs) if use_http else []
        if stopped:
            return self._stop(FAILED_WORKERS, (
                f"{len(stopped)} of {len(sess._worker_procs)} HTTP workers stopped "
                f"before this run ({stopped[0]})"))
        # The clones are made once per session, so each run copies its
        # current test files into them (a small, cheap copy).
        if use_http and sess._worker_clones and rel_test_dir is not None:
            failed = self._sync_test_dir_to_clones(
                test_dir=test_dir,
                rel_test_dir=rel_test_dir,
                clone_dirs=sess._worker_clones,
                log_fn=_log,
                test_files=suite_files,
            )
            if failed is not None:
                return self._stop(FAILED_WORKERS, failed)
        # The clones as this run's mutants start them: synced, nothing left.
        for clone in sess._worker_clones if use_http else ():
            write_run_inventory(clone, _inventory_of(clone))
        # Before any mutant, where the first mutants run (the first worker's
        # clone, as that worker's environment has it, else the project):
        # whether the unmutated suite passes there, which tests did not, and
        # where its tests import the target from. It stops at no first
        # failure, so it names every test that did not pass. Its journal, if
        # any, is cleared with the sweep's (fresh_journals).
        origin_root, origin_env = Path(project_root), env
        if use_http and sess._worker_clones:
            origin_root = sess._worker_clones[0]
            origin_env = self._clone_environment(sess.env, project_root, origin_root,
                                                 getattr(sess, "import_roots", ()))
        origin_scratch = session_path.parent / "origin_check"
        shutil.rmtree(origin_scratch, ignore_errors=True)
        self._origin_check(suite_command(suite_targets, stop_at_first_failure=False),
                           target=str(origin_root / module_path),
                           root=str(origin_root), cwd=str(origin_root), env=origin_env,
                           scratch=origin_scratch, timeout=max(per_mutant_timeout, 60),
                           files=suite_files)

        self._write_config_toml(
            config_path,
            module_path=module_path,
            test_command=test_command,
            per_mutant_timeout=per_mutant_timeout,
            worker_urls=sess.worker_urls if use_http else None,
            operators=operators,
        )

        # A cached catalogue replaces cosmic-ray init (often 30-300 s). A
        # fresh init is cached on success.
        if session_path.exists():
            try:
                session_path.unlink()
            except OSError:
                pass

        cache_path = self._resolve_cache_path(target, operators, module_path=module_path)
        cache_hit = False
        if cache_path is not None and cache_path.exists():
            try:
                shutil.copy2(str(cache_path), str(session_path))
                # A corrupt cache (an interrupted write, a bad copy) would
                # make the filter and exec fail with "file is not a
                # database": validate it, else fall back to a fresh init.
                _validate_session_db(session_path)
                cache_hit = True
                if _log is not None:
                    _log(
                        f"[CosmicRayEngine] catalogue cache hit "
                        f"({cache_path.name}), init skipped.",
                        level=1,
                    )
            except (OSError, sqlite3.DatabaseError) as exc:
                if _log is not None:
                    _log(
                        f"[CosmicRayEngine] catalogue cache restore "
                        f"failed ({type(exc).__name__}: "
                        f"{str(exc)[:200]}). The cache is invalidated and "
                        f"regenerated from scratch.",
                        level=1,
                    )
                # The bad entry goes, and whatever it left at session_path.
                with contextlib.suppress(OSError):
                    cache_path.unlink()
                with contextlib.suppress(OSError):
                    session_path.unlink()
                cache_hit = False

        def _run_cosmic_ray_init() -> Optional[str]:
            """Run ``cosmic-ray init`` into ``session_path``: None on
            success, else the reason it failed, logged. A launch whose
            command may still run raises."""
            init_timeout = int(self.config["cosmic_ray_init_timeout_s"])
            try:
                ir = _process_tree.run(
                    [*cosmic_ray, "init", str(config_path), str(session_path)],
                    text=True, encoding="utf-8", errors="replace", timeout=init_timeout,
                    env=env, cwd=str(project_root),
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                # A launch that left nothing running fails the init alone.
                if unresolved_command(exc) is not None:
                    raise
                if _log is not None:
                    _log(
                        f"[CosmicRayEngine] init failed ({type(exc).__name__}): "
                        f"{exc}",
                        level=1,
                    )
                return f"cosmic-ray init did not finish ({type(exc).__name__}: {exc})"
            if ir.returncode != 0:
                if _log is not None:
                    _log(
                        f"[CosmicRayEngine] cosmic-ray init exited "
                        f"{ir.returncode}. stderr tail:\n"
                        + ir.stderr[-2000:],
                        level=1,
                    )
                return f"cosmic-ray init exited {ir.returncode}"
            if _log is not None:
                _log(
                    f"[CosmicRayEngine] init succeeded, session={session_path.name}, "
                    f"workers={sess.workers}, per_mutant={per_mutant_timeout}s, "
                    f"operators={f'{len(operators)} ops' if operators else '(defaults)'}",
                    level=1,
                )
            # Persist the fresh catalogue to the cache before exec. The
            # cache holds the bare catalogue, not the results.
            if cache_path is not None:
                self._persist_cache(session_path, cache_path, log_fn=_log)
                # Opportunistic GC after a fresh write.
                self._gc_cache_dir(cache_path.parent, log_fn=_log)
            return None

        if not cache_hit:
            failed = _run_cosmic_ray_init()
            if failed is not None:
                # A catalogue a failed init left behind is no run's.
                with contextlib.suppress(OSError):
                    session_path.unlink()
                return self._stop(FAILED_INITIALIZATION,
                              f"the mutation tool's initialization failed: {failed}")

        # A restricted operator set, requested or configured, gave the TOML an
        # exclude block: the filter marks those mutants SKIPPED, exec passes
        # over them and the capture leaves them out of every count.
        if operators:
            self._apply_operator_filter(executable, config_path, session_path, log_fn=_log)

        # ``cosmic_ray_verbose`` logs Cosmic Ray at INFO, which confirms the
        # worker count on small targets where timing alone is ambiguous.
        verbose = str(self.config.get("cosmic_ray_verbose")).lower() in (
            "true", "1", "yes",
        )
        if use_http:
            # The execs send each job with the fleet's secret, without which
            # its workers refuse it.
            cosmic_ray = http_exec_command(self._project_adapter().interpreter, ENV_PREFIX)
            env = {**env, ENV_PREFIX + WORKER_SECRET: sess.worker_secret}
        exec_cmd = list(cosmic_ray)
        if verbose:
            exec_cmd += ["--verbosity", "INFO"]
        exec_cmd += ["exec", str(config_path), str(session_path)]
        fresh_journals()

        # Serial auxiliary runs also mutate the original project, even when the
        # main distributor uses clones. Keep a durable copy before any writer.
        local_target = (Path(project_root) / module_path
                        if not use_http or serial_auxiliary else None)
        if local_target is not None:
            from langadapter.python._mutation import SourceBackup

            sess._source_backup = SourceBackup(local_target, project_root=Path(project_root))

        def _restore_target() -> None:
            if sess._source_backup is None:
                return
            try:
                if sess._worker_procs:
                    if not sess._source_backup.matches():
                        raise RuntimeError("Mutation target restoration requires stopped worker ownership")
                    return
                sess._source_backup.restore()
            except BaseException:
                sess._source_restore_failed = True
                raise

        def _auxiliary_exec(args, timeout):
            primary = None
            disposed = True
            try:
                return _process_tree.run(
                    args, text=True, encoding="utf-8", errors="replace",
                    env=env, cwd=str(project_root), timeout=timeout,
                )
            except BaseException as error:
                primary = error
                command = getattr(error, "_langadapter_command", None)
                if command is None:
                    command = getattr(error, "_langadapter_linux_command", None)
                if command is not None:
                    disposed = getattr(command, "_langadapter_command_disposed", False)
                    if not disposed:
                        sess._target_commands.append(command)
                raise
            finally:
                if disposed:
                    try:
                        _restore_target()
                    except BaseException as restore_error:
                        if primary is None:
                            raise
                        _process_tree._cleanup_note(primary, restore_error)

        # The poller logs every ``cosmic_ray_progress_interval_s`` (off at 0,
        # the default) and runs every second for a progress callback.
        try:
            progress_interval = int(
                self.config["cosmic_ray_progress_interval_s"] or 0
            )
        except (TypeError, ValueError):
            progress_interval = 0
        progress_stop = threading.Event()
        progress_thread: Optional[threading.Thread] = None
        progress = self.config.get("_progress")
        if progress is not None or (progress_interval > 0 and _log is not None):
            progress_thread = threading.Thread(
                target=_cr_verify._poll_session_progress,
                kwargs={
                    "session_path": session_path,
                    "stop_event": progress_stop,
                    "interval_seconds": progress_interval,
                    "log_fn": _log,
                    "start_perf": time.perf_counter(),
                    "progress": progress,
                },
                name="cr-progress-poll",
                daemon=True,
            )
            progress_thread.start()

        truncated = False
        # A wait loop, so ``wall_clock_timeout`` can stop exec mid-run. It
        # reads a running exec's pipes while it waits, so an exec that writes
        # more than a pipe holds cannot stall. An ended one is drained below.
        try:
            proc = _process_tree.start(
                exec_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                cwd=str(project_root),
            )
        except OSError as exc:
            progress_stop.set()
            if progress_thread is not None:
                progress_thread.join(timeout=2.0)
            # A launch that left nothing running (a missing executable, a
            # refused fork or exec) fails this run alone. Only an owner whose
            # disposal is uncertain stays with the session.
            command = unresolved_command(exc)
            if command is not None:
                sess._target_commands.append(command)
                raise
            if _log is not None:
                _log(
                    f"[CosmicRayEngine] cosmic-ray executable not found: {exc}"
                    if isinstance(exc, FileNotFoundError) else
                    f"[CosmicRayEngine] cosmic-ray exec did not start "
                    f"({type(exc).__name__}): {exc}",
                    level=1,
                )
            return self._stop(FAILED_SESSION, f"the mutation tool's exec did not start "
                                          f"({type(exc).__name__}: {exc})")
        except BaseException as error:
            command = unresolved_command(error)
            if command is not None:
                sess._target_commands.append(command)
            progress_stop.set()
            if progress_thread is not None:
                progress_thread.join(timeout=2.0)
            raise

        terminated_reason: Optional[str] = None
        primary = None
        output = None
        sess._target_commands.append(proc)
        try:
            start_wall = time.monotonic()
            try:
                while True:
                    # Ended (a descendant may keep a pipe open): the drain
                    # below reads the rest within its bound.
                    if proc.poll() is not None:
                        break
                    output = _process_tree.collect_output(proc, 1.0)
                    if output is not None:
                        break
                    elapsed = time.monotonic() - start_wall
                    if elapsed > wall_clock_timeout:
                        truncated = True
                        terminated_reason = "wall_clock_timeout"
                        break
            except BaseException:
                with contextlib.suppress(Exception):
                    _process_tree.stop(proc, grace=5)
                raise

            if terminated_reason is not None:
                _process_tree.stop(proc)
                stdout_data, stderr_data = _process_tree.drain_output(proc)
                if _log is not None:
                    _log(
                        f"[CosmicRayEngine] wall_clock_timeout={wall_clock_timeout}s "
                        "exceeded, partial results in session.",
                        level=1,
                    )
                exec_result = subprocess.CompletedProcess(
                    args=exec_cmd,
                    returncode=proc.returncode if proc.returncode is not None else -1,
                    stdout=stdout_data,
                    stderr=stderr_data,
                )
            else:
                stdout_data, stderr_data = (output if output is not None
                                            else _process_tree.drain_output(proc))
                exec_result = subprocess.CompletedProcess(
                    args=exec_cmd,
                    returncode=proc.returncode,
                    stdout=stdout_data,
                    stderr=stderr_data,
                )
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                if _process_tree.finish(proc, primary=primary):
                    sess._target_commands.remove(proc)
                    try:
                        _restore_target()
                    except BaseException as restore_error:
                        if primary is None:
                            raise
                        _process_tree._cleanup_note(primary, restore_error)
                elif primary is None:
                    error = RuntimeError("Mutation command disposal is unconfirmed")
                    error._langadapter_command = proc
                    raise error
            finally:
                progress_stop.set()
                if progress_thread is not None:
                    progress_thread.join(timeout=2.0)

        if verbose and _log is not None:
            tail = (exec_result.stderr or exec_result.stdout or "")[-3000:]
            _log(
                f"[CosmicRayEngine] verbose exec output (tail):\n{tail}",
                level=1,
            )

        # Exec raised. Its partial results are still read, and the run reads
        # as failed when it left mutants untested (``MutationSession._finished``).
        self._last_exec_exit = exec_result.returncode
        if exec_result.returncode != 0 and not truncated and _log is not None:
            _log(
                f"[CosmicRayEngine] cosmic-ray exec exited "
                f"{exec_result.returncode}. stderr tail:\n"
                + exec_result.stderr[-2000:],
                level=2,
            )

        # Unattributed kills are verified with the same test command, fleet
        # and timeout before anything reads the session.
        def _exec_verify_local(vdb: Path, n: int) -> bool:
            fresh_journals(keep=True)
            verify_config = config_path
            if serial_auxiliary:
                verify_config = config_path.with_name(
                    "cosmic-ray-verify.toml"
                )
                self._write_config_toml(
                    verify_config,
                    module_path=module_path,
                    test_command=suite_command(suite_targets),
                    per_mutant_timeout=per_mutant_timeout,
                    worker_urls=None,
                    operators=operators,
                )
            try:
                r = _auxiliary_exec(
                    [*cosmic_ray, "exec", str(verify_config), str(vdb)],
                    timeout=n * per_mutant_timeout + 300,
                )
                return r.returncode == 0
            except (subprocess.TimeoutExpired, OSError):
                if sess._target_commands or sess._source_restore_failed:
                    raise
                return False

        self._verify_unattributed_kills(
            session_path=session_path, sess=sess,
            exec_verify=_exec_verify_local,
            suite=_suite_fingerprint(test_dir, suite_command(suite_targets)),
            log_fn=_log,
        )
        if fail_on_unresolved:
            self._raise_on_instrument_fault(
                session_path, log_fn=_log, reject_any_unresolved=True,
            )

        if self._last_unresolved_job_ids and self._adjudication_enabled():
            def _exec_adjudicate_local(vdb: Path, n: int, tf: Path) -> bool:
                fresh_journals(keep=True)
                acfg = config_path.with_name("cosmic-ray-adjudicate.toml")
                self._write_config_toml(
                    acfg,
                    module_path=module_path,
                    test_command=suite_command(
                        [f"{command_dir}/{tf.relative_to(test_dir).as_posix()}"],
                    ),
                    per_mutant_timeout=per_mutant_timeout,
                    worker_urls=(
                        None
                        if serial_auxiliary
                        else (sess.worker_urls if use_http else None)
                    ),
                    operators=operators,
                )
                try:
                    r = _auxiliary_exec(
                        [*cosmic_ray, "exec", str(acfg), str(vdb)],
                        timeout=n * per_mutant_timeout + 300,
                    )
                    return r.returncode == 0
                except (subprocess.TimeoutExpired, OSError):
                    if sess._target_commands or sess._source_restore_failed:
                        raise
                    return False

            self._adjudicate_unresolved_per_file(
                session_path=session_path,
                exec_adjudicate=_exec_adjudicate_local,
                test_files=(
                    sorted(Path(test_dir) / name for name in suite_files)
                    if suite_files
                    else sorted(Path(test_dir).glob("test_*.py"))),
                log_fn=_log, sess=sess,
            )

        self._raise_on_instrument_fault(session_path, log_fn=_log)
        return truncated

    def _container_session_path(self) -> Optional[Path]:
        """The session SQLite in container mode, which the launcher writes
        into the bind-mounted work directory and the host reads back: the one path
        for the run and the session's capture. None without a work directory or
        outside container mode (a local run keeps its session in the
        engine's own directory, ``_resolve_paths``)."""
        work_dir = self._project_adapter()._work_dir()
        if not work_dir or self._exec_mode() != "container":
            return None
        return Path(work_dir) / "cr" / "cr-session.sqlite"

    def _run_in_session_container(
        self,
        *,
        sess: "CosmicRayEngineSession",
        target: Path,
        test_dir: Path,
        per_mutant_timeout: int,
        wall_clock_timeout: int,
        operators: Optional[List[str]],
        test_files: Optional[Sequence[str]] = None,
    ) -> bool:
        """The container counterpart of ``_run_in_session``: the whole worker
        fleet (init, operator filter, clones, exec, teardown) runs in the
        container through ``_cr_session_runner`` in one blocking executor
        call. The host writes the config and the spec, restores a cached
        catalogue, runs the origin check, starts the verification and
        adjudication invocations and reads the session. Spec paths are
        rebased onto the container's view of the shared work directory (``_cz``).
        Container paths are POSIX (``PurePosixPath``) on any host."""
        import importlib.resources

        _log = self.config["log_fn"]
        project_root = sess.project_root  # the project root inside the container
        env = sess.env

        session_path = self._container_session_path()
        if session_path is None:
            raise RuntimeError(self._missing_work_dir_message)
        cr_dir = session_path.parent
        cr_dir.mkdir(parents=True, exist_ok=True)
        config_path = cr_dir / "cosmic-ray.toml"
        # Clones default beside the session in the work directory, whose (network)
        # filesystem many concurrent readers strain: ``cosmic_ray_clone_dir``
        # moves them (to /dev/shm, say). The session claims its own
        # subdirectory there, so fleets sharing a base never touch each
        # other's clones. No sweep removes it: a killed run leaves it behind.
        _clone_override = str(
            self.config["cosmic_ray_clone_dir"] or "").strip()
        if _clone_override and _clone_override.lower() != "auto":
            Path(_clone_override).mkdir(parents=True, exist_ok=True)
            clone_base = Path(tempfile.mkdtemp(
                prefix="cr_session_", dir=_clone_override))
        else:
            clone_base = cr_dir / "cr_worker_clones"
        spec_path = cr_dir / "spec.json"
        runner_path = cr_dir / "_cr_session_runner.py"
        # The per-run wrapper (``_PER_RUN_WRAPPER_SH``) of every test command.
        # Its raised open-file limit keeps a heavy scientific import stack from
        # hitting EMFILE in collection, a false kill. A file keeps quotes out
        # of the test command (TOML-literal-safe). Written once the target is
        # known (``_target_roots``). It names the target, as the host's
        # wrapper does: the launcher names it only for its own runs, and the
        # origin check, which runs outside it, needs the instrument's
        # per-test restore too.
        fdlimit_path = cr_dir / "_cr_fdlimit_run.sh"
        serial_auxiliary = self.serial_auxiliary
        fail_on_unresolved = self.fail_on_unresolved
        snapshot_path = Path(str(session_path) + ".catalogue")
        status_path = Path(str(session_path) + ".runner_status.json")

        self._remember_requested_catalogue(operators)

        # The launcher reads spec paths in the container, beyond the
        # executor's argument rebasing: ``_cz`` rebases a host work directory path
        # (a ``C:`` path on Windows) onto the container's, as the executor does.
        project = self._project_adapter()
        host_work_dir, container_work_dir = project._work_dir(), project._container_work_dir()

        def _cz(p) -> str:
            return rebase_path(p, host_work_dir, container_work_dir)

        # Container paths: ``as_posix`` turns a Windows host's backslashes into
        # forward slashes.
        pr_posix = PurePosixPath(Path(project_root).as_posix())
        td_posix = PurePosixPath(Path(test_dir).as_posix())
        target_posix = PurePosixPath(Path(target).as_posix())
        try:
            module_path = target_posix.relative_to(pr_posix).as_posix()
        except ValueError:
            module_path = target_posix.as_posix()
        record_dir = cr_dir / "record_plugin"
        record_dir.mkdir(parents=True, exist_ok=True)
        (record_dir / f"{RECORD_PLUGIN}.py").write_text(
            run_record_text(), encoding="utf-8")
        fdlimit_path.write_text(_per_run_wrapper_text(
            True, target=module_path,
            import_roots=self._target_roots(sess, module_path),
            record_dir=_cz(record_dir)),
            encoding="utf-8")
        container_python = self._project_adapter().interpreter
        bindir = PurePosixPath(container_python).parent
        cosmic_ray_exe = str(bindir / "cosmic-ray")

        try:
            rel_test_dir = td_posix.relative_to(pr_posix).as_posix()
            sync_tests = True
            # Runs start in the clone's root, which __CR_WORKER_CWD__ names in
            # the synced suite's paths. The wrapper rewrites it.
            test_dir_for_command = "__CR_WORKER_CWD__/" + rel_test_dir
        except ValueError:
            rel_test_dir = None
            sync_tests = False
            test_dir_for_command = _cz(test_dir)
        # ``-x`` changes only which killers are recorded, as on the host.
        stop_at_first_failure = not self.config.get("_omit_fail_fast")
        _diag_tb = _mutation_diag_tb()
        config = self._session_config(
            sess, project_root, test_dir,
            execution_suite_root=test_dir_for_command,
        )
        # A request may turn the journal off. A suite whose setup files carry
        # no instrument journals through the adapter's plugin, which the
        # launcher puts on every run's import path.
        container_journal = bool(self.config.get("_timeout_journal", True))
        plugin_dir = None
        if container_journal and self.config.get("_journal_plugin"):
            plugin_dir = cr_dir / "journal_plugin"
            plugin_dir.mkdir(parents=True, exist_ok=True)
            (plugin_dir / f"{JOURNAL_PLUGIN}.py").write_text(
                journal_plugin_text(), encoding="utf-8")
        plugins = ["-p", RECORD_PLUGIN] + (
            ["-p", JOURNAL_PLUGIN] if plugin_dir is not None else [])
        flags = self._project_adapter()._runtime_flags()

        def _pool_test_command(
            targets: Sequence[str], *,
            stop_at_first_failure: bool = stop_at_first_failure,
        ) -> str:
            # The normal run's command, quoted, as on the host. The per-file
            # adjudication passes a single test file as the target.
            return shlex.join(["sh", _cz(fdlimit_path), *pytest_command(
                container_python, flags, targets, [*pytest_arguments(
                    config, test_dir_for_command, traceback=_diag_tb,
                    stop_at_first_failure=stop_at_first_failure,
                    disabled_plugins=DISABLED_PLUGINS), *plugins])])

        pytest_targets = (
            [f"{test_dir_for_command}/{name}" for name in test_files]
            if test_files else [test_dir_for_command])
        test_command = _pool_test_command(pytest_targets)
        # Before any mutant: where the unmutated run imports the target from,
        # with the journal plugin's directory on the path, as the launcher puts it.
        origin_scratch = cr_dir / "origin_check"
        shutil.rmtree(origin_scratch, ignore_errors=True)
        origin_env = dict(env)
        if plugin_dir is not None:
            self._prepend_pythonpath(origin_env, _cz(plugin_dir))
        self._origin_check(_pool_test_command(pytest_targets, stop_at_first_failure=False),
                           target=(pr_posix / module_path).as_posix(),
                           root=pr_posix.as_posix(), cwd=pr_posix.as_posix(),
                           env=origin_env, scratch=origin_scratch,
                           timeout=max(per_mutant_timeout, 60),
                           files=tuple(test_files or ()))

        # A prior call's files in this work directory go, so a cache miss starts clean.
        for p in (session_path, status_path, snapshot_path):
            with contextlib.suppress(OSError):
                p.unlink()

        self._write_config_toml(
            config_path,
            module_path=module_path,
            test_command=test_command,
            per_mutant_timeout=per_mutant_timeout,
            operators=operators,
            # HTTP: the launcher appends the block after spawning the fleet.
            # Local: the local distributor block, written here.
            omit_distributor=sess.use_http,
        )

        executor = self._executor()

        # A cache hit restores the catalogue so the launcher skips init. The
        # key hashes the target's bytes, read in the container only with the
        # cache on. Unreadable bytes are a cache miss.
        cache_enabled = bool(
            str(self.config["cosmic_ray_catalogue_cache_dir"]).strip())
        source_bytes = (
            read_container_bytes(
                executor, container_python, target_posix.as_posix(), flags=flags)
            if cache_enabled else None
        )
        cache_path = self._resolve_cache_path(
            target, operators, source_bytes=source_bytes, module_path=module_path)
        run_init = True
        if cache_path is not None and cache_path.exists():
            try:
                shutil.copy2(str(cache_path), str(session_path))
                _validate_session_db(session_path)
                run_init = False
                if _log is not None:
                    _log(
                        f"[CosmicRayEngine] catalogue cache hit "
                        f"({cache_path.name}), the launcher skips init.",
                        level=1,
                    )
            except (OSError, sqlite3.DatabaseError):
                with contextlib.suppress(OSError):
                    session_path.unlink()
                run_init = True

        spec = {
            "cosmic_ray_command": self._cosmic_ray_command(cosmic_ray_exe, container_python),
            "config_path": _cz(config_path),
            "session_path": _cz(session_path),
            "catalogue_snapshot_path": _cz(snapshot_path),
            "project_root": pr_posix.as_posix(),
            "clone_base": _cz(clone_base),
            # Local disk or RAM, never the shared work directory (SQLite on a network
            # filesystem fails with I/O errors). Empty: the launcher picks
            # /dev/shm, then the system temporary directory.
            "local_session_dir": str(
                self.config.get("cosmic_ray_local_session_dir") or "").strip(),
            "test_dir": _cz(test_dir),
            "rel_test_dir": rel_test_dir,
            "sync_tests_to_clones": sync_tests,
            "workers": sess.workers,
            "use_http": sess.use_http,
            "wall_clock_timeout": wall_clock_timeout,
            "ready_timeout": int(self.config["cosmic_ray_worker_ready_timeout_s"]),
            "init_timeout": int(self.config["cosmic_ray_init_timeout_s"]),
            "run_init": run_init,
            "run_filter": bool(operators),
            # Seconds between the launcher's progress lines (0 disables).
            "progress_interval": int(
                self.config["cosmic_ray_progress_interval_s"] or 0),
            "mutation_source_root_env":
                f"{ENV_PREFIX}MUTATION_SOURCE_ROOT",
            # The prefix of the timeout-evidence variables the launcher sets.
            "env_prefix": ENV_PREFIX,
            # Each per-mutant pytest streams a per-test journal keyed by the
            # mutated module's hash, so a clock-killed run names its test. The
            # launcher snapshots the baseline module beside the journals, for
            # the capture to pair a job's diff with its journal block.
            "module_path": module_path,
            "timeout_journal": container_journal,
            "journal_plugin_dir": (_cz(plugin_dir) if plugin_dir is not None
                                   else None),
            # The clone's directories that hold the project's source, ahead of
            # the clone itself (an editable src-layout install).
            "import_roots": list(getattr(sess, "import_roots", ())),
            "clean_root": self._owns_root(project_root),
            "env_extra": {
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                **RUN_VARIABLES,
            },
            # The launcher's log, teed to the work directory, traces a run that hangs
            # or dies inside the blocking call while it runs.
            "runner_log_path": _cz(cr_dir / "cr_runner.log"),
        }
        spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")

        runner_src = (
            importlib.resources.files("langadapter.python")
            .joinpath("_cr_session_runner.py").read_text(encoding="utf-8")
        )
        runner_path.write_text(runner_src, encoding="utf-8")

        if _log is not None:
            _log(
                f"[CosmicRayEngine] container launcher: "
                f"distributor={'http' if sess.use_http else 'local'}, "
                f"workers={sess.workers if sess.use_http else 1}, "
                f"per_mutant={per_mutant_timeout}s, "
                f"operators={f'{len(operators)} ops' if operators else '(defaults)'}, "
                f"run_init={run_init}",
                level=1,
            )

        exec_timeout = wall_clock_timeout + int(
            self.config["cosmic_ray_init_timeout_s"]) + 600
        restore = [container_python, runner_path.as_posix(), "--restore", spec_path.as_posix()]
        try:
            result = executor.run(
                [container_python, runner_path.as_posix(), spec_path.as_posix()],
                cwd=pr_posix.as_posix(),
                env=env,
                timeout=exec_timeout,
            )
        except BaseException:
            # The error stays the run's. A copy that cannot be restored stays.
            with contextlib.suppress(Exception):
                _restore_container_target(executor, restore, session_path, pr_posix, _log)
            raise
        _restore_container_target(executor, restore, session_path, pr_posix, _log)
        if _log is not None and result.stdout:
            _log(
                "[CosmicRayEngine] launcher output:\n" + result.stdout[-4000:],
                level=2,
            )
        if result.returncode != 0 and _log is not None:
            _log(
                f"[CosmicRayEngine] launcher exited {result.returncode}, "
                f"stderr tail:\n{(result.stderr or '')[-2000:]}",
                level=1,
            )

        st = _launcher_status(status_path, "sweep", _log)
        truncated = bool(st.get("truncated", False))
        if st.get("session_refused"):
            raise SessionFolderRefused(st["session_refused"])
        if st.get("filter_failed"):
            message = FILTER_REFUSAL.format(failure=st["filter_failed"])
            if _log is not None:
                _log(f"[CosmicRayEngine] {message}", level=1)
            raise RuntimeError(message)
        self._last_exec_exit = st.get("exec_returncode")
        stage = st.get("failed_stage")
        if stage is not None or result.returncode != 0:
            # The launcher names the step that stopped it. An exit without
            # one is the launcher's own failure.
            return self._stop(
                _STAGE_FAILURES.get(stage, FAILED_SESSION),
                f"the mutation tool's {stage or 'launcher'} failed in the container "
                f"(launcher exit {result.returncode})")

        if not session_path.exists():
            if _log is not None:
                _log(
                    "[CosmicRayEngine] container launcher produced no session.",
                    level=1,
                )
            return truncated

        # Kills are verified, and unresolved ones adjudicated, through the same
        # launcher: a bounded second call over a suspects-only session (no
        # init, the filter already applied). The launcher appends its http
        # block to the config it gets, so each attempt writes a fresh one
        # (same module and timeout, the test command names ``targets``).
        def _auxiliary_launch(kind: str, vdb: Path, n: int, targets: Sequence[str]) -> bool:
            name = "verify" if kind == "verification" else "adjudicate"
            workers, use_http = (
                (1, False)
                if serial_auxiliary
                else _auxiliary_worker_plan(
                    sess.workers, sess.use_http, n,
                )
            )
            config_path = cr_dir / f"cosmic-ray-{name}.toml"
            self._write_config_toml(
                config_path,
                module_path=module_path,
                test_command=_pool_test_command(targets),
                per_mutant_timeout=per_mutant_timeout,
                operators=operators,
                omit_distributor=use_http,
            )
            aspec = dict(spec)
            aspec["config_path"] = _cz(config_path)
            aspec["session_path"] = _cz(vdb)
            aspec["catalogue_snapshot_path"] = _cz(
                Path(str(vdb) + ".catalogue"))
            aspec["run_init"] = False
            aspec["run_filter"] = False
            # The sweep's and earlier batches' journals stay.
            aspec["keep_evidence"] = True
            aspec["workers"] = workers
            aspec["use_http"] = use_http
            aspec["wall_clock_timeout"] = n * per_mutant_timeout + 300
            aspec["progress_interval"] = 0
            aspec["runner_log_path"] = _cz(cr_dir / f"cr_{name}.log")
            aspec_path = cr_dir / f"spec_{name}.json"
            aspec_path.write_text(json.dumps(aspec, indent=2),
                                  encoding="utf-8")
            r = executor.run(
                [container_python, runner_path.as_posix(),
                 aspec_path.as_posix()],
                cwd=pr_posix.as_posix(),
                env=env,
                timeout=(n * per_mutant_timeout
                         + int(self.config["cosmic_ray_worker_ready_timeout_s"])
                         + 600),
            )
            _launcher_status(Path(str(vdb) + ".runner_status.json"),
                             kind, _log)
            if kind == "verification" and r.returncode != 0 and _log is not None:
                _log(
                    f"[kill-verify] launcher exited {r.returncode}, "
                    f"stderr tail:\n{(r.stderr or '')[-1000:]}", 2)
            return r.returncode == 0

        def _exec_verify_container(vdb: Path, n: int) -> bool:
            return _auxiliary_launch("verification", vdb, n, pytest_targets)

        self._verify_unattributed_kills(
            session_path=session_path, sess=sess,
            exec_verify=_exec_verify_container,
            suite=_suite_fingerprint(Path(test_dir), test_command),
            log_fn=_log,
        )
        if fail_on_unresolved:
            self._raise_on_instrument_fault(
                session_path, log_fn=_log, reject_any_unresolved=True,
            )

        if self._last_unresolved_job_ids and self._adjudication_enabled():
            def _exec_adjudicate_container(vdb: Path, n: int,
                                           tf: Path) -> bool:
                return _auxiliary_launch(
                    "adjudication", vdb, n,
                    [f"{test_dir_for_command}/{tf.relative_to(test_dir).as_posix()}"])

            self._adjudicate_unresolved_per_file(
                session_path=session_path,
                exec_adjudicate=_exec_adjudicate_container,
                test_files=(
                    sorted(Path(test_dir) / name for name in test_files)
                    if test_files
                    else sorted(Path(test_dir).glob("test_*.py"))),
                log_fn=_log, sess=sess,
            )

        self._raise_on_instrument_fault(session_path, log_fn=_log)
        # The launcher's catalogue snapshot of a fresh init goes to the cache.
        if run_init and cache_path is not None and snapshot_path.exists():
            self._persist_cache(snapshot_path, cache_path, log_fn=_log)
            self._gc_cache_dir(cache_path.parent, log_fn=_log)
        return truncated

    # Unattributed-kill verification


    def _native_record(self, worker_outcome: Any, output: Any,
                       test_outcome: Any) -> NativeRecord:
        """A per-mutant record as evidence: the outcomes, the killers its
        output names, whether pytest reported with tests collected, and the
        bounded output when the run keeps outputs."""
        text = output or ""
        return NativeRecord(
            test_outcome=test_outcome, worker_outcome=worker_outcome,
            killers=_killers(text) if text else (),
            reported=(_pytest_reported(text)
                      and not _zero_collected(text)),
            output=(_bounded_output(_without_record(text))
                    if text and self.config["cosmic_ray_keep_output"]
                    else None))

    def _adjudication_enabled(self) -> bool:
        """Whether unresolved kills are adjudicated per test file: unless
        ``cosmic_ray_per_file_adjudication`` is ``false``."""
        return str(self.config["cosmic_ray_per_file_adjudication"]).strip().lower() != "false"

    # Helpers

    def _build_env(self, project_root: Path) -> Dict[str, str]:
        """The environment of the session's per-mutant runs: the captured
        launch environment or the calling process's, with the context's
        ``environment`` added (``run_launch_environment``), then the project
        root first on the import path, so a context ``PYTHONPATH`` keeps the
        root first. In a container the root is the whole import path (the
        container executor forwards ``PYTHONPATH``, whose host value names
        host directories). The variables every test run has
        (``_run_definition.RUN_VARIABLES``) are set, as in the normal run."""
        adapter = self._project_adapter()
        out = dict(run_launch_environment(adapter.context.environment))
        self._prepend_pythonpath(out, str(project_root))
        adapter._prepend_import_root(out, project_root)
        # UTF-8 in the per-mutant runs: under Windows' cp1252 default the
        # reader thread crashes on non-ASCII test output.
        out.setdefault("PYTHONIOENCODING", "utf-8")
        out.setdefault("PYTHONUTF8", "1")
        out = run_environment(out)
        if self._exec_mode() == "container":
            out["PYTHONPATH"] = Path(project_root).as_posix()
            adapter._prepend_import_root(out, project_root)
        return out

    def _resolve_paths(self) -> tuple[Path, Path]:
        """The per-run session SQLite and config TOML (the catalogue cache is
        the lasting store, elsewhere). ``cosmic_ray_session_dir``
        (``CosmicRaySettings.session_dir``) sets their directory. Prefer local
        disk, since on a network filesystem the session SQLite is slow and
        lock-contended. Without it they go in the engine's own temporary
        directory below ``cosmic_ray_local_session_dir`` when set, else in the
        system temporary directory (``_local_session_base``, which honors
        ``TMPDIR``): on a network filesystem the HTTP master's per-mutant
        commits eventually fail with ``sqlite3.OperationalError: disk I/O
        error`` and abort exec mid-run. A configured directory that cannot be
        used refuses the run, never falling back."""
        session_dir = self.config.get("cosmic_ray_session_dir")
        if session_dir:
            base = Path(session_dir)
            self._session_origin = "configured cosmic_ray_session_dir"
        else:
            # Kept on the engine, so one session's calls share the directory.
            if not getattr(self, "_default_session_dir", None):
                parent, self._session_origin = _local_session_base(
                    self.config.get("cosmic_ray_local_session_dir"))
                self._default_session_dir = Path(tempfile.mkdtemp(
                    prefix="cr_session_", dir=parent,
                ))
            base = self._default_session_dir
        base.mkdir(parents=True, exist_ok=True)
        return base / "cr-session.sqlite", base / "cosmic-ray.toml"

    # The http distributor and per-worker clones

    @staticmethod
    def _alloc_free_port_sockets(n: int, *, owner) -> List[socket.socket]:
        """Bind ``n`` free localhost TCP ports, recorded on ``owner``, and
        return the sockets. The caller closes each right before spawning the
        worker that takes its port, so the kernel cannot reissue it in
        between (a window of microseconds, not the tens to hundreds of
        milliseconds of closing all first)."""
        sockets: List[socket.socket] = owner._worker_sockets
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(s)
            s.bind(("127.0.0.1", 0))
        return sockets

    @staticmethod
    def _sync_test_dir_to_clones(
        *,
        test_dir: Path,
        rel_test_dir: str,
        clone_dirs: List[Path],
        log_fn=None,
        test_files: Sequence[Any] = (),
    ) -> Optional[str]:
        """Copy ``test_dir`` from the live work directory into each worker clone
        just before exec: clones are made once per session, so changed test
        files would be stale there. Returns the reason the first copy that
        failed gives, else None: that clone would measure the previous
        suite. The whole directory is copied (cheap,
        idempotent, and it picks up renamed or added modules). A symlink
        would not do: the conftest computes ``_PROJECT_ROOT`` from
        ``__file__``, which would resolve to the unmutated source.

        A test directory that is the project root (``rel_test_dir`` ``.``)
        is never removed from a clone, which would remove the worker's
        working directory: the suite's files and the root's files (its
        conftest and configuration) are copied over instead."""
        # ``rel_test_dir``: test_dir relative to project_root, POSIX style.
        rel = Path(rel_test_dir)
        sync_start = time.perf_counter()
        sources = []
        if rel == Path("."):
            sources = [p for p in test_dir.iterdir() if p.is_file()]
            for name in test_files:
                path = Path(name)
                path = path if path.is_absolute() else test_dir / path
                if path.is_file():
                    sources.append(path)
        for clone_dir in clone_dirs:
            try:
                if rel == Path("."):
                    for path in sources:
                        dst = clone_dir / path.relative_to(test_dir)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, dst)
                else:
                    dst = clone_dir / rel
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(test_dir, dst, symlinks=False)
            except (OSError, ValueError) as exc:
                # That clone would measure the previous suite.
                return (f"the suite could not be copied into the worker clone "
                        f"{clone_dir} ({type(exc).__name__}: {exc})")
        if log_fn is not None:
            log_fn(f"[CosmicRayEngine] re-synced the suite to {len(clone_dirs)} worker "
                   f"clone(s) in {time.perf_counter() - sync_start:.2f}s", level=2)
        return None

    # The host fleet waits for and clones its workers with the container
    # runner's helpers (each a static method, so a test can replace it).
    _wait_for_worker = staticmethod(_wait_for_worker)
    _clone_one = staticmethod(_clone_one)

    def _start_http_workers(
        self, n: int, *, project_root: Path, env: Dict[str, str],
        log_fn=None, owner,
    ) -> Tuple[List[subprocess.Popen], List[str], List[Path]]:
        """Start ``n`` HTTP workers, each recorded on ``owner`` before the
        step that can fail, and close the owner when starting fails."""
        try:
            return self._spawn_http_workers(n, project_root=project_root, env=env,
                                           log_fn=log_fn, owner=owner)
        except BaseException as exc:
            failure = None
            try:
                owner._close()
            except Exception as cleanup:
                if isinstance(exc, Exception):
                    raise MutationCleanupError(
                        owner, f"worker startup cleanup failed: {cleanup}") from exc
                failure = cleanup
            if failure is not None:
                # An interrupt stays the error. The owner keeps the workers.
                exc._langadapter_mutation_session = owner
                _process_tree._cleanup_note(exc, failure)
            raise

    def _clone_environment(self, env: Dict[str, str], project_root: Path,
                           clone_dir: Path, import_roots: Sequence[str]) -> Dict[str, str]:
        """The environment of a worker's runs in ``clone_dir``: ``env`` with
        the clone in place of the project on the import path
        (``_clone_pythonpath``), the clone as the source root, so the
        conftest imports the mutated copy even from a shared test_dir
        outside project_root, and the clone's run inventory, so each run
        first removes what earlier runs left there."""
        prefix = ENV_PREFIX
        out = dict(env)
        out["PYTHONPATH"] = _clone_pythonpath(
            out.get("PYTHONPATH", ""), project_root, clone_dir, import_roots)
        out[f"{prefix}MUTATION_SOURCE_ROOT"] = str(clone_dir)
        out[f"{prefix}RUN_INVENTORY"] = str(_inventory_of(clone_dir))
        out.pop(f"{prefix}RUN_KEEP", None)
        return out

    def _spawn_http_workers(
        self, n: int, *, project_root: Path, env: Dict[str, str],
        log_fn=None, owner,
    ) -> Tuple[List[subprocess.Popen], List[str], List[Path]]:
        """Spawn ``n`` ``cosmic-ray http-worker`` processes, each with its
        cwd and PYTHONPATH in its own source clone (a copy of
        ``project_root`` in a fresh temporary directory, copied in parallel
        since the copies dominate setup on a network filesystem), so
        mutations go into the clone and pytest imports the mutated copy.
        Returns (procs, worker_urls, clone_dirs) for
        ``_cleanup_http_workers``. A failed clone raises, failing the
        session."""
        python = self._project_adapter().interpreter
        # Each socket stays bound until its worker spawns: closing all first
        # leaves a busy node hundreds of milliseconds to take a port.
        port_sockets = self._alloc_free_port_sockets(n, owner=owner)
        ports: List[int] = [s.getsockname()[1] for s in port_sockets]
        clone_base = _clone_base_dir(self.config, project_root)
        clone_dirs = owner._worker_clones
        for i in range(n):
            clone_dirs.append(_mark_owned(Path(tempfile.mkdtemp(
                prefix=f"cr_worker_{i}_", dir=str(clone_base)))))
        if log_fn is not None:
            log_fn(
                f"[CosmicRayEngine] spawning {n} HTTP workers with isolated "
                f"source clones under {clone_base} (disk: ~{n} x project_root size)",
                level=1,
            )

        # At most 16 clone threads: on a network filesystem more mostly
        # contend on metadata.
        clone_start = time.monotonic()
        max_clone_threads = min(n, 16)
        with ThreadPoolExecutor(max_workers=max_clone_threads) as pool:
            futures = [
                pool.submit(self._clone_one, project_root, clone_dirs[i])
                for i in range(n)
            ]
            for f in futures:
                f.result()  # propagate any clone exception to the caller
        if log_fn is not None:
            log_fn(
                f"[CosmicRayEngine] cloned in "
                f"{time.monotonic() - clone_start:.1f}s "
                f"(parallel x{max_clone_threads})",
                level=1,
            )

        # Popen returns at once, so each worker imports Cosmic Ray while the
        # next spawns. Each port's socket closes right before its spawn.
        procs = owner._worker_procs
        urls = owner.worker_urls
        spawn_start = time.monotonic()
        for clone_dir, port, sock in zip(clone_dirs, ports, port_sockets):
            worker_env = self._clone_environment(
                env, project_root, clone_dir, getattr(owner, "import_roots", ()))
            write_run_inventory(clone_dir, _inventory_of(clone_dir))
            sock.close()
            # The project interpreter, whose environment holds the
            # cosmic-ray command, runs it on the loopback address only.
            p = _process_tree.start(
                worker_command(python, port, ENV_PREFIX),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env={**worker_env, ENV_PREFIX + WORKER_SECRET: owner.worker_secret},
                cwd=str(clone_dir),
            )
            procs.append(p)
            urls.append(f"http://127.0.0.1:{port}")

        # Workers not up in time are dropped: Cosmic Ray's HTTP distributor
        # re-queues failures in a LIFO pool, so one dead worker would leave
        # most mutants without a test outcome.
        ready_timeout = int(self.config["cosmic_ray_worker_ready_timeout_s"])
        ready_start = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(n, 16)) as pool:
            ready_results = list(pool.map(
                lambda p: self._wait_for_worker(p, timeout=ready_timeout),
                ports,
            ))
        not_ready = [p for p, r in zip(ports, ready_results) if not r]
        if log_fn is not None:
            log_fn(
                f"[CosmicRayEngine] {n - len(not_ready)}/{n} workers ready in "
                f"{time.monotonic() - ready_start:.1f}s "
                f"(spawn: {ready_start - spawn_start:.1f}s, "
                f"{len(not_ready)} not-ready)",
                level=1,
            )

        # Unready workers are stopped. Their clones stay tracked for cleanup.
        if not_ready:
            if log_fn is not None:
                log_fn(
                    f"[CosmicRayEngine] DROPPING {len(not_ready)} unready "
                    f"worker(s) on ports {not_ready} from URL list. "
                    f"Proceeding with {len(ports) - len(not_ready)} healthy "
                    "workers. Raise cosmic_ray_worker_ready_timeout_s "
                    "(CosmicRaySettings.worker_ready_timeout_s, default 60 s) "
                    "if workers are slow to import "
                    "cosmic-ray on this filesystem.",
                    level=1,
                )
            healthy_urls: List[str] = []
            for port, ready in zip(ports, ready_results):
                if ready:
                    healthy_urls.append(f"http://127.0.0.1:{port}")
                else:
                    # So that cleanup meets no still-bound socket.
                    idx = ports.index(port)
                    if idx < len(procs):
                        _process_tree.stop(procs[idx], grace=5)
            urls[:] = healthy_urls

        if not urls:
            # No worker came up, so the session fails.
            raise RuntimeError(
                f"All {n} cosmic-ray http-workers failed to become ready "
                f"within {ready_timeout}s. Check disk space, port "
                "availability, or raise cosmic_ray_worker_ready_timeout_s "
                "(CosmicRaySettings.worker_ready_timeout_s)."
            )
        return procs, urls, clone_dirs

    @staticmethod
    def _cleanup_http_workers(
        procs: List[subprocess.Popen],
        clone_dirs: List[Path],
        *, log_fn=None,
    ) -> None:
        """Stop the workers, then remove their clones. A failed cleanup can be
        retried. Clones kept after a failure lose their owner marker: they
        are evidence, never swept as stale. A worker whose lease ended it
        before cleanup is disposed of, and its end logged: the run it served
        already shows that end (its later mutants untested, an adjudication
        incomplete)."""
        process_count, clone_count = len(procs), len(clone_dirs)
        ended: Dict[int, ControlError] = {}

        def retain() -> None:
            for clone in clone_dirs:
                try:
                    _unmark(clone)
                except OSError:
                    pass

        def own_end(error: BaseException) -> bool:
            # Such a worker's supervisor confirmed that nothing it owned
            # remains, and every wait and poll raises the end again.
            command = getattr(error, "_langadapter_linux_command", None)
            if (isinstance(error, ControlError) and getattr(command, "_unfinished_raised", False)
                    and any(command is proc for proc in procs)):
                ended[id(command)] = error
                return True
            return False

        def running(proc) -> bool:
            try:
                return proc.poll() is None
            except ControlError as error:
                if not own_end(error):
                    raise
                return False

        try:
            try:
                _process_tree.stop_all(procs, grace=5)
            except ControlError as error:
                if not own_end(error):
                    raise
                # stop_all ended at that worker's wait: each worker is stopped.
                for proc in procs:
                    try:
                        _process_tree.stop(proc, grace=5)
                    except ControlError as again:
                        if not own_end(again):
                            raise
            if any(running(proc) for proc in procs):
                raise OSError("worker process is still running after stop")
            for proc in procs:
                if not _process_tree.finish(proc):
                    raise RuntimeError("Mutation worker disposal is unconfirmed")
        except BaseException as primary:
            # A failing tree must not prevent attempts to stop the other workers.
            for proc in procs:
                try:
                    _process_tree.stop(proc, grace=5)
                except BaseException as cleanup:
                    _process_tree._cleanup_note(primary, cleanup)
                try:
                    _process_tree.finish(proc, primary=primary)
                except BaseException as cleanup:
                    _process_tree._cleanup_note(primary, cleanup)
            retain()
            raise
        if any(running(proc) for proc in procs):
            retain()
            raise OSError("worker process is still running after stop")
        if ended and log_fn is not None:
            log_fn(f"[CosmicRayEngine] {len(ended)} of {process_count} worker(s) had ended "
                   f"before cleanup: {next(iter(ended.values()))}", level=1)
        procs.clear()
        # Brief pause so that the next fleet's _alloc_free_port_sockets does
        # not pick a port a stopped worker has not fully released yet.
        time.sleep(0.5)
        failures = []
        for d in list(clone_dirs):
            try:
                if d.is_symlink():
                    raise OSError(f"worker clone was replaced with a symlink: {d}")
                if d.exists():
                    shutil.rmtree(d)
                if d.exists() or d.is_symlink():
                    raise OSError(f"worker clone still exists after removal: {d}")
                _inventory_of(d).unlink(missing_ok=True)
                clone_dirs.remove(d)
                _unmark(d)
            except OSError as exc:
                failures.append(exc)
        if failures:
            retain()
            raise OSError(f"could not remove {len(failures)} worker clone(s): {failures[0]}") from failures[0]
        if log_fn is not None and (process_count or clone_count):
            log_fn(
                f"[CosmicRayEngine] cleaned up {process_count} worker "
                f"process(es) + {clone_count} clone dir(s)",
                level=2,
            )

    # Catalogue cache

    def _resolve_cache_path(
        self, target: Path, operators: Optional[List[str]],
        *, source_bytes: Optional[bytes] = None, module_path: str = "",
    ) -> Optional[Path]:
        """The content-hashed cache path of ``target``'s catalogue with
        ``operators``. None with the cache off (``cosmic_ray_catalogue_cache_dir``
        unset, empty or ``auto``, since the library has no artifacts directory)
        or when the target's bytes or the interpreter version cannot be read.

        The key covers the source, the operators, the Cosmic Ray and Python
        versions, the cache format and ``module_path``, the target's path in
        the project as the session records it (a restored session mutates
        the path it names, so two equal files in different packages never
        share a catalogue). ``source_bytes`` supplies the content when
        ``target`` is not readable on this host (in a container). A relative
        directory resolves against the current working directory. An absolute
        one can be shared across nodes and output directories (the
        content-hashed name keeps concurrent writers correct)."""
        cache_dir_setting = str(self.config["cosmic_ray_catalogue_cache_dir"]).strip()
        if not cache_dir_setting:
            return None
        if cache_dir_setting.lower() == "auto":
            return None
        cache_dir = Path(cache_dir_setting)
        try:
            cache_dir = cache_dir.resolve()
        except OSError:
            # Not there yet: made on the first write.
            cache_dir = cache_dir.absolute()
        if source_bytes is None:
            try:
                source_bytes = target.read_bytes()
            except OSError:
                return None
        cosmic_ray_version, _catalogue, py_key = self._cosmic_ray_metadata()
        ops_blob = ",".join(operators) if operators else "(defaults)"
        # The catalogue is AST-derived and the ast module changes between
        # Python versions, so the project interpreter's major.minor keys the
        # cache. Unreadable, the cache is off rather than stale.
        if py_key is None:
            return None
        digest_input = (
            source_bytes
            + b"\x00" + ops_blob.encode("utf-8")
            + b"\x00" + cosmic_ray_version.encode("utf-8")
            + b"\x00" + py_key.encode("ascii")
            + b"\x00" + str(_CACHE_FORMAT_VERSION).encode("ascii")
            + b"\x00" + module_path.encode("utf-8")
        )
        sha = hashlib.sha256(digest_input).hexdigest()[:16]
        # A portable file name.
        safe_name = "".join(
            c if c.isalnum() or c in ("-", "_") else "_"
            for c in target.stem
        )
        return cache_dir / f"{safe_name}_{sha}.sqlite"

    @staticmethod
    def _persist_cache(
        session_path: Path, cache_path: Path, *, log_fn=None,
    ) -> None:
        """Copy a freshly initialized session into the cache, best effort: a
        failure is logged, never raised. A session that is not usable (a file
        an interrupted run truncated) is refused, since it would make every
        later run fail with "file is not a database"."""
        try:
            _validate_session_db(session_path)
        except (sqlite3.DatabaseError, OSError) as exc:
            if log_fn is not None:
                log_fn(
                    f"[CosmicRayEngine] REFUSED to cache "
                    f"{session_path.name}: not a usable session db "
                    f"({type(exc).__name__}: {str(exc)[:200]}). "
                    f"Next run will re-init from scratch.",
                    level=1,
                )
            return
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Copy to this writer's own temporary file beside the target
            # (another run may store the same catalogue at once), then move
            # it into place with os.replace, which needs both on one volume.
            handle, tmp_name = tempfile.mkstemp(
                prefix=cache_path.name + ".", suffix=".tmp", dir=str(cache_path.parent))
            os.close(handle)
            try:
                shutil.copy2(str(session_path), tmp_name)
                os.replace(tmp_name, str(cache_path))
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
            if log_fn is not None:
                log_fn(
                    f"[CosmicRayEngine] persisted catalogue cache "
                    f"({cache_path.name})",
                    level=2,
                )
        except OSError as exc:
            if log_fn is not None:
                log_fn(
                    f"[CosmicRayEngine] catalogue cache write failed "
                    f"({type(exc).__name__}: {exc}). The next run regenerates it.",
                    level=1,
                )

    def _gc_cache_dir(self, cache_dir: Path, *, log_fn=None) -> None:
        """Remove catalogue cache entries (``_CACHE_ENTRY``, other files
        stay) after a fresh write: those older
        than ``cosmic_ray_cache_max_age_days`` (default 0, off), then all but
        the newest ``cosmic_ray_cache_max_entries`` (default 200). Errors are
        ignored."""
        if not cache_dir.exists() or not cache_dir.is_dir():
            return
        max_entries = int(self.config["cosmic_ray_cache_max_entries"])
        max_age_days = int(self.config["cosmic_ray_cache_max_age_days"])
        if max_entries <= 0 and max_age_days <= 0:
            return
        try:
            entries = [
                (p, p.stat().st_mtime)
                for p in cache_dir.glob("*.sqlite") if _CACHE_ENTRY.fullmatch(p.name)
            ]
        except OSError:
            return
        entries.sort(key=lambda item: item[1], reverse=True)  # newest first
        removed = 0
        if max_age_days > 0:
            cutoff = time.time() - max_age_days * 86400
            survivors: List[tuple] = []
            for path, mtime in entries:
                if mtime < cutoff:
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
                else:
                    survivors.append((path, mtime))
            entries = survivors
        if max_entries > 0 and len(entries) > max_entries:
            for path, _ in entries[max_entries:]:
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        if removed and log_fn is not None:
            log_fn(
                f"[CosmicRayEngine] catalogue cache GC removed {removed} "
                f"stale entr{'y' if removed == 1 else 'ies'}",
                level=2,
            )

    def _target_to_module_path(self, target: Path, project_root: Path) -> str:
        """``target``'s path relative to ``project_root`` with forward
        slashes (``pkg/sub/mod.py``), the portable ``module-path`` Cosmic Ray
        takes. A target outside the project raises ``InvalidRequest``, as
        the session refuses it before (``project_file``)."""
        try:
            return target.resolve().relative_to(project_root.resolve()).as_posix()
        except ValueError:
            raise InvalidRequest(
                "target", f"{target} does not lie under the project root") from None

    def _write_config_toml(
        self,
        config_path: Path,
        *,
        module_path: str,
        test_command: str,
        per_mutant_timeout: int,
        worker_urls: Optional[List[str]] = None,
        operators: Optional[List[str]] = None,
        omit_distributor: bool = False,
    ) -> None:
        """Write the cosmic-ray.toml driving this run.

        Without ``omit_distributor`` it holds a distributor block: ``http``
        for ``worker_urls`` (Cosmic Ray dispatches mutants to them
        asynchronously. The caller spawns the workers, each with its own
        source clone so they do not race on the file write in
        ``mutate_and_test``), else ``local`` (sequential: Cosmic Ray 8.x's
        ``LocalDistributor`` is a plain loop that ignores the worker count).
        With it, the container launcher appends the ``http`` block after
        spawning its workers.

        ``operators`` restricts the run to those operators, else Cosmic
        Ray's full catalogue runs. Init takes no allow-list, so their
        complement goes into ``[cosmic-ray.filters.operators-filter]`` as
        exclude patterns, and ``_apply_operator_filter`` marks those mutants
        SKIPPED between init and exec.

        tomli_w writes the values, quoted as TOML needs."""
        settings: Dict[str, Any] = {
            "module-path": module_path, "timeout": per_mutant_timeout,
            "excluded-modules": [], "test-command": test_command}
        if not omit_distributor:
            settings["distributor"] = (
                {"name": "http", "http": {"worker-urls": list(worker_urls)}}
                if worker_urls else {"name": "local"})
        settings["interceptors"] = {"enabled": ["spor", "pragma_no_mutate"]}
        # The exclude patterns are regexes: each name is anchored (``^name$``) so
        # it does not also match a longer name it is a prefix of.
        exclude = ([f"^{re.escape(name)}$" for name in self._excluded_operators(operators)]
                   if operators else [])
        if exclude:
            settings["filters"] = {"operators-filter": {"exclude-operators": exclude}}
        config_path.write_text(tomli_w.dumps({"cosmic-ray": settings}), encoding="utf-8")

    def _apply_operator_filter(
        self,
        python: str,
        config_path: Path,
        session_path: Path,
        *,
        log_fn=None,
    ) -> None:
        """Mark the mutants of the operators the config excludes SKIPPED in
        the session, for ``cosmic-ray exec`` to pass over: Cosmic Ray's
        ``cr-filter-operators``, run by the project interpreter ``python`` in
        isolated mode from the session's directory, so no project module
        shadows it. A failed filter raises ``RuntimeError``
        (``FILTER_REFUSAL``): exec would otherwise run the whole catalogue as
        the requested operator set."""
        try:
            done = _process_tree.run(
                [python, "-I", "-m", "cosmic_ray.tools.filters.operators_filter",
                 str(session_path), str(config_path)],
                text=True, encoding="utf-8", errors="replace",
                timeout=int(self.config["cosmic_ray_init_timeout_s"]),
                cwd=str(session_path.parent),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            if unresolved_command(exc) is not None:
                raise
            failure = f"{type(exc).__name__}: {exc}"
        else:
            if done.returncode == 0:
                if log_fn is not None:
                    log_fn("[CosmicRayEngine] operator filter applied", level=1)
                return
            failure = (f"cr-filter-operators exited {done.returncode}: "
                       f"{done.stderr[-2000:].strip()}")
        message = FILTER_REFUSAL.format(failure=failure)
        if log_fn is not None:
            log_fn(f"[CosmicRayEngine] {message}", level=1)
        raise RuntimeError(message)

    @classmethod
    def _unsettled(cls, error, sess) -> bool:
        """Whether a verification or adjudication run's ``error`` left its
        command, or the session's module under test, unresolved. No further
        run may start then: the error ends the run, which keeps the session
        and the module's backup."""
        return unresolved_command(error) is not None or bool(
            getattr(sess, "_target_commands", None)
            or getattr(sess, "_source_restore_failed", False))

class CosmicRayEngineSession:
    """The worker pool of a ``CosmicRayEngine``: the HTTP workers and their
    source clones, opened once on ``__enter__``, used by every ``.run(...)``
    in the ``with`` block and removed on ``__exit__``.

    Without the ``http`` distributor no worker starts, and ``cosmic-ray
    exec`` runs the local distributor, one mutant at a time. ``http`` starts
    its fleet even for one worker. A fleet that cannot start fails the
    opening, with no fallback to the local distributor. A worker that dies
    between runs is not detected: the next run reads whatever results its
    session holds."""

    def _remove_left_files(self) -> None:
        """Remove from an owned project root what this session's runs
        created there (``_run_definition.remove_unlisted``), so a later
        normal run starts as before the session."""
        if self._root_inventory is not None:
            remove_unlisted(*self._root_inventory)

    def __init__(
        self,
        engine: CosmicRayEngine,
        *,
        project_root: Path,
        workers: int,
        distributor: Optional[str] = None,
    ) -> None:
        self.engine = engine
        self.project_root = project_root
        self.workers = workers
        # ``local`` (sequential, reproducible), ``http`` (the parallel fleet)
        # or ``auto`` (local for one worker), as ``_resolve_distributor``
        # resolves it. The session's own choice is kept even for one worker. A
        # configured http needs more than one.
        _distributor = str(distributor or engine.config["cosmic_ray_distributor"]).strip().lower()
        self.use_http = (_resolve_distributor(_distributor, workers) == "http"
                         and (distributor is not None or workers > 1))
        # In a container the launcher runs the fleet for each exec: no host pool.
        self.container_mode = engine._exec_mode() == "container"
        self.env: Dict[str, str] = {}
        self._worker_procs: List[subprocess.Popen] = []
        self._worker_clones: List[Path] = []
        self._worker_sockets: List[socket.socket] = []
        self.worker_urls: List[str] = []
        self._target_commands = []
        self._source_backup = None
        self._source_restore_failed = False
        #: ``(root, inventory, keep)`` of an owned project root the runs
        #: start in (``CosmicRayEngine._root_inventory``), else None.
        self._root_inventory = None
        self._run_failure = None
        self._opened = False
        self._closed = False
        self._session_dir = getattr(engine, "_default_session_dir", None)
        # The token each run writes into the owner marker beside its scratch
        # directory, and their bases: the cleanup removes only this session's.
        self._run_owner = os.urandom(16).hex()
        #: The secret of the session's HTTP workers, which refuse a job
        #: without it (``_cr_session_runner.LOOPBACK_WORKER``).
        self.worker_secret = os.urandom(32).hex()
        self._run_bases: set = set()
        #: The copies of the project's pytest configuration the session's
        #: runs wrote beside a suite, removed when it closes.
        self._config_copies: set = set()
        #: Whether pytest-benchmark's storage was in the project root, where
        #: the session's runs start pytest, when it opened
        #: (``storage_exists``), None before then and after it closes.
        self._storage_existed: Optional[bool] = None
        # Genuine killed-by-all mutants of this session as (suite fingerprint,
        # (module_path, operator, occurrence)): job ids are per sweep, and a
        # kill is genuine for its suite only. Later sweeps skip them.
        # Unresolved mutants are never cached.
        self.verified_killed_by_all: set = set()

    def __enter__(self) -> "CosmicRayEngineSession":
        return self._open()

    def _open(self) -> "CosmicRayEngineSession":
        if self._closed:
            raise RuntimeError("A closed mutation session cannot be reopened; create a new session")
        if getattr(self.engine, "_retained_session", None) is not None:
            raise RuntimeError("Mutation engine has an unclosed failed session; close its retained handle first")
        log_fn = self.engine.config["log_fn"]
        # Built once, for every run of the session.
        self.env = self.engine._build_env(self.project_root)
        self.import_roots, self.target_roots = self.engine._import_from_the_copy(
            self.project_root, self.env)
        if self.engine.adapter is not None:
            self._storage_existed = storage_exists(self.engine.adapter)

        if self.container_mode:
            # The launcher manages its own fleet in the container on each exec.
            self._opened = True
            return self

        self._run_bases.add(_run_base(self.env))
        _sweep_stale_run_directories(
            float(self.engine.config["cosmic_ray_clone_sweep_hours"] or 0),
            log_fn=log_fn,
        )

        if self.use_http:
            _sweep_stale_clones(
                _clone_base_dir(self.engine.config, self.project_root),
                float(self.engine.config["cosmic_ray_clone_sweep_hours"] or 0),
                log_fn=log_fn,
            )
            try:
                procs, urls, clones = self.engine._start_http_workers(
                    self.workers,
                    project_root=self.project_root,
                    env=self.env,
                    log_fn=log_fn,
                    owner=self,
                )
                self._worker_procs = procs
                self.worker_urls = urls
                self._worker_clones = clones
                if log_fn is not None:
                    log_fn(
                        f"[CosmicRayEngineSession] http workers UP "
                        f"({len(urls)}/{self.workers} live). Mutation "
                        f"runs in this session will use the HTTP "
                        f"distributor.",
                        level=1,
                    )
            except BaseException as exc:
                if isinstance(exc, MutationCleanupError):
                    raise
                if not isinstance(exc, Exception):
                    # An interrupt stays the error. Workers that could not
                    # be released stay with this handle, and close retries.
                    failure = None
                    try:
                        self._cleanup_resources()
                    except Exception as cleanup:
                        failure = cleanup
                    if failure is None:
                        self._closed = True
                    else:
                        exc._langadapter_mutation_session = self
                        _process_tree._cleanup_note(exc, failure)
                    raise
                self._cleanup_resources()
                self._closed = True
                # A run sized for parallel workers would use up its wall
                # clock on the sequential local distributor. A caller may open
                # a local session instead (allow_sequential_fallback).
                if log_fn is not None:
                    log_fn(
                        f"[CosmicRayEngineSession] HTTP worker setup raised "
                        f"({type(exc).__name__}: {exc}): this session does not "
                        f"continue its {self.workers} workers in sequence.",
                        level=1,
                    )
                raise

        self._opened = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except BaseException as cleanup:
            if exc is None:
                raise
            exc._langadapter_mutation_session = self
            _process_tree._cleanup_note(exc, cleanup)

    def close(self) -> None:
        """Release the native pool. A failed cleanup raises
        ``MutationCleanupError`` (an interrupt or exit propagates as itself)
        and leaves this handle what it has not released, for ``close`` to
        retry."""
        try:
            self._close()
        except Exception as exc:
            raise MutationCleanupError(
                self, f"native mutation session cleanup failed: "
                      f"{type(exc).__name__}: {exc}") from exc

    def _close(self) -> None:
        if self._closed:
            return
        self._opened = False
        self._cleanup_resources()
        self._closed = True

    def _cleanup_resources(self) -> None:
        """Dispose the session's acquired state."""
        for command in list(self._target_commands):
            if not _process_tree.finish(command):
                raise RuntimeError("Mutation command disposal is unconfirmed")
            self._target_commands.remove(command)
        socket_failures = []
        for sock in self._worker_sockets:
            try:
                sock.close()
            except OSError as exc:
                socket_failures.append(exc)
        if self._worker_procs or self._worker_clones:
            log_fn = self.engine.config["log_fn"]
            self.engine._cleanup_http_workers(
                self._worker_procs, self._worker_clones, log_fn=log_fn,
            )
        if socket_failures:
            raise OSError(f"could not close worker sockets: {socket_failures[0]}") from socket_failures[0]
        # Every process of its runs is confirmed gone above, so nothing works
        # in the temporary directories they left.
        _remove_run_directories(self._run_bases, self._run_owner)
        # Not evidence: the configuration copies go after any run.
        for copy in sorted(self._config_copies):
            with contextlib.suppress(FileNotFoundError):
                copy.unlink()
        self._config_copies = set()
        existed, self._storage_existed = self._storage_existed, None
        if self.engine.adapter is not None:
            remove_created_storage(self.engine.adapter, existed)
        # A failed run keeps its evidence (the original source, the session
        # database). One an interrupt or exit stopped failed in nothing: once
        # disposal is confirmed and the source restored, both go.
        evidence = self._run_failure is not None and isinstance(self._run_failure, Exception)
        if self._source_backup is not None:
            self._source_backup.restore()
            self._source_restore_failed = False
            if evidence:
                self._source_backup.retain(self._run_failure)
            else:
                self._source_backup.remove()
            self._source_backup = None
        # The engine-made session directory is this session's. An explicit
        # cosmic_ray_session_dir is the caller's.
        default_dir = self._session_dir
        if default_dir is not None and not evidence:
            if default_dir.is_symlink():
                raise OSError(f"session directory was replaced with a symlink: {default_dir}")
            if default_dir.exists():
                shutil.rmtree(default_dir)
            if default_dir.exists() or default_dir.is_symlink():
                raise OSError(f"session directory still exists after removal: {default_dir}")
            if getattr(self.engine, "_default_session_dir", None) == default_dir:
                self.engine._default_session_dir = None
        elif default_dir is not None:
            # Preserve failed-run databases independently of subsequent sessions.
            self._run_failure._langadapter_mutation_artifacts = default_dir
            if getattr(self.engine, "_default_session_dir", None) == default_dir:
                self.engine._default_session_dir = None
        self._session_dir = None
        self._worker_procs = []
        self._worker_clones = []
        self.worker_urls = []
        self._worker_sockets = []
        if getattr(self.engine, "_retained_session", None) is self:
            self.engine._retained_session = None

    def run(
        self,
        *,
        target: Path,
        test_dir: Path,
        per_mutant_timeout: int = 60,
        wall_clock_timeout: int = 3600,
        operators: Optional[List[str]] = None,
        test_files: Optional[Sequence[str]] = None,
    ) -> bool:
        """Run the mutants of ``target`` in this session's pool. Returns
        whether the wall clock ended the run. The session database holds
        its results."""
        if not self._opened:
            raise RuntimeError(
                "CosmicRayEngineSession.run called outside its 'with' block. "
                "Use 'with engine.session(...) as sess: sess.run(...)'."
            )
        if self._run_failure is not None or self._target_commands or self._source_backup is not None:
            raise RuntimeError("Mutation session has retained failed-run state; open a new session")
        try:
            result = self.engine._run_in_session(
                sess=self, target=target, test_dir=test_dir,
                per_mutant_timeout=per_mutant_timeout,
                wall_clock_timeout=wall_clock_timeout, operators=operators,
                test_files=test_files,
            )
            if self._source_backup is not None:
                self._source_backup.remove()
                self._source_backup = None
            self._remove_left_files()
            return result
        except BaseException as error:
            command = unresolved_command(error)
            if (isinstance(error, OriginRefused) and command is None
                    and not self._target_commands and self._source_backup is None):
                # Refused before any mutant, nothing changed or left
                # running: the session's next run can go ahead.
                self._remove_left_files()
                raise
            if command is not None and not any(command is item for item in self._target_commands):
                self._target_commands.append(command)
            self._run_failure = error
            self.engine._retained_session = self
            error._langadapter_mutation_session = self
            if self._source_backup is not None:
                self._source_backup.retain(error)
            raise


#: Every per-mutant run journals unless the request turns it off, locally
#: as in a container, so a kill by the clock keeps its evidence.
DEFAULT_TIMEOUT_JOURNAL = True


class MutationSession(CommonMutationSession):
    """A Cosmic Ray session for the project of a Python adapter.

    Opening starts the ``http`` distributor's worker fleet, each worker with
    its own source clone. The ``local`` distributor runs one mutant at a
    time and starts nothing. ``auto``, the default, is ``local`` for one
    worker and ``http`` for more. ``distributor`` then names the one the
    session runs. ``workers_started`` says how many workers started. When
    fewer start than asked for, the session fails unless
    ``allow_fewer_workers`` is set: ``outcome`` and ``reason`` say so, and
    a run of a failed session returns that outcome. ``close`` stops the
    workers and removes the clones and temporary files. A session is also a
    context manager."""

    def __init__(self, adapter: Any, *, workers: int = 1,
                 distributor: str = "auto",
                 allow_fewer_workers: bool = False,
                 settings: Optional[CosmicRaySettings] = None) -> None:
        _validate_session_options(workers=workers, distributor=distributor,
                                  allow_fewer_workers=allow_fewer_workers, settings=settings)
        super().__init__(workers_requested=workers)
        self.adapter = adapter
        self.settings = settings or CosmicRaySettings()
        self.distributor = _resolve_distributor(distributor, workers)
        self._root = Path(adapter.context.root)
        self._engine = CosmicRayEngine(
            self.settings._engine_config(self.distributor),
            adapter=adapter)
        self._session: Any = None
        self._nested_session: Any = None
        self._prepared: Any = None
        self._container = isinstance(adapter.context.execution,
                                     ContainerExecution)
        session = self._engine.session(project_root=self._root,
                                       workers=workers, distributor=self.distributor)
        self._session = session
        try:
            session.__enter__()
            self.register_owned_commands(getattr(session, "_worker_procs", ()))
        except BaseException as exc:
            try:
                self.close()
            except MutationCleanupError as cleanup:
                if not _interrupted(exc):
                    raise cleanup from exc
                _carry_cleanup(exc, self, cleanup)
            if not isinstance(exc, Exception):
                raise
            self._opening_failed(f"the worker fleet did not start: {exc}")
            return
        if self._container:
            # The launcher starts the fleet inside the container for each
            # run, and the run's outcome says when it could not.
            self._workers_started = workers
            return
        self._workers_started = (len(session.worker_urls) if session.use_http
                                 else 1)
        if self.workers_started < workers:
            detail = f"{self.workers_started} of {workers} workers started"
            if allow_fewer_workers:
                self._reason = detail
            else:
                self.close()
                self._opening_failed(f"{detail}; the session does not allow fewer",
                                     self.workers_started)

    def release(self) -> None:
        """Clear native handles only after their cleanup succeeds."""
        if self._nested_session is not None:
            self._nested_session.__exit__(None, None, None)
            self._nested_session = None
        if self._session is not None:
            self._session.__exit__(None, None, None)
            self._session = None
        self._prepared = None

    # Runs

    def _failed_result(self, request: MutationRequest, outcome: Outcome,
                       reason: str) -> MutationRun:
        return MutationRun.failed(request.target, outcome, reason,
                                  failure=FAILED_SESSION,
                                  settings=self._effective_settings(request))

    def _validate_request(self, request: MutationRequest) -> None:
        """Resolve request inputs before touching the shared engine's settings."""
        request.validate()
        target = self._target(request.target)
        files = self._suite_files(request)
        settings = self._effective_settings(request)
        workers = settings["workers"].value
        if workers > self.workers_started:
            raise InvalidRequest(
                "workers", f"the session has {self.workers_started} "
                f"worker(s); a run can use them or run sequentially")
        operators = self._operators(request.catalogue)
        # An unreadable registry (None) refuses the run when its filter is
        # written (``_compute_excluded_operators``).
        registry = self._engine.full_operators() if operators else None
        unknown = sorted(set(operators) - set(registry)) if registry is not None else []
        if unknown:
            raise InvalidRequest(
                "catalogue.operators", f"Cosmic Ray does not register "
                f"{', '.join(unknown)}. A catalogue lists registered operators "
                f"or names an operator set ({', '.join(sorted(_OPERATOR_SET_SHORTCUTS))})")
        self._prepared = target, files, settings, operators

    def _configure(self, settings: Dict[str, EffectiveSetting], *,
                   journal_plugin: bool, progress: Any) -> None:
        """Set the shared engine for a measurement with ``settings``."""
        engine = self._engine
        engine.config["_omit_fail_fast"] = (
            settings["attribution"].value == "all")
        engine.fail_on_unresolved = settings["fail_on_unresolved"].value
        engine.serial_auxiliary = settings["workers"].value == 1
        journal = settings["timeout_journal"].value
        engine.config["_timeout_journal"] = journal
        engine.config["_journal_plugin"] = journal and journal_plugin
        engine.config["_progress"] = progress

    def measure(self, request: MutationRequest) -> MutationRun:
        """Run the already validated request using this session's native pool."""
        target, files, settings, operators = self._prepared
        workers = settings["workers"].value
        engine = self._engine
        self._configure(settings, journal_plugin=not self._carries_instrument(request),
                        progress=request.progress)
        session, nested = self._session, None
        pmt = settings["per_mutant_timeout"].value
        wall = settings["wall_clock_limit"].value
        try:
            # A run that asks fewer workers than the session started runs on
            # a session of that many (one worker runs sequentially).
            if workers != self.workers_started:
                nested = engine.session(project_root=self._root, workers=workers)
                self._nested_session = nested
                nested.__enter__()
                session = nested
            try:
                truncated = session.run(
                    target=target, test_dir=Path(request.suite.root),
                    per_mutant_timeout=pmt, wall_clock_timeout=wall,
                    operators=operators,
                    test_files=files)
            except MutationInstrumentFault as fault:
                return self._with_origin(self._finished(
                    request, target, files, settings, False, fault, None, wall))
            session_path = (engine._container_session_path()
                            or engine._resolve_paths()[0])
            return self._with_origin(self._finished(
                request, target, files, settings, truncated, None, session_path, wall))
        finally:
            if nested is not None:
                original = sys.exc_info()[1]
                try:
                    nested.__exit__(None, None, None)
                except BaseException as exc:
                    # An interrupt, of the run or of this cleanup, stays the
                    # error: the session's own close retries the nested one.
                    if original is not None and _interrupted(original):
                        _carry_cleanup(original, self, exc)
                    elif _interrupted(exc):
                        raise
                    else:
                        cleanup = MutationCleanupError(self, f"{type(exc).__name__}: {exc}")
                        raise cleanup from (original if original is not None else exc)
                else:
                    self._nested_session = None

    def _with_origin(self, run: MutationRun) -> MutationRun:
        """``run`` with the time of its origin run, when it made one."""
        seconds = getattr(self._engine, "_last_origin_run_s", None)
        return run if seconds is None else dataclasses.replace(run, origin_run_s=seconds)

    def _finished(self, request: MutationRequest, target: ProjectPath,
                  files: List[str], settings: Dict[str, EffectiveSetting],
                  truncated: bool, fault: Optional[BaseException],
                  session_path: Optional[Path], wall: int) -> MutationRun:
        """The run of ``request`` read from its settled session: its
        outcome, identity and the killers placed in its suite."""
        if fault is not None:
            return MutationRun.failed(
                target.name, Outcome.INFRASTRUCTURE_FAILURE, str(fault),
                failure=FAILED_INSTRUMENT_CHECK, settings=settings,
                stopping_tests=getattr(fault, "stopping_tests", ()))
        if self._engine._last_failure is not None:
            failure, reason = self._engine._last_failure
            return MutationRun.failed(target.name, Outcome.INFRASTRUCTURE_FAILURE, reason,
                                      failure=failure, settings=settings)
        if not session_path.exists() or session_path.stat().st_size == 0:
            return MutationRun.failed(
                target.name, Outcome.INFRASTRUCTURE_FAILURE,
                "the mutation tool produced no session: its "
                "initialization failed (the log names the cause)",
                failure=FAILED_INITIALIZATION, settings=settings)
        try:
            run = self._engine._read_mutation_run(session_path, target,
                                                  self.settings.log)
        except sqlite3.Error as exc:
            return MutationRun.failed(
                target.name, Outcome.INFRASTRUCTURE_FAILURE,
                f"the session could not be read: {exc}",
                failure=FAILED_SESSION_READ, settings=settings)
        outcome, reason = Outcome.COMPLETED, ""
        tested = [m for m in run.mutants
                  if m.classification.mutation_class is not MutationClass.NOT_RUN]
        if truncated:
            outcome = Outcome.TIMED_OUT
            reason = (f"the wall-clock limit of {wall} s ended the run with "
                      f"{run.summary.get('untested', 0)} mutant(s) untested")
        elif not run.mutants:
            reason = "the catalogue has no mutant for this target"
        elif not tested:
            reason = "no mutant was tested"
        exit_status = self._engine._last_exec_exit
        if exit_status and not truncated:
            untested = run.summary.get("untested", 0)
            if untested:
                return MutationRun.failed(
                    target.name, Outcome.INFRASTRUCTURE_FAILURE,
                    f"the mutation tool's exec exited {exit_status} with {untested} of "
                    f"{len(run.mutants)} mutants untested", failure=FAILED_SESSION,
                    settings=settings)
            reason = reason or (f"the mutation tool's exec exited {exit_status} after every "
                                f"mutant was measured")
        # A result that measured nothing: a request to a worker that failed, a
        # mutation that could not be applied, or tests that raised (Cosmic
        # Ray's INCOMPETENT).
        unmeasured = [m for m in run.mutants
                      if m.classification.mutation_class is MutationClass.NOT_RUN
                      and (m.test_outcome or m.worker_outcome)]
        # Cosmic Ray reports a worker exception (a mutant not applied, tests
        # not started) as incompetent: with one for every mutant, nothing was
        # measured. With some, the reason says how many.
        raised = [m for m in unmeasured
                  if str(m.worker_outcome or "").upper() == "EXCEPTION"]
        if raised and len(raised) == len(run.mutants):
            lines = (str(dict(raised[0].row).get("output") or "")
                     .strip().splitlines())
            return MutationRun.failed(
                target.name, Outcome.INFRASTRUCTURE_FAILURE,
                f"the worker raised an exception for every one of the "
                f"{len(raised)} mutants, so none was measured"
                + (f": {lines[-1]}" if lines else ""),
                failure=FAILED_WORKERS, settings=settings)
        if raised and not reason:
            reason = (f"{len(raised)} of {len(run.mutants)} mutants' workers raised "
                      f"an exception (reported incompetent by Cosmic Ray)")
        # The first one's native result: its test outcome when it has one
        # (INCOMPETENT), else its worker outcome.
        native = (unmeasured[0].test_outcome or unmeasured[0].worker_outcome) if unmeasured else ""
        if run.mutants and len(unmeasured) == len(run.mutants):
            return MutationRun.failed(
                target.name, Outcome.INFRASTRUCTURE_FAILURE,
                f"none of the {len(run.mutants)} mutants has a test outcome "
                f"({native}), so none was measured",
                failure=FAILED_WORKERS, settings=settings)
        if unmeasured and not reason:
            reason = (f"{len(unmeasured)} of {len(run.mutants)} mutants have a "
                      f"result without a test outcome ({native}) "
                      f"and count as untested")
        # The instrument a listed setup file carries ran in every mutant's
        # run, as in the suite's normal run, which records it alike.
        from langadapter.python._runs import carried_instrument

        identity = MutationIdentity(
            observation=ObservationIdentity(
                target=file_identity(self._engine.adapter, target),
                suite=request.suite.identity(),
                toolchain=self.adapter.toolchain_identity(),
                instrument=carried_instrument(
                    [request.suite._resolve(p).resolve() for p in request.suite.setup_files]),
                execution=execution_identity(self.adapter.context)),
            catalogue=(request.catalogue.name
                       or request.catalogue.kind),
            operators=tuple(run.requested_operators),
            per_mutant_timeout_s=settings["per_mutant_timeout"].value,
            attribution=settings["attribution"].value,
            workers=settings["workers"].value)
        mutants = []
        for mutant in run.mutants:
            tests, unmatched = self._placed_killers(mutant, files)
            # A killer as pytest named it from where the run's copy of the
            # suite lay (../../dev/shm/cr_1/suite_x/tests/test_a.py::test_b)
            # is kept as the suite's own test id.
            killers = tuple(self._suite_killer(killer, files) for killer in mutant.killers)
            row = mutant.row
            if killers != mutant.killers:
                row = {**row, "killer": killers[0], "killers": list(killers)}
            mutants.append(dataclasses.replace(
                mutant, killers=killers, row=row, killing_tests=tests,
                unmatched_killers=unmatched))
        return dataclasses.replace(run, mutants=tuple(mutants),
                                   outcome=outcome, reason=reason,
                                   identity=identity, settings=settings)

    @staticmethod
    def _suite_killer(killer: str, files: List[str]) -> str:
        """A killer as the suite's test id, or as reported when it names no
        test of the suite (or a file, the per-file adjudication's)."""
        test = identify_test(killer, files)
        return str(test) if test is not None and test.qualname != FILE_ATTRIBUTION else killer

    @staticmethod
    def _placed_killers(mutant: Any, files: List[str]) -> tuple:
        """The mutant's killers as tests of the suite, and the killers that
        name no test of it. A kill the per-file adjudication attributed to a
        file is neither: its evidence names the file."""
        tests, unmatched = [], []
        for killer in mutant.killers:
            test = identify_test(killer, files)
            if test is None:
                unmatched.append(killer)
            elif test.qualname != FILE_ATTRIBUTION:
                tests.append(test)
        return tuple(tests), tuple(unmatched)

    @staticmethod
    def _carries_instrument(request: MutationRequest) -> bool:
        """Whether a setup file of the suite, listed or loaded by pytest for
        its test files, carries the instrument, whose hooks then write the
        journal: the journal plugin would write every line twice."""
        suite = request.suite
        root = Path(suite.root).resolve()
        tests = [suite._resolve(p).resolve() for p in suite.test_files]
        files = set(loaded_setup_files(root, tests))
        files.update(suite._resolve(p).resolve() for p in suite.setup_files)
        return carried_instrument(sorted(files)) is not None

    def _target(self, target: str) -> ProjectPath:
        return project_file(self._engine.adapter, "target", target)

    @staticmethod
    def _suite_files(request: MutationRequest) -> List[str]:
        root = Path(request.suite.root).resolve()
        names = [request.suite._resolve(path).resolve().relative_to(
            root).as_posix() for path in request.suite.test_files]
        stems = [PurePosixPath(name).name for name in names]
        shared = sorted({stem for stem in stems if stems.count(stem) > 1})
        if shared:
            raise InvalidRequest(
                "suite.test_files", f"test files share a name "
                f"({', '.join(shared)}); pytest cannot run them in one "
                f"session without packages")
        return names

    def _effective_settings(
        self, request: MutationRequest,
    ) -> Dict[str, EffectiveSetting]:
        timeout, passing_s = request.per_mutant_timeout, request.passing_suite_s
        if isinstance(timeout, TimeoutRule):
            per_mutant = EffectiveSetting(
                timeout.seconds(passing_s), FROM_REQUEST, timeout.inputs(passing_s))
        elif timeout is not None:
            per_mutant = EffectiveSetting(int(timeout), FROM_REQUEST)
        else:
            per_mutant = EffectiveSetting(
                _DEFAULT_TIMEOUT_RULE.seconds(passing_s), FROM_DEFAULT,
                _DEFAULT_TIMEOUT_RULE.inputs(passing_s))
        return _common_settings(request, request.catalogue.operators or (), per_mutant,
                                _chosen(request.workers, self.workers_started or 1),
                                DEFAULT_TIMEOUT_JOURNAL)

    @staticmethod
    def _operators(catalogue: Catalogue) -> Optional[List[str]]:
        if catalogue.kind == "operators":
            return list(catalogue.operators)
        if catalogue.kind == "named":
            from langadapter.python.cosmic_ray import _refuse_unknown_set
            _refuse_unknown_set("catalogue.name", catalogue.name)
            return list(_OPERATOR_SET_SHORTCUTS[catalogue.name]())
        return None


def open_session(adapter: Any, engine: str, **options: Any) -> MutationSession:
    """A mutation session of ``engine`` for ``adapter``'s project."""
    _require_engine(engine, "cosmic_ray", "Python")
    return MutationSession(adapter, **options)


__all__ = ["MutationSession", "open_session"]
