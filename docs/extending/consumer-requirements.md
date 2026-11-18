# What Testuary requires

Testuary, a consumer of LangAdapter, generates test suites with a model.
This page states, family by family, which operations Testuary calls in
which part of its run, which of its settings make an operation required,
and what it does when your language lacks the operation.

[Who needs what](who-needs-what.md) explains the tiers of a consumer's
profile and how `langadapter check --for` combines profiles.
[Hooks and facts](../reference/hooks.md) lists each family's hooks under the
family's heading.

## When a language lacks an operation

An operation is missing when your adapter does not provide its hook.
LangAdapter then answers without running anything:

- An operation on tests, such as `tests.benchmark`, ends with the outcome
  `unsupported`.
- An operation on sources, such as `sources.skeleton`, answers with the
  status `unsupported`.
- `mutations.engines()` is empty.

Each consumer decides what such an answer means. It refuses the run before
any work, it ends the work on one file, or it takes a fallback and goes on.
For Testuary, the sections below say which, family by family.

## Testuary's phases

A Testuary run has four phases. The starter phase, the coverage phase and
the mutation phase generate tests with a model. Each of them ends with a
phase end, which merges the phase's candidate suites, measures them,
reduces them to the tests that add coverage or kills, and chooses one
suite. Finishing measures the chosen suite and sends no model request. A
run that stops after a phase (`--stop-after-phase`) or reuses a phase's
results (`--reuse`) leaves the other phases out, and with them their
requirements.

## The check before the first phase

Before any phase, a run reads the project's capability report for the
families of Testuary's profile.

- Run tests is required in every run. Coverage is required when a
  generation phase or Finishing runs. Mutation is required when the
  mutation phase runs, when Finishing measures mutation, or when a
  generation phase runs while the phase ends measure mutation. A required
  family your language lacks refuses the run with "required tools are
  missing for this run", and no phase runs. A capability report that does
  not complete refuses the run the same way.
- An optional family your language lacks gives its fallback. The run lists
  each such family under "Optional language adapter features this run goes
  without" and records the fallback under `fallbacks` in the run's records.
  Three fallbacks change Testuary's method, and the run's summary names
  them too: merging, validity screening and kill attribution.
- The check asks for an optional family only where the run uses it: code
  smells only with `[finishing] measure_lint` on, delivered runs only with
  `report_reverse_order_failures` on, and kill attribution only when the
  run measures mutation.
- A capability report cannot show instrumentation, versions or containers,
  so the check neither lists nor records their fallbacks.
- `testuary check` asks the same questions of a project without running a
  phase.

## Families by phase

"Required" means that the phase cannot work without the family. "Optional"
means that the phase uses the family when your language has it and takes
the fallback below when it does not.

| Family | Starter phase | Coverage phase | Mutation phase | Phase ends | Finishing |
| --- | --- | --- | --- | --- | --- |
| run tests | required | required | required | required | required |
| coverage | required | required | required | required | required |
| mutation | not used | not used | required | required | required |
| load repair | optional | not used | not used | optional | not used |
| import pruning | optional | not used | not used | optional | not used |
| syntax repair | optional | optional | optional | optional | not used |
| skip removal | optional | not used | not used | not used | not used |
| import guidance | optional | not used | not used | not used | not used |
| kill attribution | not used | not used | not used | optional | not used |
| merging | optional | optional | optional | optional | not used |
| per-test evidence | not used | not used | not used | optional | not used |
| instrumentation | optional | optional | optional | optional | not used |
| delivered runs | optional | optional | optional | optional | optional |
| validity screening | optional | not used | not used | optional | not used |
| timing | not used | not used | not used | optional | optional |
| structural metrics | not used | optional | optional | not used | not used |
| code smells | not used | not used | not used | not used | optional |

- **Run tests.** Every phase runs suites with `tests.run_suite`.
  `sources.prune_tests` removes tests from a suite in the starter phase and
  at the phase ends, and `sources.test_metrics` counts a suite's tests,
  which a phase end needs for every suite it compares.
- **Coverage.** Every phase measures the target's coverage with
  `tests.run_suite` and a `coverage_target`.
- **Mutation.** The mutation phase measures mutants with the language's
  engine, and so does its phase end. The starter and coverage phase ends
  measure mutation while `[main] phase_end_mutation` is on, and Finishing
  while `[finishing] measure_mutation` is on. An engine that cannot start
  there is an error, not a fallback. A run that measures mutation outside a
  container also asks your adapter whether it refuses the session directory
  or the number of workers, and a refusal stops the run before any phase.
- **Load repair** and **import pruning.** In the starter phase and at the
  phase ends, they repair a generated suite that does not load. Without load
  repair, such a suite is discarded. Without import pruning, a suite with an
  import that does not resolve does not load and is discarded.
- **Syntax repair.** `sources.validate_source`, else
  `sources.check_structure`, judges a generated source in the starter phase
  and at the phase ends, and `sources.check_structure` judges each edit in
  the coverage and mutation phases. `sources.finalize_source` completes a
  source before it is kept. Without the family, each source passes these
  checks as it is, and a starter suite that does not parse is discarded.
- **Skip removal.** The starter phase removes whole-file skips. Without it,
  they stay, and a suite without a passing test is rejected.
- **Import guidance.** The starter prompt names the import line a test
  needs. Without it, the prompt asks the model to follow the language's
  import conventions.
- **Kill attribution.** The reductions at the phase ends that keep every
  mutant kill need each kill attributed to a test. Without it, those
  reductions are skipped.
- **Merging.** At each phase end, `sources.resolve_collisions` settles
  test names that occur in several candidates and `sources.merge_sources`
  merges the candidates into one suite. `sources.normalize_bindings`
  normalizes a suite's sources in the starter phase and when the coverage
  and mutation phases start. Without merging, the phase end keeps the
  better single candidate, by mutation score, then branch coverage, then
  line coverage, then the timing tie-break. With `[starter] suite_mutation`
  set to `derive` or `both`, the starter phase end stages its suites as one
  pool with `sources.stage_pool`, and without it measures each suite alone.
- **Per-test evidence.** At each phase end, each test's coverage and kills
  decide the reduction. Without it, the reduction is skipped and each phase
  chooses among unreduced suites, and the starter phase end scores each
  starter suite on its own measurements.
- **Instrumentation.** `prepare_suite` prepares the suite's directory when
  the coverage and mutation phases start. Without it, the project must
  resolve instead. A test whose run warns of leaked state, such as an
  unawaited coroutine, is removed by the starter phase's cleanup and counts
  as failing in the coverage and mutation phases. Without the family, such
  tests are neither removed nor counted as failing. The phase ends log the
  adapter's warnings of tests whose verdict depends on the order in which a
  mock's results are consumed.
- **Delivered runs.** For a language whose runs as delivered take a
  session seed (`seeds_delivered_runs`), the normal run of every phase is a
  run as delivered. With `report_reverse_order_failures` on, the coverage
  and mutation phases add the reverse-order note to a passing run. Without
  the family, the normal run is the language's own test run, and the
  reverse-order note is off. For C, whose normal run runs each test file
  whole as one program ([C projects](../guide/c.md#tests)), the normal run
  and the run as delivered run the tests the same way.
- **Validity screening.** In the starter phase and at the phase ends,
  tests that read the implementation are rejected. Without it, they are
  not rejected.
- **Timing.** At each phase end, `tests.benchmark` decides between suites
  that tie on every other measure. Finishing records the chosen suite's run
  time with `[timing] measure` on. Without timing, the input order decides
  a tie, and no run time is recorded.
- **Structural metrics.** After a timeout in the coverage or mutation
  phase, `sources.skeleton` and `sources.function_fingerprints` name the
  tests added since the suite last ran in time, as the suspects. Without
  them, the phase names no suspect.
- **Code smells.** With `[finishing] measure_lint` on, Finishing counts a
  linter's findings in the chosen suite. Without it, those counts stay
  empty.
- **Versions** and **containers.** No phase calls an operation of these
  families. Without versions, every runtime version is reported as
  untested. Without containers, suites run on this machine only.

Where a measuring run of coverage, per-test evidence or mutation fails a
suite that passed the normal run, Testuary removes the tests the run names
before it chooses a suite, as its
[Measurement and recovery](https://k-kammler.github.io/testuary/explanation/measurement-and-recovery/#tests-removed-after-a-failing-measurement)
page describes. [One run decides](add-a-language.md#one-run-decides) says
what your adapter does for it.

Every phase reads the language's facts, such as its test file names and the
names and code fences of its prompts. A fact your language does not declare
takes its default.

## Testuary's settings

Testuary's [Settings](https://k-kammler.github.io/testuary/reference/settings/)
page gives each setting's default. With Testuary's packaged settings, a run
of all four phases requires run tests, coverage and mutation, and its check
asks for neither code smells nor delivered runs.

| Setting | What it changes |
| --- | --- |
| [`[main] phase_end_mutation`](https://k-kammler.github.io/testuary/reference/settings/#main) | On, the starter and coverage phase ends measure mutation, so a run with a generation phase requires mutation. |
| [`[finishing] measure_mutation`](https://k-kammler.github.io/testuary/reference/settings/#finishing) | On, Finishing measures mutation, so a run that reaches Finishing requires mutation. |
| [`[mutation] engine`](https://k-kammler.github.io/testuary/reference/settings/#mutation) | The engine among those your language declares. A name your language does not declare is refused, except Testuary's default name, which takes your language's first engine. |
| [`[starter] suite_mutation`](https://k-kammler.github.io/testuary/reference/settings/#starter) | `derive` and `both` stage the starter suites as one pool, through merging. |
| [`[timing] measure`](https://k-kammler.github.io/testuary/reference/settings/#timing) | On, Finishing records the chosen suite's run time, through timing. |
| [`[finishing] measure_lint`](https://k-kammler.github.io/testuary/reference/settings/#finishing) | On, Finishing counts code smells, and the check asks for them. |
| `report_reverse_order_failures` in [`[coverage]`](https://k-kammler.github.io/testuary/reference/settings/#coverage) and [`[mutation]`](https://k-kammler.github.io/testuary/reference/settings/#mutation) | On, that phase adds the reverse-order note, and the check asks for delivered runs. |
