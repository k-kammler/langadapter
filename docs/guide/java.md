# Java projects

`langadapter.java.adapter.JavaAdapter` supports Maven and Gradle projects
with the context's JDK. JUnit runs the tests, JaCoCo measures coverage and PIT
measures mutation outcomes. Java tests run as Java runs them: the measurement
helper that makes Python tests deterministic is not used. The context names
the JDK by its `java` executable, such as
`Interpreter(path="/opt/jdk-21/bin/java")`, in a container by its path
inside the container.

## Build and tools

The build can use a different JDK from the measured runtime, which
`LANGADAPTER_BUILD_JAVA_HOME` in `ProjectContext.environment` selects.
Project resolution never rereads it from the process environment. The
context's runtime still compiles and runs the measured tests.
[Settings](settings.md#java-projects) gives this variable and the build
launcher's variables with their defaults.

The setup check looks up the build launcher as a run does and reports a
missing launcher before any run. Outside Windows the project's wrapper
(`mvnw`, `gradlew`) runs with the interpreter its first line names, `sh` when
it names none, without
being executed itself, so a project copy in a directory mounted without
execution (a cluster's `/dev/shm`, many `/tmp` directories) still builds, and a
bash wrapper runs under bash where `sh` is another shell. Windows runs
`mvnw.cmd` and `gradlew.bat` directly.

The measurement tools (the JUnit console launcher, JaCoCo and PIT) are jars in
a tool cache, which a run fills from Maven Central once.
[Settings](settings.md#java-projects) names the cache's directory, the switch
that forbids downloads and where a container reads both. JaCoCo's and PIT's jars and the other jars of fixed versions must match
the SHA-256 that LangAdapter pins for them, downloaded or found in the cache.
The other jars, chosen by the project's JUnit version, are checked against
their published SHA-1 when they are downloaded. The quick check
never downloads, and the tool check downloads only when the request sets
`allow_acquisition`, as
[Setup checks](../explanation/environment-selection.md#setup-checks)
explains with the build steps it tries. Only then does the tool check fetch
the jars it needs within its time limit: the JUnit launcher's for its test
discovery, and JaCoCo's and PIT's before it starts them. Otherwise a jar not
yet downloaded is left to the first run. When
`LANGADAPTER_JAVA_TOOLS_OFFLINE` forbids downloads, either check reports such
a jar as missing setup. `probe_capabilities()` is not a setup check: it resolves the
project with its build and prepares the tool jars as a run does, so it
downloads a missing jar unless downloads are off. The jars keep their own
licenses. `THIRD_PARTY_NOTICES.md`, which ships with LangAdapter beside its
`LICENSE`, lists each one, what it is used for and its license.

## Suites

A file whose name ends in `Test.java`, `Tests.java`, `TestCase.java` or
`IT.java` is a test file. The test file named for a source file `Calc.java`
is `CalcTest.java`.

Suite runs compile every declared Java helper source and put declared resources
on the test classpath. A class from the project's test source directories that a
listed file uses is compiled with it, and the build's own test output directory
stays on the classpath. Other unlisted files are not added. The context's
JVM flags and the project's test JVM settings apply to execution and
measurement. Every run executes one test at a time, as PIT does: Jupiter's
parallel execution is off whatever the project configures. PIT measures the
top-level test classes a suite run selects, on its classpath in its order,
so a JUnit 4 nested class runs under PIT only where the suite run runs it.
`collect_suite` runs native JUnit discovery without executing
test bodies, so parameterized invocations and dynamic children appear only at
execution. `run_as_delivered` checks each declared test file on its own in a
private copy, with its declared helpers, under the request's `timeout_s` as
[Python projects](python.md#generated-suites) describes. Optional source
replacements do not change the input files.

Suite, collection and as-delivered results keep the launcher's whole output,
with the bound [Python projects](python.md#suite-results) describes. A
structure check returns the offending source text whole under the same bound.

Per-test coverage needs tests that do not overlap: JaCoCo's probes are
process-wide and cannot separate concurrent tests. When a run still observes
overlapping tests, per-test coverage is `unsupported`. A requested warm-up pass
of the per-test evidence operation is discarded, and each pass starts a fresh JVM, so it does not warm the
measured JVM. `timeout_journal=True` on a suite run records the tests active
when the process was stopped. Several active tests stay a diagnostic, never an
invented single killer.

## Mutation runs

PIT takes a session's `workers` as
[Mutation sessions](mutation-sessions.md#engines-and-sessions) describes, as
the number of its minion threads (`--threads`) a run uses. PIT runs one unit
of mutants per thread and by default puts all mutants of a class in one
unit, which keeps one or two threads busy. With more than one worker, a
unit holds at most ten mutants of a class (PIT's `mutationUnitSize`), so the
target's mutants spread over the threads. PIT counts the mutants only within the run, so the size is
fixed rather than taken from their number. Each unit starts its own minion
JVM. With one worker, PIT's default of one unit per class applies. A run
records the size it used as its `mutation_unit_size` setting. PIT's timeout
rule and each minion's JVM settings do not change with it.

PIT times out each test of a mutant's run, not the suite: a test's time
under the mutant is limited to a factor times its time in the unmutated
run, plus a constant. A `TimeoutRule` gives PIT its `factor` and, as the
constant, its `minimum_s`, which the run records as its
`per_mutant_timeout`. PIT has no cap, so a rule with `cap_s` is refused. A
whole number of seconds is a fixed limit per test, and without either PIT's
own factor and constant apply.

## What Java does not provide

Automatic restoration of static state, arbitrary random seeding, leak recording
and reverse-order execution are not implemented for Java. A suite run also
refuses `module_under_test` and `isolation_config`: Java runs the project's
own test configuration. Requests for them are refused explicitly, never
silently ignored. `sources.code_smells` and `tests.profile_suite` are not
implemented and answer `unsupported`.
[Scores and partial results](../explanation/outcomes.md#scores-and-partial-results)
says what a failed or incomplete measurement supplies.

Files that a Java test writes into the project directory stay for later test
runs unless a run asks to remove them with `remove_created_files=True`, as
[Prepared copies](../explanation/prepared-copies.md) describes. The project's
build output stays.

Java in a Linux container you run yourself is described in
[Containers](containers.md), and warmed JVM timing in [Timing](timing.md).
