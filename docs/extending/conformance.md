# The conformance check

`langadapter.conformance` checks an adapter or a metric provider against the
rules every adapter and provider must follow, the shared contract, on a small
example of its language. Each check returns one
sentence per problem it finds, naming the rule broken, so an adapter's own
tests can assert that there are none.

`langadapter tutorial --auto --output-dir toy` writes the Toy's adapter as
one file, `toy/toy.py`, and its example project, `toy/project`. This script
checks them:

```python
import sys

import langadapter as la
from langadapter.conformance import AdapterExample, check_adapter

la.load_extension("toy/toy.py")
context = la.ProjectContext("toy/project", la.Interpreter(path=sys.executable))
suite = la.Suite(context.root, ("test_calc.toy",))
example = AdapterExample("toy", context, suite, coverage_target="calc.toy",
                         expected={la.TestId("test_calc.toy", "doubles"): "passed"},
                         collection_breaker="test adds: 1 + 1 == 2\nthis line is not a test\n")
problems = check_adapter(example)
assert not problems
```

`langadapter.load_extension` registers the language a file adds in your own
script, as `langadapter check --extension` does for the command's check. A
language registered in code (`register`) needs no load.

An `AdapterExample` names the language, a `ProjectContext` and a `Suite` of
that project that runs: its tests pass or fail rather than fail to collect, and
each declared test file holds at least one test. `expected` optionally maps
some of its tests, as `TestId(file, name)` keys, to their outcome words, such
as `passed`. A `coverage_target` (a source file of the project) adds the
coverage check. An adapter that prunes collection breakers also needs a
`collection_breaker`: the text of a test source with one unit that keeps its
tests from loading, the language's unit that
[Collection breakers](../guide/source-analysis.md#collection-breakers)
names. For Python that is a module-level statement that fails at
collection:

```python
collection_breaker="BROKEN = undefined_name\n\n\ndef test_ok():\n    assert True\n"
```

## What `check_adapter` checks

- the registered file extensions map the example's test files to the language
- `create` binds an adapter of the language to the context
- the adapter declares its support as a `LanguageSupport` and its profile is a
  `VersionProfile` (`langadapter.versions`)
- each of its `project_settings` has the kind `path`, `executable`, `choice`
  or `text`, which `langadapter check` names too
- an adapter that prunes collection breakers declares the unit it removes
  (the adapter's `collection_unit`), removes the example's
  `collection_breaker` and names what it removed, and leaves the result
  unchanged when it prunes it again
- the operation report lists every operation, with a reason for each
  unsupported one
- the capability report names only known capabilities
- a suite run reports consistent counts, at least one test outcome for every
  declared test file and the outcomes `expected` names, or answers
  `unsupported` where suite runs are not implemented
- where `provides_timeout_journal` is on, a run with the journal reports as
  consistently
- coverage is measured where the capability report offers it and refused where
  it does not
- the quick check (`inspect_project`) reports on every operation, given the
  test files that lie inside the project (none for a suite outside it)
- each of the language's own metric providers (`metric_providers`) gives every
  file metric a value for the example's first test file

## What `compare_runs` compares

The normal test run decides whether a test passes, and the coverage and
mutation tools only measure ([One run decides](add-a-language.md#one-run-decides)
says why). `compare_runs(example)` runs the example suite normally
and, with its `coverage_target`, through each tool the adapter offers: the
coverage run, the per-test evidence and a mutation run of that file with the
first engine's default catalogue, on a new adapter object, so that a mutation
run which works only after another run of the same object shows. It names
each difference: a test the
coverage run gives another outcome, a green evidence run of a suite the normal
run fails or none of a suite it passes, and a mutation tool that measures a
suite the normal run fails, refuses one it passes, or names other tests than
the normal run's failing ones as the tests that stopped it
(`MutationRun.stopping_tests`).

## Find differences early {#find-differences-early}

A long run of a program such as Testuary meets a difference between runs only
when a generated test happens to trigger it, often after hours and many model
requests. `compare_runs` looks for differences on your example in the time of
a few runs, one of them a mutation run with the first engine's whole default
catalogue. It returns one sentence per difference, which names the run and,
where a test differs, the test. Give the example a `coverage_target`: the
tools' runs measure that file, and without one `compare_runs` runs only the
normal run and compares no tool. A tool the adapter or the context does not
offer is skipped, so an empty tuple means that every tool offered agrees
with the normal run. A shared language package's own test can assert that it
finds no difference.

It finds what the example's tests can show. Run it on two examples: one whose
tests pass, and one with a failing test, which checks that the mutation tool
refuses that suite and names that test. Tests that read their working directory,
a file by a relative path, the import or class path, or an environment
variable show whether the tools' runs see the normal run's environment. A
test that writes a file shows whether a run leaves anything behind for the
next one.

What each sentence means, and where to look:

- `normal run: the adapter runs no suite, so no tool's run compares`, or
  `normal run: the example suite did not run (<outcome>: <reason>)`: nothing
  else is compared. Write `run_suite` first, or give the example a suite
  whose tests pass or fail rather than fail to load.
- `coverage run: <test> was failed, in the normal run passed`: the coverage
  run starts the tests otherwise than the normal run. Let it reuse the normal
  run's command and environment. When the tool itself causes the difference,
  such as a hidden member a test inspects, it stays, and a consumer gives
  such a suite no coverage number.
- `coverage run: no outcome for <test>, which the normal run ran`, or
  `coverage run: <test> is no test of the normal run`: the coverage run
  selects other tests than the normal run, or names them otherwise. Give it
  the normal run's selection and test ids.
- `coverage run: the suite did not run (<outcome>: <reason>)`: the coverage
  run failed where the normal run ran. Its reason says why.
- `per-test evidence: no evidence of a suite the normal run passed`, or `a
  green run of a suite whose normal run failed`: the same for the evidence
  run.
- `per-test evidence: no evidence for <test>, which the normal run ran`: the
  evidence run left out a test that the normal run ran, as a coverage run
  that selects other tests does.
- `mutation tool's run: stopped by <tests>, which the normal run passed`: the
  tool's check of the unchanged suite runs in another place or environment
  than the normal run. Run it where the first mutants run, with the normal
  run's environment.
- `mutation tool's run: measured nothing of a suite the normal run passed`,
  with the tool's outcome and reason: the tool failed or refused a passing
  suite without naming a test. Its reason says why, and the same causes as
  above apply.
- `mutation tool's run: measured a suite whose normal run failed`: under the
  tool a failing test passed or did not run. Compare the tool's test
  selection, order and environment with the normal run's.
- `mutation tool's run: stopped by ..., where the normal run failed ...`: the
  tool refused the suite, but the tests the adapter names as stopping it, or
  `no named test`, are not the normal run's failing tests. Read
  `MutationRun.stopping_tests` from the tool's report.

## What `check_metric_provider` checks

[A metric provider](metric-provider.md#check-it) shows the call and in which
process to run it. It registers the provider on the language's adapter, as
its users do. A provider without a name, a name defined twice, or a metric
the adapter
already has, such as `nloc`, is refused there. Then it checks that every
file metric gets a value. When `unparsable=` gives source
the provider cannot read, it also checks that no metric reports a number for
it.

## What `check_per_suite` checks

A mutation session may answer `per_suite` with its own faster
implementation (`per_suite_fast`). `check_per_suite(session, request)` runs it and the
reference, which measures each suite on its own, in the same session. Both
must give every suite the same outcome, the same mutants and, for each
mutant, the same verdict and killers. Choose suites that reach every
verdict the engine can give, and suites that would disturb each other if
they ran in one process. A session without a faster implementation has
nothing to compare.

None of the checks judges whether a language's measurements are right.
That is the job of the adapter's own tests against its tools.

## Running it on the example

`examples/toy_adapter.py --check <directory>` runs `check_adapter` on the Toy's
adapter at step 1 of `langadapter tutorial` and prints each problem it finds. The directory must hold
at least one `test_*.toy` test file, and the `.toy` sources its tests use.
The tutorial's example project is one, which
`langadapter tutorial --auto --output-dir toy` writes to `toy/project`, at a
pace for watching, or faster with `--speed 50`. The examples are where
[Getting started](../getting-started.md#run-a-suite) says. With `<prefix>`
the environment's directory named there, give the example's full path from
the directory that holds `toy`:

```text
python <prefix>/share/doc/langadapter/examples/toy_adapter.py --check toy/project
```

A pass prints `the Toy adapter keeps the adapter contract`, and each problem
is one line. A missing directory, or one without a `test_*.toy` file, is
refused in one line.
