"""A source metric registered from outside the package.

``long_lines`` counts the lines of a source file that are longer than 80
characters, in any registered language. The example checks the provider
against the shared contract and prints ``contract: no problem`` or each
problem, registers it on an adapter for the file's language and prints the
file's value::

    python examples/metric_provider.py <source file>

A provider reports each metric for the scopes its definition names (here the
file). A source it cannot read gets no number: the provider returns an
unavailable observation instead. The file names its provider as
``langadapter_metric_provider``, so ``langadapter.load_extension`` loads it
as an extension for every adapter created afterwards.
"""
from __future__ import annotations

import sys
from pathlib import Path

import langadapter as la
from langadapter.conformance import check_metric_provider

LIMIT = 80
LONG_LINES = la.MetricDefinition(name="long_lines", unit="lines",
                                 definition_version="1", scopes=("file",))


class LongLines:
    """Counts lines longer than ``LIMIT`` characters. It reads any text."""

    name = "example.long_lines"

    def definitions(self):
        return (LONG_LINES,)

    def measure(self, source, *, language, parser):
        count = sum(len(line) > LIMIT for line in source.splitlines())
        value = la.MetricValue(LONG_LINES, self.name, None, la.SourceStatus.COMPLETED, count)
        return la.SourceObservation(
            la.SourceStatus.COMPLETED, (la.MetricScope("file", "", None, None, None, (value,)),))


# LangAdapter reads this name from the file to add the provider.
langadapter_metric_provider = LongLines()


def measure(path: str) -> la.SourceObservation:
    """The ``long_lines`` metric of one source file of a registered language."""
    file = Path(path).resolve()
    adapter = la.create(la.language_for(file.name), la.ProjectContext(str(file.parent)))
    adapter.sources.register_metric_provider(LongLines())
    return adapter.sources.metrics(file.read_text(encoding="utf-8"), names=("long_lines",))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    source_file = sys.argv[1]
    problems = check_metric_provider(
        LongLines(), language=la.language_for(Path(source_file).name),
        source=Path(source_file).read_text(encoding="utf-8"))
    for problem in problems:
        print(f"contract problem: {problem}")
    if not problems:
        print("contract: no problem")
    observation = measure(source_file)
    if observation.status != la.SourceStatus.COMPLETED:
        print(f"{observation.status.value}: {observation.reason}")
        sys.exit(1)
    for value in observation.value.file.metrics:
        print(f"{value.definition.name}: {value.value} {value.definition.unit}")
    sys.exit(1 if problems else 0)
