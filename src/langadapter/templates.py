"""The repair loops every language shares, written once over its hooks.

- ``repair_syntax``: while the language's parser rejects a source, let the
  language fix the reported place, for a bounded number of rounds.
- ``repair_loading``: while loading a test source (collection, compilation)
  fails, let the language remove the units around the failing lines, for a
  bounded number of loads, adopting the result only once it loads.
- ``prune_units``: remove a source's failing units (unresolved imports) and
  look again, for bounded rounds, keeping removals although faults remain.
- ``named_unit``: the rule by which a test name names a unit of a test file.

A language supplies only its own steps (its validator and local fix, or its
load step and removal) and turns the neutral result into its own messages.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, List, Mapping, Optional, Sequence, Tuple

from langadapter import sources as _lines

#: The end of a syntax or load repair whose rounds ran out.
EXHAUSTED = "exhausted"
#: The other kinds of a syntax repair's end.
VALID, REPAIRED, VALIDATOR_FAILED, BEYOND_REPAIR = (
    "valid", "repaired", "validator_failed", "beyond_repair")
#: The rounds a syntax repair takes by default.
_REPAIR_ROUNDS = 25


@dataclass(frozen=True)
class SyntaxRepair:
    """The repaired text, or None. ``status`` is valid, repaired, validator_failed,
    beyond_repair (the fix could not be applied) or exhausted. ``repairs`` counts
    the fixes before the last validation, which is kept."""

    source: Optional[str]
    status: str
    repairs: int
    validation: Any


def repair_syntax(source: str, validate: Callable[[str], Any],
                  local_fix: Callable[[List[str], Any], bool], *,
                  rounds: int = _REPAIR_ROUNDS) -> SyntaxRepair:
    """Repair ``source`` in at most ``rounds`` validations. ``validate`` returns
    a verdict with ``valid``, ``status`` and the error's place. ``local_fix``
    changes the lines (numbered as the parser numbers them) at that place and
    returns false when it cannot."""
    lines = _lines.lines(source)
    validation: Any = None
    repairs = 0
    for repairs in range(rounds):
        text = "".join(lines)
        validation = validate(text)
        if validation.valid:
            return SyntaxRepair(text, REPAIRED if repairs else VALID, repairs, validation)
        if validation.status != "syntax_error":
            return SyntaxRepair(None, VALIDATOR_FAILED, repairs, validation)
        if not local_fix(lines, validation):
            return SyntaxRepair(None, BEYOND_REPAIR, repairs, validation)
    return SyntaxRepair(None, EXHAUSTED, repairs, validation)


#: The kinds of a load repair's end.
LOADS, NO_LOCATION, NOT_REMOVABLE, NO_TEST_LEFT, STOPPED = (
    "loads", "no_location", "not_removable", "no_test_left", "stopped")


@dataclass(frozen=True)
class Load:
    """One load: whether it loads, else the failing lines and text. ``stop`` names
    why the repair cannot go on (no loader run, a timeout), ``info`` is the language's."""

    loads: bool
    lines: Tuple[int, ...] = ()
    failure: str = ""
    stop: str = ""
    info: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadRepair:
    """The end of a load repair: loads (unchanged if ``removed`` is empty, else
    adopted), no_location, not_removable, no_test_left, exhausted or stopped.
    ``source`` is the repaired text once it loads, else the input. For exhausted,
    ``unmappable`` says whether the last failing lines named no unit."""

    status: str
    source: str
    removed: Tuple[str, ...] = ()
    failure: str = ""
    loads: int = 0
    stop: str = ""
    info: Mapping[str, Any] = field(default_factory=dict)
    unmappable: bool = False

    @property
    def repaired(self) -> bool:
        """Whether the repair removed units and the source then loads."""
        return self.status == LOADS and bool(self.removed)


def repair_loading(source: str, load: Callable[[str], Load],
                   remove: Callable[[str, Sequence[int]], Optional[Tuple[str, Sequence[str]]]],
                   *, loads: int,
                   keeps_a_test: Optional[Callable[[str], bool]] = None,
                   report_unmappable: bool = True) -> LoadRepair:
    """Load ``source`` at most ``loads`` times. After a load failing at known
    lines, ``remove`` returns the source without the units around them and
    their labels, or None when a line lies in no unit. With ``keeps_a_test``, a
    removal that leaves no test ends the repair. At the last failing load
    ``remove`` runs only for ``unmappable``, and ``report_unmappable=False`` skips it."""
    text = source
    removed: List[str] = []
    for count in range(1, loads + 1):
        result = load(text)
        if result.stop:
            return LoadRepair(STOPPED, source, (), result.failure, count, result.stop, result.info)
        if result.loads:
            return LoadRepair(LOADS, text if removed else source, tuple(removed), "", count)
        if not result.lines:
            return LoadRepair(NO_LOCATION, source, (), result.failure, count)
        if count == loads:
            unmappable = report_unmappable and remove(text, result.lines) is None
            return LoadRepair(EXHAUSTED, source, (), result.failure, count,
                              unmappable=unmappable)
        edit = remove(text, result.lines)
        if edit is None:
            return LoadRepair(NOT_REMOVABLE, source, (), result.failure, count)
        text, labels = edit[0], list(edit[1])
        removed += labels
        if keeps_a_test is not None and not keeps_a_test(text):
            return LoadRepair(NO_TEST_LEFT, source, tuple(removed), result.failure, count)
    raise AssertionError("loads must be positive")


#: The kinds of a unit pruning's end.
PRUNED, UNCHANGED = "pruned", "unchanged"


@dataclass(frozen=True)
class Failing:
    """What one look at a source found: its failing units, none, or why the
    language could not look (``stop``), with ``info`` for the language."""

    units: Sequence[Any] = ()
    stop: str = ""
    info: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UnitPruning:
    """The end of a unit pruning: pruned (``source`` without the ``removed``
    units), unchanged (nothing failed) or stopped (the language could not look,
    the input unchanged). ``rounds`` counts the looks. ``info`` is the last look's."""

    status: str
    source: str
    removed: Tuple[str, ...] = ()
    rounds: int = 0
    stop: str = ""
    info: Mapping[str, Any] = field(default_factory=dict)


def prune_units(source: str, find: Callable[[str], Failing],
                remove: Callable[[str, Sequence[Any]], Tuple[str, Sequence[str]]],
                *, rounds: int) -> UnitPruning:
    """Look at ``source`` at most ``rounds`` times. While a look finds failing
    units, ``remove`` returns the source without them and their labels. Every
    removal is kept although other faults remain (the result need not load). A
    look that cannot be made ends the pruning with the input unchanged."""
    text = source
    removed: List[str] = []
    found = Failing()
    count = 0
    for count in range(1, rounds + 1):
        found = find(text)
        if found.stop:
            return UnitPruning(STOPPED, source, (), count, found.stop, found.info)
        if not found.units:
            break
        text, labels = remove(text, found.units)
        removed += labels
    if not removed:
        return UnitPruning(UNCHANGED, source, (), count, "", found.info)
    return UnitPruning(PRUNED, text, tuple(removed), count, "", found.info)


def named_unit(name: str, qualified: Iterable[str]) -> Optional[str]:
    """The longest ``qualified`` unit name that ``name`` equals or ends with after
    a dot (``module.Class.test`` names ``Class.test``, not ``test``), else None.
    The language chooses the candidates and what removing one cuts."""
    best: Optional[str] = None
    for candidate in qualified:
        if (name == candidate or name.endswith("." + candidate)) and (
                best is None or len(candidate) > len(best)):
            best = candidate
    return best
