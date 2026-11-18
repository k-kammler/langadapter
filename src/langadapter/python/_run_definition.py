"""What every Python test run of a suite shares, whatever starts it: the
normal run, the coverage tool's run, the per-test evidence, the timing runs
and Cosmic Ray's origin check and per-mutant runs. The normal test run alone
decides whether a test passes. The tools run the same definition and only
measure, so a test that passes the normal run is not failed by a tool's
choice of environment."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence

#: The numeric thread pools held to one thread in every run: a float
#: reduction's order then does not depend on how many workers measure, and
#: no pool holds descriptors between tests.
THREAD_VARIABLES = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMBA_NUM_THREADS")

#: The fixed variables of every run's environment: hash seed 0, so set and
#: dict order are the same in two identical runs, and one thread per
#: numeric pool. Bytecode is no variable of the run: the normal run writes
#: it (prepared copies reuse it), and Cosmic Ray turns it off for its
#: mutants, which the source's timestamps could not tell apart.
RUN_VARIABLES: Dict[str, str] = {
    "PYTHONHASHSEED": "0",
    **dict.fromkeys(THREAD_VARIABLES, "1"),
}


def run_environment(env: Dict[str, str]) -> Dict[str, str]:
    """``env`` with the fixed variables of every run (``RUN_VARIABLES``)."""
    env.update(RUN_VARIABLES)
    return env


#: The entries the run inventory leaves out: a project's git directory, which
#: tests do not write and which would make every listing long.
INVENTORY_SKIPPED = (".git",)


def run_inventory(root: Path, keep: Sequence[str] = ()) -> List[str]:
    """The files and links below ``root`` as the per-run wrappers list them
    (``./<relative/posix/path>``, sorted by their UTF-8 bytes, as ``sort``
    orders them in the C locale): no links followed, the git directory and the
    directories ``keep`` names (relative to ``root``, ``./a/b/``) left out."""
    entries: List[str] = []
    pending = [""]
    while pending:
        relative = pending.pop()
        with os.scandir(root / relative if relative else root) as listing:
            for entry in listing:
                name = f"{relative}/{entry.name}" if relative else entry.name
                if not relative and entry.name in INVENTORY_SKIPPED:
                    continue
                if any(f"./{name}/".startswith(prefix) for prefix in keep):
                    continue
                if entry.is_symlink() or entry.is_file(follow_symlinks=False):
                    entries.append(f"./{name}")
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(name)
    return sorted(entries, key=lambda text: text.encode("utf-8", "surrogateescape"))


def write_run_inventory(root: Path, path: Path, keep: Sequence[str] = ()) -> None:
    """Write ``root``'s inventory (``run_inventory``) to ``path``, outside
    ``root``, one entry per line, for the wrappers' cleanup."""
    path.write_bytes("".join(f"{entry}\n" for entry in run_inventory(root, keep))
                     .encode("utf-8", "surrogateescape"))


def remove_unlisted(root: Path, path: Path, keep: Sequence[str] = ()) -> List[str]:
    """Remove the files and links below ``root`` that its inventory at
    ``path`` does not list (``keep`` and the git directory untouched) and return
    them, as each wrapper does before a run: the files earlier runs created."""
    listed = set(path.read_bytes().decode("utf-8", "surrogateescape").splitlines())
    removed = []
    for entry in run_inventory(root, keep):
        if entry not in listed:
            (root / entry[2:]).unlink(missing_ok=True)
            removed.append(entry)
    return removed


#: The pytest plugins every run of the adapter's purposes turns off, and the
#: mutation tool's runs with them (``PythonAdapter.request_options``).
DISABLED_PLUGINS = ("logfire", "anyio")


def pytest_arguments(config: Optional[str], rootdir: str, *, traceback: str,
                     stop_at_first_failure: bool = False,
                     disabled_plugins: Sequence[str] = ()) -> List[str]:
    """The pytest arguments of every test run, after its test files: quiet,
    the traceback, no cache provider, the plugins turned off, the
    configuration (``-c``, none for a suite directory the host cannot
    anchor), and the suite's directory as rootdir and confcutdir, so no
    configuration or conftest above the suite applies. Each kind adds
    its own plugins and report after these."""
    args = ["-x"] if stop_at_first_failure else []
    args += ["-q", f"--tb={traceback}", "-p", "no:cacheprovider"]
    for plugin in disabled_plugins:
        args += ["-p", f"no:{plugin}"]
    if config is not None:
        args += ["-c", config]
    return args + [f"--rootdir={rootdir}", f"--confcutdir={rootdir}"]


def pytest_command(python: str, flags: Sequence[str], files: Sequence[str],
                   arguments: Sequence[str]) -> List[str]:
    """Every test run's command: the interpreter with the context's flags,
    pytest, the test files in the suite's order, then ``arguments``
    (``pytest_arguments`` and what the kind adds)."""
    return [python, *flags, "-m", "pytest", *files, *arguments]
