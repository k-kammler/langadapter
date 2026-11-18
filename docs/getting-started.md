# Getting started

## Install

Install the library into the Python environment of the program that uses
it, as the [home page](https://k-kammler.github.io/langadapter/#install) shows.

The project under test keeps its own runtime and tools. A Python project needs
an interpreter with pytest, plus coverage.py for coverage and Cosmic Ray for
mutation, such as `python -m pip install pytest coverage cosmic-ray` in the
project's environment, each pinned as `name==version` to the version
[Tested environments](reference/tested-environments.md) names. A Java project
needs a JDK and the project's Maven or Gradle build (its wrapper or the tool
on the `PATH`). A C project needs what [C projects](guide/c.md#what-it-needs)
lists. On Ubuntu 24.04 on x86_64, also in WSL,
`langadapter toolchains python java c` installs these tools, as
[Install the toolchains](guide/toolchains.md) describes. The library
downloads the JUnit launcher, JaCoCo and PIT into its cache on first use,
unless `LANGADAPTER_JAVA_TOOLS_OFFLINE` is set to 1, as
[Java projects](guide/java.md) describes.

## Run a suite

The examples are in the `examples` directory of the
[repository](https://github.com/k-kammler/langadapter/tree/v0.1.0/examples). A pip installation also puts them in
`share/doc/langadapter/examples` inside the environment, the directory that
`python -c "import sys; print(sys.prefix)"` prints. Run the commands below
from the directory that holds `examples`, or give the examples' full path.
Besides `python_suite.py`, which this section runs, they are
`python_mutation.py` ([Mutation sessions](guide/mutation-sessions.md)),
`metric_provider.py` ([A metric provider](extending/metric-provider.md)) and
`toy_adapter.py` ([The conformance check](extending/conformance.md)).

`examples/python_suite.py` runs a project's pytest suite and prints what the
adapter observed:

```
python examples/python_suite.py path/to/project tests/test_example.py src/example.py
```

The arguments are the project directory, the test file and, optionally, the
source file whose line coverage the example prints. The test file and the
source file are paths inside the project directory. The example runs the project
with the Python that runs the example, so install pytest, coverage.py and
Cosmic Ray into the environment that holds LangAdapter before you run the
examples. The tests must import the code the way
pytest runs them from the project directory. With the code in `src`, as here,
that takes pytest's own setting (`pythonpath = ["src"]` under
`[tool.pytest.ini_options]` in `pyproject.toml`) or the project installed
into that Python. Without either, the run reports `collection_failed`.

In outline, where `project`, `test_file` and `target` are the three arguments
and `interpreter` is the path of the project's Python:

```python
import langadapter as la
from langadapter.runs import SuiteRunRequest

context = la.ProjectContext(root=project, interpreter=la.Interpreter(path=interpreter))
adapter = la.create("python", context)
suite = la.Suite(project, (test_file,))
result = adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=120, coverage_target=target))
print(result.outcome, result.counts)
```

A `ProjectContext` names the project directory and the interpreter (or JDK) that
runs it. A relative interpreter path starts at the current directory, and a
path with no file there is refused with `InvalidRequest` when the adapter is
created. `create` returns the adapter registered for the language. Every
operation takes a typed request and returns a typed result.

## Read the result

Every result of an operation that runs the project's tools carries an
outcome and a reason. Source operations report a status (`SourceStatus`)
instead. `completed` means the operation
ran and its observation is valid. Test failures, a collection error, a
timeout, a broken tool, missing setup and an unsupported request each have
their own outcome, described in [Outcomes](explanation/outcomes.md), which
also says how a value the adapter did not observe reads.

To check before running: `adapter.operation_capabilities()` lists what the adapter
implements for the context without starting a tool, and `inspect_project`
reports what a project's setup is missing: `adapter.inspect_project(request)`
with a `ProjectSetupRequest`, or `la.inspect_project("python", request)`.
The request names the context, the source files and the operations the
program needs, by the names `operation_capabilities()` lists
(`langadapter.capabilities.OPERATION_NAMES`):

```python
request = la.ProjectSetupRequest(context, ("src/example.py",), ("tests.run_suite",))
report = la.inspect_project("python", request)
```

The report's outcome and reason say whether the check could run. Each item
is one thing the project's setup needs, such as the runtime or pytest, with
its readiness, the reason when it is not available and the next step
(`action`). Each operation entry says whether the operation can run here:

```python
print(report.outcome, report.reason)
for item in report.items:
    print(item.key, item.required, item.readiness, item.reason, item.action)
run_suite = next(entry for entry in report.operations if entry.name == "tests.run_suite")
print(run_suite.readiness, run_suite.reason, run_suite.action)
```

See [Environment selection](explanation/environment-selection.md).

## Keep the project unchanged

Tests can write, change or remove files. To run them against a disposable
copy, prepare the project first. An environment inside the project, such as
its `.venv`, is copied too, which takes time: leave it out with
`prepare_project(context, inputs=ProjectInputSelection(exclude=(".venv",)))`,
`ProjectInputSelection` from `langadapter`.

```python
from pathlib import Path
from langadapter import Interpreter, ProjectContext, Suite, create, prepare_project
from langadapter.runs import SuiteRunRequest

context = ProjectContext("/projects/example", Interpreter(path="/tools/python"))
prepared = prepare_project(context)

with prepared.use() as scoped:
    adapter = create("python", scoped)
    suite = Suite(str(Path(scoped.root) / "tests"), ("test_example.py",))
    result = adapter.tests.run_suite(SuiteRunRequest(suite, timeout_s=60))

prepared.close()
```

A suite's root matters. Its test files are named relative to it, and test IDs
and the suite's identity use those names. pytest's root is set to it, and
pytest loads no `conftest.py` above it. Every `conftest.py` in the root or in
a directory between the root and a test file runs. List each of them in the
suite's `setup_files`: a run refuses a suite that leaves one out or lists one
that pytest would not load. Rooting the suite at the project, as the first
example does, is the general case, and a `conftest.py` in the project directory
then runs. Here the suite is rooted at the copy's `tests` directory, which holds
its test file, so a `conftest.py` in the project directory does not run.

[Prepared copies](explanation/prepared-copies.md) describes what a prepared
copy guarantees.

## Stop an operation from another thread

An operation runs on the caller's thread. To stop it from another thread,
for example from a Ctrl+C handler, run it in a `CancelScope`:

```python
import threading
from pathlib import Path
from langadapter import (CancelScope, Interpreter, OperationCancelled, ProjectContext, Suite,
                         create, prepare_project)
from langadapter.runs import SuiteRunRequest

context = ProjectContext("/projects/example", Interpreter(path="/tools/python"))
prepared = prepare_project(context)
scope = CancelScope()

def measure():
    try:
        with scope:
            with prepared.use() as scoped:
                suite = Suite(str(Path(scoped.root) / "tests"), ("test_example.py",))
                create("python", scoped).tests.run_suite(SuiteRunRequest(suite, timeout_s=60))
    except OperationCancelled:
        print("cancelled")

worker = threading.Thread(target=measure)
worker.start()
# On this thread, or in a signal handler:
scope.cancel()
worker.join()
# Once the scope has ended:
prepared.close()
```

`cancel()` returns at once. Every process started in the scope is asked to
end now, and nothing new starts. The operation raises `OperationCancelled`,
an interrupt and no result, after its own cleanup removed its temporary
files. Leaving the scope after a cancel raises it too, whatever the block
returned. Close the prepared copy afterwards as usual, inside the scope or
after it: cleanup runs despite the cancel. One scope may be
entered on several threads at once. A thread you start inside the block is
outside the scope unless it runs in `contextvars.copy_context()`.

## Next steps

- [Python projects](guide/python.md) and [Java projects](guide/java.md)
- [Mutation sessions](guide/mutation-sessions.md) and [Timing](guide/timing.md)
- [Add a language](extending/add-a-language.md)
