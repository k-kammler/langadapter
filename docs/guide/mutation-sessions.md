# Mutation sessions

A mutation session measures a suite against the mutants of one target, the
source file under test, with the language's mutation engine: Cosmic Ray
(`cosmic_ray`) for Python, PIT (`pit`) for Java and Mull (`mull`) for C. A
mutant is a copy of the target with one small
change. A test that fails on a mutant kills it, and the mutation score is the
percentage of mutants the suite kills.

`examples/python_mutation.py` measures a project's pytest suite against the
mutants of one module and prints each mutant's verdict, the tests that killed
it and the score:

```
python examples/python_mutation.py path/to/project tests/test_example.py src/example.py
```

Each mutant's line starts with its class (`MutationClass`) and its
subcategory, as in
`detected   failing_test       MutantKey(file='src/example.py', operator='core/ReplaceBinaryOperator_Add_Div', ...)  killed by: tests/test_example.py::test_add`.
`detected` means a failing test, a crash or a timeout found the mutant, or
that it is not a valid program, and `failing_test` that a named test failed
under it. `undetected   survived`
means the suite passed under the mutant. The other subcategories are
`no_named_test`, `timeout` and `non_viable` for a detected mutant, and a
mutant `not_run` is `incompetent`, `skipped` or `untested`
(`langadapter.mutation.SUBCATEGORIES`). The key's `file` is the module path
as the system writes it, with backslashes on Windows.

The examples are where [Getting started](../getting-started.md#run-a-suite) says.

The test file and the target are paths inside the project directory. The tests
import the module as [Getting started](../getting-started.md#run-a-suite)
describes for code in `src`. Further arguments name mutation operators, such as
`core/ReplaceTrueWithFalse`. Without them, the run uses
all of Cosmic Ray's operators. A name Cosmic Ray does not register refuses
the run.

## Engines and sessions

`adapter.mutations.engines()` lists the implemented engine names without
checking whether they are installed. `operators(engine)` reads the engine's
own list of mutation operators. `open_session(engine, ...)` returns a session whose run
and close operations exclude each other. Engine options are validated,
never silently ignored, and missing setup stays distinct from an unsupported
engine or context.

A session's `workers` is the generic number of mutants measured at once.
An adapter whose engine has no parameter of that meaning translates the
count into the engine's own parallelism settings, and a run records the
values it used. Cosmic Ray's
`distributor` follows the count by default, as
[Settings](settings.md#mutation-sessions) describes with its other values.
PIT runs that many threads, with units
of mutants small enough to spread over them ([Java projects](java.md#mutation-runs)).
A Mull session takes `workers` only and refuses any other option
([C projects](c.md#mutation)).

The Cosmic Ray configuration is
`langadapter.python.cosmic_ray.CosmicRaySettings`. Pass it as `settings` to
`adapter.mutations.open_session`, which checks the options and the project
before it opens the session. Each run takes its own
`langadapter.mutation.MutationRequest` with the target, suite,
catalogue, timeouts and attribution mode:

```python
from langadapter.python.cosmic_ray import CosmicRaySettings

with adapter.mutations.open_session(
    "cosmic_ray", settings=CosmicRaySettings(keep_output=True),
) as session:
    run = session.run(request)
```

Opening the HTTP distributor starts worker processes. In a container, each
run's launcher starts and stops its own workers there. The workers listen on
the loopback address only and run only the jobs that carry their fleet's
secret, which they and the command that sends the jobs receive through their
environment. Other users of a shared machine cannot read it. Processes of the
same user can, as that user can already run any program. A job waits for
its worker as long as the worker runs it, which the per-mutant timeout
bounds. A failed request to a worker leaves its mutant untested and the run
marked incomplete, and a run stops before its first mutant when a worker of
its session has stopped. Closing the session
stops the workers and removes its temporary files. A run that failed keeps its
session database and a copy of the original target for diagnosis, named on
its error. A run an interrupt stopped keeps neither once the cleanup has
restored the target. In a container, while the local distributor mutates
the target in place, a copy of its original lies beside the session. A
stopped launcher restores the target first, and when the launcher ended
before it could, the session restores it from the copy. A copy that cannot
be written back is named on the run's error. Building the settings alone
runs nothing.

`session.cancel()` stops the measurement in flight from any thread, a signal
handler included, for Cosmic Ray and PIT alike. The processes the operation
started are asked to end at once, and a command in a container is stopped
through its supervisor. The operation then closes the session and raises
`MeasurementCancelled` (from `langadapter.errors`), an `OperationCancelled`:
an interrupt and no result. A `CancelScope` around the run
cancels the session with it. If that cleanup cannot finish, the error carries the session
as `cleanup_handle`. A cancelled session runs nothing more, and cancelling
again or while idle is harmless. A cleanup that fails raises `MutationCleanupError`, and its
`cleanup_handle.close()` retries it. An interrupt or exit
(`KeyboardInterrupt`, `SystemExit`) during cleanup is no cleanup failure: it
propagates as itself and carries the same `cleanup_handle`. Until a retried
`close()` succeeds, the session keeps what it did not release and refuses
runs.

Cosmic Ray's session database lies where `session_dir` and
`local_session_dir` put it, as
[Cosmic Ray settings](settings.md#cosmic-ray-settings) describes.
`langadapter.python.cosmic_ray.check_local_session_dir` applies their rule
before a run, so a caller can refuse before costly work. The work directory
never holds a live session database. For a container run it holds a cached
catalogue the host copies in at the start, the catalogue snapshot the
launcher writes right after `init`, and the launcher's log of each pass,
written as the pass goes. At the end the run adds the finished session
database the host reads and, unless the request turns the journal off, the
per-test journals of the run and of its verification and per-file passes in
the directory `evidence`, each pass before the last in a numbered directory. Each
verification or per-file pass also returns its session database, which the
host deletes once it has read it. Each run logs where its session ran and,
with the HTTP distributor, how many workers were ready. A run on Linux or
another POSIX system, in a container too, also logs the open-file limits of
its workers and of each mutant's test run. A local run on Windows has no
such limits to log.

## Results

Failed or incomplete runs have no score, as
[Outcomes](../explanation/outcomes.md) states for every measurement. The
mutant outcomes the engine
reported and its diagnostics stay in the result. A run's kill matrix
(`run.to_kill_matrix()`) records where it executed under `execution`: local
or in a container, and how its commands were supervised: on Linux `pidfd`,
or `process_group` where the kernel provides no process descriptors, and on
macOS and other POSIX systems `process_group_unconfirmed`. A local run on
Windows records no supervision.

Before the first mutant, a Python run runs the unmutated suite once, where
the first mutants run: in the first worker's clone when the session has
workers, else in the project. A suite that does not pass is refused without
a score, because the suite's own failure would kill every mutant:
`run.stopping_tests` names every test that did not pass, and the reason
keeps the end of pytest's output. With a `session_dir` in the
`CosmicRaySettings`, the check run's whole report stays in that directory,
in `origin_check`. The same run checks which file the tests execute the target from, as
[Prepared copies](../explanation/prepared-copies.md#python-imports-in-a-copy)
describes. `run.origin_run_s` is its time, kept apart from the time of
measuring mutants. A suite that keeps state between its runs sees this one. `run.progress_score()`
reports the score also for an incomplete run, each untested mutant counted
as not killed, so it is a lower bound: `run.score()` still returns
`None` for a run marked incomplete (`run.incomplete`). For a stored kill matrix,
`langadapter.mutation.matrix_score_percent(matrix)` withholds the score of a
matrix stamped incomplete, and `matrix_lower_bound_percent(matrix)` gives its
lower bound.

A kill matrix records its `schema_version`. Each status is one constant of
`langadapter.mutation`, such as `KILLED_STATUS`, and `DETECTED_STATUSES`
holds those a score counts as killed. The module's readers read a stored
matrix of an earlier version as the current one. A caller that reads a
stored matrix's rows itself first calls
`langadapter.mutation.upgrade_kill_matrix(matrix)`, which returns the
matrix in the current version's words.

A request's `progress` callback receives `MutationProgress` records from a
Cosmic Ray run. Each says how many items are finished, of how many, after how
long, and its kind names the items: `mutants`, about once a second while a
local run measures its mutants, and `kill verifications` while any run
verifies its kills. A run in a container reports the kill verifications and
not the mutants. A PIT run and a Mull run report none. It is progress, not
evidence, because a finished mutant's verdict can still change when its kill
is verified.

Every run names the tests that killed each mutant. With attribution `first`,
the default, a run may stop testing a mutant at its first kill, and which test
that is does not depend on timing. With `all`, the run names every killing
test. The adapter's `per_test_kill_attribution` capability says whether its
tools can name killers.

## Per-suite verdicts

`session.per_suite(request)` measures several suites against the mutants of
one target and gives each suite's verdict for every mutant: `killed`,
`collection_error`, `non_viable`, `timeout`, `survived` or `untested`
([`VERDICTS`](https://k-kammler.github.io/langadapter/reference/mutation/#langadapter.mutation.VERDICTS)),
with the killing tests. A `PerSuiteRequest` lists the suites as
`BudgetedSuite` values, each with a unique name and its own per-mutant
budget, and the settings they share. Each suite's verdicts are defined as
those of its standalone run, the `MutationRequest` that
`request.standalone(name)` returns:

```python
from langadapter.mutation import BudgetedSuite, PerSuiteRequest

request = PerSuiteRequest("src/calc.py", (
    BudgetedSuite("first", first_suite, per_mutant_timeout=20),
    BudgetedSuite("second", second_suite, per_mutant_timeout=35),
))
with adapter.mutations.open_session("cosmic_ray") as session:
    result = session.per_suite(request)
verdicts = result.verdicts("first")
```

Every language can do this: the session measures each suite on its own,
one after the other, within one operation. An adapter may offer a faster
implementation, which `result.implementation` names and the shared
conformance check compares with that reference.
`result.runs` keeps each suite's own run, with its outcome, identity and
settings. A suite whose run failed gets `untested` for every mutant,
and the other suites keep their verdicts. `result.kill_matrix(name)` is that
suite's own kill matrix, and `result.kill_matrix()` the union of all suites,
with each suite's verdict on every row.

## Measured and derived results

A measurement runs the tests against the mutants. A derived result applies
a rule to an earlier measurement instead: it can be much faster, and it is
not guaranteed to equal a fresh measurement. Use the measurement where a
result must be exact, and keep a derived result beside it for comparison.

- `session.run` and `session.per_suite` measure. No result they return is
  derived.
- `run.derive_for_tests(kept, covered_lines=...)` derives the result for a
  subset of a measured run's tests. A killed mutant stays killed when a
  kept test is one of its recorded killers, when it is `non_viable`, when it was
  killed without a named test, or, for a timeout or a kill whose run did not
  record every killing test, when the kept tests cover its line or its line
  is unknown. Otherwise it counts as survived. The result is marked derived
  (`run.derivation`, with its assumption), and each mutant keeps its
  measured status and the rule that decided it. A kill the run attributed
  to a file rather than a test is refused, since no rule decides it.
- `langadapter.mutation.restrict_kill_matrix` applies the same
  rule to a saved kill matrix and marks its result the same way: the
  matrix's `derived` and each row's measured status and rule.
- A pooled run of several suites, staged with `adapter.sources.stage_pool`
  ([Source analysis](source-analysis.md#pooled-suites)), serves one such
  derivation per suite. `pool.derive(kill_matrix, slot, covered_lines=...)`
  restricts the pooled run's saved matrix to the mutants that suite's tests
  killed and the lines they cover, and names the suite's killers as its
  standalone run names them. Run the pooled suite with attribution `all`,
  so that every killing test is recorded.

Every saved row carries the mutant's `key`: its file, operator, occurrence,
line, column and the hash of its change. The key names the same mutant in
every run of one target and catalogue on one platform with one engine
version, a pooled and a standalone run included, where the engine's own ids
may differ between runs. A derived and a measured verdict therefore compare
mutant by mutant. Keys from different platforms or engine versions need not
match: the file is written with the platform's path separator, and the hash
is that of the change as the engine writes it.
