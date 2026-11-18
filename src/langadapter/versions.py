"""Versions of runtimes and tools, and what a language supports.

Every adapter describes a runtime's or tool's version as a ``Version``. Each
language declares the versions it is tested with and expected to work with
(``LanguageSupport``), against which setup and runs classify a found version.
An adapter asks every version-dependent question through one
``VersionProfile``, chosen once from the versions it found.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import enum
import re
from typing import Mapping, Optional, Tuple

from langadapter.errors import InvalidRequest

#: Where a version was read from.
SOURCES = ("probe", "context", "release file", "guest probe", "jar name", "metadata",
           "pinned", "library", "unknown")


@dataclass(frozen=True)
class Version:
    """A runtime's or a tool's version: ``text`` as found, ``source`` where (one
    of ``SOURCES``), and ``numbers`` its leading numeric parts ((3, 12, 10) for
    "3.12.10", (8, 0, 412) for the JDK text "1.8.0_412"), empty if it has none."""

    implementation: str
    numbers: Tuple[int, ...]
    text: str
    source: str

    def __post_init__(self) -> None:
        if not isinstance(self.implementation, str) or not self.implementation:
            raise InvalidRequest("implementation", "must be nonempty text")
        if not isinstance(self.text, str):
            raise InvalidRequest("text", "must be text")
        numbers = tuple(self.numbers)
        if any(type(number) is not int or number < 0 for number in numbers):
            raise InvalidRequest("numbers", "must be nonnegative integers")
        object.__setattr__(self, "numbers", numbers)
        if self.source not in SOURCES:
            raise InvalidRequest("source", f"must be one of {', '.join(SOURCES)}")

    @property
    def feature(self) -> Optional[int]:
        """The first number (Python's major, a JDK's feature version), or None."""
        return self.numbers[0] if self.numbers else None


_LEADING = re.compile(r"\s*(\d+(?:\.\d+)*)")


def numbers_of(text: str) -> Tuple[int, ...]:
    """The leading dotted numbers of ``text``: "3.13.0rc1" gives (3, 13, 0)."""
    match = _LEADING.match(text or "")
    return tuple(int(part) for part in match.group(1).split(".")) if match else ()


def unknown(implementation: str) -> Version:
    """The version of a runtime that was not found: the text "unknown"."""
    return Version(implementation, (), "unknown", "unknown")


def described(implementation: str, text: Optional[str], source: str) -> Version:
    """A version stated as text (a context's runtime line), else the unknown one."""
    return unknown(implementation) if not text else Version(implementation, numbers_of(text), text, source)


def python_version(implementation: str, text: str, source: str) -> Version:
    """A Python runtime's version from ``platform.python_version()``'s text."""
    return Version(implementation, numbers_of(text), text, source)


def python_line(text: str, source: str) -> Version:
    """A Python version line ("Python 3.12.3", or a context's), kept whole, with its numbers."""
    match = re.search(r"\d+(?:\.\d+)*", text or "")
    return Version("python", numbers_of(match.group(0)) if match else (), text, source)


def java_version(text: str, source: str, implementation: str = "java") -> Version:
    """A JDK's version from its ``JAVA_VERSION`` text. The legacy scheme
    "1.8.0_412" is the feature version 8 with its update: (8, 0, 412)."""
    value = (text or "").strip()
    parts = value.split(".")
    if parts[0] == "1" and len(parts) > 1:
        value = ".".join(parts[1:]).replace("_", ".")
    return Version(implementation, numbers_of(value.replace("_", ".")), text, source)


class Support(enum.Enum):
    """How a language supports a version: tested, expected to work, untested or unsupported."""

    TESTED = "tested"
    EXPECTED = "expected"
    UNTESTED = "untested"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class VersionSpan:
    """The versions whose leading numbers lie from ``low`` to ``high`` (None: no
    bound): (3, 10) to (3, 13) holds 3.10.0 to 3.13.x, (11,) to (11,) every JDK 11."""

    low: Tuple[int, ...]
    high: Optional[Tuple[int, ...]]

    def __post_init__(self) -> None:
        object.__setattr__(self, "low", tuple(self.low))
        if self.high is not None:
            object.__setattr__(self, "high", tuple(self.high))
        if not self.low or (self.high is not None and (not self.high or self.high < self.low)):
            raise InvalidRequest("span", "needs a low bound not above its high bound")

    def holds(self, numbers: Tuple[int, ...]) -> bool:
        """Whether the version numbers ``numbers`` lie in the span, compared
        by as many leading numbers as each bound has."""
        if not numbers or numbers[:len(self.low)] < self.low:
            return False
        return self.high is None or numbers[:len(self.high)] <= self.high

    def text(self) -> str:
        """The span as text, such as "3.10 to 3.13", "11" or "3.10 or
        newer"."""
        low = ".".join(map(str, self.low))
        if self.high is None:
            return f"{low} or newer"
        high = ".".join(map(str, self.high))
        return low if high == low else f"{low} to {high}"


@dataclass(frozen=True)
class RuntimeSupport:
    """The runtime versions a language is tested with and expected to work
    with. A version below every span is unsupported: the language's profile
    refuses it and names ``reason``, why that version is too old. Any other
    outside every span, or without numbers, is untested, as is one with fewer
    numbers than the oldest bound that matches its first ones ("3" for 3.9)."""

    name: str
    tested: Tuple[VersionSpan, ...]
    expected: Tuple[VersionSpan, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "tested", tuple(self.tested))
        object.__setattr__(self, "expected", tuple(self.expected))

    def classify(self, version: Optional[Version]) -> Support:
        """The support of the runtime ``version``: tested, expected,
        unsupported or untested, as the class says."""
        if version is None or not version.numbers:
            return Support.UNTESTED
        if any(span.holds(version.numbers) for span in self.tested):
            return Support.TESTED
        if any(span.holds(version.numbers) for span in self.expected):
            return Support.EXPECTED
        lows = [span.low for span in (*self.tested, *self.expected)]
        if lows:
            # Only the numbers both give compare: "3" is not below 3.9.
            shared = min(len(version.numbers), len(min(lows)))
            if version.numbers[:shared] < min(lows)[:shared]:
                return Support.UNSUPPORTED
        return Support.UNTESTED

    def oldest(self) -> Optional[str]:
        """The oldest version that is tested or expected, as text."""
        spans = [*self.tested, *self.expected]
        return min(spans, key=lambda span: span.low).text().split(" ")[0] if spans else None

    def describe(self) -> str:
        """The range in words: "3.9 or newer (tested 3.10 to 3.15)"."""
        tested = ", ".join(span.text() for span in self.tested)
        oldest = self.oldest()
        return f"{oldest} or newer (tested {tested})" if oldest else "no declared versions"


@dataclass(frozen=True)
class Wording:
    """How one runtime implementation's versions read in reports: templates over
    ``implementation`` and ``text`` for ``CapabilityReport.runtime``, a runtime's
    ``ProjectSetupItem.version`` and ``LanguageFacts.runtime_version``."""

    capability: str = "{implementation} {text}"
    setup: str = "{implementation} {text}"
    facts: str = "{text}"


#: The wording of an implementation no language declares one for.
DEFAULT_WORDING = Wording()
_WORDING: dict = {}


def declare_wording(implementation: str, wording: Wording) -> None:
    """Record how ``implementation``'s versions read (``LanguageSupport.wording``).
    Two languages cannot declare different wordings for one implementation."""
    known = _WORDING.get(implementation)
    if known is not None and known != wording:
        raise InvalidRequest("wording", f"{implementation} already reads {known}")
    _WORDING[implementation] = wording


def _worded(template: str, version: Version) -> str:
    return template.format(implementation=version.implementation, text=version.text)


def capability_runtime(version: Version) -> str:
    """``CapabilityReport.runtime`` of a report whose runtime is ``version``."""
    return _worded(_WORDING.get(version.implementation, DEFAULT_WORDING).capability, version)


def setup_runtime(version: Version) -> str:
    """``ProjectSetupItem.version`` of a runtime item whose runtime is ``version``."""
    return _worded(_WORDING.get(version.implementation, DEFAULT_WORDING).setup, version)


def facts_runtime(version: Version) -> str:
    """``LanguageFacts.runtime_version`` of facts whose runtime is ``version``."""
    return _worded(_WORDING.get(version.implementation, DEFAULT_WORDING).facts, version)


@dataclass(frozen=True)
class LanguageSupport:
    """What a language supports: its runtime's versions and how its runtime
    versions read in reports."""

    runtime: Optional[RuntimeSupport] = None
    wording: Mapping[str, Wording] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "wording", dict(self.wording))

    def classify(self, version: Optional[Version]) -> Support:
        """The support of the runtime ``version``
        (``RuntimeSupport.classify``), untested when the language declares
        no runtime."""
        return Support.UNTESTED if self.runtime is None else self.runtime.classify(version)


#: The declaration of a language that declares nothing: every version untested.
UNDECLARED = LanguageSupport()


class VersionProfile:
    """The version-dependent behavior of one adapter instance, chosen once from
    the versions it found. Languages subclass it with their operations' questions."""

    def __init__(self, runtime: Optional[Version], support: LanguageSupport = UNDECLARED) -> None:
        self.runtime = runtime
        self.declaration = support
        self.support = support.classify(runtime)

    def refusal(self) -> str:
        """Why no execution-backed operation runs on this runtime, or "". The base refuses
        none."""
        return ""

    def note(self) -> str:
        """What the setup check adds about this runtime, or ""."""
        return ""

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.runtime!r}, {self.support.value})"
