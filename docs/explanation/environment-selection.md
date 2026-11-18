# Environment selection

An adapter runs the project's own tools with the project's own runtime: a
Python interpreter, a JDK or, for C, the project's GCC. It never runs them
with the interpreter that runs the library unless the caller selects it.
Python's `code_smells` is no project command: it runs ruff from the
library's own environment.

## The selected runtime

`ProjectContext(root, Interpreter(path=...))` selects the runtime by its path.
For Java the path is the JDK home or its `bin/java`. An
`Interpreter(provisioning=...)` asks for a version instead. The library does
not act on such a request by itself: a caller asks the adapter's
`provision_runtime` for it (Python's makes an environment with uv, the
project's requirements and the tools the library pins for that version) and
selects the interpreter it returns. Java's provisions nothing and raises
`InvalidRequest`.

## Fixed environment variables

`ProjectContext.environment` holds the environment variables the project's
commands run with. An `Environment(values, inherit=False)` is captured, that
is, fixed: commands start from these values instead of the environment
variables of the calling process, and lookups such as a command on the `PATH`
read them. A captured context therefore behaves the same when the calling
process changes its own variables later. A Python run that starts from the
calling process's variables leaves out those pytest-xdist sets in a worker
for itself (`PYTEST_XDIST_WORKER`, `PYTEST_XDIST_WORKER_COUNT`,
`PYTEST_XDIST_TESTRUNUID`), so the project's plugins and conftest do not
take a run started inside a worker of the caller's own tests for that
worker. The context's environment can still set them. Python runs add their
own settings to these values: the project directory on `PYTHONPATH` and UTF-8
streams (`PYTHONUTF8`, `PYTHONIOENCODING`). Every run of the tests, the collection
and Cosmic Ray's runs of mutants included, sets `PYTHONHASHSEED` to 0 and
holds the numeric thread pools (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`,
`MKL_NUM_THREADS`, `NUMEXPR_NUM_THREADS`, `VECLIB_MAXIMUM_THREADS`,
`NUMBA_NUM_THREADS`) to one thread. A run as delivered sets
`PYTHONHASHSEED` to its session seed instead. Cosmic Ray's runs of mutants,
and the run of the unmutated copy before them, also set
`PYTHONDONTWRITEBYTECODE`. Each run gets a temporary directory of its own in
`TMPDIR`. A run without a bytecode store, such as a run in the project
directory itself, also compiles into a cache directory of its own
(`PYTHONPYCACHEPREFIX`), and a benchmark's repetitions share one. So a file
rewritten within the same second at the same size runs as it is written,
and the project directory gets no `__pycache__`. A prepared copy with a
bytecode store keeps its seeded caches, which the store checks against the
sources. Runs on the host keep the calling process's `HOME`, and Cosmic
Ray's runs in a container keep the container's `HOME`. There the session
makes and removes one directory that holds its runs' temporary directories.

## Python environment discovery

When no interpreter is selected, the setup check looks at the project's
`.venv` and at the virtual environments that `VIRTUAL_ENV` and `CONDA_PREFIX`
name in the context's `environment`, never at the interpreter running the
library. Only the values given to `environment` count: a variable that
commands inherit from the calling process is not looked at.

- One environment found: it is selected, and the report says so.
- Several environments: the report lists them and asks for an explicit choice.
  Two environments made from the same base Python stay two.
- None: the report asks for an interpreter or the project's `.venv`.

A virtual environment is identified by its directory: the project's `.venv` that
is also the activated environment counts once. The activated environment the
library itself runs in, outside the project (a tool's own environment), is not
one of the project's: beside the project's `.venv` the `.venv` is selected, and
alone it is used only when it is selected explicitly.
Environments in a container are not discovered.
Select the container's interpreter explicitly.

`discover_runtime` follows the same rules when the settings
[`python_executable` and `python_version`](../guide/settings.md#python-settings)
are blank. Where the report would ask for a choice, it
raises `InvalidRequest` naming `python_executable`. The interpreter question
of the setup (`setup_questions`) proposes the one environment found as its
default, and no default where the report would ask for a choice. It reads
the variables through the setup's probe. Java's takes
[`java_home`](../guide/settings.md#java-settings), else on the host the `JAVA_HOME` of the environment it is
given.

An interpreter inside the project directory is used where it is. The tool
check leaves its environment directory out of its copy and imports
tools with it without writing bytecode into it. `prepare_project` does not
leave it out by itself, so name it in
`inputs=ProjectInputSelection(exclude=(...))`.

## Setup checks

`inspect_project(language, request)`, the quick check, looks at the selected
runtime and tools without running anything in the project.
`verify_project(language, request)`, the tool check, goes further: in a copy of the project, it imports the needed tools with the
selected Python interpreter, or for Java runs the JDK's `java -version` and
`javac -version`. It runs none of the project's tests. Neither check installs
anything, and only Java's tool check downloads, when the request sets
`allow_acquisition`.

For Java, when the version commands succeed and a requested operation needs
the build, the tool check tries the build in the copy as a run does. The item
`build_resolution` reports the build's resolution, offline unless the request
sets `allow_acquisition`. `test_compilation` compiles every test source, and
`test_discovery` lists the compiled tests with JUnit without running one. Each
of JaCoCo and PIT that a requested operation needs starts once on the selected
JDK when its jars are in the tool cache, PIT only after the build resolved. A
step that needs a download first, or that fails offline for a reason the check
does not know, stays an item the first run tries, as does a step the check has
no time left for. When the project's own runs build offline (`-o` or
`--offline` in `MAVEN_ARGS` or in `.mvn/maven.config`), such a failure is the
run's too: a download the build needs is missing setup, and an offline failure
for another reason is the build's failure. When the build's launcher is a
wrapper (`mvnw`, `gradlew`), such as the project's own, which may download its
build tool before the build starts, the build and the steps after it stay
items the first run tries unless the request sets `allow_acquisition`.
Compilation and discovery report without being required, since a run compiles
and discovers only the tests it selects.

For Python, once the tools import and the requested operations need pytest,
the tool check collects the project's tests in the copy, as pytest finds them
with the run's settings, without running one, and writes no bytecode.
Its import path starts with the copy's package directories, as
[Suite profiles](../guide/timing.md#suite-profiles) describes for a
profiled run in a prepared copy, and as a run that names the module puts its
directory first. The item `test_collection` reports the collection. A test module that
does not collect shows there without failing the check, since a run collects
only the tests it selects. The item `project_origin` reports where the
tests still imported a module of the project from outside the copy. It is
required only where the module came through a finder placed ahead of the
import path, as meson-python's editable installs place theirs, since a run
that names the module is refused behind such a finder too. Another source,
such as an install of a layout this placement does not cover, is reported
without failing the check: a run that names the module checks it itself and
puts the module's directory first where another tree provides the module. When
no source file is selected, or the check has no time left for the collection
or it times out, both items are left to the first run.

A `ProjectSetupRequest` names the operations the caller will need. The report
says, for each setup item and operation, whether it is available, missing,
unsupported, tried without success or not tried yet. Every unconfirmed item
that the requested operations need carries a next step in `action`, and
`tried_by` says what first tries it: `"verification"` for the tool check,
`"run"` for a real run. `required` says whether an item counts for the
check's result. After a tool check that completed, the needed items left
unconfirmed are those that only a run tries.
