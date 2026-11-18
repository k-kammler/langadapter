"""In-container launcher for one Cosmic Ray mutation run, with the ``http``
or the ``local`` distributor.

This module is copied into the shared work directory and executed by the
container's own interpreter, so it imports nothing from its package and uses
only the standard library plus the ``cosmic-ray`` CLI already present in the
container. The engine on the host writes a spec JSON, makes a single blocking
executor call to run this script, and then reads the resulting session
SQLite back from the work directory.

The sequence mirrors the host-side worker fleet (clone the source per worker,
spawn ``cosmic-ray http-worker`` processes on loopback ports, run
``cosmic-ray exec``, tear the fleet down) but runs entirely inside the
container, where the workers can import and mutate the code under test in the
project's own environment. The ``local`` distributor is single-threaded, so
the http distributor with a worker fleet is what makes the run parallel.

Spec JSON (one argv): see ``run_session`` for the consumed keys. The script
exits 0 when exec ran, including a run the wall-clock limit ended. It exits
non-zero when the session directory is refused, init fails, no worker becomes
ready, the isolation probe fails or its wrapper is missing, or exec cannot
start. It writes ``<session>.runner_status.json`` for the host to read:
``truncated``, ``exec_returncode``, ``ready_workers``, the distributor, the
worker count, the open-file limit and where the session ran.
"""
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple


_LOG_FH = None


def _log(msg: str) -> None:
    line = f"[cr-runner] {msg}"
    print(line, flush=True)
    fh = _LOG_FH
    if fh is not None:
        try:
            fh.write(line + "\n")
            fh.flush()
        except OSError:
            pass


# Open-file soft-limit target for the worker fleet. The scientific import stack
# (numpy / pandas / numba / xarray) opens a few thousand files transiently, so a
# few-thousand headroom is the actual need. 65536 is comfortably above it. The
# value is deliberately bounded rather than raised to the hard cap: a very large
# limit (1048576 / unlimited) makes every subprocess spawn slow where descriptor
# closing falls back to iterating up to the limit (no readable /proc/self/fd),
# which a fleet of pytest launches turns into a stall.
_OPEN_FILES_TARGET = 65536


def _raise_open_file_limit() -> Optional[tuple]:
    """Raise this process's open-file soft limit to a bounded headroom.

    Each worker runs pytest, which imports the scientific stack and transiently
    opens many files. Under a large worker fleet the default soft RLIMIT_NOFILE
    is reached and pytest fails collection with EMFILE ("Too many open files"),
    which a mutant run records as a false kill, the source of run-to-run
    mutation-score drift at high worker counts. Workers are spawned as children
    of this process and inherit the raised limit, so the kill/survive verdict is
    reproducible at any worker count. Raises the soft limit to
    ``min(hard, _OPEN_FILES_TARGET)`` (never lowering an already-higher soft
    limit). Returns the resulting (soft, hard), or None where the platform has no
    RLIMIT_NOFILE (Windows) or the raise is refused.
    """
    try:
        import resource
    except ImportError:
        return None
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = (_OPEN_FILES_TARGET if hard == resource.RLIM_INFINITY
                  else min(hard, _OPEN_FILES_TARGET))
        if soft != resource.RLIM_INFINITY and soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        return (soft, hard)
    except (ValueError, OSError):
        return None


def _progress_snapshot(session_path: Path) -> Optional[Dict[str, int]]:
    """Live mutant counts from the session SQLite, or None on any error.

    Read-only and best-effort: a polling glitch (a transient lock, a partial
    write) must never disturb the exec, so every failure returns None."""
    try:
        with contextlib.closing(sqlite3.connect(
            f"file:{session_path}?mode=ro", uri=True, timeout=5,
        )) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM mutation_specs").fetchone()[0]
            # A per-mutant timeout is KILLED with the output "timeout".
            rows = conn.execute(
                "SELECT test_outcome, worker_outcome, "
                "lower(trim(coalesce(output, ''))) = 'timeout', COUNT(*) "
                "FROM work_results GROUP BY 1, 2, 3"
            ).fetchall()
    except Exception:
        return None
    killed = survived = timed_out = done = 0
    for test_outcome, _, clock, n in rows:
        done += n
        to = str(test_outcome).upper() if test_outcome else ""
        if to == "KILLED" and clock:
            timed_out += n
        elif to == "KILLED":
            killed += n
        elif to == "SURVIVED":
            survived += n
    return {"total": total, "done": done, "killed": killed,
            "survived": survived, "timed_out": timed_out}


def _load_avg() -> str:
    """The 1-minute load average: with one process per worker it tracks the live
    parallelism, so it is the proof the fleet is actually running in parallel.
    Empty when the platform does not expose it."""
    try:
        return f"load {os.getloadavg()[0]:.0f}"
    except (OSError, AttributeError):
        return ""


def _clone_one(project_root: Path, clone_dir: Path) -> None:
    """Copy ``project_root``'s contents into ``clone_dir``, all of them, as
    the host engine's clones are copied (a test may read the repository's
    metadata)."""
    for item in project_root.iterdir():
        dst = clone_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, symlinks=False)
        else:
            shutil.copy2(item, dst)


def _wait_for_worker(port: int, timeout: float) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


#: The start of the ``-c`` programs below. ``-c`` puts the working directory
#: first on the path, where the console script puts its own directory: it is
#: taken off again.
_PATH_START = "if sys.path and sys.path[0] == '':\n    del sys.path[0]\n"
#: The variable, after the variable prefix, that gives an HTTP fleet's secret
#: to its workers and to the exec that sends them jobs. Other users cannot
#: read a process's environment, unlike its command line.
WORKER_SECRET = "WORKER_SECRET"
#: The request header that carries the fleet's secret on each job.
SECRET_HEADER = "Worker-Secret"
#: The secret, from the variable the program's first argument names, taken
#: out of the environment so the processes the program starts do not
#: inherit it. Without it the program stops.
_SECRET = (
    "variable = sys.argv.pop(1)\n"
    "secret = os.environ.pop(variable, '')\n"
    "if not secret:\n"
    "    sys.exit(variable + ' is not set')\n")
#: Cosmic Ray's command line (the console script's ``cosmic_ray.cli:main``)
#: in a process that has an event loop.
_EVENT_LOOP_MAIN = (
    "loop = asyncio.new_event_loop()\n"
    "asyncio.set_event_loop(loop)\n"
    "from cosmic_ray.cli import main\n"
    "sys.argv[0] = 'cosmic-ray'\n"
    "try:\n"
    "    code = main()\n"
    "finally:\n"
    "    loop.close()\n"
    "sys.exit(code)\n")
#: Cosmic Ray's command line in a process that has an event loop, its
#: arguments after ``-c``.
EVENT_LOOP_COSMIC_RAY = "import asyncio, sys\n" + _PATH_START + _EVENT_LOOP_MAIN
#: The program an HTTP worker runs, the secret's variable and the port its
#: arguments: Cosmic Ray's own command line (``cosmic-ray http-worker
#: --port N``) with the server bound to the loopback address, refusing with
#: 403 a job without the fleet's secret. Cosmic Ray 8.4.1's run_worker gives
#: aiohttp no host, so a worker would listen on every interface, and on the
#: loopback address any process of the node could send it a job.
LOOPBACK_WORKER = (
    "import hmac, os, sys\n" + _PATH_START + _SECRET + (
        "from aiohttp import web\n"
        "@web.middleware\n"
        "async def _signed(request, handler):\n"
        f"    sent = request.headers.get({SECRET_HEADER!r}, '')\n"
        "    if not hmac.compare_digest(sent.encode('utf-8', 'replace'), secret.encode()):\n"
        "        raise web.HTTPForbidden()\n"
        "    return await handler(request)\n"
        "_run_app = web.run_app\n"
        "def _loopback(app, **options):\n"
        "    options['host'] = '127.0.0.1'\n"
        "    app.middlewares.append(_signed)\n"
        "    return _run_app(app, **options)\n"
        "web.run_app = _loopback\n"
        "from cosmic_ray.cli import main\n"
        "port = sys.argv[1]\n"
        "sys.argv = ['cosmic-ray', 'http-worker', '--port', port]\n"
        "sys.exit(main())\n"))
#: The program that runs Cosmic Ray's command line for an exec with the
#: HTTP distributor, the secret's variable its first argument: each job it
#: sends carries the fleet's secret, and its wait has no limit of its own.
#: Cosmic Ray 8.4.1 sends each job with aiohttp's default limit of five
#: minutes, which a longer per-mutant timeout outlasts, and the job then
#: reads as a failed request. The worker ends each job at the per-mutant
#: timeout, and the run's wall clock ends the wait for a worker that stops
#: answering.
HTTP_EXEC_COSMIC_RAY = (
    "import asyncio, os, sys\n" + _PATH_START + _SECRET + (
        "import aiohttp\n"
        "_request = aiohttp.request\n"
        "def _signed(method, url, **options):\n"
        f"    options['headers'] = {{**(options.get('headers') or {{}}), {SECRET_HEADER!r}: secret}}\n"
        "    options['timeout'] = aiohttp.ClientTimeout()\n"
        "    return _request(method, url, **options)\n"
        "aiohttp.request = _signed\n") + _EVENT_LOOP_MAIN)


def worker_command(python: str, port: int, env_prefix: str) -> List[str]:
    """The command that starts an HTTP worker on ``port`` with the
    interpreter ``python``, the one whose environment holds Cosmic Ray
    (``LOOPBACK_WORKER``). Its environment gives the fleet's secret as
    ``env_prefix`` + ``WORKER_SECRET``."""
    return [python, "-c", LOOPBACK_WORKER, env_prefix + WORKER_SECRET, str(port)]


def http_exec_command(python: str, env_prefix: str) -> List[str]:
    """The start of an exec command with the HTTP distributor
    (``HTTP_EXEC_COSMIC_RAY``), its environment as ``worker_command``'s."""
    return [python, "-c", HTTP_EXEC_COSMIC_RAY, env_prefix + WORKER_SECRET]


def _alloc_port_sockets(n: int) -> List[socket.socket]:
    socks: List[socket.socket] = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", 0))
        socks.append(s)
    return socks


def _append_http_distributor(config_path: Path, urls: List[str]) -> None:
    """Append the http distributor block to the engine-written config. The
    config arrives without a distributor block (the host omits it). The
    worker URLs are known only after this launcher allocates the ports."""
    blob = ", ".join(f"'{u}'" for u in urls)
    block = (
        "\n[cosmic-ray.distributor]\n"
        "name = 'http'\n\n"
        "[cosmic-ray.distributor.http]\n"
        f"worker-urls = [{blob}]\n"
    )
    with config_path.open("a", encoding="utf-8") as fh:
        fh.write(block)


def _apply_operator_filter(session_path: Path, config_path: Path, timeout: int) -> None:
    """Mark the mutants of the operators the config excludes SKIPPED: Cosmic
    Ray's cr-filter-operators, run by this interpreter in isolated mode from
    the session's directory, as the host runs it. A failure raises, and the run
    stops."""
    done = subprocess.run(
        [sys.executable, "-I", "-m", "cosmic_ray.tools.filters.operators_filter",
         str(session_path), str(config_path)],
        cwd=str(session_path.parent), timeout=timeout, capture_output=True,
        text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0:
        raise RuntimeError(f"cr-filter-operators exited {done.returncode}: "
                           f"{done.stderr[-2000:].strip()}")
    _log("operator filter applied")


#: The run inventory of a root, as the per-run wrappers read it: this
#: standalone runner's copy of ``langadapter.python._run_definition``'s
#: ``run_inventory``, ``write_run_inventory`` and ``remove_unlisted``.
def _entries(root: Path, keep) -> List[str]:
    entries: List[str] = []
    pending = [""]
    while pending:
        relative = pending.pop()
        with os.scandir(root / relative if relative else root) as listing:
            for entry in listing:
                name = f"{relative}/{entry.name}" if relative else entry.name
                if not relative and entry.name == ".git":
                    continue
                if any(f"./{name}/".startswith(prefix) for prefix in keep):
                    continue
                if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                    entries.append(f"./{name}")
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(name)
    return sorted(entries, key=lambda text: text.encode("utf-8", "surrogateescape"))


def _write_inventory(root: Path, path: Path, keep) -> None:
    path.write_bytes("".join(f"{entry}\n" for entry in _entries(root, keep))
                     .encode("utf-8", "surrogateescape"))


def _remove_unlisted(root: Path, path: Path, keep) -> None:
    listed = set(path.read_bytes().decode("utf-8", "surrogateescape").splitlines())
    for entry in _entries(root, keep):
        if entry not in listed:
            (root / entry[2:]).unlink(missing_ok=True)


def _clone_pythonpath(pythonpath: str, project_root: Path, clone_dir: Path,
                      import_roots=()) -> str:
    """A worker's PYTHONPATH: the clone's directories of ``import_roots`` and the
    clone first, then ``pythonpath`` with each entry inside the project root
    moved to the clone's same directory. No run of the worker sees the original
    tree, which a namespace package would join to the clone's."""
    project = Path(project_root).resolve()
    entries = [str(clone_dir / name) for name in import_roots] + [str(clone_dir)]
    for entry in pythonpath.split(os.pathsep):
        if not entry:
            continue
        try:
            relative = Path(entry).resolve().relative_to(project)
        except ValueError:
            entries.append(entry)
        else:
            entries.append(str(clone_dir / relative))
    return os.pathsep.join(dict.fromkeys(entries))


def _base_env(project_root: Path, env_extra: Dict[str, str]) -> Dict[str, str]:
    env = os.environ.copy()
    env.update(env_extra or {})
    pr = str(project_root)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{pr}{os.pathsep}{existing}" if existing else pr
    # The host engine's spec names the variables every test run has (the
    # hash seed, bytecode, the thread pools) in env_extra. The thread pools
    # are pinned here too, for a spec that names none: ``--cleanenv``
    # strips the host env at the container boundary, so what this launcher
    # sets is what reaches the test process.
    for _thread_var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                        "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        env.setdefault(_thread_var, "1")
    return env


def _session_tmpdir() -> Tuple[Path, Path]:
    """A new directory of the session's own under the system temp, which the
    session removes at its end, and inside it the TMPDIR that holds each
    run's own temporary directory (the per-run wrapper's)."""
    session_tmp_dir = Path(tempfile.mkdtemp(prefix="cr_tmp_"))
    run_tmp = session_tmp_dir / "tmp"
    run_tmp.mkdir()
    return session_tmp_dir, run_tmp


def _stop_groups(procs: List[subprocess.Popen], grace: float) -> None:
    """End each process with every process it started. Each leads a
    session of its own (``start_new_session``), so its process group holds
    the test run it has in flight, which ending the process alone would
    leave running. Every group gets SIGTERM before any is waited for, and
    SIGKILL for what remains after ``grace`` seconds. Without process
    groups (or a process id) the process alone is ended."""
    group = hasattr(os, "killpg")
    for sig in (signal.SIGTERM, getattr(signal, "SIGKILL", signal.SIGTERM)):
        for proc in procs:
            pid = getattr(proc, "pid", None)
            try:
                if group and pid is not None:
                    os.killpg(pid, sig)
                elif sig == signal.SIGTERM:
                    proc.terminate()
                else:
                    proc.kill()
            except (ProcessLookupError, PermissionError):
                pass
            except OSError:
                pass
        deadline = time.monotonic() + grace
        for proc in procs:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass


def _restore_bytes(path: Optional[Path], original: Optional[bytes],
                   stage: str) -> bool:
    """Write ``original`` back to ``path`` when the file differs: the local
    distributor applies each mutant in place, and a stopped exec cannot
    write the original back itself. False when the target could not be
    checked or restored."""
    if path is None or original is None:
        return True
    try:
        if path.read_bytes() == original:
            return True
        path.write_bytes(original)
    except OSError as exc:
        _log(f"{stage}: the target could not be checked or restored: {exc}")
        return False
    _log(f"{stage}: the target held a mutant after cosmic-ray exec ended, "
         f"so its original source is written back")
    return True


def target_original_copy(session_path: Path) -> Path:
    """The durable copy of the target's original bytes beside the host's
    session, written before a local exec and removed once the target is
    restored. A launcher that ended first leaves it for ``restore_target``."""
    return Path(str(session_path) + ".original")


def restore_target(spec: Dict[str, Any]) -> int:
    """Write the target of ``spec`` back from its durable copy
    (``target_original_copy``) when a launcher ended before it could, and
    remove the copy: 0 when the target holds its original, 1 when it could
    not be restored and the copy stays."""
    copy = target_original_copy(Path(spec["session_path"]))
    if not copy.exists():
        return 0
    target = Path(spec["project_root"]) / spec["module_path"]
    if not _restore_bytes(target, copy.read_bytes(), "restore"):
        return 1
    copy.unlink()
    return 0


def _run_cli(argv: List[str], *, cwd: Path, env: Dict[str, str],
             timeout: Optional[int]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=str(cwd), env=env, timeout=timeout,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def _wrapper_probe_argv(wrapper: Path,
                        paths: Sequence[str] = ()) -> List[str]:
    """Build a probe that exercises every enabled wrapper prerequisite and,
    given ``paths`` the runs need (the suite), checks that each is still
    visible where the run's command starts (exit 71 names the first one
    missing)."""
    if not paths:
        return ["sh", str(wrapper), "true"]
    return ["sh", str(wrapper), "sh", "-c",
            'for p; do [ -e "$p" ] || { echo "not visible to the run: $p"; '
            'exit 71; }; done', "_", *[str(p) for p in paths]]


def _probe_failure_detail(result: subprocess.CompletedProcess) -> str:
    """Return a bounded, single-line wrapper-probe diagnostic."""
    lines = []
    for stream in (result.stderr, result.stdout):
        lines.extend(str(stream or "").splitlines())
    return " | ".join(line.strip() for line in lines if line.strip())[-4000:]


class _SessionRefused(Exception):
    """The configured node-local session directory cannot be used."""


def _local_session_base(configured: str) -> "tuple[Optional[str], str]":
    """Pick the directory that holds the node-local session SQLite (a few MB),
    and say where it was chosen.

    The session must be on local disk or RAM, never the shared work directory.
    An explicit configured dir is made when missing and used, or the run is
    refused (``_SessionRefused``): it never falls back to another place.
    Without one: /dev/shm (RAM-backed on Linux, so this avoids the small
    node-local /tmp disk and home), then the system default (``tempfile``
    picks $TMPDIR or /tmp), returned as None. /dev/shm is a candidate only
    on POSIX: on Windows the string resolves to a plain disk directory
    (``C:\\dev\\shm``), which is neither RAM-backed nor node-scoped."""
    configured = (configured or "").strip()
    if configured:
        # The host's rule and text (``langadapter.python.cosmic_ray.
        # check_local_session_dir``), which this standalone script cannot
        # import: make the directory, then make and remove a directory in it, since
        # a directory with the write bit and no search bit passes a test of the
        # write bit and fails the run.
        try:
            Path(configured).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            reason = f"{type(exc).__name__}: {exc}"
        else:
            probe = os.path.join(configured, f"cr_session_check_{uuid.uuid4().hex}")
            try:
                os.mkdir(probe)
            except OSError as exc:
                reason = (f"no directory can be made in it "
                          f"({type(exc).__name__}: {exc.strerror or 'refused'})")
            else:
                try:
                    os.rmdir(probe)
                except OSError:
                    pass
                return configured, "configured cosmic_ray_local_session_dir"
        raise _SessionRefused(
            f"the configured node-local session directory {configured} "
            f"(cosmic_ray_local_session_dir, CosmicRaySettings.local_session_dir) "
            f"cannot be used: {reason}. "
            f"The run is refused rather than moved to another directory")
    if os.name == "posix":
        try:
            if Path("/dev/shm").is_dir() and os.access("/dev/shm", os.W_OK):
                return "/dev/shm", "/dev/shm"
        except OSError:
            pass
    return None, "system temporary directory"


def _snapshot_module(project_root: Path, module_path: str,
                     evidence_live: Path) -> bool:
    """Start this invocation's evidence: remove what an earlier one left
    (a local distributor's clone base outlives the invocation, and one work
    directory holds consecutive sessions), then write the module under mutation
    as Cosmic Ray reads it (its declared encoding without a byte order
    mark, universal newlines), the text its diffs apply to, as the
    baseline the capture pairs journals by. This mirrors the host's
    ``langadapter.python._mutation.read_as_cosmic_ray``, which
    this standalone script cannot import. The project can lie inside the
    container, where only this script reads it. False, logged, when the
    module cannot be read: the run goes without journals."""
    shutil.rmtree(evidence_live, ignore_errors=True)
    try:
        evidence_live.mkdir(parents=True, exist_ok=True)
        with tokenize.open(project_root / module_path) as handle:
            baseline = handle.read()
        (evidence_live / "baseline_module.txt").write_text(
            baseline, encoding="utf-8")
        return True
    except (OSError, SyntaxError, ValueError) as exc:
        _log(f"timeout-evidence baseline snapshot failed: {exc}")
        return False


#: The directory of an earlier execution's journals (``x0001``, ``x0002`` and on).
_EXECUTION = re.compile(r"x\d{4}")


def _keep_earlier_executions(evidence_base: Path) -> None:
    """Move the worker directories an earlier invocation of the session synced
    into the next numbered directory (``x0001``, ``x0002`` and on), since a
    verification batch's or an adjudication's workers write to directories of
    the same names. This mirrors the host's
    ``langadapter.python._mutation.keep_earlier_executions``,
    which this standalone script cannot import."""
    if not evidence_base.is_dir():
        return
    sub_dirs = [p for p in evidence_base.iterdir() if p.is_dir()]
    runs = [p for p in sub_dirs if not _EXECUTION.fullmatch(p.name)]
    if not runs:
        return
    archive = evidence_base / f"x{len(sub_dirs) - len(runs) + 1:04d}"
    archive.mkdir()
    for run in runs:
        run.rename(archive / run.name)


def _sync_evidence_to(evidence_live: Path, evidence_base: Path, *,
                      keep: bool) -> None:
    """Copy this invocation's journals to their host-visible place over
    the copy an earlier status write made. With ``keep`` the earlier
    executions' numbered directories stay beside them."""
    if not keep:
        if evidence_base.exists():
            shutil.rmtree(evidence_base, ignore_errors=True)
        shutil.copytree(evidence_live, evidence_base)
        return
    evidence_base.mkdir(parents=True, exist_ok=True)
    for entry in evidence_base.iterdir():
        if entry.is_dir() and _EXECUTION.fullmatch(entry.name):
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink()
    for entry in evidence_live.iterdir():
        if entry.is_dir():
            shutil.copytree(entry, evidence_base / entry.name)
        else:
            shutil.copy2(entry, evidence_base / entry.name)


def _wait_for_exec(proc: Any, start: float, wall_clock_timeout: int,
                   progress_interval: int, work_session: Path, workers: int,
                   status: Dict[str, Any]) -> int:
    """Wait for the exec ``proc`` started at ``start``, logging its progress
    every ``progress_interval`` seconds, and stop its group at the wall
    clock, marking ``status`` truncated. Its exit status, -1 when the stop
    left none."""
    last_progress = start
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        now = time.monotonic()
        if progress_interval > 0 and now - last_progress >= progress_interval:
            last_progress = now
            snap = _progress_snapshot(work_session)
            if snap is not None:
                elapsed = now - start
                rate = snap["done"] / elapsed if elapsed > 0 else 0.0
                pct = (100 * snap["done"] / snap["total"]) if snap["total"] else 0.0
                la = _load_avg()
                _log(
                    f"progress {snap['done']}/{snap['total']} mutants "
                    f"({pct:.1f}%): killed {snap['killed']}, survived "
                    f"{snap['survived']}, timeout {snap['timed_out']}, "
                    f"{elapsed:.0f}s @ {rate:.1f}/s, "
                    f"{(la + ' ') if la else ''}(workers={workers})")
        if now - start > wall_clock_timeout:
            status["truncated"] = True
            _log(f"wall_clock_timeout {wall_clock_timeout}s, terminating exec")
            _stop_groups([proc], 10)
            return proc.returncode if proc.returncode is not None else -1
        time.sleep(1.0)


def run_session(spec: Dict[str, Any]) -> int:
    cosmic_ray = list(spec["cosmic_ray_command"])
    config_path = Path(spec["config_path"])
    session_path = Path(spec["session_path"])
    project_root = Path(spec["project_root"])
    clone_base = Path(spec["clone_base"])
    test_dir = Path(spec["test_dir"]) if spec.get("test_dir") else None
    rel_test_dir = spec.get("rel_test_dir")  # POSIX str under project_root, or None
    sync_tests = bool(spec.get("sync_tests_to_clones", False))
    workers = int(spec["workers"])
    wall_clock_timeout = int(spec.get("wall_clock_timeout", 3600))
    ready_timeout = int(spec.get("ready_timeout", 60))
    init_timeout = int(spec.get("init_timeout", 600))
    run_init = bool(spec.get("run_init", True))
    run_filter = bool(spec.get("run_filter", False))
    progress_interval = int(spec.get("progress_interval", 0) or 0)
    # The prefix of the variables the measurement instrument reads.
    env_prefix = str(spec.get("env_prefix", "LANGADAPTER_"))
    src_root_env = spec.get("mutation_source_root_env",
                            f"{env_prefix}MUTATION_SOURCE_ROOT")
    import_roots = list(spec.get("import_roots", []))
    env_extra = dict(spec.get("env_extra", {}))

    global _LOG_FH
    _log_path = spec.get("runner_log_path")
    if _log_path and _LOG_FH is None:
        try:
            _LOG_FH = open(_log_path, "a", buffering=1, encoding="utf-8")
        except OSError:
            _LOG_FH = None

    _limit = _raise_open_file_limit()
    if _limit is not None:
        _log(f"open-file limit set to {_limit[0]} (hard cap {_limit[1]})")

    # The host logs the session's place, its fleet and its open-file limit
    # from this status: the launcher's own output reaches the run's record
    # only as a tail.
    http = bool(spec.get("use_http", workers > 1))
    status = {"truncated": False, "exec_returncode": None, "ready_workers": 0,
              "distributor": "http" if http else "local",
              "workers": workers if http else 1,
              "open_file_limit": list(_limit) if _limit is not None else None,
              "session_dir": None, "session_origin": None}
    # The node-local session directory, once made: every session end
    # removes it, an early one included.
    local_dirs: List[Path] = []

    def _write_status() -> None:
        # Every status write marks a session-end path, so the evidence
        # journals sync to their host-visible place here. The sync helper is
        # defined below with the evidence directories, before the first status
        # write, and the closure resolves it at call time.
        _sync_evidence()
        for directory in local_dirs:
            shutil.rmtree(directory, ignore_errors=True)
        try:
            Path(str(session_path) + ".runner_status.json").write_text(
                json.dumps(status), encoding="utf-8")
        except OSError:
            pass

    base_env = _base_env(project_root, env_extra)
    # The session's runs share one TMPDIR, as a host run's share the calling
    # process's, and keep the launcher's HOME and library caches, as a host
    # run keeps the caller's. TMPDIR lies under the system temp, not under
    # the clone: it must sit on a filesystem with POSIX unlink-open
    # semantics, because pytest's output capture truncates an
    # already-unlinked TemporaryFile there. On a 9p or drvfs clone directory
    # that raises FileNotFoundError during collection, every per-mutant run
    # exits with "no tests ran", and the whole catalogue is scored killed.
    tmp_dir, run_tmp = _session_tmpdir()
    local_dirs.append(tmp_dir)
    base_env["TMPDIR"] = str(run_tmp)
    # The journal plugin a suite without the instrument loads (``-p`` in the
    # test command) imports in every run, a worker's included.
    plugin_dir = spec.get("journal_plugin_dir")
    if plugin_dir:
        base_env["PYTHONPATH"] = f"{plugin_dir}{os.pathsep}{base_env['PYTHONPATH']}"
    clone_base.mkdir(parents=True, exist_ok=True)

    # Timeout-evidence journals stream a flushed line per test start and
    # report, so during the run they live in local RAM: on a shared network
    # file system that write pattern can slow every per-mutant run past its
    # budget. The session copies the journals to their canonical
    # host-visible place beside the config at teardown, one bulk copy,
    # where the capture pairs them with the session's timeout verdicts.
    # A run the per-mutant clock kills has already flushed its lines to
    # the live dir, so its evidence survives. Only a kill of the whole
    # session loses journals, which matches their best-effort contract.
    # A request may turn the journal off. The module stays named for the
    # per-test restore. Without a journal no evidence of an earlier
    # invocation may stay where the capture pairs it. A verification
    # batch or an adjudication of the session (``keep_evidence``) keeps
    # the journals of the invocations before it in numbered directories, and
    # the capture reads every invocation's journals in order.
    module_path = spec.get("module_path")
    mutated_module = module_path
    journal = bool(module_path) and bool(spec.get("timeout_journal", True))
    keep_evidence = bool(spec.get("keep_evidence", False))
    evidence_base = config_path.parent / "evidence"
    evidence_live = clone_base / "evidence_live"
    if journal and not _snapshot_module(project_root, module_path,
                                        evidence_live):
        module_path = None
        journal = False
    if not journal:
        shutil.rmtree(evidence_base, ignore_errors=True)
    elif keep_evidence:
        try:
            _keep_earlier_executions(evidence_base)
        except OSError as exc:
            _log(f"timeout-evidence of earlier invocations not kept: {exc}")

    def _sync_evidence() -> None:
        if not journal or not evidence_live.is_dir():
            return
        try:
            _sync_evidence_to(evidence_live, evidence_base, keep=keep_evidence)
        except OSError as exc:
            _log(f"timeout-evidence sync failed: {exc}")

    # cosmic-ray's session SQLite must live on local disk: the http exec
    # writes results to it concurrently, and on a shared network file system
    # those writes can raise "disk I/O error" or be lost, so the run records
    # 0 mutants after using its whole wall-clock budget. Do every
    # SQLite operation on a local tempdir, then copy the finished session back to
    # session_path for the host to parse. (The status + catalogue snapshot are
    # single static-file writes, fine on the shared mount.)
    host_session = session_path
    try:
        local_base, origin = _local_session_base(spec.get("local_session_dir", ""))
    except _SessionRefused as refused:
        _log(f"session SQLite refused: {refused}")
        status["session_refused"] = str(refused)
        _write_status()
        return 5
    work_session = (
        Path(tempfile.mkdtemp(prefix="cr_localsession_", dir=local_base))
        / host_session.name)
    local_dirs.append(work_session.parent)
    status["session_dir"] = str(work_session.parent)
    status["session_origin"] = origin
    _log(f"session SQLite on local disk: {work_session.parent} ({origin})")
    if not run_init and host_session.exists():
        try:
            shutil.copy2(str(host_session), str(work_session))
        except OSError as exc:
            _log(f"could not stage restored session locally: {exc}")

    if run_init:
        _log(f"init: {session_path.name}")
        try:
            ir = _run_cli(
                [*cosmic_ray, "init", str(config_path), str(work_session)],
                cwd=project_root, env=base_env, timeout=init_timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            _log(f"init failed: {type(exc).__name__}: {exc}")
            status["failed_stage"] = "initialization"
            _write_status()
            return 3
        if ir.returncode != 0:
            _log(f"init exit {ir.returncode}, stderr tail:\n{ir.stderr[-2000:]}")
            status["failed_stage"] = "initialization"
            _write_status()
            return ir.returncode
        # Snapshot the bare (post-init, pre-filter) catalogue so the host can
        # persist it to the cross-run cache. The next call restores it and
        # skips init. Matches the local path's post-init cache write.
        snap = spec.get("catalogue_snapshot_path")
        if snap:
            try:
                shutil.copy2(str(work_session), snap)
                _log("catalogue snapshot written for host cache")
            except OSError as exc:
                _log(f"snapshot copy skipped: {exc}")
    else:
        _log("init skipped (cache hit, session restored by host)")

    if run_filter:
        try:
            _apply_operator_filter(work_session, config_path, init_timeout)
        except Exception as exc:
            # A failed filter stops the run, as on the host: exec would
            # otherwise measure every mutant of the catalogue as the
            # requested operators. The host raises from the status.
            status["filter_failed"] = f"{type(exc).__name__}: {exc}"
            _log(f"operator filter failed: {status['filter_failed']}")
            _write_status()
            return 8

    # use_http selects the distributor: the http worker fleet (parallel) or
    # cosmic-ray's local distributor (sequential, single process). Default by
    # worker count when the host does not pass the flag.
    use_http = bool(spec.get("use_http", workers > 1))
    # The HTTP fleet's secret, in the environment of its workers and its
    # exec only.
    fleet_secret = {env_prefix + WORKER_SECRET: os.urandom(32).hex()}
    # The environment and directory the isolation probe runs with: a
    # worker's own (its TMPDIR decides the wrapper's branch), else the
    # local distributor's.
    probe_env: Optional[Dict[str, str]] = None
    probe_cwd: Optional[Path] = None

    if use_http:
        clone_dirs = [clone_base / f"cr_worker_{i}" for i in range(workers)]
        for d in clone_dirs:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)
            d.mkdir(parents=True)

        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
            list(pool.map(
                lambda d: _clone_one(project_root, d), clone_dirs))
        _log(f"cloned x{workers} in {time.monotonic() - t0:.1f}s")

        if sync_tests and test_dir is not None and rel_test_dir and not Path(rel_test_dir).is_absolute():
            for d in clone_dirs:
                dst = d / rel_test_dir
                try:
                    if dst.exists():
                        shutil.rmtree(dst)
                    shutil.copytree(test_dir, dst, symlinks=False)
                except OSError as exc:
                    # That clone would measure the previous suite.
                    _log(f"copying the suite into {d.name} failed: {exc}")
                    shutil.rmtree(clone_base, ignore_errors=True)
                    status["failed_stage"] = "workers"
                    _write_status()
                    return 4

        # Held until each worker starts, after the copies that can fail.
        port_socks = _alloc_port_sockets(workers)
        ports = [s.getsockname()[1] for s in port_socks]
        procs: List[subprocess.Popen] = []
        worker_err_fhs: List[Any] = []
        for clone_dir, port, sock in zip(clone_dirs, ports, port_socks):
            w_env = dict(base_env)
            w_env["PYTHONPATH"] = _clone_pythonpath(
                w_env.get("PYTHONPATH", ""), project_root, clone_dir, import_roots)
            w_env[src_root_env] = str(clone_dir)
            if module_path:
                w_env[f"{env_prefix}MUTATION_TARGET"] = str(module_path)
            if journal:
                worker_evidence = evidence_live / clone_dir.name
                w_env[f"{env_prefix}TIMEOUT_EVIDENCE_DIR"] = str(worker_evidence)
            if probe_env is None:
                probe_env, probe_cwd = dict(w_env), clone_dir
            try:
                sock.close()
            except OSError:
                pass
            # Capture each worker's stderr to a file (not DEVNULL): a worker that
            # dies on its first mutant (import error in the clone, etc.) is otherwise
            # invisible and only shows up as the whole run recording 0 mutants.
            werr = open(clone_dir / "worker.err", "wb")
            worker_err_fhs.append(werr)
            # Each run first removes what earlier runs left in the clone.
            _write_inventory(clone_dir, Path(str(clone_dir) + ".inventory"), ())
            w_env[f"{env_prefix}RUN_INVENTORY"] = str(clone_dir) + ".inventory"
            # This script runs with the project interpreter, which has Cosmic Ray.
            procs.append(subprocess.Popen(
                worker_command(sys.executable, port, env_prefix),
                stdout=subprocess.DEVNULL, stderr=werr,
                env={**w_env, **fleet_secret}, cwd=str(clone_dir), start_new_session=True))

        with ThreadPoolExecutor(max_workers=min(workers, 16)) as pool:
            ready = list(pool.map(lambda p: _wait_for_worker(p, ready_timeout), ports))
        ready_urls = [f"http://127.0.0.1:{p}" for p, ok in zip(ports, ready) if ok]
        status["ready_workers"] = len(ready_urls)
        _log(f"{len(ready_urls)}/{workers} workers ready")
        # A worker that did not become ready is stopped now, as the host
        # engine stops it, so it does not run beside the measured mutants.
        unready = [proc for proc, ok in zip(procs, ready) if not ok]
        if unready:
            _stop_groups(unready, 5)

        def _teardown() -> None:
            _sync_evidence()
            _stop_groups(procs, 5)
            time.sleep(0.3)
            for fh in worker_err_fhs:
                try:
                    fh.close()
                except OSError:
                    pass
            # On a problem run, surface a few worker stderrs before the clones are
            # removed: a worker dying on its first mutant explains 0 recorded mutants.
            if status.get("truncated") or status.get("exec_returncode") not in (0, None):
                shown = 0
                for d in clone_dirs:
                    if shown >= 3:
                        break
                    try:
                        wt = (d / "worker.err").read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        wt = ""
                    if wt.strip():
                        _log(f"worker {d.name} stderr tail:\n{wt[-1200:]}")
                        shown += 1
            for d in clone_dirs:
                shutil.rmtree(d, ignore_errors=True)
            # The clone base is per-session by construction (the engine
            # claims a unique dir under any shared configured base), so the
            # session removes its own shell. A killed session leaves it
            # behind.
            shutil.rmtree(clone_base, ignore_errors=True)

        if not ready_urls:
            _log("no workers became ready, aborting")
            _teardown()
            status["failed_stage"] = "workers"
            _write_status()
            return 4

        _append_http_distributor(config_path, ready_urls)
    else:
        # Local distributor: cosmic-ray applies each mutation in place under
        # project_root and runs the test command sequentially in one process,
        # no clones, no worker fleet, no ports, so the kill/survive verdict is
        # reproducible. Point the source-root env at project_root so the suite
        # imports the in-place-mutated source. Left unset it falls through to
        # the conftest's non-mutation fallback and every mutant survives. The
        # engine wrote a local distributor block into the config, so nothing is
        # appended here.
        base_env[src_root_env] = str(project_root)
        if spec.get("clean_root"):
            # An owned project root loses what earlier runs created there.
            root_keep = tuple(
                f"./{kept_dir.resolve().relative_to(project_root.resolve()).as_posix()}/"
                for kept_dir in (config_path.parent, clone_base)
                if project_root.resolve() in kept_dir.resolve().parents)
            root_inventory = config_path.parent / "run-inventory.txt"
            _write_inventory(project_root, root_inventory, root_keep)
            base_env[f"{env_prefix}RUN_INVENTORY"] = str(root_inventory)
            base_env[f"{env_prefix}RUN_KEEP"] = "\n".join(root_keep)
        if module_path:
            base_env[f"{env_prefix}MUTATION_TARGET"] = str(module_path)
        if journal:
            local_evidence = evidence_live / "local"
            base_env[f"{env_prefix}TIMEOUT_EVIDENCE_DIR"] = str(local_evidence)
        _log("local distributor (sequential, single process)")

        def _teardown() -> None:
            _sync_evidence()

    _exec_mode = f"{len(ready_urls)} workers" if use_http else "local distributor (1 process)"
    # The isolation probe runs the wrapper itself (the engine writes it
    # beside the session) with a no-op command, under the same environment
    # every per-mutant run gets. The wrapper exits 70 when it cannot prepare
    # a run (its cleanup, its TMPDIR directory).
    # A probe failure means every per-mutant run would die the same way, so
    # the session start aborts here instead of burning the sweep.
    _wrapper = config_path.parent / "_cr_fdlimit_run.sh"
    if probe_env is None:
        probe_env, probe_cwd = base_env, project_root
    _branch = "in its root, with a TMPDIR directory of its own"
    if _wrapper.is_file():
        _probe_detail = ""
        try:
            _probe_result = _run_cli(
                _wrapper_probe_argv(
                    _wrapper, [str(test_dir)] if test_dir is not None else []),
                cwd=probe_cwd, env=probe_env, timeout=60)
            _probe_rc = _probe_result.returncode
            _probe_detail = _probe_failure_detail(_probe_result)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _probe_rc = -1
            _probe_detail = f"{type(exc).__name__}: {exc}"
        if _probe_rc != 0:
            _log(
                f"isolation probe failed: the per-run wrapper exited "
                f"{_probe_rc} on the branch the runs would take "
                f"({_branch}). Every per-mutant run would die the same "
                f"way, so the session start is aborted.")
            if _probe_detail:
                _log(f"isolation probe diagnostic: {_probe_detail}")
            _teardown()
            status["failed_stage"] = "isolation"
            _write_status()
            return 6
        _log(f"exec: {_exec_mode}, wall_clock={wall_clock_timeout}s, "
             f"per-run isolation: wrapper probe passed ({_branch})")
    else:
        # The engine writes the wrapper beside the config before launching
        # this runner, and the test command invokes it on every run. Missing
        # here means every run would fail on a nonexistent script, so abort
        # exactly like a probe failure.
        _log(f"per-run isolation wrapper missing at {_wrapper}. The test "
             f"command invokes it on every run, so every per-mutant run "
             f"would die on a nonexistent script ({_branch}). Aborting the "
             f"session start.")
        _teardown()
        status["failed_stage"] = "isolation"
        _write_status()
        return 6
    exec_rc: Optional[int] = None
    # Redirect exec stderr to a file, not a pipe: the poll loop below never
    # drains the pipe, so a chatty cosmic-ray exec (per-mutant errors) would
    # fill the ~64KB pipe buffer and block, a hang that looks like a slow run
    # and records 0 mutants. A file never blocks and is logged on completion.
    exec_err_path = work_session.parent / "exec.stderr"
    exec_err_fh = open(exec_err_path, "wb")
    # The local distributor mutates the module in place. Keep its bytes to
    # write back after an exec that left a mutant applied.
    local_target = (project_root / mutated_module
                    if not use_http and mutated_module else None)
    try:
        original_target = (local_target.read_bytes()
                           if local_target is not None else None)
    except OSError:
        original_target = None
    original_copy = target_original_copy(session_path)
    if original_target is not None:
        # The host restores the target from it when this launcher ends first.
        try:
            original_copy.write_bytes(original_target)
        except OSError as exc:
            _log(f"keeping the target's original beside the session failed: {exc}")
    exec_start, exec_env = ((http_exec_command(sys.executable, env_prefix),
                             {**base_env, **fleet_secret})
                            if use_http else (cosmic_ray, base_env))
    try:
        proc = subprocess.Popen(
            [*exec_start, "exec", str(config_path), str(work_session)],
            stdout=subprocess.DEVNULL, stderr=exec_err_fh,
            env=exec_env, cwd=str(project_root), start_new_session=True)
    except FileNotFoundError as exc:
        _log(f"exec launch failed: {exc}")
        exec_err_fh.close()
        original_copy.unlink(missing_ok=True)
        _teardown()
        status["failed_stage"] = "exec"
        _write_status()
        return 5

    # A SIGTERM (a cancel, the host's stop) ends exec's group and restores
    # the target before the launcher exits. Only the main thread can catch it.
    def _terminated(signum, frame):
        raise SystemExit(128 + signum)

    catch = threading.current_thread() is threading.main_thread()
    previous = signal.signal(signal.SIGTERM, _terminated) if catch else None
    start = time.monotonic()
    try:
        exec_rc = _wait_for_exec(proc, start, wall_clock_timeout, progress_interval,
                                 work_session, workers, status)
    except SystemExit:
        _stop_groups([proc], 10)
        exec_err_fh.close()
        status["failed_stage"] = "exec"
        _write_status()
        raise
    finally:
        if catch:
            signal.signal(signal.SIGTERM, signal.SIG_DFL if previous is None else previous)
        if _restore_bytes(local_target, original_target, "exec"):
            original_copy.unlink(missing_ok=True)

    status["exec_returncode"] = exec_rc
    if not use_http and spec.get("clean_root"):
        _remove_unlisted(project_root, config_path.parent / "run-inventory.txt", root_keep)
    try:
        exec_err_fh.close()
    except OSError:
        pass
    _log(f"exec done in {time.monotonic() - start:.0f}s rc={exec_rc} "
         f"truncated={status['truncated']}")
    try:
        err_tail = exec_err_path.read_text(encoding="utf-8", errors="replace")
        if err_tail.strip():
            _log("exec stderr tail:\n" + err_tail[-3000:])
    except OSError:
        pass
    _teardown()
    # Copy the finished local session back to the shared path the host parses
    # (and the per-run host cache). A truncated exec still leaves partial
    # results worth reading.
    try:
        shutil.copy2(str(work_session), str(host_session))
    except OSError as exc:
        _log(f"copying session back to {host_session.name} failed: {exc}")
    shutil.rmtree(work_session.parent, ignore_errors=True)
    _write_status()
    # A non-zero exec exit (exec raised) can still leave a readable
    # session. The host reads partial results either way.
    return 0


def main(argv: List[str]) -> int:
    """Run the session of a spec file, or with ``--restore`` write its
    target back from the durable copy a stopped launcher left."""
    if len(argv) == 3 and argv[1] == "--restore":
        return restore_target(json.loads(Path(argv[2]).read_text(encoding="utf-8")))
    if len(argv) != 2:
        _log("usage: _cr_session_runner.py [--restore] <spec.json>")
        return 2
    spec = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    return run_session(spec)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
