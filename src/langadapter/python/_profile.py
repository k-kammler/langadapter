"""The Python adapter's version profile: every decision that depends on the
project's or the library's Python version."""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from langadapter.python._cr_session_runner import EVENT_LOOP_COSMIC_RAY
from langadapter.versions import (
    LanguageSupport, RuntimeSupport, Support, Version, VersionProfile, VersionSpan,
)


@dataclass(frozen=True)
class Tools:
    """The tools a project Python is measured with: their pinned
    ``versions``, the ``coverage_core`` coverage.py measures with (None: its
    own choice), and whether Cosmic Ray starts with an ``event_loop`` set."""

    versions: Mapping[str, str]
    coverage_core: Optional[str] = None
    event_loop: bool = False


#: The tools by project Python: the first entry whose span holds the
#: version. The last holds every other version, an unknown one included.
#: Python 3.14 evaluates annotations lazily (PEP 649), which pytest reads
#: from 8.4.2 on. Coverage 7.16.2 has 3.14 wheels and fixes, and stays on
#: the C tracer, whose arcs every other version records (it would choose
#: sys.monitoring there). Cosmic Ray's HTTP distributor asks asyncio for the
#: current event loop, which 3.14 no longer creates on request.
TOOLS: Tuple[Tuple[Optional[VersionSpan], Tools], ...] = (
    (VersionSpan((3, 14), None),
     Tools({"pytest": "8.4.2", "coverage": "7.16.2", "cosmic-ray": "8.4.1"},
           coverage_core="ctrace", event_loop=True)),
    (None, Tools({"pytest": "8.1.1", "coverage": "7.6.8", "cosmic-ray": "8.4.1"})),
)
#: The pinned tools by their package names, the same for every project
#: Python, with the module a run imports for each.
TOOL_MODULES = {"pytest": "pytest", "coverage": "coverage", "cosmic-ray": "cosmic_ray"}
#: The pinned tools' names.
TOOL_NAMES = tuple(TOOL_MODULES)


def tools_of(runtime: Optional[Version]) -> Tools:
    """The tools of the project Python ``runtime`` (None: an unknown one)."""
    numbers = runtime.numbers if runtime is not None else ()
    return next(tools for span, tools in TOOLS if span is None or span.holds(numbers))


_EARLIEST = TOOLS[-1][1].versions
#: The tested and expected project Pythons. 2.x lies below the range:
#: Python 2 would be a language of its own.
SUPPORT = LanguageSupport(
    runtime=RuntimeSupport(
        "project Python", tested=(VersionSpan((3, 10), (3, 15)),),
        expected=(VersionSpan((3, 9), (3, 9)),),
        reason=(f"the pinned tools need Python 3.9 (coverage {_EARLIEST['coverage']} and "
                f"Cosmic Ray {_EARLIEST['cosmic-ray']})")))
#: The unit Python's prune_collection_breakers removes.
COLLECTION_UNIT = ("the smallest module-level or class-level statement around the line of "
                   "the file that pytest's collection traceback names, replaced by pass")


def library_parser() -> str:
    """The parser that reads project sources in the library's process."""
    return f"{sys.implementation.name} {sys.version.split()[0]}; Python ast"


def parser_rejects(message: str) -> str:
    """The reading error for a source the library's parser rejects."""
    return f"the parser of Python {'.'.join(map(str, PythonProfile.library[:2]))} cannot read the source: {message}"


def _named(text: str) -> str:
    """A Python version as a sentence names it: "Python 3.12.3"."""
    return text if text.startswith("Python") else f"Python {text}"


class PythonProfile(VersionProfile):
    """The version-dependent answers for a project Python, classified
    against the Python declaration, and for the library's own Python."""

    #: The library's own Python, whose parser reads the project's sources.
    library: Tuple[int, ...] = tuple(sys.version_info[:3])

    def refusal(self) -> str:
        """Why no operation runs on this project Python, or "" when they run."""
        if self.support is not Support.UNSUPPORTED:
            return ""
        return (f"{_named(self.runtime.text)} is not supported: the Python adapter runs project "
                f"Python {self.declaration.runtime.describe()}, since {self.declaration.runtime.reason}")

    def action(self) -> str:
        """The next step for a refused project Python."""
        return f"select a Python {self.declaration.runtime.oldest()} or newer interpreter"

    @property
    def tools(self) -> Dict[str, str]:
        """The tool versions pinned for this project Python."""
        return dict(tools_of(self.runtime).versions)

    def coverage_settings(self) -> str:
        """The coverage.py configuration of a measured run: branch coverage,
        on the core this project Python's tools name."""
        core = tools_of(self.runtime).coverage_core
        return "[run]\nbranch = True\n" + (f"core = {core}\n" if core else "")

    def cosmic_ray_command(self, python: str, cosmic_ray: str) -> List[str]:
        """The start of a Cosmic Ray command line: the console script
        ``cosmic_ray``, or the project interpreter ``python`` running Cosmic
        Ray's command line once it has set an event loop."""
        if not tools_of(self.runtime).event_loop:
            return [cosmic_ray]
        return [python, "-c", EVENT_LOOP_COSMIC_RAY]

    def note(self) -> str:
        """The setup check's note on an untested project Python or on the
        library's parser, or ""."""
        notes = []
        tested = ", ".join(span.text() for span in self.declaration.runtime.tested)
        if self.support is Support.EXPECTED:
            notes.append(f"{_named(self.runtime.text)} is expected to work but is not among the "
                         f"tested versions ({tested})")
        elif self.support is Support.UNTESTED and self.runtime is not None and self.runtime.numbers:
            notes.append(f"{_named(self.runtime.text)} is not among the tested versions ({tested})")
        if not self.library_reads_project_syntax:
            notes.append(f"LangAdapter runs on {self._library_text()} and reads this project's "
                         f"Python {self._project_text()} sources with its own parser. A source "
                         "using newer syntax is reported as unreadable")
        return ". ".join(notes)

    @property
    def library_reads_project_syntax(self) -> bool:
        """Whether the library's parser knows the project Python's syntax:
        its Python is not older (true for an unknown project version)."""
        if self.runtime is None or len(self.runtime.numbers) < 2:
            return True
        return self.library[:2] >= self.runtime.numbers[:2]

    def reading_note(self) -> str:
        """A reading error's note when the library's Python is older than
        the project's, with its one action, or ""."""
        if self.library_reads_project_syntax:
            return ""
        return (f"LangAdapter runs on {self._library_text()}, older than this project's Python "
                f"{self._project_text()}, and its parser does not know the newer syntax: run the "
                f"program that uses LangAdapter on Python {self._project_text()} or newer")

    def _library_text(self) -> str:
        return "Python " + ".".join(map(str, self.library[:2]))

    def _project_text(self) -> str:
        return ".".join(map(str, self.runtime.numbers[:2]))
