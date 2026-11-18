# Outcomes

Every result of an operation that executes a tool carries exactly one outcome
and a reason. A failure of the execution is an outcome, not an exception,
unless it is uncertain whether a command the operation started has ended.
Then the operation raises the original exception with its recovery handle, as
[Prepared copies](prepared-copies.md) describes. A value the adapter did not
observe is absent from the result (`None`), never reported as zero, full
coverage or success.

## The categories

| Outcome | Meaning |
| --- | --- |
| `completed` | The operation ran and its observation is valid. For a suite run, no collected test failed or raised an error. Skipped tests and expected failures do not change that. |
| `test_failures` | The suite ran and at least one test failed or raised an error. |
| `no_tests` | Nothing was collected. |
| `collection_failed` | An error during collection stopped the run before its tests ran. |
| `timed_out` | The operation reached its timeout. |
| `infrastructure_failure` | A tool crashed, its report is unreadable, a session or file it needs is gone, or an instrument check failed. |
| `unsupported` | The language, engine or context does not provide the operation. |
| `missing_setup` | A tool or file the operation needs is absent from the project environment. |

`unsupported` and `missing_setup` are different answers. The first says the
adapter cannot do this for the context. The second says it could, once the
named tool or file is present. Neither is a measured zero.

## Scores and partial results

Failed or incomplete measurements never supply a complete score. A mutation
run that its time limit ended (`timed_out`) has no score, though the
mutants the tool reported stay in the result with their outcomes. A failed
mutation run holds no mutants, counts or score, only its reason, its
`failure` kind and the tests that stopped it. A warmed timing result that
is not complete carries no summary statistics. Progress values, such as a
mutation run's progress score, are for display only.

## Readiness before running

The capability and setup reports use a related vocabulary for readiness:
`available`, `missing_setup` and `unsupported`, or no readiness at all when it
was not checked, which leaves it unknown, never assumed. Each
unconfirmed setup item that the requested operations need names a next step,
and those that only a real run can try say so. See [Environment selection](environment-selection.md).
