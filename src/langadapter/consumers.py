"""Consumers' requirement profiles: what a program that uses language
adapters needs of a language, by capability family.

A consumer publishes its requirement profile as the file ``langadapter_profile.json``
in its module's directory and is named by its module's name. LangAdapter reads
the file without importing the consumer. A profile grades capability families (``langadapter.hooks``) by
the consumer's levels as ``required``, ``optional`` (with what it improves and
its fallback) or ``not used``, the default, and may give the closing step of
``langadapter tutorial --for``. ``composed`` merges a consumer's profile with
those of its installed dependency closure, the strictest tier winning, and
keeps each requirement's source.
"""
from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from langadapter import hooks

#: The profile file's name in a consumer module's directory.
_PROFILE_FILE = "langadapter_profile.json"
#: The newest profile schema this version reads.
SCHEMA = 1
#: A family the consumer cannot run without.
REQUIRED = "required"
#: A family the consumer uses where it is there, with a fallback without it.
OPTIONAL = "optional"
#: A family the consumer does not use.
NOT_USED = "not used"
#: The tiers, the strictest first.
TIERS = (REQUIRED, OPTIONAL, NOT_USED)
_RANK = {REQUIRED: 2, OPTIONAL: 1, NOT_USED: 0}


class ProfileError(ValueError):
    """A profile that cannot be read, or does not follow the schema."""


@dataclass(frozen=True)
class Need:
    """One family's tier for one consumer and, if optional, what it improves and its
    fallback."""

    family: str
    tier: str
    level: Optional[int] = None
    improves: str = ""
    fallback: str = ""


@dataclass(frozen=True)
class Level:
    """A consumer's level: its number, its title and its families."""

    level: int
    title: str
    families: Tuple[str, ...]


@dataclass(frozen=True)
class TutorialRun:
    """A consumer's closing step of ``langadapter tutorial --for``: title,
    paragraphs, the command run as a separate process, and how to install it."""

    title: str
    text: Tuple[str, ...]
    command: Tuple[str, ...]
    install: str


@dataclass(frozen=True)
class Profile:
    """A consumer's requirement profile. ``version`` and ``distribution`` name
    what ships it (empty for a profile read from a file)."""

    consumer: str
    title: str
    levels: Tuple[Level, ...]
    needs: Mapping[str, Need]
    version: str = ""
    distribution: str = ""
    source: str = ""
    tutorial: Optional[TutorialRun] = None

    def need(self, name: str) -> Need:
        """The need of the family ``name``, not used when the profile omits it."""
        return self.needs.get(name) or Need(name, NOT_USED)


@dataclass(frozen=True)
class Problem:
    """A consumer whose profile could not be used, and why, in one sentence."""

    consumer: str
    message: str


@dataclass(frozen=True)
class Source:
    """Where a family's tier comes from: the naming consumer, its tier there,
    and the chain of consumers from the one asked for to it."""

    consumer: str
    title: str
    tier: str
    chain: Tuple[str, ...]

    def text(self) -> str:
        """The source as text, such as "required by Tool, via App"."""
        via = "".join(f", via {name}" for name in reversed(self.chain[:-1]))
        word = "for" if self.tier == OPTIONAL else "by"
        return f"{self.tier} {word} {self.title}{via}"


@dataclass(frozen=True)
class ComposedNeed:
    """A family's merged tier (the strictest of its sources), every optional
    source's texts, and the level it is shown in."""

    family: str
    tier: str
    level: str
    sources: Tuple[Source, ...]
    improves: Tuple[str, ...] = ()
    fallbacks: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Composed:
    """The merged requirements of consumers and their dependencies, the
    profiles used, notes on what was left out, and the problems found."""

    consumers: Tuple[str, ...]
    needs: Mapping[str, ComposedNeed]
    profiles: Tuple[Profile, ...]
    levels: Tuple[Tuple[str, Tuple[str, ...]], ...]
    notes: Tuple[str, ...] = ()
    problems: Tuple[Problem, ...] = ()

    def need(self, name: str) -> ComposedNeed:
        """The merged need of the family ``name``, of the tier ``NOT_USED``
        when no consumer names it."""
        return self.needs.get(name) or ComposedNeed(name, NOT_USED, "", ())


def parse(data: Any, *, version: str = "", distribution: str = "",
          source: str = "") -> Profile:
    """A profile from its JSON data. ProfileError names what is wrong."""
    if not isinstance(data, Mapping):
        raise ProfileError("a profile is a JSON object")
    schema = data.get("schema")
    if not isinstance(schema, int) or isinstance(schema, bool) or schema < 1:
        raise ProfileError("a profile names its schema, a whole number from 1")
    if schema > SCHEMA:
        raise ProfileError(f"the profile's schema {schema} is newer than this LangAdapter "
                           f"reads ({SCHEMA}): upgrade LangAdapter")
    consumer = data.get("consumer")
    if not isinstance(consumer, str) or not consumer.strip():
        raise ProfileError("a profile names its consumer")
    title = data.get("title") or consumer
    known = {f.name for f in hooks.families()}
    levels: List[Level] = []
    needs: Dict[str, Need] = {}
    raw_levels = data.get("levels")
    if not isinstance(raw_levels, list) or not raw_levels:
        raise ProfileError("a profile has at least one level")
    for raw in raw_levels:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("level"), int):
            raise ProfileError("each level has a number")
        names: List[str] = []
        for entry in raw.get("families") or ():
            if not isinstance(entry, Mapping):
                raise ProfileError(f"level {raw['level']} lists families as objects")
            name, tier = entry.get("family"), entry.get("tier")
            if name not in known or name == "facts":
                raise ProfileError(f"level {raw['level']} names {name!r}, which is no family: "
                                   f"the families are {', '.join(sorted(known - {'facts'}))}")
            if tier not in TIERS:
                raise ProfileError(f"the family {name!r} has the tier {tier!r}: "
                                   f"use {', '.join(TIERS)}")
            if name in needs:
                raise ProfileError(f"the family {name!r} appears twice")
            if tier == OPTIONAL and not (entry.get("improves") and entry.get("fallback")):
                raise ProfileError(f"the optional family {name!r} says what it improves and "
                                   "its fallback")
            needs[name] = Need(name, tier, raw["level"], str(entry.get("improves") or ""),
                               str(entry.get("fallback") or ""))
            names.append(name)
        levels.append(Level(raw["level"], str(raw.get("title") or f"Level {raw['level']}"),
                            tuple(names)))
    return Profile(consumer.strip(), str(title),
                   tuple(sorted(levels, key=lambda lv: lv.level)), needs, version,
                   distribution, source, _tutorial(data.get("tutorial")))


def _tutorial(raw: Any) -> Optional[TutorialRun]:
    """A profile's optional ``tutorial`` block."""
    if raw is None:
        return None

    def words(value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)

    if not (isinstance(raw, Mapping) and isinstance(raw.get("title"), str)
            and raw["title"].strip() and words(raw.get("command")) and raw["command"]
            and words(raw.get("text")) and isinstance(raw.get("install"), str)):
        raise ProfileError("the tutorial run names a title, a command of words, its text and "
                           "how to install the command")
    return TutorialRun(raw["title"].strip(), tuple(raw["text"]), tuple(raw["command"]),
                       raw["install"])


def read_file(path: Any) -> Profile:
    """A profile from a JSON file given directly, as an author tries it. Its
    ``source`` is ``path`` as given, so that a caller finds the profile of
    the path it gave."""
    file = Path(path)
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{file} cannot be read as JSON: {exc}") from None
    return parse(data, source=str(path))


def _canonical(name: str) -> str:
    from packaging.utils import canonicalize_name
    return canonicalize_name(name)


class Installed:
    """The installed distributions of this environment or ``path``, none of them
    imported."""

    def __init__(self, path: Optional[Sequence[str]] = None) -> None:
        self.path = None if path is None else [str(p) for p in path]
        self._by_name: Optional[Dict[str, Any]] = None

    def distributions(self) -> List[Any]:
        """Each installed distribution once: the first found, which imports
        use, rather than a stale copy later on the path."""
        if self._by_name is None:
            found = (metadata.distributions() if self.path is None
                     else metadata.distributions(path=self.path))
            self._by_name = {}
            for dist in found:
                dist_name = dist.metadata["Name"]
                if dist_name:
                    self._by_name.setdefault(_canonical(dist_name), dist)
        return list(self._by_name.values())

    def distribution(self, name: str) -> Optional[Any]:
        """The installed distribution named ``name``, compared in its
        canonical form, or None."""
        self.distributions()
        return self._by_name.get(_canonical(name))

    def profile_files(self) -> List[Tuple[str, Any, str]]:
        """``(consumer, distribution, path)`` for each installed module directory
        holding ``langadapter_profile.json``, the consumer named by its
        module, found without import and once, for the first distribution on
        the path: in each distribution's file list, for an editable install
        in the directories its ``.pth`` files add to the import path, and else in
        this environment where the import system shows the distribution's own
        packages (``top_level.txt``, or its own name). A ``.pth`` directory's
        package counts only for the distribution that names it so."""
        found: Dict[str, Tuple[str, Any, str]] = {}
        for dist in self.distributions():
            for consumer, path in self._profiles_of(dist):
                found.setdefault(consumer, (consumer, dist, path))
        return list(found.values())

    def _profiles_of(self, dist: Any) -> List[Tuple[str, str]]:
        try:
            files = [PurePosixPath(str(file)) for file in dist.files or ()]
            own = set((dist.read_text("top_level.txt") or "").split())
            own.add(_canonical(dist.metadata["Name"]).replace("-", "_"))
        except Exception:  # another package's broken metadata
            return []
        hits = _listed_profiles(files) + [hit for file in files if file.suffix == ".pth"
                                          for hit in _editable_profiles(Path(dist.locate_file(file)))
                                          if hit[0] in own]
        if not hits and self.path is None:
            hits = [(name, str(file)) for name in sorted(own)
                    if (file := _package_profile_file(name)) is not None]
        return hits

    def read(self, dist: Any, path: str) -> str:
        """The file ``path`` of ``dist``: an absolute path as it is, else from
        its installed files (directory or zip) or an editable install's package
        directory, found without import."""
        if Path(path).is_absolute():
            return Path(path).read_text(encoding="utf-8")
        located = dist.locate_file(path)
        if located.is_file():
            return located.read_text(encoding="utf-8")
        parts = PurePosixPath(path).parts
        if self.path is None and len(parts) > 1 and parts[0].isidentifier():
            spec = importlib.util.find_spec(parts[0])
            for package_dir in (spec.submodule_search_locations or ()) if spec else ():
                candidate = Path(package_dir).joinpath(*parts[1:])
                if candidate.is_file():
                    return candidate.read_text(encoding="utf-8")
        raise ProfileError(f"the file {path} is not in the installed distribution "
                           f"{dist.metadata['Name']}")


def discover(environment: Optional[Installed] = None
             ) -> Tuple[Dict[str, Profile], Tuple[Problem, ...]]:
    """Every installed consumer's profile, and the problems of unreadable ones."""
    environment = environment or Installed()
    found: Dict[str, Profile] = {}
    problems: List[Problem] = []
    for consumer, dist, path in environment.profile_files():
        try:
            data = json.loads(environment.read(dist, path))
            profile = parse(data, version=dist.version, distribution=dist.metadata["Name"],
                            source=path)
        except Exception as exc:  # one consumer's broken profile never stops the others
            problems.append(Problem(consumer, f"the profile of {consumer} "
                                    f"({dist.metadata['Name']} {dist.version}) cannot be used: "
                                    f"{exc}"))
            continue
        if profile.consumer != consumer:
            problems.append(Problem(consumer, f"the module {consumer}'s profile names the "
                                    f"consumer {profile.consumer}"))
            continue
        found[consumer] = profile
    return found, tuple(problems)


def not_installed(consumer: str) -> Problem:
    """The problem of a consumer that no installed package provides: its
    requirements cannot be read, and the message says how to install it."""
    return Problem(consumer, f"No installed package provides the consumer {consumer} in this "
                   "environment, so its requirements cannot be read. Install the package that "
                   f"provides it, for example pip install {consumer}, in this environment.")


def _listed_profiles(files: Sequence[PurePosixPath]) -> List[Tuple[str, str]]:
    """``(consumer, path)`` for each profile a distribution's file list names
    in a package directory."""
    return [(".".join(file.parts[:-1]), file.as_posix()) for file in files
            if file.name == _PROFILE_FILE and len(file.parts) > 1
            and all(part.isidentifier() for part in file.parts[:-1])]


def _editable_profiles(pth: Path) -> List[Tuple[str, str]]:
    """``(consumer, path)`` for each package directory holding a profile in the
    directories the ``.pth`` file ``pth`` adds to the import path: its lines
    that are neither comments nor imports, relative to its own directory."""
    try:
        lines = pth.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    roots = [pth.parent / line.strip() for line in lines
             if line.strip() and not line.startswith(("#", "import ", "import\t"))]
    return [(profile.parent.name, str(profile)) for root in roots
            for profile in sorted(root.glob(f"*/{_PROFILE_FILE}"))
            if profile.parent.name.isidentifier()]


def _package_profile_file(name: str) -> Optional[Path]:
    """The profile file in the directory of the package ``name``, found as
    ``resolve_extension`` finds a module, without import, or None where the
    package has none or is not found."""
    from langadapter.errors import InvalidRequest
    from langadapter.registry import _module_origin
    try:
        origin = Path(_module_origin(name))
    except InvalidRequest:
        return None
    file = origin.parent / _PROFILE_FILE
    return file if origin.name == "__init__.py" and file.is_file() else None


def _module_profile(name: str) -> Optional[Profile]:
    """The profile in the directory of the package ``name``, or None."""
    file = _package_profile_file(name)
    if file is None:
        return None
    found = read_file(file)
    if found.consumer != name:
        raise ProfileError(f"the module {name}'s profile names the consumer {found.consumer}")
    return found


def _unpublished(name: str, environment: Installed) -> Tuple[Optional[Profile], Optional[Problem]]:
    """The profile in the directory of the package ``name`` of this environment,
    or the problem why there is none."""
    try:
        found = _module_profile(name) if environment.path is None else None
    except ProfileError as exc:
        return None, Problem(name, str(exc))
    return (found, None) if found is not None else (None, not_installed(name))


def profile(name: str, environment: Optional[Installed] = None) -> Profile:
    """The profile of the consumer ``name``: an installed distribution's, else
    the one in the directory of the package ``name``. ProfileError says why
    there is none."""
    found, problems = discover(environment)
    if name in found:
        return found[name]
    for problem in problems:
        if problem.consumer == name:
            raise ProfileError(problem.message)
    module, problem = _unpublished(name, environment or Installed())
    if module is None:
        raise ProfileError(problem.message)
    return module


def _requirements(dist: Any, environment: Installed, publishing: Iterable[str] = ()
                  ) -> Tuple[List[str], List[str]]:
    """The distributions ``dist`` requires here (markers holding, or of an extra
    whose requirements are all installed), and a note on each extra left out
    that requires a profile-publishing distribution (``publishing``)."""
    publishing = set(publishing)
    from packaging.requirements import InvalidRequirement, Requirement

    plain: List[str] = []
    by_extra: Dict[str, List[str]] = {}
    for text in dist.requires or ():
        try:
            requirement = Requirement(text)
        except InvalidRequirement:
            continue
        marker = requirement.marker
        try:
            if marker is None or marker.evaluate({"extra": ""}):
                plain.append(requirement.name)
                continue
            for extra in dist.metadata.get_all("Provides-Extra") or ():
                if marker.evaluate({"extra": extra}):
                    by_extra.setdefault(extra, []).append(requirement.name)
        except Exception:  # a marker this packaging cannot evaluate
            continue
    notes: List[str] = []
    for extra, names in sorted(by_extra.items()):
        if all(environment.distribution(name) is not None for name in names):
            plain.extend(names)
        elif any(_canonical(name) in publishing for name in names):
            notes.append(f"{dist.metadata['Name']}'s extra {extra} is not installed, so "
                         f"{', '.join(sorted(set(names)))} is left out")
    return plain, notes


def composed(consumers: Sequence[str] = (), *, files: Sequence[Any] = (),
             environment: Optional[Installed] = None,
             discovered: Optional[Tuple[Dict[str, Profile], Tuple[Problem, ...]]] = None
             ) -> Composed:
    """The merged requirements of ``consumers`` (with their installed dependency
    closures) and of the profiles in ``files``: the strictest tier wins, each
    family keeps its sources, and a consumer not installed is a problem."""
    environment = environment or Installed()
    installed, problems = discovered if discovered is not None else discover(environment)
    problems = list(problems)
    by_distribution: Dict[str, List[Profile]] = {}
    for found in installed.values():
        by_distribution.setdefault(_canonical(found.distribution), []).append(found)
    entries: List[Tuple[Profile, Tuple[str, ...]]] = []
    notes: List[str] = []
    for name in consumers:
        root = installed.get(name)
        if root is None:
            if not any(p.consumer == name for p in problems):
                module, problem = _unpublished(name, environment)
                if module is None:
                    problems.append(problem)
                else:
                    entries.append((module, (module.title,)))
                    notes.append(f"the profile of {name} is read from its module's directory, which "
                                 "no installed distribution lists, so the requirements of the "
                                 "tools it builds on are not added")
            continue
        seen = {_canonical(root.distribution)}
        queue: List[Tuple[Any, Tuple[str, ...]]] = [
            (environment.distribution(root.distribution), (root.title,))]
        entries.append((root, (root.title,)))
        while queue:
            dist, chain = queue.pop(0)
            if dist is None:
                continue
            names, extra_notes = _requirements(dist, environment, by_distribution)
            notes.extend(extra_notes)
            for required in names:
                key = _canonical(required)
                if key in seen:
                    continue
                seen.add(key)
                child = environment.distribution(required)
                if child is None:
                    continue
                step = chain
                for found in by_distribution.get(key, ()):
                    step = chain + (found.title,)
                    entries.append((found, step))
                queue.append((child, step))
    for path in files:
        try:
            given = read_file(path)
        except ProfileError as exc:
            problems.append(Problem(str(path), str(exc)))
            continue
        entries.append((given, (given.title,)))
        notes.append(f"the requirement profile {path} is used alone: the requirements of the "
                     "tools it builds on are not added, since it is not an installed package")
    needs: Dict[str, ComposedNeed] = {}
    for found, chain in entries:
        for name, need in found.needs.items():
            source = Source(found.consumer, found.title, need.tier, chain)
            level = f"{found.title} level {need.level}"
            previous = needs.get(name)
            improves = (need.improves,) if need.improves else ()
            fallbacks = (need.fallback,) if need.fallback else ()
            if previous is None:
                needs[name] = ComposedNeed(name, need.tier, level, (source,), improves, fallbacks)
                continue
            winner = need.tier if _RANK[need.tier] > _RANK[previous.tier] else previous.tier
            needs[name] = ComposedNeed(
                name, winner, previous.level if winner == previous.tier else level,
                previous.sources + (source,), previous.improves + improves,
                previous.fallbacks + fallbacks)
    levels: List[Tuple[str, Tuple[str, ...]]] = []
    # An unused family belongs to no level, so it never keeps one incomplete.
    used = {key: need for key, need in needs.items() if need.tier != NOT_USED}
    for name in sorted({n.level for n in used.values()}, key=_level_order):
        levels.append((name, tuple(f.name for f in hooks.families()
                                   if f.name in used and used[f.name].level == name)))
    return Composed(tuple(consumers), needs, tuple(found for found, _ in entries), tuple(levels),
                    tuple(dict.fromkeys(notes)), tuple(problems))


def _level_order(label: str) -> Tuple[int, str]:
    number = label.rsplit(" ", 1)[-1]
    return (int(number) if number.isdigit() else 99, label)



__all__ = [
    "SCHEMA", "REQUIRED", "OPTIONAL", "NOT_USED", "TIERS", "ProfileError", "Need",
    "Level", "Profile", "TutorialRun", "Problem", "Source", "ComposedNeed", "Composed",
    "Installed", "parse",
    "read_file", "discover", "profile", "not_installed", "composed",
]
