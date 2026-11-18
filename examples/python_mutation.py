"""Measure a project's pytest suite against mutants of one module with the
Python adapter's mutation session, and print each mutant's verdict, the
tests that killed it, and the score.

The project's interpreter must have pytest and cosmic-ray installed. The
capability report says so before anything runs. The target is mutated in
place while a mutant's tests run and written back afterwards.

    python examples/python_mutation.py <project root> <test file> <target> [<operators>]

The test file and the target are paths under the project root. Any further
arguments name operators. Without them the run uses Cosmic Ray's whole
catalogue.
"""
from __future__ import annotations

import sys
from typing import Optional, Sequence

import langadapter as la
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.mutation import MutationRun


def run(project: str, test_file: str, target: str,
        operators: Optional[Sequence[str]] = None,
        interpreter: str = sys.executable) -> MutationRun:
    """The mutation run of ``target`` under the suite ``test_file``."""
    context = la.ProjectContext(root=project,
                                interpreter=la.Interpreter(path=interpreter))
    adapter = la.create("python", context)
    needed = adapter.capabilities().capability("mutation")
    if not needed.available:
        raise SystemExit(f"{needed.name} is {needed.availability}: "
                         f"{needed.reason}")
    catalogue = Catalogue(operators=tuple(operators)) if operators else Catalogue()
    with adapter.mutations.open_session("cosmic_ray") as session:
        return session.run(MutationRequest(
            target=target,
            suite=la.Suite(root=project, test_files=(test_file,)),
            catalogue=catalogue, per_mutant_timeout=60,
            wall_clock_limit_s=1800))


if __name__ == "__main__":
    if len(sys.argv) < 4:
        raise SystemExit(__doc__)
    result = run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:] or None)
    for mutant in result.mutants:
        verdict = mutant.classification
        killers = ", ".join(str(test) for test in mutant.killing_tests) or "-"
        print(f"{verdict.mutation_class.value:10} {verdict.subcategory:18} "
              f"{mutant.key}  killed by: {killers}")
    score = result.score()
    print(f"{result.outcome}: score " + (f"{score:.1f}" if score is not None else "none"))
    if result.reason:
        print(f"reason: {result.reason}")
    sys.exit(0 if result.outcome is la.Outcome.COMPLETED else 1)
