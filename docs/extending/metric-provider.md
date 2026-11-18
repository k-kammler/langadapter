# A metric provider

A metric provider adds source metrics to an adapter without changing the
adapter. It declares its metric definitions and measures source text.

## A provider in one file

A whole provider fits in one file. This `long_lines.py` counts the lines
longer than 80 characters in a source file of any language:

```python
import langadapter as la

LONG_LINES = la.MetricDefinition(name="long_lines", unit="lines",
                                 definition_version="1", scopes=("file",))


class LongLines:
    name = "example.long_lines"

    def definitions(self):
        return (LONG_LINES,)

    def measure(self, source, *, language, parser):
        count = sum(len(line) > 80 for line in source.splitlines())
        value = la.MetricValue(LONG_LINES, self.name, None, la.SourceStatus.COMPLETED, count)
        return la.SourceObservation(
            la.SourceStatus.COMPLETED, (la.MetricScope("file", "", None, None, None, (value,)),))


langadapter_metric_provider = LongLines()
```

The top-level name `langadapter_metric_provider` makes the file an
extension: every adapter created after the file is loaded measures the
provider's metrics, in every language. A program loads it with
`langadapter.load_extension("long_lines.py")`, a path relative to the
current directory unless `relative_to` names another directory. A consumer
loads it from its settings, as [Extension points](index.md) says, such as
Testuary's
[`[main] extensions`](https://k-kammler.github.io/testuary/reference/settings/#main).
A provider whose metric name a provider loaded before or the
structural metrics already hold is refused at the load, with the file named,
and nothing of the file is registered. `langadapter check python --extension
long_lines.py` names the provider it loaded and the metrics it measures.

## Check it

`langadapter.conformance.check_metric_provider(provider, language=...,
source=...)` returns the provider's problems, an empty tuple when it keeps
the contract:

```python
from langadapter.conformance import check_metric_provider
from long_lines import LongLines

problems = check_metric_provider(LongLines(), language="python", source="x = 1\n")
```

`unparsable=`, a source the provider cannot read, also checks that the
provider returns an unavailable observation for it. A provider that reads
any text, as this one does, takes none. Run the check in a process that has
not loaded the provider's file as an extension: the load registers the
file's metric names, and the check then reports them as already registered.
[The conformance check](conformance.md#what-check_metric_provider-checks)
says what it checks.

## The contract

A provider has a `name`, returns its `MetricDefinition`s from `definitions()`
and implements `measure(source, *, language, parser)`, which returns a
`SourceObservation` of the `MetricScope`s it measured, each with a
`MetricValue` per metric.

- `language` is the adapter's language. `parser` is the text that names the
  adapter's structural parser, such as
  `tree-sitter 0.25.2; tree-sitter-python 0.25.0`, or None where the adapter
  has none. Each `MetricValue` names the provider by its `name`, and its
  parser as the text of the parser it read the source with, `parser` when it
  used the adapter's, else None.
- It reports each metric for the scopes its definition names: the file,
  functions or both. A file scope is `MetricScope("file", "", None, None,
  None, values)`. A function scope is `MetricScope("function", name,
  start_line, end_line, signature, values)`, and its values join the
  adapter's own scope of the same name, lines and signature, the signature
  None in a language without one, such as Python.
- Source it cannot read gets no number: the provider returns an unavailable
  observation, such as
  `la.SourceObservation(la.SourceStatus.PARSE_ERROR, reason="the source does not parse")`,
  and `metrics` then lists the metric under `unavailable`.
- A provider may report only a standard metric
  (`langadapter.sources.STANDARD_METRICS`) that the `metrics` operation
  reports, not one that `call_statistics` reports, with its unit and at most
  its scopes. Python and Java measure every standard metric of `metrics`
  already, and C every one but `cyclomatic_complexity`, so a provider's
  other standard metric is refused there as already registered.

A program can also register a provider on one adapter's source operations:

```python
adapter.sources.register_metric_provider(LongLines())
observation = adapter.sources.metrics(source, names=("long_lines",))
for value in observation.value.file.metrics:
    print(value.definition.name, value.value)
for value in observation.value.unavailable:
    print(value.definition.name, value.status, value.reason)
```

`observation.value` holds the file's scope (`file`), each function's
(`functions`) and the metrics no number was measured for (`unavailable`).

Registration is local to that adapter object. An adapter from another `create`
call, even for the same context, does not have it. Replacing an existing
metric name requires `replace=True`.

A language package registers its own providers on every adapter of the
language: its adapter class returns them from `metric_providers()`, and each
adapter object registers them when its source operations are created, after
the structural metrics. A provider whose metric name is already registered is
refused there.

## The example

`examples/metric_provider.py` holds the provider above. It checks the
provider against the shared contract, prints `contract: no problem` or each
problem, and prints the file's value:

```
python examples/metric_provider.py path/to/source.py
```

The examples are where [Getting started](../getting-started.md#run-a-suite) says:
[examples/metric_provider.py](https://github.com/k-kammler/langadapter/blob/v0.1.0/examples/metric_provider.py).
