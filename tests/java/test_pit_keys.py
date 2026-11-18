"""PIT mutant keys stay the same across separate runs over the same mutations,
whatever order a report lists them in, even for one mutator applied twice on
a line."""
from __future__ import annotations

from langadapter.java import _pit

ADD = "org.pitest.mutationtest.engine.gregor.mutators.MathMutator"
NEGATE = "org.pitest.mutationtest.engine.gregor.mutators.NegateConditionalsMutator"


def _mutation(mutator, line, index, status):
    return (f'<mutation detected="{str(status == "KILLED").lower()}" status="{status}" '
            f'numberOfTestsRun="1"><sourceFile>Calc.java</sourceFile>'
            f"<mutatedClass>demo.Calc</mutatedClass><mutatedMethod>sum</mutatedMethod>"
            f"<methodDescription>(III)I</methodDescription><lineNumber>{line}</lineNumber>"
            f"<mutator>{mutator}</mutator><indexes><index>{index}</index></indexes>"
            f"<blocks><block>0</block></blocks><killingTest></killingTest>"
            f"<description>Replaced integer addition with subtraction</description>"
            f"</mutation>")


def _keys(path):
    records = _pit.read(path)
    return [(record.engine_id, record.status, occurrence)
            for record, occurrence in zip(records, _pit.occurrences(records))]


def test_occurrences_follow_the_mutations_not_the_reports_order(tmp_path):
    mutations = [_mutation(ADD, 7, 9, "SURVIVED"), _mutation(NEGATE, 5, 2, "KILLED"),
                 _mutation(ADD, 7, 4, "KILLED"), _mutation(ADD, 9, 1, "SURVIVED")]
    reports = []
    for name, order in (("first.xml", mutations), ("second.xml", mutations[::-1])):
        path = tmp_path / name
        path.write_text("<mutations>" + "".join(order) + "</mutations>", encoding="utf-8")
        reports.append(_keys(path))
    assert reports[0] == reports[1]
    # The two additions on line 7 keep their own occurrence and verdict.
    on_line = [(engine_id.rsplit("@", 1)[1], status, occurrence)
               for engine_id, status, occurrence in reports[0] if "@4:" in engine_id
               or "@9:" in engine_id]
    assert on_line == [("4:MathMutator", "KILLED", 0), ("9:MathMutator", "SURVIVED", 1)]
