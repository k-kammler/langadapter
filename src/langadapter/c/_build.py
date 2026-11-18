"""The project's build, recorded once, and the target's builds replayed from it.

The project's build runs once in a directory of the adapter's, with the
compiler recorder (``_recorder``) as its compiler and archiver, so the
project's own tree is never built in. From the record the adapter takes how
the build compiles the target and what links the target's object. Every
later build of the target replays that compile on a staged copy of the
source: the source as it is now, a mutant of it, or the source with coverage
counters. Kinds of build:

- ``make``: a Makefile in the project's root. The project is copied into the
  directory and ``make`` runs there, with ``CC`` and ``AR`` given to every make
  through ``MAKEFLAGS``.
- ``configure``: an autotools project. ``configure`` runs in
  a build directory of the adapter's, outside the source tree, with the
  recorder as CC, then ``make -j4`` (or ``c_build``) runs there. Such a
  build may persist in ``c_build_cache`` across adapters, and then reads a
  snapshot of the tree in its directory. Otherwise it reads the project's
  tree, which stays unwritten when the project's configure builds out of
  tree without writing into its source directory.
- ``cmake``: a ``CMakeLists.txt`` in the project's root. ``cmake``
  configures a build directory of the adapter's, outside the source tree, with
  the recorder as its C compiler and archiver, then builds it. ``c_build``
  holds further options of the configure step (``-DNAME=VALUE``). The
  sources are read where they are and nothing is written into the
  project.
- ``setuptools``: a ``setup.py`` that builds Python extensions. The project
  is copied into the directory and ``setup.py build_ext`` runs there with the
  project's interpreter, its build and library directories the adapter's.
  setuptools gives its linker the compiler it is given, so the record holds
  the extension's link too.
- ``flags``: no build file. The target compiles alone with the declared
  ``c_flags`` and links with the declared ``c_link``. C sources that
  ``c_link`` names, also as patterns (``src/*.c``), are compiled once with
  ``c_flags``, the target left out, and link as objects.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from langadapter.outcomes import Outcome

from langadapter.c._recorder import (Command, holders_of, is_link_input, read_record,
                                     write_wrapper)
from langadapter.c._toolchain import which

#: The build kinds this version supports, in the order discovery tries them.
KINDS = ("configure", "setuptools", "cmake", "make", "flags")
#: The files that make a project's root a make project.
MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
#: How long nm may read one object of the link.
NM_TIMEOUT_S = 60.0
#: The directories a copy of the project leaves out.
_NOT_COPIED = (".git", ".hg", ".svn", "__pycache__", ".venv", "venv", ".tox")
#: Compile flags a test file shares with the target: include directories,
#: definitions, the language standard, the target machine and the options that
#: change the meaning of C. The values of the first group are paths or names.
_SHARED_VALUED = ("-I", "-D", "-U", "-include", "-imacros", "-isystem", "-iquote",
                  "-idirafter")
_SHARED_F = frozenset({
    "-fsigned-char", "-funsigned-char", "-fshort-enums", "-fshort-wchar", "-fpack-struct",
    "-fms-extensions", "-fplan9-extensions", "-fgnu89-inline", "-fno-common", "-fcommon",
    "-fwrapv", "-fno-strict-aliasing", "-fno-strict-overflow",
})
#: Compile options whose value is a path, made absolute in a replay.
_PATH_VALUED = ("-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros")
#: Flags that write dependency files beside the build's outputs. A replay
#: leaves them out (the second group takes a value).
_DEPENDENCY = frozenset({"-MD", "-MMD", "-MP", "-M", "-MM", "-MG"})
_DEPENDENCY_VALUED = frozenset({"-MF", "-MT", "-MQ"})
#: The variants of a target compile.
PLAIN, COVERAGE, MULL = "plain", "coverage", "mull"
#: The flags of the coverage variant, after the build's own without their
#: optimization level. The counters update as GCC's default does.
COVERAGE_FLAGS = ("-O0", "--coverage")
#: The recorded flags Mull's variant leaves out: link-time optimization,
#: whose objects are LLVM bitcode where the project's link takes native code,
#: and the prefix maps, which rename the source in the debug information Mull
#: reads its mutants' places from.
_MULL_LEFT_OUT = ("-flto", "-ffat-lto-objects", "-ffile-prefix-map=", "-fdebug-prefix-map=")
#: The name the target's own ``main`` takes when tests are linked with it.
TARGET_MAIN = "langadapter_target_main"
#: A build's record of its compiles and links, and the build directory of
#: the configure and cmake kinds, both in the build's scratch directory.
RECORD = "record.jsonl"
BUILD_DIR = "build"


@dataclass(frozen=True)
class BuildProblem:
    """Why the target cannot be built: the outcome, the reason and the last
    lines of the build's output."""

    outcome: Outcome
    reason: str
    detail: Tuple[str, ...] = ()


@dataclass(frozen=True)
class TargetBuild:
    """How the project's build compiles and links the target.

    ``compile`` is the recorded compile of ``source`` (the target as the build
    saw it), whose source operand was ``written``. ``link_inputs`` are the
    other objects and archives of the program or library that took the
    target's object, without one that defines ``main``, and ``link_flags`` its
    libraries and library directories. ``test_flags`` are the compile flags a test
    file shares with the target. ``defines_main`` says whether the target
    defines ``main``."""

    kind: str
    scratch_dir: Path
    source: Path
    written: str
    compile: Command
    link_inputs: Tuple[Path, ...]
    link_flags: Tuple[str, ...]
    test_flags: Tuple[str, ...]
    defines_main: bool
    #: The recorded link that took the target's object.
    consumer: Optional[Command] = None
    #: For a Python extension: its module's dotted name.
    module: Optional[str] = None

    @property
    def artifact(self) -> Optional[Path]:
        """The program or library the recorded link wrote."""
        return self.consumer.output() if self.consumer is not None else None

    @property
    def operand(self) -> str:
        """The source operand of a staged compile: the build's own when it was
        relative, else the source's name."""
        return Path(self.written).name if os.path.isabs(self.written) else self.written

    def stage_dir(self, compile_dir: Path) -> Path:
        """The stage directory of a compile in ``compile_dir``, the directory
        the compile runs in: nested as many directories deep as the operand
        climbs with "..", so that its staged copy stays in ``compile_dir``."""
        depth = lowest = 0
        for part in PurePosixPath(self.operand).parts:
            depth += -1 if part == ".." else 0 if part == "." else 1
            lowest = min(lowest, depth)
        return compile_dir.joinpath("stage", *["nested"] * -lowest)

    def staged_source(self, stage: Path) -> Path:
        """Where a staged copy of the target goes in the stage directory ``stage``,
        the directory its compile runs in (``stage_dir``)."""
        return stage / self.operand

    def compile_command(self, stage: Path, output: Path, variant: str = PLAIN, *,
                        mull=None, refused: Sequence[str] = ()) -> List[str]:
        """The target's compile, run in the stage directory ``stage`` on the copy
        at ``staged_source(stage)``, into ``output``. It is the recorded
        command with its source and output replaced, its directories made
        absolute and without its dependency files. The source operand reads
        as the build wrote it, so the object and ``__FILE__`` do not name the
        stage directory: two stage directories compile one source to one object.
        ``#include "..."`` still finds the target's own directory. The coverage
        variant compiles without optimization and with gcov's counters. The
        plain one compiles without debug information. Mull's variant compiles with
        ``mull``'s clang and IR frontend, every mutant in the object, and
        without the flags ``mull_left_out`` names."""
        arguments = self._replayed_arguments(variant, refused)
        arguments += ["-iquote", str(self.source.parent)]
        if os.path.isabs(self.written):
            arguments.append(f"-fmacro-prefix-map={self.operand}={self.written}")
        if self.defines_main:
            arguments.append(f"-Dmain={TARGET_MAIN}")
        if variant == COVERAGE:
            arguments += list(COVERAGE_FLAGS)
        elif variant == MULL:
            arguments += ["-g", "-grecord-command-line", f"-fpass-plugin={mull.frontend}"]
        else:
            arguments.append("-g0")
        arguments += ["-o", str(output)]
        return [mull.clang if variant == MULL else self.compile.compiler, *arguments]

    def _replayed_arguments(self, variant: str, refused: Sequence[str]) -> List[str]:
        """The recorded compile's arguments as a replay of ``variant`` passes
        them (``compile_command``): without its output and dependency files,
        a path option's value made absolute."""
        arguments: List[str] = []
        values = iter(self.compile.arguments)
        for value in values:
            if value in ("-o",) or value in _DEPENDENCY_VALUED:
                next(values, None)
                continue
            replayed = self._replayed(value, variant, refused)
            if replayed is None:
                continue
            following = next(values, None) if value in _PATH_VALUED else None
            if following is not None:
                arguments += [value, _resolved(value, following, self.compile)]
                continue
            arguments.append(replayed)
        return arguments

    def _replayed(self, value: str, variant: str, refused: Sequence[str]) -> Optional[str]:
        """The recorded argument ``value`` as a replay of ``variant`` passes
        it, or None for one it leaves out: a dependency file's, a joined
        output, coverage's optimization and Mull's left-out flags."""
        if value in _DEPENDENCY or (value.startswith("-o") and len(value) > 2):
            return None
        if value.startswith(("-MF", "-MT", "-MQ")):
            return None
        if value == self.written:
            return self.operand
        if variant == COVERAGE and (value.startswith("-O") or value.startswith("-flto")):
            return None
        if variant == MULL and (value.startswith(_MULL_LEFT_OUT) or value in refused):
            return None
        return _joined_path(value, self.compile)

    def mull_left_out(self, refused: Sequence[str] = ()) -> Tuple[str, ...]:
        """The recorded flags Mull's variant leaves out: those it never
        takes and those clang ``refused``."""
        return tuple(value for value in self.compile.arguments
                     if value.startswith(_MULL_LEFT_OUT) or value in refused)

    def artifact_command(self, target_object: Path, output: Path, *,
                         coverage: bool, exports: Optional[Path] = None) -> List[str]:
        """The recorded link of the target's program or library, with
        ``target_object`` in place of the target's object and ``output`` as
        what it writes. Coverage links gcov's runtime and ``exports``, the
        adapter's object that exports gcov's dump and reset under its own
        names, so that a process can write the counters per test."""
        wanted = os.path.realpath(self.compile.output())
        arguments: List[str] = []
        skip = False
        recorded = list(self.consumer.arguments)
        for index, value in enumerate(recorded):
            if skip:
                skip = False
                continue
            if value == "-o" and index + 1 < len(recorded):
                skip = True
                continue
            if is_link_input(value) and os.path.realpath(self.consumer.path(value)) == wanted:
                arguments.append(str(target_object))
                continue
            arguments.append(str(self.consumer.path(value)) if is_link_input(value) else value)
        if coverage:
            arguments += ["--coverage", *([str(exports)] if exports is not None else [])]
        return [self.consumer.compiler, *arguments, "-o", str(output)]

    def program_command(self, link: Command, target_object: Path, output: Path, *,
                        coverage: bool) -> List[str]:
        """The recorded ``link`` of one of the project's programs with
        ``target_object`` ahead of its objects and archives, so that it
        defines the target's symbols before the project's archive does, the
        target's own recorded object left out, and ``output`` as what it
        writes. With ``coverage`` the link adds gcov's runtime. The link's
        dependency file is left out, since the project's build owns it."""
        wanted = os.path.realpath(self.compile.output())
        arguments: List[str] = []
        placed = False
        skip = False
        recorded = list(link.arguments)
        for index, value in enumerate(recorded):
            if skip:
                skip = False
                continue
            if value == "-o" and index + 1 < len(recorded):
                skip = True
                continue
            if value.startswith("-Wl,--dependency-file"):
                continue
            if is_link_input(value):
                path = link.path(value)
                if not placed:
                    arguments.append(str(target_object))
                    placed = True
                if os.path.realpath(path) != wanted:
                    arguments.append(str(path))
                continue
            arguments.append(value)
        if not placed:
            arguments.insert(0, str(target_object))
        if coverage:
            arguments.append("--coverage")
        return [link.compiler, *arguments, "-o", str(output)]

    def link_command(self, objects: Sequence[Path], output: Path, *,
                     coverage: bool) -> List[str]:
        """The link of a test program from ``objects`` (the test file's and the
        target's), the target's other link inputs and its libraries, and the
        C standard library's math (``-lm``), since a test may call any of the
        standard library's functions."""
        command = [self.compile.compiler, *map(str, objects), *map(str, self.link_inputs),
                   *self.link_flags, "-lm"]
        if coverage:
            command.append("--coverage")
        return command + ["-o", str(output)]


def discover_kind(root: Path, selected: str = "") -> str:
    """The build kind: the setting's value when it names a kind, else
    ``configure`` for a generated ``configure``, ``setuptools`` for a
    ``setup.py`` that builds extensions, ``cmake`` for a
    ``CMakeLists.txt``, ``make`` for a Makefile, else ``flags``."""
    if selected in KINDS:
        return selected
    if (root / "configure").is_file():
        return "configure"
    if builds_extensions(root):
        return "setuptools"
    if (root / "CMakeLists.txt").is_file():
        return "cmake"
    return "make" if any((root / name).is_file() for name in MAKEFILES) else "flags"


def builds_extensions(root: Path) -> bool:
    """Whether the project's ``setup.py`` declares a Python extension. A
    ``setup.py`` that cannot be read counts as one, so the setuptools build
    runs it and reports its own error."""
    found = setup_extensions(root)
    return found is not None and (bool(found.modules) or bool(found.problem))


@dataclass(frozen=True)
class SetupExtensions:
    """The extensions a project's ``setup.py`` declares, each its module and
    sources, or why it could not be read."""

    modules: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()
    problem: str = ""


#: The line of the reading that carries the declared extensions.
_SETUP_MARK = "langadapter-c extensions: "
#: setuptools' run_setup stops after the Distribution is made, so no command
#: runs. The reading runs in a process of its own, with the project's
#: ``setup.py`` in its directory.
_SETUP_SCRIPT = (
    "import json\n"
    "import setuptools\n"
    "from distutils.core import run_setup\n"
    "distribution = run_setup('setup.py', stop_after='init')\n"
    "print(" + repr(_SETUP_MARK) + " + json.dumps([[e.name, list(e.sources)] "
    "for e in distribution.ext_modules or ()]))\n")
#: Readings by ``setup.py``'s path, modification time and size.
_setup_readings: Dict[Tuple[str, int, int], SetupExtensions] = {}


def setup_extensions(root: Path) -> Optional[SetupExtensions]:
    """The extensions the project's ``setup.py`` declares, read once per
    version of the file with setuptools' ``run_setup(stop_after="init")``
    by this interpreter, or None for a project without one."""
    setup = root / "setup.py"
    try:
        stat = setup.stat()
    except OSError:
        return None
    key = (str(setup.resolve()), stat.st_mtime_ns, stat.st_size)
    if key not in _setup_readings:
        _setup_readings[key] = _read_setup(root)
    return _setup_readings[key]


def _read_setup(root: Path) -> SetupExtensions:
    try:
        done = subprocess.run([sys.executable, "-c", _SETUP_SCRIPT], cwd=str(root),
                              capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SetupExtensions(problem=f"setup.py could not be read: {exc}")
    marked = [line for line in (done.stdout or "").splitlines() if line.startswith(_SETUP_MARK)]
    if done.returncode != 0 or not marked:
        text = ((done.stdout or "") + (done.stderr or "")).strip()
        return SetupExtensions(problem=f"setup.py could not be read with setuptools' "
                                       f"run_setup (exit {done.returncode}): {text[-500:]}")
    return SetupExtensions(tuple(
        (name, tuple(Path(source).as_posix() for source in sources))
        for name, sources in json.loads(marked[-1][len(_SETUP_MARK):])))


Run = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class BuildRequest:
    """The build ``record_build`` records: the project at ``root``, built in
    ``scratch_dir`` (an empty directory of the caller's), for ``target`` (relative to
    ``root``), with ``compiler``, as the build ``kind``. ``run(argv, cwd=,
    env=, timeout=)`` runs a command, and every build step starts from
    ``env``, the context's environment. ``build_command``, ``flags`` and
    ``link`` are the settings c_build, c_flags and c_link. ``python`` is the
    project's interpreter, which a setuptools build runs with. A ``kept``
    configure build builds a snapshot of the tree in ``scratch_dir``, so it
    outlives the tree it was made from (a consumer's temporary copy)."""

    root: Path
    target: str
    compiler: str
    scratch_dir: Path
    run: Run
    kind: str
    env: Mapping[str, str]
    build_command: str = ""
    flags: str = ""
    link: str = ""
    python: Optional[str] = None
    kept: bool = False
    timeout_s: float = 1800.0


def record_build(request: BuildRequest) -> "TargetBuild | BuildProblem":
    """Record the build ``request`` names and return how it builds the
    target, or the problem."""
    builds = {"flags": _flags_build, "make": _make_build, "setuptools": _setuptools_build,
              "configure": _configure_build, "cmake": _cmake_build}
    if request.kind not in builds:
        return BuildProblem(Outcome.UNSUPPORTED,
                            f"the build kind {request.kind!r} is not supported; "
                            f"this version supports {', '.join(KINDS)}")
    return builds[request.kind](request)


def _flags_build(request: BuildRequest):
    root, compiler, scratch_dir, flags = request.root, request.compiler, request.scratch_dir, request.flags
    run, base, timeout_s = request.run, request.env, request.timeout_s
    source = root / request.target
    arguments = (*shlex.split(flags), "-c", str(source), "-o", str(scratch_dir / "target.o"))
    command = Command(str(root), compiler, arguments, 0)
    words, sources = _link_sources(shlex.split(request.link), root, source)
    if isinstance(words, BuildProblem):
        return words
    objects: List[Path] = []
    for index, path in enumerate(sources):
        output = scratch_dir / "sources" / f"{index}-{path.stem}.o"
        output.parent.mkdir(parents=True, exist_ok=True)
        argv = [compiler, *shlex.split(flags), "-c", str(path), "-o", str(output)]
        try:
            done = run(argv, cwd=str(root), env=dict(base), timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return BuildProblem(Outcome.TIMED_OUT, f"compiling {path.relative_to(root)} "
                                f"reached its timeout of {timeout_s:g} s")
        if done.returncode != 0:
            text = ((done.stdout or "") + (done.stderr or "")).splitlines()
            return BuildProblem(Outcome.INFRASTRUCTURE_FAILURE,
                                f"{path.relative_to(root)}, which c_link names, does not "
                                f"compile with c_flags (exit {done.returncode})",
                                tuple(text[-20:]))
        objects.append(output)
    inputs, link_flags = _split_link(words, root)
    return TargetBuild("flags", scratch_dir, source, str(source), command,
                       (*inputs, *objects), link_flags, _test_flags(command),
                       _defines_main(source))


def _link_sources(words: Sequence[str], root: Path, target: Path):
    """The words of ``c_link`` other than its C sources, and those sources in
    order (each pattern's matches sorted, the target left out). For a pattern
    that names no file, the problem takes the words' place and the sources
    are empty."""
    kept: List[str] = []
    sources: List[Path] = []
    wanted = os.path.realpath(target)
    for word in words:
        if not word.endswith(".c"):
            kept.append(word)
            continue
        matches = sorted(root.glob(word)) if any(c in word for c in "*?[") else [root / word]
        matches = [path for path in matches if path.is_file()]
        if not matches:
            return BuildProblem(Outcome.MISSING_SETUP,
                                f"c_link names {word}, which matches no C source in the "
                                "project"), ()
        sources += [path for path in matches if os.path.realpath(path) != wanted
                    and path not in sources]
    return kept, tuple(sources)


def _make_build(request: BuildRequest):
    root, target, compiler, scratch_dir = request.root, request.target, request.compiler, request.scratch_dir
    run, base, build_command = request.run, request.env, request.build_command
    project = scratch_dir / "project"
    shutil.copytree(root, project, symlinks=True,
                    ignore=shutil.ignore_patterns(*_NOT_COPIED))
    record = scratch_dir / RECORD
    cc = write_wrapper(scratch_dir / "cc", record, compiler)
    archiver = which("ar", base)
    ar = write_wrapper(scratch_dir / "ar", record, archiver) if archiver else None
    env = dict(base)
    overrides = [f"CC={cc}"] + ([f"AR={ar}"] if ar else [])
    env["MAKEFLAGS"] = " ".join(overrides + ([env["MAKEFLAGS"]] if env.get("MAKEFLAGS") else []))
    env["CC"] = str(cc)
    argv = shlex.split(build_command) if build_command else ["make"]
    problem = _step(run, argv, str(project), env, request.timeout_s, "the build")
    if problem is not None:
        return problem
    return locate(read_record(record), project / target, "make", scratch_dir, run)


def _step(run, argv, cwd: str, env, timeout_s, label: str) -> Optional[BuildProblem]:
    """None when the build step ``argv`` succeeded, else why not: its timeout,
    or its exit status with its output's last lines. ``label`` names it in
    the reason ("the build", "the build step")."""
    try:
        done = run(argv, cwd=cwd, env=env, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return BuildProblem(Outcome.TIMED_OUT,
                            f"{label} {' '.join(argv)} reached its timeout of {timeout_s:g} s")
    if done.returncode != 0:
        output = ((done.stdout or "") + (done.stderr or "")).splitlines()
        return BuildProblem(Outcome.INFRASTRUCTURE_FAILURE,
                            f"{label} {' '.join(argv)} failed (exit {done.returncode})",
                            tuple(output[-20:]))
    return None


def _setuptools_build(request: BuildRequest):
    root, target, compiler, scratch_dir = request.root, request.target, request.compiler, request.scratch_dir
    run, base, python = request.run, request.env, request.python
    build_command = request.build_command
    if not python:
        return BuildProblem(Outcome.MISSING_SETUP, "a setuptools build needs the project's "
                            "Python, and the context names none")
    project = scratch_dir / "project"
    shutil.copytree(root, project, symlinks=True,
                    ignore=shutil.ignore_patterns(*_NOT_COPIED))
    record = scratch_dir / RECORD
    cc = write_wrapper(scratch_dir / "cc", record, compiler)
    library = scratch_dir / "lib"
    env = dict(base)
    # setuptools links with the compiler it is given (LDSHARED follows CC).
    env["CC"] = str(cc)
    env.pop("LDSHARED", None)
    argv = shlex.split(build_command) if build_command else [
        python, "setup.py", "build_ext", "--build-temp", str(scratch_dir / "temp"),
        "--build-lib", str(library)]
    problem = _step(run, argv, str(project), env, request.timeout_s, "the build")
    if problem is not None:
        return problem
    located = locate(read_record(record), project / target, "setuptools", scratch_dir, run)
    if isinstance(located, BuildProblem):
        return located
    artifact = located.artifact
    if artifact is None or not artifact.name.endswith(".so"):
        return BuildProblem(Outcome.INFRASTRUCTURE_FAILURE,
                            f"the build did not link {Path(target).name} into an extension")
    try:
        parts = artifact.resolve().relative_to(library.resolve()).parts
    except ValueError:
        parts = (artifact.name,)
    module = ".".join((*parts[:-1], parts[-1].split(".")[0]))
    return replace(located, module=module)


#: The parallel jobs of a configure build's make, unless ``c_build`` says.
CONFIGURE_JOBS = 4
#: The file that marks a configure build as complete, for its reuse.
BUILT = "built"


def source_fingerprint(root: Path) -> str:
    """A digest of the build inputs of the tree at ``root``: its C sources and
    headers, its configure, Makefile and Setup files, by their paths in the
    tree and their contents. Two copies of one tree have the same."""
    digest = hashlib.sha256()
    for path in sorted(_build_inputs(root)):
        digest.update(path.relative_to(root).as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


#: What the fingerprint of a configure build reads.
_INPUT_SUFFIXES = frozenset({".c", ".h", ".in", ".ac", ".m4"})
_INPUT_NAMES = ("configure", "Makefile", "Setup", "setup.py")
#: Never part of a tree's build: its history and bytecode. A directory named
#: build may hold build scripts (CPython's Tools/build).
_SKIPPED = frozenset({".git", "__pycache__"})


def _build_inputs(root: Path):
    for walked_dir, names, files in os.walk(root):
        names[:] = [name for name in names if name not in _SKIPPED]
        for name in files:
            if Path(name).suffix in _INPUT_SUFFIXES or name.startswith(_INPUT_NAMES):
                path = Path(walked_dir) / name
                if path.is_file() and not path.is_symlink():
                    yield path


def _configure_build(request: BuildRequest):
    import fcntl

    with open(request.scratch_dir / ".lock", "w") as lock:
        # Copies of one tree share a kept build: one builds it, the others wait.
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _configure_build_locked(request)


def _configure_build_locked(request: BuildRequest):
    root, scratch_dir, run = request.root, request.scratch_dir, request.run
    build_command = request.build_command
    build = scratch_dir / BUILD_DIR
    record = scratch_dir / RECORD
    if request.kept:
        source = scratch_dir / "source"
        if not source.is_dir():
            # Named source once whole: an interrupted copy is made again.
            partial = scratch_dir / "source.partial"
            shutil.rmtree(partial, ignore_errors=True)
            shutil.copytree(root, partial, symlinks=True,
                            ignore=shutil.ignore_patterns(*_SKIPPED))
            partial.rename(source)
        root = source
    if not (scratch_dir / BUILT).is_file():
        build.mkdir(parents=True, exist_ok=True)
        cc = write_wrapper(scratch_dir / "cc", record, request.compiler)
        env = dict(request.env)
        env["CC"] = str(cc)
        configure = root / "configure"
        first = [str(configure)] if os.access(configure, os.X_OK) else ["sh", str(configure)]
        for argv in (first,
                     shlex.split(build_command) if build_command
                     else ["make", f"-j{CONFIGURE_JOBS}"]):
            problem = _step(run, argv, str(build), env, request.timeout_s, "the build step")
            if problem is not None:
                return problem
        (scratch_dir / BUILT).write_text("", encoding="utf-8")
    return locate(read_record(record), root / request.target, "configure", scratch_dir, run)


def _cmake_build(request: BuildRequest):
    root, compiler, scratch_dir, run = request.root, request.compiler, request.scratch_dir, request.run
    base, options = request.env, request.build_command
    cmake = which("cmake", base)
    if cmake is None:
        return BuildProblem(Outcome.MISSING_SETUP, "a CMake project needs cmake on PATH")
    build = scratch_dir / BUILD_DIR
    record = scratch_dir / RECORD
    cc = write_wrapper(scratch_dir / "cc", record, compiler)
    archiver = which("ar", base)
    ar = write_wrapper(scratch_dir / "ar", record, archiver) if archiver else None
    env = dict(base)
    env["CC"] = str(cc)
    steps = ([cmake, "-S", str(root), "-B", str(build), f"-DCMAKE_C_COMPILER={cc}",
              *([f"-DCMAKE_AR={ar}"] if ar else []), *shlex.split(options)],
             [cmake, "--build", str(build), "-j", str(CONFIGURE_JOBS)])
    for argv in steps:
        problem = _step(run, argv, str(scratch_dir), env, request.timeout_s, "the build step")
        if problem is not None:
            return problem
    return locate(read_record(record), root / request.target, "cmake", scratch_dir, run)


def locate(commands: Sequence[Command], source: Path, kind: str, scratch_dir: Path,
           run: Run) -> "TargetBuild | BuildProblem":
    """How the recorded ``commands`` compile ``source`` and link its object.
    ``run`` runs ``nm`` on the link's objects."""
    wanted = os.path.realpath(source)
    compiles = [command for command in commands
                if command.returncode == 0 and command.compiles
                and any(os.path.realpath(path) == wanted for path in command.sources())]
    if not compiles:
        return BuildProblem(Outcome.INFRASTRUCTURE_FAILURE,
                            f"the build did not compile {source.name}: no recorded compile "
                            "names it")
    linked = [(compile_, _consumer(commands, compile_.output())) for compile_ in compiles]
    compile_, consumer = next(((c, k) for c, k in linked if k is not None), linked[0])
    written = _source_operand(compile_, wanted)
    inputs, link_flags = (_link_inputs(consumer, compile_, run) if consumer is not None
                          else ((), ()))
    return TargetBuild(kind, scratch_dir, source, written, compile_, inputs, link_flags,
                       _test_flags(compile_), _defines_main(source),
                       consumer if consumer is not None and consumer.compiler else None)


def _source_operand(compile_: Command, wanted: str) -> str:
    """The operand by which the recorded ``compile_`` names the source at the
    real path ``wanted``."""
    return next(value for value in compile_.arguments
                if value.endswith(".c") and os.path.realpath(compile_.path(value)) == wanted)


def _link_inputs(consumer: Command, compile_: Command,
                 run: Run) -> Tuple[Tuple[Path, ...], Tuple[str, ...]]:
    """The objects and archives the recorded link ``consumer`` takes beside
    the object of ``compile_``, without one that defines ``main``, and its
    libraries and library directories. ``run`` runs ``nm`` on the objects."""
    objects, link_flags = _split_link(list(consumer.arguments), Path(consumer.cwd))
    target_object = os.path.realpath(compile_.output())
    inputs = tuple(path for path in objects
                   if os.path.realpath(path) != target_object
                   and not _object_has_main(path, run))
    return inputs, link_flags


def _consumer(commands: Sequence[Command], output: Optional[Path]) -> Optional[Command]:
    """The first successful link that takes ``output``, directly or through an
    archive a recorded ``ar`` put it in. Without a recorded link that takes
    the archive, it is the archive's own command. It is None without
    ``output`` and when nothing takes it."""
    if output is None:
        return None
    wanted = holders_of(commands, output)
    archive_command = None
    for command in commands:
        if command.returncode != 0:
            continue
        if command.archive is not None:
            if os.path.realpath(command.archive) in wanted:
                archive_command = command
            continue
        if command.compiles:
            continue
        if any(os.path.realpath(path) in wanted for path in command.inputs()):
            return command
    if archive_command is not None:
        return Command(archive_command.cwd, "", (str(archive_command.archive),), 0)
    return None


def _split_link(arguments: Sequence[str], cwd: Path) -> Tuple[Tuple[Path, ...], Tuple[str, ...]]:
    """A link's objects and archives (resolved against ``cwd``) and its
    libraries, library directories, threads and run paths. Other options, the
    output and ``-shared`` are left out."""
    objects: List[Path] = []
    flags: List[str] = []
    skip = False
    for index, value in enumerate(arguments):
        if skip:
            skip = False
            continue
        if value == "-o":
            skip = True
            continue
        if value in ("-l", "-L") and index + 1 < len(arguments):
            flags += [value, arguments[index + 1]]
            skip = True
            continue
        if value.startswith(("-l", "-L")) or value == "-pthread" or value.startswith("-Wl,-rpath"):
            flags.append(value)
            continue
        if value.startswith("-"):
            continue
        path = Path(value) if os.path.isabs(value) else cwd / value
        if is_link_input(value):
            objects.append(path)
    return tuple(objects), tuple(flags)


def _test_flags(command: Command) -> Tuple[str, ...]:
    """The flags of the target's compile that a test file shares with it,
    with relative directories resolved against the compile's directory."""
    flags: List[str] = []
    arguments = list(command.arguments)
    skip = False
    for index, value in enumerate(arguments):
        if skip:
            skip = False
            continue
        joined = next((option for option in _SHARED_VALUED
                       if value.startswith(option) and value != option), None)
        if value in _SHARED_VALUED and index + 1 < len(arguments):
            flags += [value, _resolved(value, arguments[index + 1], command)]
            skip = True
        elif joined is not None and joined in ("-I", "-isystem", "-iquote", "-idirafter"):
            flags += [joined, _resolved(joined, value[len(joined):], command)]
        elif (joined is not None or value.startswith("-std=") or value.startswith("-m")
              or value == "-pthread" or value in _SHARED_F):
            flags.append(value)
    return tuple(flags)


def _joined_path(value: str, command: Command) -> str:
    """``value`` with the path a path option joined to it (``-Idir``)
    made absolute, else ``value`` itself."""
    joined = next((option for option in _PATH_VALUED
                   if value.startswith(option) and value != option), None)
    if joined is None:
        return value
    return joined + _resolved(joined, value[len(joined):], command)


def _resolved(option: str, value: str, command: Command) -> str:
    """``option``'s ``value`` for a compile in another directory: a directory made
    absolute against ``command``'s directory, and the file ``-include`` or
    ``-imacros`` names only when it is there. GCC looks for that file in
    the working directory first, then along the include path."""
    if option not in _PATH_VALUED:
        return value
    path = command.path(value)
    if option in ("-include", "-imacros") and not path.is_file():
        return value
    return str(path)


def _defines_main(source: Path) -> bool:
    """Whether ``source`` defines a top-level ``main`` function."""
    from langadapter.c._syntax import defines_function

    try:
        return defines_function(source.read_bytes(), "main")
    except OSError:
        return False


def _object_has_main(path: Path, run: Run) -> bool:
    """Whether the object file at ``path`` defines ``main`` (``nm``, found on
    the context's PATH). An archive or a shared library is kept, since a
    link takes only the members it needs, and so is an object ``nm`` cannot
    read."""
    if path.suffix != ".o":
        return False
    try:
        done = run(["nm", "-P", "--defined-only", str(path)], timeout=NM_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(line.split()[:2] in (["main", "T"], ["main", "t"])
               for line in (done.stdout or "").splitlines() if line.strip())


def declared_module(root: Path, target: str) -> Optional[str]:
    """The module of the extension ``setup.py`` declares with ``target`` among
    its sources (``setup_extensions``), or None. The run takes the module
    from the build itself. This is the hint a first test is given."""
    found = setup_extensions(root)
    wanted = Path(target).as_posix()
    return next((name for name, sources in (found.modules if found else ())
                 if wanted in sources), None)


def extension_modules(root: Path) -> Dict[str, str]:
    """Each Python extension module the project builds from one C file, with
    that file's path in the project: ``setup.py``'s ``Extension`` entries."""
    found: Dict[str, str] = {}
    declared = setup_extensions(root)
    for name, sources in (declared.modules if declared else ()):
        c_sources = sorted({source for source in sources if source.endswith(".c")})
        if len(c_sources) == 1 and len(set(sources)) == 1:
            found[name] = c_sources[0]
    return found
