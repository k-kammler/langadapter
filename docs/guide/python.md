# Python projects

The Python adapter runs a project's pytest suite with the project's own
interpreter, measures coverage with coverage.py and mutation with Cosmic Ray.
Select the interpreter in the context. When none is selected, the setup
check reports a candidate, as
[Environment selection](../explanation/environment-selection.md) describes.

An environment that `provision_runtime` makes for a requested Python version
lives in the directory the caller names (`store`), in a directory of its own
named after the caller's `name` and a hash of its inputs: the version, the
requirements file's resolved path and content and the tools the library
pins for that version. uv installs the requirements file from its own
directory, so a relative entry in it, such as `-e .`, names a path beside the
file. Equal inputs reuse it. Changed inputs make another environment
beside it, so a run that uses the earlier one keeps it. Earlier
environments stay, and one that no run uses may be removed.

## Test operations

`adapter.tests` is the typed entry point for `run_suite`, `collect_suite`,
`run_as_delivered`, `benchmark`, `benchmark_warmed`, `profile_suite` and
`per_test_evidence`.
It takes the requests from `langadapter.runs` and returns what the tools
observed. The adapter's own methods, such as `adapter.run_suite`, are the
language-specific operations these groups call, and can be used directly.

Python does not implement `benchmark_warmed`. An operation the adapter does
not provide still validates its request and returns an explicit
`unsupported` observation, with unknown counts and no score. An implemented operation without a runtime reports missing setup.

`adapter.operation_capabilities()` lists what is implemented without starting
a tool. Whether an operation is ready stays unknown until it is checked: the
list does not try anything. `adapter.probe_capabilities()` adds readiness by
checking what the tools need. An implemented operation can still be
unsupported for the checked project, which is different from having no
implementation. A ready test runner does not mean that every optional request
is ready: coverage and kill attribution need their own capabilities. Reading
source works even when the check of the runtime failed.

## Suite results

Suite results keep the runner's whole output, its return code and its
structured warnings, and the caller decides how much of the output to show.
Only a text beyond four million characters is cut, a bound against memory
exhaustion: it keeps the first third of those four million characters and the
last two thirds, around `[... N chars of suite output omitted ...]`. A reason that summarizes the
runner's output lines keeps all of them in `reason_detail`, and
`reason_text(lines)` shows the reason with as many as the caller chooses.
The [`SuiteRun` entry](https://k-kammler.github.io/langadapter/reference/runs/#langadapter.runs.SuiteRun) lists
the result's fields.

Coverage separates the raw traced lines from covered statements, and only the
statements count toward line coverage. coverage.py records the lines of each
test under a context name. Per-test evidence lists contexts that match no
collected test, with their lines, apart from the identified tests and never
credited to a test. Missing timings stay missing.

`collect_suite(CollectionRequest(...))` collects an explicit suite without
running its test bodies. Imports, setup files and collection hooks still run.
The result tells a collection error, no tests, a timeout and an execution
failure apart. It is not evidence that the tests pass. The exception summary
is kept apart from the output. The pytest configuration a run uses is the one
the context selects (`ProjectContext.test_framework`, see
[Settings](settings.md#test-framework-configuration)), passed to pytest
explicitly.

A file named `test_*.py` or `*_test.py` is a test file, as are `conftest.py`
and `tests.py`, whatever their case. The test file named for a source file
`calc.py` is `test_calc.py`.

`adapter.test_files(paths)` replaces each directory by the test files pytest
collects in it. `adapter.project_test_files()` gives the project's own
test files as its configuration bounds them: pytest collects from the
project's root with no file or directory named, so its `testpaths` apply. A
directory of test files that `testpaths` leaves out, such as type tests that
do not collect, is in `test_files` of the root and not in
`project_test_files`. Both list the files with collected tests first, then
those pytest could not collect.

Python runner options are `langadapter.python.project.PytestOptions`.
Benchmark requests may receive progress events after every warm-up run and
after some measured repetitions: the first, the last and about every quarter
of the run. The callbacks are outside the timed interval and do not change a
repetition's verdict.

## Generated suites

`PythonAdapter.prepare_suite(suite_root)` writes an import shim, a
`conftest.py` that makes the project importable, into an existing directory of
generated tests. It replaces that directory's `conftest.py`, so do not point
it at a setup file that people maintain. The configuration anchor, a
`pytest.ini` that fixes pytest's root and options for the generated suite, is
written only on request:

```python
prepared = adapter.prepare_suite(generated_directory, isolation_config=True)
print(prepared.outcome, prepared.reason, prepared.setup_files)
```

The import shim follows the project context, including container path
mappings. The variables through which the library talks to its own helpers
all start with `LANGADAPTER_`. Every run also sets the variables
[Environment selection](../explanation/environment-selection.md) lists. Ordinary
suite runs never prepare a suite implicitly.
`langadapter.python.project` provides the configuration lookup and the
staged copy for callers that build their own runner commands.

`PythonAdapter.run_as_delivered` runs each file of a generated suite in
its own session with the request's `session_seed`. It keeps the setup
file's import paths, leaves out a measurement helper the setup file
carries, and does not modify the suite. The request's
`timeout_s` bounds the whole run: each file runs in what is left of it,
and a run whose time is spent ends `timed_out` before the next file.
With `record_leaks` and the request's
`module_under_test` it names the tests that add, rebind or remove a name
of that module, or that change numpy's error or print settings or xarray's
options when those libraries are loaded. A read-only observer, loaded on
the command line, takes these before each test and compares them after the
test's teardown. A value changed in place, such as an item appended to a
list the module holds, is not named. The observer imports no library and
restores nothing, so a test that undoes an earlier test's change is named
too. `request_options("delivered", ...)` passes the caller's traceback and
first-failure stop, pytest's own traceback by default, and `record_leaks`.
A named `target` becomes the module under test, whether or not the record
is asked for: in a prepared project it also decides which file the run
imports, the copy's, and it is the module the leak record compares.
[Python imports in a copy](../explanation/prepared-copies.md#python-imports-in-a-copy)
says when a run is refused instead, such as under an editable install's
import hook. The request's `timeout_journal` applies as in `run_suite`.
The optional
`sources` replace the text of a listed test file for that run only, and
the observation identifies the replacement bytes. This operation refuses
`conftest.py` files in directories above the test files. Ordinary suites use
`run_suite`.

## Which test killed which mutant

`PythonAdapter.kill_tokens` reads named kills and crash observations from a
saved kill matrix: each kill's killing tests, as the run recorded them, and
the test a crash ended in. It runs no measurement and derives no score. It
reports named killers it cannot attribute uniquely, and crash observations
never become kill items.
