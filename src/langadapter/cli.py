"""The ``langadapter`` command and the new-language scaffold.

``langadapter check [LANGUAGE] [--extension ITEM]... [--for ITEM]...
[--example DIR]`` loads each extension item, then checks a language
adapter, neutrally or for consumers, each named by its module or by a
requirement profile. With ``--example`` it runs the language's suite on a
directory.
Without a language it lists the registered languages, the installed
consumers and the problems found. ``langadapter tutorial`` builds an adapter
for a tiny example language step by step. ``langadapter toolchains LANGUAGE...``
installs the named languages' tools on this system (``langadapter.toolchains``).

``langadapter new-language LANGUAGE --file-extension EXT [--for ITEM]...``
writes one file, ``LANGUAGE.py``, from the hook table
(``langadapter.hooks``): the adapter with every fact and hook in build-up
order, commented out so the check reads each as missing until written, and
the line ``langadapter_language = <Class>`` that names it to LangAdapter.
With consumers (``langadapter.consumers``) each hook carries its tier, a
required one without a default marked ``TODO`` with its level and source.
``--diff`` shows what the consumers recorded in a shared language package's
``pyproject.toml`` require now, writing nothing. ``--update-record`` records
their versions.
"""
from __future__ import annotations

import argparse
import sys
from typing import Any, List, Optional, Sequence, Dict, Tuple
import re
import textwrap
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from langadapter import consumers as _consumers, hooks as _hooks
from langadapter._words import listed


def parser(prog: str = "langadapter") -> argparse.ArgumentParser:
    """The command line ``main`` parses: its commands, options and help texts.
    ``prog`` names the command in usage lines."""
    result = argparse.ArgumentParser(prog=prog, allow_abbrev=False,
                                     description="Language adapters for test tools.")
    commands = result.add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check", allow_abbrev=False,
        help="Check a language adapter, for every consumer or for one (--for).",
        description="Check a language adapter: which facts and hooks it provides, in the "
                    "order of the reference page Hooks and facts, or by the levels of a "
                    "consumer.")
    check.add_argument("language", nargs="?",
                       help="A registered language. Without it, list the languages and "
                            "consumers.")
    check.add_argument("--extension", dest="extensions", action="append", default=[],
                       metavar="ITEM", help="Load ITEM first, a file such as toy.py or an "
                                            "installed module that adds a language or a "
                                            "metric provider "
                                            "(repeatable).")
    check.add_argument("--for", dest="consumers", action="append", default=[],
                       metavar="ITEM", help="Check what a consumer needs: ITEM "
                                            "names it by its module, or names its "
                                            "requirement profile, a .json file (repeatable).")
    check.add_argument("--example", default="", metavar="DIR",
                       help="Run the language's suite on the test files in the directory DIR, "
                            "found by its test file rule.")
    new = commands.add_parser(
        "new-language", allow_abbrev=False,
        help="Write a new language's file, with every hook commented out.",
        description="Write a new language as one file, LANGUAGE.py: its adapter with every "
                    "fact and hook in the order of the reference page Hooks and facts, and "
                    "the line that names it to LangAdapter. With --for, the hooks the "
                    "consumer requires are marked TODO with its levels.")
    new.add_argument("language", help="The language's name, such as lua.")
    new.add_argument("--file-extension", help="Its source file extension, such as .lua (required "
                                         "unless --diff or --update-record).")
    new.add_argument("--for", dest="consumers", action="append", default=[],
                     metavar="ITEM", help="Write it for a consumer: ITEM names "
                                          "it by its module, or names its requirement profile, "
                                          "a .json file (repeatable).")
    new.add_argument("--test-pattern", default="", metavar="PATTERN",
                     help="The test file pattern, such as *_test.lua, with one * for the "
                          "source file's stem. It gives the adapter's test_file_name "
                          "(default test_*<file extension>).")
    new.add_argument("--runtime", default="", metavar="PROGRAM",
                     help="The program that runs the tests, such as lua. Without it, the "
                          "adapter marks it TODO.")
    new.add_argument("--grammar", default="", metavar="NAME",
                     help="A tree-sitter grammar for the toolkit, such as tree-sitter-lua.")
    new.add_argument("--runtime-name", default="", metavar="NAME",
                     help="The runtime's name in version messages (default: the --runtime value).")
    new.add_argument("--tested-runtime", default="", metavar="RANGE",
                     help="The tested runtime versions, such as 5.3-5.4. Without it, versions "
                          "stay undeclared.")
    new.add_argument("--expected-runtime", default="", metavar="RANGE",
                     help="Versions expected to work but not tested (needs --tested-runtime).")
    new.add_argument("--output-dir", default="", metavar="DIR",
                     help="The directory to write LANGUAGE.py in (default: the current "
                          "directory). With --diff or --update-record, the shared package's "
                          "directory.")
    new.add_argument("--diff", action="store_true",
                     help="Compare the consumer versions recorded in a shared package's "
                          "pyproject.toml with what the installed consumers require now, "
                          "and write nothing.")
    new.add_argument("--update-record", action="store_true",
                     help="Record the installed consumers' versions in a shared package's "
                          "pyproject.toml.")
    tutorial = commands.add_parser(
        "tutorial", allow_abbrev=False,
        help="Build an adapter for a tiny example language, step by step.",
        description="Build an adapter for the Toy, a tiny example language, step by step: "
                    "run its tests, find and remove tests, measure coverage, run mutation "
                    "testing, repair loading and declare versions, each step checked.")
    tutorial.add_argument("--for", dest="consumers", action="append", default=[],
                          metavar="ITEM",
                          help="Also show what a consumer needs, from its installed "
                               "requirement profile: ITEM names it by its module "
                               "(repeatable).")
    from langadapter.tutorial import _add_options

    _add_options(tutorial, "langadapter-tutorial")
    toolchains = commands.add_parser(
        "toolchains", allow_abbrev=False,
        help="Install the tools of the named languages, each proved by the check.",
        description="Install the tools of the named languages for this system, each language's "
                    "part ending with langadapter check on a small example. The parts run on "
                    "Ubuntu 24.04, also in WSL and in a container.")
    toolchains.add_argument("languages", nargs="+", metavar="LANGUAGE",
                            help="python, java, c, a language an extension adds, or all.")
    toolchains.add_argument("--into", default="", metavar="DIR",
                            help="Install below DIR, by default the user's data directory "
                                 "(langadapter/toolchains there).")
    toolchains.add_argument("--list", dest="list_only", action="store_true",
                            help="List each part and what it downloads, and run none.")
    toolchains.add_argument("--extension", dest="extensions", action="append", default=[],
                            metavar="ITEM", help="Load ITEM first, a file or an installed "
                                                 "module that adds a language (repeatable).")
    return result


def _new_language_command(options: argparse.Namespace) -> int:
    from pathlib import Path


    project_dir = Path(options.output_dir) if options.output_dir else None
    if options.diff or options.update_record:
        project_dir = project_dir or Path.cwd()
        if not (project_dir / "pyproject.toml").is_file():
            print(f"langadapter new-language: {project_dir} holds no pyproject.toml. Name the "
                  "package's directory with --output-dir.", file=sys.stderr)
            return 2
        refusal = _record_refusal(options)
        if refusal:
            print(f"langadapter new-language: {refusal}", file=sys.stderr)
            return 2
        if options.diff:
            print("\n".join(_diff(project_dir, options.consumers)))
        else:
            print("\n".join(_update_record(project_dir)))
        return 0
    if not options.file_extension:
        print("langadapter new-language: --file-extension is required, such as --file-extension .lua.",
              file=sys.stderr)
        return 2
    consumers, files = _split_for(options.consumers)
    try:
        written = _new_language(_Options(
            options.language, options.file_extension, project_dir=project_dir,
            consumers=consumers, profiles=files,
            test_pattern=options.test_pattern, runtime=options.runtime,
            grammar=options.grammar, runtime_name=options.runtime_name,
            tested_runtime=options.tested_runtime, expected_runtime=options.expected_runtime))
    except _ScaffoldError as exc:
        print(f"langadapter new-language: {exc}", file=sys.stderr)
        return 2
    print(f"Wrote {written}.")
    print(f"Next: write run_suite, then langadapter check {options.language} --extension "
          f"{written if options.output_dir else written.name}"
          + "".join(f" --for {item}" for item in options.consumers) + ".")
    return 0


def _overview() -> List[str]:
    from langadapter import consumers
    from langadapter.registry import names

    lines = ["Registered languages: " + (", ".join(names()) or "none")]
    found, problems = consumers.discover()
    if found:
        lines.append("Installed consumers: " + ", ".join(
            f"{p.title} {p.version}".strip() for p in found.values()))
    else:
        lines.append("Installed consumers: none")
    if problems:
        lines.append("Problems:")
        lines.extend(f"  {p.message}" for p in problems)
    return lines


def _record_refusal(options: argparse.Namespace) -> str:
    """Why ``--diff`` or ``--update-record`` cannot take the ``--for`` items,
    or "": both read installed consumers' versions, which a profile file lacks,
    and ``--update-record`` updates the versions of the consumers pyproject.toml
    records."""
    if options.update_record and options.consumers:
        return ("--update-record updates the versions of the consumers pyproject.toml records. "
                "Leave out --for.")
    files = _split_for(options.consumers)[1]
    if files:
        return ("--diff compares installed consumers' versions, so --for names a consumer's "
                f"module, not a profile file: {', '.join(files)}.")
    return ""


def _split_for(items: Sequence[str]) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """``--for`` items as consumers' module names and profile files: an item
    that ends in ``.json`` or holds a path separator is a file."""
    from langadapter.registry import _is_file_item
    files = tuple(item for item in items if _is_file_item(item, ".json"))
    return tuple(item for item in items if item not in files), files


def _load_extensions(items: Sequence[str], command: str = "check") -> Optional[List[Any]]:
    """Each ``--extension`` item loaded, relative to the current directory, with
    its file and SHA-256 printed, or None once one cannot be loaded, which
    ``command`` names."""
    from langadapter.errors import InvalidRequest, RegistrationConflict
    from langadapter.registry import _loaded_metric_providers, load_extension
    loaded = []
    for item in items:
        try:
            extension = load_extension(item)
        except (InvalidRequest, RegistrationConflict) as exc:
            print(f"langadapter {command}: {exc}", file=sys.stderr)
            return None
        provider = next((held for held, source in _loaded_metric_providers()
                         if source is extension), None)
        print(f"Extension {item}: {extension.file}, SHA-256 {extension.sha256}"
              + (_provider_sentence(provider) if provider is not None else ""))
        loaded.append(extension)
    return loaded


def _provider_sentence(provider: Any) -> str:
    """The sentence that names a metric provider an extension added and
    the metrics it measures."""
    metrics = [definition.name for definition in provider.definitions()]
    if not metrics:
        return f". It adds the metric provider {provider.name}."
    return f". It adds the metric provider {provider.name}, which measures {listed(metrics)}."


def _not_registered(language: str, loaded: Sequence[Any]) -> str:
    """What to do about the unknown ``language``: add the line a loaded file
    lacks where it defines the language's adapter class without naming it,
    else load the file or module that adds the language."""
    from langadapter.adapter import LangAdapter
    wanted = language.strip().lower()
    for extension in loaded:
        module = sys.modules.get(extension.module or "")
        classes = [] if module is None or extension.languages else [
            value for value in vars(module).values()
            if isinstance(value, type) and issubclass(value, LangAdapter)
            and str(getattr(value, "language", "")).strip().lower() == wanted]
        if classes:
            return (f"{extension.item} defines {classes[0].__name__} for {wanted} but names no "
                    f"langadapter_language: add the line "
                    f"langadapter_language = {classes[0].__name__}.")
    return (f"Name the file or installed module that adds {wanted} with --extension, such as "
            f"langadapter check {wanted} --extension {wanted}.py.")


def _test_file_rule(adapter: Any) -> str:
    """The language's test file rule in words: the patterns its facts name,
    else its ``is_test_file``."""
    try:
        patterns = adapter.language_facts().test_file_patterns
    except Exception:  # an unfinished adapter is the normal case
        patterns = ()
    if patterns:
        return f"whose test files are named {listed(patterns, 'or')}"
    return "as its is_test_file names them"


def _example(language: str, example_dir: Path) -> Any:
    """The example of ``example_dir`` for ``language``: its test files by the
    language's test file rule, run by the program the runtime fact names,
    found on PATH, and the coverage of its source file when it holds one
    (``check._example_sources``), as a conformance test runs an example.
    A runtime not on PATH or a directory without a test file by the
    language's rule raises InvalidRequest before anything runs."""
    import shutil

    import langadapter as la
    from langadapter.check import _example_sources
    from langadapter.conformance import AdapterExample
    from langadapter.errors import InvalidRequest
    root = example_dir.resolve()
    runtime = getattr(la.registration(language).factory, "runtime", None)
    found = shutil.which(runtime) if runtime else None
    if runtime and found is None:
        raise InvalidRequest("--example", (
            f"the example's tests run with {runtime}, the language's runtime, and no {runtime} "
            f"is on PATH. Put the project's {runtime} first on PATH, such as by activating its "
            "environment."))
    context = la.ProjectContext(str(root), la.Interpreter(path=found) if found else None)
    adapter = la.create(language, context)
    tests = [Path(test).relative_to(root).as_posix() for test in adapter.test_files([str(root)])]
    if not tests:
        raise InvalidRequest("--example", (
            f"{example_dir} holds no test file of {la.registration(language).name}, "
            f"{_test_file_rule(adapter)}."))
    sources = _example_sources(root, adapter, tests)
    return AdapterExample(la.registration(language).name, context, la.Suite(str(root), tuple(tests)),
                          coverage_target=sources[0] if len(sources) == 1 else None)


def _check(options: argparse.Namespace) -> int:
    from langadapter.check import FAILING, report
    from langadapter.errors import InvalidRequest, UnsupportedLanguage

    loaded = _load_extensions(options.extensions)
    if loaded is None:
        return 2
    if not options.language:
        print("\n".join(_overview()))
        return 0
    if options.example and not Path(options.example).is_dir():
        print(f"langadapter check: --example names {options.example}, which is not a "
              "directory.",
              file=sys.stderr)
        return 2
    consumers, files = _split_for(options.consumers)
    try:
        example = _example(options.language, Path(options.example)) if options.example else None
        result = report(options.language, example, consumer=consumers, profiles=files)
        if "run tests" in result._unavailable:
            # The report started no run: the example's tests cannot run.
            raise InvalidRequest("--example", (
                f"the example's tests cannot run with {example.context.interpreter.path}: "
                f"{result._unavailable['run tests']}."))
    except UnsupportedLanguage as exc:
        print(f"langadapter check: {exc}", file=sys.stderr)
        print(_not_registered(options.language, loaded), file=sys.stderr)
        return 2
    except InvalidRequest as exc:
        print(f"langadapter check: {exc}", file=sys.stderr)
        return 2
    print("\n".join(result.lines()))
    failing = any(item.status == FAILING for item in result.items)
    return 0 if result.ready in (None, True) and not result.problems and not failing else 1


def _toolchains(options: argparse.Namespace) -> int:
    from langadapter import toolchains

    loaded = _load_extensions(options.extensions, "toolchains")
    if loaded is None:
        return 2
    into = Path(options.into) if options.into else toolchains._default_directory()
    return toolchains._run(options.languages, into.absolute(), list_only=options.list_only,
                           extensions=loaded)


def main(argv: Optional[Sequence[str]] = None, *, prog: str = "langadapter") -> int:
    """The ``langadapter`` command. ``prog`` names the command in usage and
    help lines, for a tool that hands one of its commands to this one."""
    options = parser(prog).parse_args(argv)
    if options.command == "check":
        return _check(options)
    if options.command == "new-language":
        return _new_language_command(options)
    if options.command == "tutorial":
        from langadapter.tutorial import language

        return language.run(options)
    if options.command == "toolchains":
        return _toolchains(options)
    return 2


def _skeletons() -> Dict[str, str]:
    """The files of the skeletons directory, by hook name."""
    skeletons_dir = resources.files("langadapter") / "skeletons"
    return {item.name[:-len(".txt")]: item.read_text(encoding="utf-8").rstrip("\n")
            for item in sorted(skeletons_dir.iterdir(), key=lambda item: item.name)
            if item.name.endswith(".txt")}


#: What each hook looks like once written, shown commented out.
_SKELETONS: Dict[str, str] = _skeletons()

#: The facts the scaffold writes as code. The others are shown commented.
_WRITTEN_FACTS = ("language", "file_extensions", "display_name", "runtime", "test_file_name")


class _ScaffoldError(ValueError):
    """A name, a version range, a profile or a directory the scaffold cannot use."""


@dataclass(frozen=True)
class _Options:
    """What ``langadapter new-language`` was given (``name`` and ``file_extension``
    required): ``consumers`` by module name and ``profiles`` as files."""

    name: str
    file_extension: str
    project_dir: Optional[Path] = None
    consumers: Tuple[str, ...] = ()
    profiles: Tuple[str, ...] = ()
    test_pattern: str = ""
    runtime: str = ""
    grammar: str = ""
    runtime_name: str = ""
    tested_runtime: str = ""
    expected_runtime: str = ""
    environment: Optional[_consumers.Installed] = field(default=None, compare=False)

    @property
    def suffix(self) -> str:
        text = self.file_extension.strip()
        return text if text.startswith(".") else f".{text}"

    @property
    def title(self) -> str:
        return "".join(part[:1].upper() + part[1:] for part in self.name.split("_"))

    @property
    def target(self) -> Path:
        return self.project_dir if self.project_dir is not None else Path.cwd()


def _span(text: str, flag: str) -> str:
    """``"18-22"`` or ``"18"`` as VersionSpan code."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)*)\s*(?:-\s*(\d+(?:\.\d+)*))?\s*", text)
    if not match:
        raise _ScaffoldError(f"{flag} takes a version or a range such as 18-22, not {text!r}")
    low = tuple(int(n) for n in match.group(1).split("."))
    high = tuple(int(n) for n in (match.group(2) or match.group(1)).split("."))
    return f"VersionSpan({low!r}, {high!r})"


def _comment(text: str, indent: str = "    ") -> str:
    return "\n".join(f"{indent}# {line}" for line in textwrap.wrap(text, 78 - len(indent) - 2))


def _commented(code: str, indent: str = "    ") -> str:
    return "\n".join(f"{indent}# {line}" if line.strip() else f"{indent}#"
                     for line in code.splitlines())


@dataclass(frozen=True)
class _Asked:
    """The composed requirements and the titles of the consumers asked for."""

    composed: _consumers.Composed
    titles: Tuple[str, ...]


def _asked(options: _Options) -> Optional[_Asked]:
    if not options.consumers and not options.profiles:
        return None
    composed = _consumers.composed(options.consumers, files=options.profiles,
                                   environment=options.environment)
    if composed.problems:
        raise _ScaffoldError(" ".join(p.message for p in composed.problems))
    titles = [p.title for name in options.consumers for p in composed.profiles
              if p.consumer == name]
    titles += [p.title for path in options.profiles for p in composed.profiles
               if p.source == str(path)]
    return _Asked(composed, tuple(dict.fromkeys(titles)))


def _tier_line(entry: _hooks.Hook, family: _hooks.Family, asked: Optional[_Asked]) -> str:
    label = f"{entry.kind} {entry.name} ({entry.title})"
    if asked is None:
        return _comment(f"{label}. It makes possible {entry.enables}.")
    need = asked.composed.need(entry.family)
    if need.tier == _consumers.REQUIRED and not entry.works_by_default:
        sources = ", and ".join(s.text() for s in need.sources
                                if s.tier == _consumers.REQUIRED)
        return _comment(f"TODO ({need.level}): {label}, {sources}.")
    if need.tier == _consumers.REQUIRED:
        return _comment(f"{label}: works by default ({entry.default}). Override it when the "
                        "language needs more.")
    if need.tier == _consumers.OPTIONAL:
        titles = ", ".join(dict.fromkeys(s.title for s in need.sources
                                         if s.tier == _consumers.OPTIONAL))
        text = f"{label}: optional for {titles}"
        text += f", where it {' and '.join(need.improves)}." if need.improves else "."
        if need.fallbacks:
            text += f" Without it, {' and '.join(need.fallbacks)}."
        return _comment(text)
    return _comment(f"{label}: not needed by {', '.join(asked.titles)}. Useful for "
                    f"{family.useful_for}.")


def _versions(options: _Options) -> str:
    runtime = options.runtime_name or options.runtime or options.title
    if options.tested_runtime:
        spans = f"tested=({_span(options.tested_runtime, '--tested-runtime')},)"
        if options.expected_runtime:
            spans += f", expected=({_span(options.expected_runtime, '--expected-runtime')},)"
        return ("    support = LanguageSupport(runtime=RuntimeSupport(\n"
                f'        "{runtime}", {spans},\n'
                '        reason="other versions are not tested"))')
    if options.expected_runtime:
        raise _ScaffoldError("--expected-runtime needs --tested-runtime")
    return _commented(
        "Undeclared: every version is reported untested and never refused.\n"
        f"Once versions of {runtime} are tested, declare their range, here 1.0 to 1.4\n"
        "as an example, or write the file with --tested-runtime:\n"
        "support = LanguageSupport(runtime=RuntimeSupport(\n"
        f'    "{runtime}", tested=(VersionSpan((1, 0), (1, 4)),),\n'
        '    reason="other versions are not tested"))')


def _test_file_name(options: _Options) -> str:
    """The ``test_file_name`` template: ``--test-pattern`` with ``{stem}``
    for its one ``*``, else ``test_{stem}`` with the file extension."""
    pattern = options.test_pattern.strip()
    if not pattern:
        return f"test_{{stem}}{options.suffix}"
    if (pattern.count("*") != 1 or any(c in pattern for c in "?[]{}/\\")
            or not pattern.endswith(options.suffix)):
        raise _ScaffoldError(f"--test-pattern takes one * for a source file's stem and ends "
                             f"with the file extension, such as *_test{options.suffix}, not "
                             f"{pattern!r}")
    return pattern.replace("*", "{stem}")


def _facts(options: _Options) -> str:
    suffix = options.suffix
    template = _test_file_name(options)
    pattern = template.replace("{stem}", "*")
    lines = ["    # ---- Language facts " + "-" * 50,
             f'    language = "{options.name}"',
             f'    file_extensions = ("{suffix}",)',
             f'    display_name = "{options.title}"']
    if options.runtime:
        lines.append(f'    runtime = "{options.runtime}"')
    else:
        lines.append(_comment("TODO: fact runtime (The program that runs the tests), for "
                              "example node. The check reads None as missing."))
        lines.append("    runtime = None")
    lines.append(f'    test_file_name = "{template}"')
    for entry in _hooks.hooks_of("facts"):
        if entry.name in _WRITTEN_FACTS:
            continue
        lines.append(_comment(f"fact {entry.name} ({entry.title}): {entry.what} "
                              f"Default: {entry.default}."))
        if entry.name == "test_file_patterns":
            lines.append(_commented(
                "def is_test_file(self, path) -> bool:\n"
                f'    return fnmatch.fnmatch(Path(path).name, "{pattern}")'))
    return "\n".join(lines)


def _adapter(options: _Options, asked: Optional[_Asked]) -> str:
    families = {f.name: f for f in _hooks.families()}
    blocks: List[str] = [_facts(options)]
    if options.grammar:
        module = options.grammar.strip().replace("-", "_")
        blocks.append(_comment(
            f"The tree-sitter toolkit (langadapter.treesitter) finds a source's "
            f'declarations by a rule: Rules(grammar="{module}", ...) with the grammar\'s node '
            "kinds and test rule. test_units can keep the members of "
            "TreeSitterUnits(rules).members(source) that rules.is_test accepts. Install the "
            f"grammar package {options.grammar.strip()}."))
    current = ""
    for entry in _hooks.table():
        if entry.family == "facts":
            continue
        family = families[entry.family]
        if entry.family != current:
            current = entry.family
            blocks.append(f"\n    # ---- {family.title} " + "-" * max(4, 62 - len(family.title)))
        blocks.append(_tier_line(entry, family, asked))
        if entry.name == "support":
            blocks.append(_versions(options))
            continue
        blocks.append(_comment(f"What: {entry.what}"))
        blocks.append(_comment(f"Test it: {entry.test}"))
        if entry.versions:
            blocks.append(_comment(f"Versions: {entry.versions}"))
        blocks.append(_comment(f"Guide: {entry.guide}"))
        if entry.name in _SKELETONS:
            blocks.append(_commented(_SKELETONS[entry.name]))
    check = f"langadapter check {options.name} --extension {options.name}.py" + "".join(
        f" --for {name}" for name in options.consumers + options.profiles)
    target = f" for {', '.join(asked.titles)}" if asked is not None else ""
    return (f'"""The {options.title} adapter.\n\n'
            "Each hook below is commented out until it is written. `" + check + "`\n"
            f'reports what is missing{target}."""\n'
            "from __future__ import annotations\n\n"
            "import fnmatch  # noqa: F401\n"
            "from pathlib import Path  # noqa: F401\n\n"
            "import langadapter as la\n"
            "from langadapter.adapter import RuntimeChoice, SetupQuestion  # noqa: F401\n"
            "from langadapter.mutation import OperatorCatalogue  # noqa: F401\n"
            "from langadapter.mutation import MutationSession  # noqa: F401\n"
            "from langadapter.runs import SuiteRun, SuiteRunRequest  # noqa: F401\n"
            "from langadapter.sources import PolicyReport, SourceResult, "
            "SourceValidation, TargetIdentity, TestUnit  # noqa: F401\n"
            "from langadapter.versions import LanguageSupport, RuntimeSupport, "
            "VersionSpan  # noqa: F401\n\n\n"
            f"class {options.title}Adapter(la.LangAdapter):\n"
            f'    """The {options.title} language\'s adapter."""\n\n'
            + "\n".join(blocks) + "\n\n\n"
            "# LangAdapter reads this name from the file to register the language.\n"
            f"langadapter_language = {options.title}Adapter\n\n"
            "# langadapter toolchains reads this name for the part that installs the\n"
            "# language's tools, by system: Install the toolchains, section A language from\n"
            "# an extension.\n"
            '# langadapter_toolchain = {"ubuntu-24.04": "#!/bin/sh\\n..."}\n')


def _new_language(options: _Options) -> Path:
    """Write the file ``LANGUAGE.py`` of ``options`` into its directory and return
    its path. It writes no file over an existing one."""
    if not re.fullmatch(r"[a-z][a-z0-9_]*", options.name):
        raise _ScaffoldError("a language name is lower-case letters, digits and underscores, "
                             f"starting with a letter, not {options.name!r}")
    if not re.fullmatch(r"\.?[A-Za-z0-9_]+", options.file_extension.strip()):
        raise _ScaffoldError(f"a file extension is letters and digits, such as .js, not "
                             f"{options.file_extension!r}")
    path = options.target / f"{options.name}.py"
    if path.exists():
        raise _ScaffoldError(f"{path} exists already, and the scaffold writes a new file only")
    text = _adapter(options, _asked(options))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


_RECORD = re.compile(r"^(\[tool\.langadapter\]\s*\nfor\s*=\s*)\{([^}]*)\}", re.M)


def _recorded(project_dir: Path) -> Dict[str, str]:
    match = _RECORD.search((project_dir / "pyproject.toml").read_text(encoding="utf-8"))
    return dict(re.findall(r'([\w.-]+)\s*=\s*"([^"]*)"', match.group(2))) if match else {}


def _diff(project_dir: Path, consumers: Sequence[str] = (),
          environment: Optional[_consumers.Installed] = None) -> List[str]:
    """As lines, what the installed profiles of the consumers recorded in
    ``project_dir``'s pyproject.toml (or of ``consumers``) require where their
    versions differ from the recorded ones. Writes nothing."""
    recorded = _recorded(project_dir)
    names = tuple(consumers) or tuple(recorded)
    if not names:
        return ["No consumer is recorded in pyproject.toml's [tool.langadapter] table, and "
                "none was named with --for."]
    found, problems = _consumers.discover(environment)
    lines = [p.message for p in problems if p.consumer in names]
    for name in names:
        profile = found.get(name)
        if profile is None:
            if not any(p.consumer == name for p in problems):
                lines.append(_consumers.not_installed(name).message)
            continue
        before = recorded.get(name, "")
        if before == profile.version:
            lines.append(f"{profile.title} {profile.version}: as recorded.")
            continue
        lines.append(f"{profile.title} {profile.version} (recorded: {before or 'none'}) "
                     "requires:")
        for level in profile.levels:
            for family_name in level.families:
                need = profile.need(family_name)
                for entry in _hooks.hooks_of(family_name):
                    if need.tier == _consumers.REQUIRED:
                        state = "works by default" if entry.works_by_default else "write it"
                        lines.append(f"  level {level.level}, required: {entry.kind} "
                                     f"{entry.name} ({entry.title}), {state}. "
                                     f"Guide: {entry.guide}")
                    elif need.tier == _consumers.OPTIONAL:
                        lines.append(f"  level {level.level}, optional: {entry.kind} "
                                     f"{entry.name} ({entry.title}). Guide: {entry.guide}")
    return lines


def _update_record(project_dir: Path, environment: Optional[_consumers.Installed] = None
                   ) -> List[str]:
    """Record the installed versions of the consumers in ``project_dir``'s
    pyproject.toml and return one line per consumer."""
    path = project_dir / "pyproject.toml"
    text = path.read_text(encoding="utf-8")
    recorded = _recorded(project_dir)
    if not recorded:
        return ["No consumer is recorded in pyproject.toml's [tool.langadapter] table."]
    found, _ = _consumers.discover(environment)
    updated = {name: found[name].version if name in found else version
               for name, version in recorded.items()}
    record = ", ".join(f'{name} = "{version}"' for name, version in updated.items())
    path.write_text(_RECORD.sub(lambda m: f"{m.group(1)}{{ {record} }}", text, count=1),
                    encoding="utf-8", newline="\n")
    return [f"Recorded {name} {version}." if name in found else
            f"Kept {name} {version}: it is not installed." for name, version in updated.items()]


if __name__ == "__main__":
    raise SystemExit(main())
