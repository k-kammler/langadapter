"""``langadapter tutorial``: the Toy language, built step by step.

The tutorial writes the Toy's example project and, one step at a time, its
adapter as the one file ``toy.py`` into a new or empty directory. A step's text
comes from the step file's docstring and the hook table
(``langadapter.hooks``), and its check is ``langadapter.check.report`` on
the class the file names as ``langadapter_language``, read again at each
step, so a learner who edits the file sees their own version checked. With ``--for NAME`` each step
also says what that requirement profile (``langadapter.consumers``) needs,
and the check reports its levels.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import Callable, List, Sequence, Tuple

from langadapter._words import listed
from langadapter.tutorial import AUTO, INTERACTIVE, CheckResult, Step, Tutorial, play

PROG = "langadapter tutorial"
TITLE = "LangAdapter tutorial: add the Toy language"
EXAMPLE =Path(__file__).resolve().parent / "example"
PROJECT_FILES = ("calc.toy", "test_calc.toy")


def _step_files() -> List[Path]:
    return sorted((EXAMPLE / "steps").glob("0*.py"))


def _docstring(source: str) -> Tuple[str, Tuple[str, ...]]:
    """A step file's title and paragraphs from its docstring, whose first line
    reads "Step N of the Toy tutorial: <title>."."""
    text = ast.get_docstring(ast.parse(source)) or ""
    first, _, rest = text.partition("\n")
    title = first.split(":", 1)[-1].strip().rstrip(".")
    paragraphs = tuple(" ".join(block.split()) for block in rest.split("\n\n") if block.strip())
    return title[:1].upper() + title[1:], paragraphs


def _records(step_name: str):
    from langadapter import hooks

    return [record for record in hooks.table() if record.step == step_name]


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith(".") else text + "."


def _need_label(record, composed, titles: str) -> str:
    """A step's label: the family's name, or what the named profiles need,
    in the check's words ("required by X, via Y"), from the sources of the
    tier that wins."""
    if composed is None:
        return record.family
    need = composed.need(record.family)
    texts = list(dict.fromkeys(source.text() for source in need.sources
                               if source.tier == need.tier))
    return " and ".join(texts) if texts else f"not used by {titles}"


def _need_text(record, composed, titles: str) -> str:
    from langadapter.consumers import OPTIONAL, REQUIRED

    need = composed.need(record.family)
    label = _need_label(record, composed, titles)
    text = label[:1].upper() + label[1:]
    if need.tier not in (REQUIRED, OPTIONAL):
        return text + "."
    text += f", in {need.level}"
    # As the check words it: what a profile says the family improves
    # follows "where it".
    if need.tier == OPTIONAL and need.improves:
        text += f", where it {' and '.join(need.improves)}"
    text += "."
    if need.tier == OPTIONAL:
        for fallback in need.fallbacks:
            text += f" Without it, {_sentence(fallback)}"
    return text


def _defined(source: str) -> set:
    """The names a step's code assigns or defines, at any depth."""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return names


def _facts_paragraph(facts, source: str) -> str:
    """The paragraph on the facts a step's code ``source`` declares: those
    without a default it sets, those it leaves out, and the others' defaults."""
    own = [record for record in facts if not record.works_by_default]
    defined = _defined(source)
    set_here = [record.name for record in own if defined & set(record.methods)]
    left_out = [record.name for record in own if not defined & set(record.methods)]
    paragraph = ("This step also declares the language's facts. "
                 f"{listed(set_here)} have no default and are set here. ")
    if left_out:
        paragraph += (f"{listed(left_out)} have no default either. The Toy leaves "
                      "them out, and the check reads them missing. ")
    return paragraph + ("The other facts have defaults, which the Toy changes "
                        "where it differs, such as the words for its tests: the guide lists "
                        "them all.")


def _record_paragraphs(record, composed, titles: str) -> List[str]:
    """A record's paragraphs: what it does and makes possible, its default
    and versions, the programs' need of it and how the check tests it."""
    paragraph = f"{record.title} ({record.name}): {_sentence(record.what)} "
    paragraph += f"It makes possible {_sentence(record.enables)}"
    if record.works_by_default:
        paragraph += f" It works by default: {_sentence(record.default)}"
    if record.versions:
        paragraph += f" Versions: {_sentence(record.versions)}"
    need = [_need_text(record, composed, titles)] if composed is not None else []
    return [paragraph, *need, f"The check tests it: {_sentence(record.test)}"]


def _step_text(paragraphs: Sequence[str], records, composed, titles: str, source: str
               ) -> Tuple[str, ...]:
    facts = [record for record in records if record.kind == "fact" and record.family == "facts"]
    text = list(paragraphs) + ([_facts_paragraph(facts, source)] if facts else [])
    for record in records:
        if record not in facts:
            text += _record_paragraphs(record, composed, titles)
    return tuple(text)


def _load_adapter(project_dir: Path):
    """The class ``toy.py`` names as ``langadapter_language``, read from the
    file as it is now: ``load_extension`` imports a file once per process,
    and the learner changes it between steps."""
    spec = importlib.util.spec_from_file_location("tutorial_toy_adapter", project_dir / "toy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.langadapter_language


def _checker(names: Sequence[str], consumers: Sequence[str], titles: str, environment
             ) -> Callable[[Path], CheckResult]:
    def check_step(project_dir: Path) -> CheckResult:
        import langadapter as la
        from langadapter import check
        from langadapter.conformance import AdapterExample

        adapter = _load_adapter(project_dir)
        project = project_dir / "project"
        example = AdapterExample(
            "toy", la.ProjectContext(str(project), la.Interpreter(path=sys.executable)),
            la.Suite(str(project), ("test_calc.toy",)), coverage_target="calc.toy")
        report = check.report(adapter, example, consumer=list(consumers) or None,
                              environment=environment)
        items = [item for item in report.items if item.name in names]
        done = all(item.status in (check.PRESENT, check.DEFAULT_USED) for item in items)
        lines = [item.message for item in items]
        lines += [readiness.line(examined=True) for readiness in report.readiness
                  if readiness.consumer in consumers]
        return CheckResult(done, tuple(lines))
    return check_step


def _closing_runs(consumers: Sequence[str], environment) -> List[Step]:
    """The closing steps the named profiles give (``Profile.tutorial``):
    each an optional step that runs the profile's command, such as a
    recorded run of the Toy."""
    from langadapter import consumers as profiles

    steps = []
    for name in consumers:
        run = profiles.profile(name, environment).tutorial
        if run is not None:
            steps.append(Step(run.title, run.text, command=run.command,
                              install_hint=run.install, optional=True))
    return steps


def tutorial(consumers: Sequence[str] = (), environment=None) -> Tutorial:
    """The tutorial, with what the profiles ``consumers`` need when named,
    read from ``environment`` (the installed packages by default)."""
    composed = None
    titles = ""
    if consumers:
        from langadapter import consumers as profiles

        titles = ", ".join(profiles.profile(name, environment).title for name in consumers)
        composed = profiles.composed(list(consumers), environment=environment)
    project = {f"project/{name}": (EXAMPLE / "project" / name).read_text(encoding="utf-8")
               for name in PROJECT_FILES}
    steps = []
    previous = None
    for path in _step_files():
        source = path.read_text(encoding="utf-8")
        title, paragraphs = _docstring(source)
        records = _records(path.name)
        names = [record.name for record in records
                 if not (record.kind == "fact" and record.family == "facts")]
        files = {"toy.py": source}
        if previous is None:
            files = {**project, **files}
        hooks = [record for record in records
                 if not (record.kind == "fact" and record.family == "facts")]
        steps.append(Step(
            title, _step_text(paragraphs, records, composed, titles, source), files,
            show=("toy.py",), changes={"toy.py": previous} if previous else {},
            check=_checker(names, consumers, titles, environment),
            label=_need_label(hooks[0], composed, titles) if hooks else ""))
        previous = source
    steps += _closing_runs(consumers, environment)
    intro = [
        "This tutorial builds an adapter for the Toy, a tiny language made for it. A "
        "Toy source file holds one function per line, such as double(x) = x * 2. A "
        "Toy test file holds one test per line, such as test doubles: double(2) == 4. "
        "The Toy's own tools run its tests, measure coverage and make mutants.",
        "Each step writes toy.py, a complete adapter at that point in one file, shows "
        "what changed, and runs the check on your directory. Edit toy.py before a step "
        "checks it and keep your version, or take the prepared one.",
    ]
    if titles:
        intro.append(f"Each step also says what {titles} needs, and the check shows "
                     f"how far the adapter is for {titles}.")
    closing = (
        "Done: the Toy's adapter has every step of this tutorial. Check the file at any "
        "time with langadapter check toy --extension toy.py --example project. The guide "
        "Add a language describes each step for a real language, and langadapter "
        "new-language writes the file to start from.",
    )
    return Tutorial(TITLE, tuple(steps), intro=tuple(intro), closing=closing)


def run(options) -> int:
    """Run the tutorial with the options of ``langadapter tutorial``:
    ``output_dir``, ``auto``, ``speed`` and ``consumers``."""
    if options.speed <= 0:
        print(f"{PROG}: error: --speed must be positive", file=sys.stderr)
        return 2
    if options.consumers:
        from langadapter import consumers

        for name in options.consumers:
            try:
                consumers.profile(name)
            except consumers.ProfileError as error:
                print(f"{PROG}: {error}", file=sys.stderr)
                return 2
    return play(tutorial(options.consumers), options.output_dir,
                mode=AUTO if options.auto else INTERACTIVE, speed=options.speed)
