# Settings

LangAdapter reads no configuration file of its own. A run is configured by the objects
you pass to it, by the project settings of its language and by a few
environment variables. The API reference
describes the request objects, such as `SuiteRunRequest`, field by field.

## Project settings

Each adapter declares the settings a project of its language takes
(`project_settings`), each with its purpose, kind and default. A program
that uses LangAdapter asks for them, for example with the adapter's
`setup_questions`, and passes the values to two class methods before it
builds the project context. `discover_runtime` selects the runtime, its
interpreter flags and the test framework configuration (`RuntimeChoice`),
and `context_environment` gives the variables of the context's
`environment`. An empty value leaves a setting to the adapter's discovery.
[C projects](c.md#settings) lists the C adapter's settings.

### Python settings

| Setting | What |
| --- | --- |
| `python_executable` | the project's Python interpreter. Empty: the environment uv provisions for `python_version`, else the project's only environment, as [Environment selection](../explanation/environment-selection.md) describes |
| `python_version` | a Python version, such as `3.12`, for which uv provisions an environment when `python_executable` is empty |
| `requirements` | the requirements file of the environment that `python_version` provisions |
| `python_import_root` | the directory below the project that its package is imported from, such as `src`, given to the runs as `LANGADAPTER_PYTHON_IMPORT_ROOT`. Empty: the project directory |
| `python_test_environment` | variables every process of the adapter's runs gets, as `NAME=value` words, such as `PURE_PYTHON=1` |
| `frozen_module_strategy` | `unfreeze`, the default, runs the project's Python with `-X frozen_modules=off`, so coverage and mutation treat the standard library's frozen modules as ordinary files. `skip` leaves them frozen |
| `pytest_config` | `auto`, the default, uses the project's own pytest configuration. A path selects a configuration file, and `none` runs pytest with its defaults, as [Test framework configuration](#test-framework-configuration) describes |

### Java settings

| Setting | What |
| --- | --- |
| `java_home` | the project's JDK: its home or its `java` executable. Empty: `JAVA_HOME`, except in a container, where it must be named |
| `build_java_home` | the JDK the build tool runs on when it needs a newer Java than the project's, such as Gradle 9, which runs on Java 17 or newer. Empty: `java_home`. The runs get it as `LANGADAPTER_BUILD_JAVA_HOME` |
| `maven` | the Maven launcher, when the project has no Maven wrapper. Empty: `mvn` on the path. The runs get it as `LANGADAPTER_MAVEN` |
| `gradle` | the Gradle launcher, when the project has no Gradle wrapper. Empty: `gradle` on the path. The runs get it as `LANGADAPTER_GRADLE` |

The adapters' `project_settings` entries in the
[Python](https://k-kammler.github.io/langadapter/reference/python/#langadapter.python.adapter.PythonAdapter.project_settings)
and [Java](https://k-kammler.github.io/langadapter/reference/java/#langadapter.java.adapter.JavaAdapter.project_settings)
references list the same settings.

## Environment variables

### Java projects

Set these in `ProjectContext.environment`, which is where the Java adapter
looks for them. `LANGADAPTER_JAVA_TOOLS` and `LANGADAPTER_JAVA_TOOLS_OFFLINE`
are also read from the calling process's environment when the context lacks
them, unless its environment is captured
(`Environment(..., inherit=False)`). A captured environment's default cache
lies below its own `HOME`, or `USERPROFILE` on Windows. In a container they
are read on the host, where the tool cache stays: from
`ContainerExecution.control_environment` when it is set, else from the
calling process's environment, never from the context's environment.

| Variable | Default | Effect |
| --- | --- | --- |
| `LANGADAPTER_MAVEN` | `mvn` on the path | The Maven launcher, for a Maven project without its own `mvnw` wrapper. |
| `LANGADAPTER_GRADLE` | `gradle` on the path | The Gradle launcher, for a Gradle project without its own `gradlew` wrapper. |
| `LANGADAPTER_BUILD_JAVA_HOME` | The interpreter's JDK | The JDK that runs the project's build, when the build needs another JDK than the one that runs the tests. |
| `LANGADAPTER_JAVA_TOOLS` | `~/.cache/langadapter/java` | The tool cache for the jars LangAdapter downloads: the JUnit launcher, JaCoCo and PIT. |
| `LANGADAPTER_JAVA_TOOLS_OFFLINE` | Unset | `1`, `true`, `yes` or `on` forbids downloads. A jar missing from the cache is then unavailable, and a setup check reports it as missing setup. |

### C projects

The C adapter reads its settings from `ProjectContext.environment`, as the
variables `LANGADAPTER_C_TESTS`, `LANGADAPTER_C_COMPILER` and the others
that [C projects](c.md#settings) lists with each setting.

### Python projects

| Variable | Default | Effect |
| --- | --- | --- |
| `LANGADAPTER_PYTHON_IMPORT_ROOT` | Unset: the project directory | The directory below the project root that the project's package is imported from, such as `src`. The adapter reads it from `ProjectContext.environment`, not from the calling process's environment. |

### Diagnostics of Python runs

These change only what a run records about itself, never a verdict. They are
read from the calling process's environment, which the runs LangAdapter
starts for Python tests inherit. The two descriptor watchers need Linux's
`/proc` and write a histogram of what the open descriptors point to, as
`fdleak_<pid>.txt`.

| Variable | Default | Effect |
| --- | --- | --- |
| `LANGADAPTER_MUTATION_DIAG_TB` | `no` | pytest's `--tb` level for every mutant's test run in a Cosmic Ray session. `short` or `line` keeps a failing test's traceback in the run's output, which `CosmicRaySettings.keep_output` stores. |
| `LANGADAPTER_FDLIMIT_DEBUG` | Unset | Any value makes each mutant's run on a POSIX host print its open-file limits before and after they are raised. In a suite that `prepare_suite` set up, it also starts a watcher thread in the test process, which writes its file once the process holds `LANGADAPTER_FDLEAK_THRESHOLD` descriptors. |
| `LANGADAPTER_FDLEAK_THRESHOLD` | `3000` | The descriptor count at which the watcher thread of `LANGADAPTER_FDLIMIT_DEBUG` writes its file. |
| `LANGADAPTER_FDLEAK_WATCH` | Unset | Any value makes each mutant's run on a POSIX host start a watcher outside the interpreter, which writes its file once the run holds more than 3000 descriptors. |
| `LANGADAPTER_FDLEAK_DIR` | `LANGADAPTER_PROJECT_WORKZONE`, else the run's directory | Where both watchers write their files. Unset, they write to the directory `LANGADAPTER_PROJECT_WORKZONE` names when it is set. In a Cosmic Ray run the run's directory is a worker's copy of the project, whose next run removes the files earlier runs left there, so set it to keep the files. |

## Mutation sessions

`adapter.mutations.open_session(engine, **options)` opens a session. The
options are:

| Option | Default | Effect |
| --- | --- | --- |
| `workers` | `1` | How many mutants run at once. |
| `distributor` | `auto` (Cosmic Ray only) | `local` runs one mutant at a time in one process, `http` runs a fleet of worker processes with a copy of the project each, and `auto` picks `local` for one worker and `http` for more. An explicit `http` starts as many workers as `workers` names, one included. `local` with more than one worker is refused, never run with one. |
| `allow_fewer_workers` | `False` (Cosmic Ray only) | Lets a session open when fewer workers than requested start, instead of refusing it. |
| `settings` | `CosmicRaySettings()` (Cosmic Ray only) | The engine's own settings, below. |

A PIT session takes `workers` only.

### Cosmic Ray settings

`langadapter.python.cosmic_ray.CosmicRaySettings` holds the settings of the
Cosmic Ray engine. Every field has a default, and its
[reference entry](https://k-kammler.github.io/langadapter/reference/python/#langadapter.python.cosmic_ray.CosmicRaySettings)
lists the same fields.

| Field | Default | Effect |
| --- | --- | --- |
| `init_timeout_s` | `600` | Seconds `cosmic-ray init` may take to build the mutant catalogue. |
| `worker_ready_timeout_s` | `60` | Seconds to wait for the HTTP workers to come up. |
| `catalogue_cache_dir` | Unset | An absolute directory for a cache of mutant catalogues, keyed by the target's content, so a run of an unchanged target skips `cosmic-ray init`. Unset: no cache. |
| `cache_max_entries` | `200` | The most catalogues the cache keeps. The oldest go first. |
| `cache_max_age_days` | `0` | Catalogues older than this many days are removed. `0` keeps them regardless of age. |
| `clone_dir` | Node-local storage | Where the HTTP workers' copies of the project go. Unset: `/dev/shm` (on POSIX), else the system's temporary directory, each only when it is writable with at least 1 GiB free, else the directory the caller runs in. |
| `clone_sweep_hours` | `12` | Worker copies and per-run directories that a killed run left behind are removed at a session's start once they are older than this. `0` turns the sweep off. |
| `session_dir` | A new temporary directory | The directory of a local run's session database. Cosmic Ray writes the database once per mutant, so keep it on local storage, never on a network file system. |
| `local_session_dir` | Unset | The node-local directory for the live session database. A container run keeps it there, else in memory-backed storage, else in the temporary directory. A local run without `session_dir` makes its own directory below it, or in the temporary directory when this is unset. A directory that cannot be made, or in which no directory can be made, refuses the run: the database never moves elsewhere. |
| `progress_interval_s` | `0` | Seconds between progress lines in the log. `0` writes none. |
| `per_file_adjudication` | `True` | Re-measures, file by file, the kills whose verification left them unresolved. |
| `keep_output` | `False` | Keeps each mutant's test output in its kill-matrix row. The output can be large. |
| `verbose` | `False` | Runs Cosmic Ray's execution with its informational log, for runs on the host. |
| `log` | Unset | A function that receives the engine's log lines, as `log(message, level)`. |

## Test runs

### pytest options

`SuiteRunRequest.runner_options` takes a
[`langadapter.python.project.PytestOptions`](https://k-kammler.github.io/langadapter/reference/python/#langadapter.python.project.PytestOptions)
for a Python suite:

| Field | Default | Effect |
| --- | --- | --- |
| `traceback` | `short` | pytest's `--tb` style. |
| `stop_at_first_failure` | `False` | Stops the run at the first failing test (pytest's `-x`). |
| `disabled_plugins` | `()` | pytest plugins to turn off (pytest's `-p no:<name>`). |
| `leave_out_timing_plugins` | `False` | Leaves out the plugins that change a run's time where the project's pytest loads them: pytest-cov's tracer (`--no-cov`), pytest-xdist's workers (`-n 0`) and pytest-benchmark's measurement (`--benchmark-disable`). The project's own options for them stay valid, a benchmark test runs its function once and saves no data, and a project whose `addopts` hold `--benchmark-only` is refused by pytest-benchmark. |

pytest-benchmark looks up the project's version control at every pytest
start, before `--benchmark-disable` applies, so each run, probe and
collection pays that time. Leave the plugin out of the project's
environment where an operation's time matters.

### Test framework configuration

`ProjectContext.test_framework` takes a
[`TestFrameworkConfig`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.TestFrameworkConfig):

| Field | Default | Effect |
| --- | --- | --- |
| `source` | `project` | `project` runs the test framework with the project's own configuration, found in the framework's own order. `isolated` runs it with the framework's defaults. |
| `path` | Unset | With `source="project"`, the configuration file to use instead of the one the framework would find. |

The Java adapter takes only the default, the project's own configuration:
`isolated` or a `path` makes its operations `unsupported`.
