# Timing

`tests.benchmark` times new processes: every repetition starts a new process,
so a discarded warm-up warms only what lasts between processes, such as
Python's bytecode cache, which the repetitions share. Benchmark results keep
the final repetition's exit code when it is available, and progress callbacks
run outside the timed interval.

## Warmed Java timing

`tests.benchmark_warmed` (Java, locally or in a container) starts a number of forks,
each a fresh JVM process, and runs every warm-up and measured iteration of the
suite in the fork's JVM:

```python
from langadapter.runs import WarmedSuiteBenchmarkRequest

result = adapter.tests.benchmark_warmed(WarmedSuiteBenchmarkRequest(
    suite, "native_lifecycle", iteration_timeout_s=60, total_timeout_s=900,
    forks=3, warmup_runs=5, runs=10))
```

The project is built and the suite compiled once, outside the timed
intervals, and each fork runs in its own copy of that build. Only
`Launcher.execute` of a fresh JUnit test plan is timed, in nanoseconds, by the
JVM. The suite's fixtures and the engine's listeners run inside that interval.
`"native_lifecycle"` states that each iteration runs the suite's own fixtures
as JUnit runs them, inside the timed interval, and that static state stays in
the JVM: nothing is reset for you.

A warmed benchmark runs on the project's own context. Inside a prepared
copy's use it raises `OwnershipRefused`: each fork prepares its own copy of
the build, and a prepared copy refuses a further copy while an operation runs
in it.

In a container (see [Containers](containers.md)), the copy of the build and
each fork's copy are made inside the container, as a prepared project's copy
is, and the build, the forks and their JVMs run there. The timed interval is
the same `Launcher.execute`, so the runner's round trip into the container is
no part of a sample. A requested processor set uses the container's own
`taskset`, and `allowed_cpus` is read inside the container. Without the
container's helper Python the operation answers `missing_setup` and runs
nothing.

A suite is repeat-safe when its fixtures restore what its tests change:

```java
class CounterTest {
    private Counter counter;
    @BeforeEach void fresh() { counter = new Counter(); }
    @Test void counts() { counter.add(); assertEquals(1, counter.value()); }
}
```

A suite that keeps state between iterations is not:

```java
class CounterTest {
    static Counter counter = new Counter();
    @Test void counts() { counter.add(); assertEquals(1, counter.value()); }
}
```

Its second iteration fails, the outcome pattern changes, and the result is
`non_repeatable`.

## Validity

A result is `complete`, `incomplete`, `non_repeatable` or `no_samples`. Only a
complete result carries the mean of fork means, the median of fork medians and
the sample range, and with at least two forks the standard deviation between
forks. These describe the samples and are not confidence intervals. Every
iteration's raw record is kept either way. Python and other languages answer
`unsupported`.

## Suite profiles

`tests.profile_suite(SuiteProfileRequest(suite, timeout_s))` runs a suite
once under the language's profiler and attributes the run's time to the
functions it entered. Each
`ProfiledFunction` has its file as `path` (relative to the project root
where it lies inside it), its `name` and `start_line`, its `calls`, its own
time as `own_time_s`, its time with its callees as `inclusive_time_s`, and
its `share` of the run's total own time.

A suite profile attributes time but does not time the suite. Python's
profiler is the standard library's cProfile, around each test's setup, call
and teardown: interpreter start, imports and collection are not profiled,
although a benchmark's repetitions include them. cProfile is deterministic:
it adds a cost at every call and return, so functions with many short calls
take a larger share than in an unprofiled run, and every time is longer.
The result names the profiler, its version and its kind (`deterministic`),
and reports no suite time: `total_own_time_s` is the profiled run's. Time a
suite with `tests.benchmark`. Built-in functions are not listed, and their
time stays in the total. Under pytest-xdist each worker profiles the tests it
runs, and the suite profile sums the workers' times per function.

Only a run whose every test passed lists functions. Failing tests, a
collection failure, a timeout and a missing pytest give the run's outcome
and reason. Java answers `unsupported` and runs nothing.

In a prepared copy (`prepare_project`) the profiled run puts the copy's
`src`, `lib` and `python` directories that hold a package first on the import
path, so the tests execute the copy's files where an editable install or an
installed copy of the project would provide the original's. A context that
names its import root (`LANGADAPTER_PYTHON_IMPORT_ROOT` in
`ProjectContext.environment`) has that directory first instead.
An import hook that still makes the tests execute a module of the project
from outside the copy is caught by the check suite runs in a prepared copy
share: the outcome is `infrastructure_failure`, the reason names the module
and where it came from, and the profile lists no functions.
