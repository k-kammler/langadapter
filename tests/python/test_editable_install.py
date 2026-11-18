"""A project installed in editable mode, measured in a prepared copy.

The install maps the package to the original tree, while coverage and Cosmic
Ray work on the copy or a worker's clone: the measured run must import the
measured code or be refused without a value. A regular install holds a third
copy, and meson-python's hook names a search location that is no directory."""
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest

import langadapter as lb
from langadapter.capabilities import Availability
from langadapter.mutation import Catalogue, MutationRequest
from langadapter.outcomes import Outcome
from langadapter.preparation import prepare_project
from langadapter.python._plugins import UNSUPPORTED
from langadapter.python.project import environment_dir
from langadapter.runs import (
    BenchmarkRequest, PerTestEvidenceRequest, SuiteProfileRequest, SuiteRunRequest,
)
from langadapter.suites import LineItem

coverage = pytest.importorskip("coverage")

TARGET = "def sign(x):\n    if x > 0:\n        return 1\n    return -1\n"
TEST = "from pkg.mod import sign\n\n\ndef test_positive():\n    assert sign(2) == 1\n"
#: The finder module an editable install of setuptools writes: it maps the
#: package, and a namespace directory above it, to the original tree and
#: appends itself to the import hooks.
FINDER = """import importlib.machinery, importlib.util, sys
MAPPING = {mapping!r}
class _EditableFinder:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name not in MAPPING:
            return None
        init, package = MAPPING[name]
        if init is None:
            spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
            spec.submodule_search_locations = [package]
            return spec
        return importlib.util.spec_from_file_location(
            name, init, submodule_search_locations=[package])
def install(first=False):
    if first:
        sys.meta_path.insert(0, _EditableFinder)
    else:
        sys.meta_path.append(_EditableFinder)
"""
#: The hook an editable install of meson-python (0.16 and later) writes: first
#: among the import hooks, with the original's ``__init__.py`` as the origin
#: and a search location below the loader's own file, which is no directory.
MESON_FINDER = """import importlib.util, os, sys
class _MesonFinder:
    @classmethod
    def find_spec(cls, name, path=None, target=None):
        if name.split(".")[0] != "pkg":
            return None
        if name == "pkg":
            return importlib.util.spec_from_file_location(
                name, os.path.join({package!r}, "__init__.py"),
                submodule_search_locations=[os.path.join(__file__, name)])
        return importlib.util.spec_from_file_location(
            name, os.path.join({package!r}, *name.split(".")[1:]) + ".py")
def install():
    sys.meta_path.insert(0, _MesonFinder)
"""


def _environment(env_dir):
    """A virtual environment at ``env_dir`` whose site directory reaches this
    process's pytest and coverage: its interpreter and site directory."""
    import pytest as runner

    venv.EnvBuilder(with_pip=False, symlinks=os.name != "nt").create(env_dir)
    python = env_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    site_dir, = [path for path in env_dir.rglob("site-packages") if path.is_dir()]
    tools = sorted({str(Path(module.__file__).resolve().parents[1])
                    for module in (runner, coverage)})
    (site_dir / "tools.pth").write_text("\n".join(tools) + "\n", encoding="utf-8")
    return python, site_dir


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    """An environment outside the projects. Each test writes its own install
    into it."""
    return _environment(tmp_path_factory.mktemp("editable") / "venv")


#: Where the package lies below the project root, by layout.
SOURCE = {"src": Path("src"), "root": Path(), "lib": Path("lib"), "python": Path("python"),
          "pysrc": Path("pysrc")}
#: The module under test below its source directory and the name the test
#: imports it by, by shape: a package, a package in a namespace directory
#: (``company`` without ``__init__.py``) and a module in no package.
SHAPES = {"package": (Path("pkg", "mod.py"), "pkg.mod"),
          "namespace": (Path("company", "pkg", "mod.py"), "company.pkg.mod"),
          "module": (Path("mod.py"), "mod")}


@pytest.fixture
def installed(site, tmp_path):
    """Writes a project and its editable or regular install. Only a regular
    install and an appended finder take shapes other than a package, the
    finder only a namespace one."""
    python, site_dir = site
    root = tmp_path / "proj"
    written = []

    def install(style, layout="src", shape="package"):
        source = root / SOURCE[layout]
        module, name = SHAPES[shape]
        (source / module).parent.mkdir(parents=True, exist_ok=True)
        (root / "tests").mkdir()
        if shape != "module":
            (source / module.parent / "__init__.py").write_text("", encoding="utf-8")
        (source / module).write_text(TARGET, encoding="utf-8")
        (root / "tests" / "test_mod.py").write_text(
            TEST.replace("pkg.mod", name), encoding="utf-8")
        pth = site_dir / "__editable__.pkg-0.1.pth"
        assert style == "regular" or shape == "package" or (style, shape) == ("finder", "namespace")
        if style == "regular":
            top = module.parts[0]
            (shutil.copytree if (source / top).is_dir() else shutil.copy2)(
                source / top, site_dir / top)
            written.append(site_dir / top)
        elif style == "path":
            pth.write_text(str(source) + "\n", encoding="utf-8")
        elif style == "meson":
            finder = site_dir / "_pkg_editable_loader.py"
            finder.write_text(MESON_FINDER.format(package=str(source / "pkg")),
                              encoding="utf-8")
            written.append(finder)
            pth.write_text("import _pkg_editable_loader; _pkg_editable_loader.install()\n",
                           encoding="utf-8")
        else:
            finder = site_dir / "__editable___pkg_finder.py"
            package = source / module.parent
            mapping = {".".join(module.parent.parts): (str(package / "__init__.py"), str(package))}
            if shape == "namespace":
                top = module.parts[0]
                mapping[top] = (None, str(source / top))
            finder.write_text(FINDER.format(mapping=mapping), encoding="utf-8")
            written.append(finder)
            pth.write_text("import __editable___pkg_finder; __editable___pkg_finder"
                           f".install(first={style == 'first'})\n", encoding="utf-8")
        if pth.exists():
            written.append(pth)
        return lb.ProjectContext(root=str(root), interpreter=lb.Interpreter(path=str(python)))

    yield install
    for path in written:
        shutil.rmtree(path) if path.is_dir() else path.unlink()


def _measured(context, measure, layout="src", shape="package", **preparation):
    owner = prepare_project(context, **preparation)
    try:
        with owner.use() as scoped:
            copy = Path(scoped.root)
            return measure(lb.create("python", scoped), lb.Suite(str(copy / "tests"), ("test_mod.py",)),
                           str(copy / SOURCE[layout] / SHAPES[shape][0]))
    finally:
        owner.close()


def _covered(context, layout="src", shape="package", **preparation):
    return _measured(context, lambda adapter, suite, target: adapter.tests.run_suite(
        SuiteRunRequest(suite, timeout_s=120, coverage_target=target)), layout, shape, **preparation)


@pytest.mark.parametrize("layout", ["src", "pysrc"])
@pytest.mark.parametrize("style", ["path", "finder"])
def test_the_measured_run_imports_the_copy(installed, style, layout):
    """A path entry (setuptools for a plain src layout, hatch, flit, poetry)
    and an appended import hook (setuptools otherwise) both map the package
    to the original; the copy's directory goes ahead of them, whatever its name."""
    run = _covered(installed(style, layout), layout)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.coverage.covered == (1, 2, 3) and run.coverage.line_percent == 75.0


def test_per_test_evidence_imports_the_copy(installed):
    evidence = _measured(installed("path"), lambda adapter, suite, target:
                         adapter.tests.per_test_evidence(PerTestEvidenceRequest(
                             suite, target, timeout_s=120, warmup=False)))
    assert evidence.outcome is Outcome.COMPLETED, evidence.reason
    assert evidence.coverage.covered == (1, 2, 3)
    test, = evidence.tests
    # Line 1 runs at import, outside the test.
    assert {item.line for item in test.items if isinstance(item, LineItem)} == {2, 3}


def test_per_test_evidence_behind_an_import_hook_is_refused(installed):
    evidence = _measured(installed("meson"), lambda adapter, suite, target:
                         adapter.tests.per_test_evidence(PerTestEvidenceRequest(
                             suite, target, timeout_s=120, warmup=False)))
    assert evidence.outcome is Outcome.INFRASTRUCTURE_FAILURE and evidence.coverage is None
    assert "editable install" in evidence.reason and "pip install . instead of" in evidence.reason


@pytest.mark.parametrize("layout", ["src", "root"])
@pytest.mark.parametrize("style", ["first", "meson"])
def test_an_import_hook_ahead_of_the_path_is_refused_without_a_value(installed, style, layout):
    """meson-python installs its hook first (pandas, numpy, scipy): it wins
    even for a root layout, whose copy's root is first on the path. Its
    search location is no directory, so the location is read from the
    origin."""
    run = _measured(installed(style, layout), lambda adapter, suite, target:
                    adapter.tests.run_suite(SuiteRunRequest(
                        suite, timeout_s=120, coverage_target=target)), layout)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert run.coverage is None and run.tests == ()
    assert "editable install" in run.reason and "pip install . instead of" in run.reason


@pytest.mark.parametrize("layout", ["src", "root"])
def test_a_regular_install_does_not_hide_the_measured_copy(installed, layout):
    """The tests import the copy, not the installed package."""
    run = _measured(installed("regular", layout), lambda adapter, suite, target:
                    adapter.tests.run_suite(SuiteRunRequest(
                        suite, timeout_s=120, coverage_target=target)), layout)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.coverage.covered == (1, 2, 3) and run.coverage.line_percent == 75.0


def test_a_root_layout_installed_in_editable_mode_is_measured_from_its_copy(installed):
    run = _measured(installed("path", "root"), lambda adapter, suite, target:
                    adapter.tests.run_suite(SuiteRunRequest(
                        suite, timeout_s=120, coverage_target=target)), "root")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.coverage.covered == (1, 2, 3)


@pytest.mark.parametrize("layout, shape", [
    ("src", "namespace"), ("lib", "namespace"), ("python", "package"), ("src", "module"),
    ("lib", "module"), ("pysrc", "package")])
def test_a_regular_install_of_another_layout_does_not_hide_the_measured_copy(
        installed, layout, shape):
    """The copy's directory, above a namespace directory if there is one, goes
    ahead of the installed package."""
    run = _covered(installed("regular", layout, shape), layout, shape)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.coverage.covered == (1, 2, 3) and run.coverage.line_percent == 75.0


def test_the_run_itself_refuses_a_module_imported_from_elsewhere(installed, monkeypatch):
    """The probe only finds the directory to put first: the measured run itself
    records what the tests imported. With the probe's answer lost, the tests
    import the original through the path entry and the run's record refuses
    it as a layout this release does not measure."""
    from langadapter.python._runs import _SuiteRunner

    monkeypatch.setattr(_SuiteRunner, "_where_imported", lambda self, env: None)
    run = _covered(installed("path"))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.coverage is None
    assert run.tests == () and "imported pkg.mod from" in run.reason
    assert "could not be put ahead of it" in run.reason and UNSUPPORTED in run.reason


def test_a_module_the_tests_never_import_measures_zero(installed):
    """0% is the measurement when the tests never imported the module, even
    behind an import hook."""
    context = installed("meson")
    (Path(context.root) / "tests" / "test_mod.py").write_text(
        "def test_nothing():\n    assert True\n", encoding="utf-8")
    run = _covered(context)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.coverage.line_percent == 0.0


@pytest.mark.parametrize("style", ["path", "meson"])
def test_an_editable_install_in_the_projects_own_environment_is_measured_or_refused(tmp_path, style):
    """An interpreter in the project's own .venv runs in place, from the
    original's environment directory, which the copy leaves out, as verification
    and a caller's runs do. Its editable install maps the package to the original:
    the copy's directory goes ahead of a path entry, and an import hook ahead of
    the path is refused without a value."""
    root = tmp_path / "proj"
    source = root / "src"
    (source / "pkg").mkdir(parents=True)
    (source / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (source / "pkg" / "mod.py").write_text(TARGET, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_mod.py").write_text(TEST, encoding="utf-8")
    python, site_dir = _environment(root / ".venv")
    pth = site_dir / "__editable__.pkg-0.1.pth"
    if style == "path":
        pth.write_text(str(source) + "\n", encoding="utf-8")
    else:
        (site_dir / "_pkg_editable_loader.py").write_text(
            MESON_FINDER.format(package=str(source / "pkg")), encoding="utf-8")
        pth.write_text("import _pkg_editable_loader; _pkg_editable_loader.install()\n", encoding="utf-8")
    context = lb.ProjectContext(root=str(root), interpreter=lb.Interpreter(path=str(python)))
    run = _covered(context, inputs=lb.ProjectInputSelection(
        exclude=(environment_dir(str(python), str(root)),)))
    if style == "path":
        assert run.outcome is Outcome.COMPLETED, run.reason
        assert run.coverage.covered == (1, 2, 3)
    else:
        assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.coverage is None
        assert "editable install" in run.reason


def _whole_suite(context, layout="src"):
    """A run of the suite that names no module under test."""
    return _measured(context, lambda adapter, suite, target: adapter.tests.run_suite(
        SuiteRunRequest(suite, timeout_s=120)), layout)


@pytest.mark.parametrize("style, named", [
    ("first", "import hook of an editable install"), ("path", "keeps the import path"),
    ("regular", "from the installed copy")])
def test_a_run_without_a_module_importing_the_project_from_elsewhere_is_refused(
        installed, style, named):
    """A run that names no module keeps its import path: it records where the
    tests executed the copy's modules from and is refused when one came from
    outside the copy."""
    run = _whole_suite(installed(style))
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.tests == ()
    assert "imported pkg.mod from" in run.reason and named in run.reason


def test_a_suite_in_the_copys_root_is_checked_too(installed):
    """A suite whose directory is the copy's root: the copy plugin leaves the
    suite's directory out as test data only when it is not the root itself, so
    the copy's modules stay checked."""
    context = installed("first")
    (Path(context.root) / "test_root.py").write_text(TEST, encoding="utf-8")
    owner = prepare_project(context)
    try:
        with owner.use() as scoped:
            run = lb.create("python", scoped).tests.run_suite(SuiteRunRequest(
                lb.Suite(scoped.root, ("test_root.py",)), timeout_s=120))
    finally:
        owner.close()
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.tests == ()
    assert "imported pkg.mod from" in run.reason


def test_a_suite_in_a_source_dir_is_checked_too(installed):
    """A suite whose directory is a source directory (test files directly in a src
    layout's src): that directory is one of the copy's import roots, so it is not
    left out as test data and the copy's modules stay checked."""
    context = installed("first")
    (Path(context.root) / "src" / "test_in_src.py").write_text(TEST, encoding="utf-8")
    owner = prepare_project(context)
    try:
        with owner.use() as scoped:
            run = lb.create("python", scoped).tests.run_suite(SuiteRunRequest(
                lb.Suite(str(Path(scoped.root) / "src"), ("test_in_src.py",)), timeout_s=120))
    finally:
        owner.close()
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.tests == ()
    assert "imported pkg.mod from" in run.reason


@pytest.mark.parametrize("held", ["tests/helper", "helper", "pkg/helper.py"])
def test_a_name_the_copy_holds_no_module_for_is_no_refusal(installed, site, held):
    """The copy provides a module only under its import roots, with a file for
    the module's full name. Test data in the suite's directory, a data directory in
    the root and a package's own module, each named like an installed module
    the tests import, do not make that module the copy's."""
    _, site_dir = site
    context = installed("path", "root")
    root = Path(context.root)
    if held.endswith(".py"):
        (root / held).write_text("VALUE = 2\n", encoding="utf-8")
    else:
        (root / held).mkdir()
        (root / held / "data.txt").write_text("fixture data\n", encoding="utf-8")
    tests = root / "tests"
    (tests / "test_mod.py").write_text(
        "import helper\nfrom pkg.mod import sign\n\n\n"
        "def test_positive():\n    assert helper.VALUE == 1 and sign(2) == 1\n", encoding="utf-8")
    installed_helper = site_dir / "helper.py"
    installed_helper.write_text("VALUE = 1\n", encoding="utf-8")
    try:
        run = _whole_suite(context, "root")
    finally:
        installed_helper.unlink()
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.counts.passed == 1


def test_a_run_without_a_module_importing_the_copy_keeps_its_outcomes(installed):
    """The copy's root comes first on the import path, ahead of the path entry
    to the original."""
    run = _whole_suite(installed("path", "root"), "root")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.counts.passed == 1


def _profiled(context, layout="src"):
    """A suite profile of the copy's suite, which names no module under test."""
    return _measured(context, lambda adapter, suite, target: adapter.tests.profile_suite(
        SuiteProfileRequest(suite, 120)), layout)


@pytest.mark.parametrize("style", ["path", "finder", "regular"])
def test_a_suite_profile_runs_the_copys_package_ahead_of_an_install(installed, style):
    """A profiled run puts the copy's source directory first, so a path entry, an
    appended import hook and an installed copy leave the copy's package to
    the tests, and the profile names the copy's file."""
    profile = _profiled(installed(style))
    assert profile.outcome is Outcome.COMPLETED, profile.reason
    assert ("src/pkg/mod.py", "sign") in {(item.path, item.name) for item in profile.functions}


def test_a_suite_profile_behind_an_import_hook_is_refused(installed):
    """An import hook ahead of the path still wins: the shared origin check
    refuses the profiled run, which lists no functions."""
    profile = _profiled(installed("first"))
    assert profile.outcome is Outcome.INFRASTRUCTURE_FAILURE and profile.functions == ()
    assert "imported pkg.mod from" in profile.reason
    assert "import hook of an editable install" in profile.reason


def _benchmark(context, warmup_runs, named, layout="src", runs=1):
    return _measured(context, lambda adapter, suite, target: adapter.tests.benchmark(BenchmarkRequest(
        suite, runs=runs, timeout_s=120, warmup_runs=warmup_runs,
        module_under_test=target if named else None)), layout)


@pytest.mark.parametrize("named", [True, False])
def test_a_benchmark_behind_an_import_hook_is_refused_by_its_warmup(installed, named):
    """The first warmup repetition records where the tests imported the
    copy's modules from, with the module's plugin or the copy's, and the
    benchmark is refused before its measured repetitions."""
    result = _benchmark(installed("first"), 1, named)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE and result.times_ms == ()
    assert "import hook of an editable install" in result.reason


def test_a_benchmark_without_a_warmup_is_not_checked(installed):
    result = _benchmark(installed("first"), 0, False)
    assert result.outcome is Outcome.COMPLETED and len(result.times_ms) == 1


def test_the_measured_repetitions_carry_no_plugin(installed, monkeypatch):
    """Only the first warmup repetition loads the plugin: the measured
    repetitions' command and environment are those without it."""
    from langadapter.python._plugins import COPY_ORIGIN_PLUGIN
    from langadapter.python._runs import _BenchmarkRunner

    calls = []
    repetition = _BenchmarkRunner._repetition

    def recorded(self, command, env):
        calls.append((list(command), dict(env)))
        return repetition(self, command, env)

    monkeypatch.setattr(_BenchmarkRunner, "_repetition", recorded)
    result = _benchmark(installed("path", "root"), 1, False, "root", runs=2)
    assert result.outcome is Outcome.COMPLETED, result.reason
    (check, check_env), *measured = calls
    assert len(measured) == 2 and measured[0] == measured[1]
    command, env = measured[0]
    assert check == command + ["-p", COPY_ORIGIN_PLUGIN]
    assert not any("IMPORT_ORIGIN" in name for name in env)
    assert {name: value for name, value in check_env.items()
            if "IMPORT_ORIGIN" not in name and name != "PYTHONPATH"} == {
        name: value for name, value in env.items() if name != "PYTHONPATH"}
    first, _, rest = check_env["PYTHONPATH"].partition(os.pathsep)
    assert first.endswith("origin_plugin") and rest == env["PYTHONPATH"]


@pytest.fixture
def listed_pytest(site):
    """pytest's distribution record in the environment, which the setup
    check's isolated probe reads: the environment reaches pytest itself
    through a .pth file, which that probe does not process."""
    import pytest as runner

    _, site_dir = site
    record = site_dir / f"pytest-{runner.__version__}.dist-info"
    record.mkdir()
    (record / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: pytest\nVersion: {runner.__version__}\n", encoding="ascii")
    yield
    shutil.rmtree(record)


def _verified(context, layout="src", shape="package"):
    """The tool check of a project whose runs run its suite."""
    source = (SOURCE[layout] / SHAPES[shape][0]).as_posix()
    return lb.verify_project("python", lb.ProjectSetupRequest(context, (source,), ("tests.run_suite",)))


def _item(report, key):
    return next(item for item in report.items if item.key == key)


def test_the_tool_check_collects_the_projects_tests_from_the_copy(installed, listed_pytest):
    report = _verified(installed("path", "root"), "root")
    assert report.outcome is Outcome.COMPLETED, report.reason
    collection, origin = _item(report, "test_collection"), _item(report, "project_origin")
    assert (collection.outcome, collection.required) == (Outcome.COMPLETED, False)
    assert (origin.outcome, origin.readiness, origin.required) == (
        Outcome.COMPLETED, Availability.AVAILABLE, False)


@pytest.mark.parametrize("style", ["first", "meson"])
def test_the_tool_check_requires_the_origin_behind_a_finder_placed_first(
        installed, listed_pytest, style):
    """A finder placed ahead of the import path (setuptools' placed first,
    meson-python's) wins over any placement, so a run that names the module
    is refused too: the refusal a run without a module gets, required."""
    report = _verified(installed(style))
    origin = _item(report, "project_origin")
    assert report.outcome is Outcome.COMPLETED, report.reason
    assert origin.required and origin.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "imported pkg.mod from" in origin.reason
    assert "import hook of an editable install" in origin.reason
    assert _item(report, "test_collection").outcome is Outcome.COMPLETED


#: Appended to the finder placed first: it logs each question for the
#: top-level package that names no search path.
COUNTING = """
_find = _EditableFinder.find_spec.__func__
def _counted(cls, name, path=None, target=None):
    if name == "pkg" and path is None:
        with open({log!r}, "a", encoding="utf-8") as handle:
            handle.write(name + "\\n")
    return _find(cls, name, path, target)
_EditableFinder.find_spec = classmethod(_counted)
"""


def test_the_tool_check_asks_a_finder_placed_first_once_for_each_package(
        installed, listed_pytest, site, tmp_path):
    """The tests import pkg and pkg.mod from the original: the import asks
    the finder for pkg once, and the origin check once more for both
    modules, so a package with many modules costs one question."""
    context = installed("first")
    log = tmp_path / "asked.txt"
    with open(site[1] / "__editable___pkg_finder.py", "a", encoding="utf-8") as handle:
        handle.write(COUNTING.format(log=str(log)))
    assert _item(_verified(context), "project_origin").required
    assert log.read_text(encoding="utf-8").split() == ["pkg", "pkg"]


@pytest.mark.parametrize("style", ["regular", "path"])
def test_an_install_a_named_run_puts_the_copy_ahead_of_passes_the_tool_check(
        installed, listed_pytest, style):
    """The collection puts the copy's source directories first, as a run that
    names the module does: a regular install and an editable install's path
    entry leave the copy's package to the tests."""
    report = _verified(installed(style))
    origin = _item(report, "project_origin")
    assert origin.outcome is Outcome.COMPLETED, origin.reason


def test_an_appended_finder_of_a_namespace_package_is_reported_without_failing_the_check(
        installed, listed_pytest):
    """setuptools' appended finder maps a namespace layout the placement does
    not reach: the tests import the original, which a run naming the module
    puts the copy's directory ahead of. project_origin reports it only."""
    context = installed("finder", "src", "namespace")
    report = _verified(context, "src", "namespace")
    origin = _item(report, "project_origin")
    assert report.outcome is Outcome.COMPLETED, report.reason
    assert (origin.outcome, origin.required) == (Outcome.INFRASTRUCTURE_FAILURE, False)
    assert "imported company.pkg" in origin.reason
    run = _covered(context, "src", "namespace")
    assert run.outcome is Outcome.COMPLETED, run.reason


def test_a_namespace_package_installed_elsewhere_is_reported_without_failing_the_check(
        installed, listed_pytest):
    """A run naming the module puts the directory above a namespace directory
    first, which the collection's placement does not: project_origin reports
    the installed copy without being required."""
    report = _verified(installed("regular", "src", "namespace"), "src", "namespace")
    origin = _item(report, "project_origin")
    assert report.outcome is Outcome.COMPLETED, report.reason
    assert (origin.outcome, origin.required) == (Outcome.INFRASTRUCTURE_FAILURE, False)
    assert "imported company.pkg.mod from the installed copy" in origin.reason


def test_a_test_module_that_does_not_collect_is_reported_without_being_required(
        installed, listed_pytest):
    """A run collects only the tests it selects: a module of the project's own
    that needs an optional dependency shows without failing the check."""
    context = installed("path", "root")
    (Path(context.root) / "tests" / "test_optional.py").write_text(
        "import not_installed_anywhere\n", encoding="utf-8")
    report = _verified(context, "root")
    collection = _item(report, "test_collection")
    assert collection.outcome is Outcome.COLLECTION_FAILED and not collection.required
    assert "not_installed_anywhere" in collection.reason
    assert _item(report, "project_origin").outcome is Outcome.COMPLETED


def test_the_tool_check_collects_a_project_with_a_root_conftest(installed, listed_pytest):
    """A conftest.py in the root, a common layout, is a setup file of the
    suite the collection names."""
    context = installed("path", "root")
    (Path(context.root) / "conftest.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = _verified(context, "root")
    assert report.outcome is Outcome.COMPLETED, report.reason
    assert _item(report, "test_collection").outcome is Outcome.COMPLETED
    assert _item(report, "project_origin").outcome is Outcome.COMPLETED


def test_a_project_without_tests_collects_none_and_leaves_the_origin_to_a_run(
        installed, listed_pytest):
    context = installed("path", "root")
    (Path(context.root) / "tests" / "test_mod.py").unlink()
    report = _verified(context, "root")
    assert _item(report, "test_collection").outcome is Outcome.NO_TESTS
    origin = _item(report, "project_origin")
    assert (origin.outcome, origin.tried_by) == (None, "run")


@pytest.mark.parametrize("ends", ["no_time_left", "timed_out"])
def test_a_collection_out_of_time_leaves_both_items_to_the_first_run(
        installed, listed_pytest, monkeypatch, ends):
    """Too little time left to start the collection, or the collection's own
    timeout: the first run tries both items, and the check's outcome is the
    one without them."""
    from langadapter.python import _runs

    if ends == "no_time_left":
        remaining = lb.ProjectSetupRequest.remaining_timeout

        def no_time(self, stage="project setup", **options):
            if stage == "test collection":
                raise subprocess.TimeoutExpired(stage, self.total_timeout_s)
            return remaining(self, stage, **options)

        monkeypatch.setattr(lb.ProjectSetupRequest, "remaining_timeout", no_time)
    else:
        monkeypatch.setattr(_runs, "verification_collection", lambda adapter, source, timeout: (
            SimpleNamespace(outcome=Outcome.TIMED_OUT), None, False))
    report = _verified(installed("path", "root"), "root")
    assert report.outcome is Outcome.COMPLETED, report.reason
    for key in ("test_collection", "project_origin"):
        item = _item(report, key)
        assert (item.outcome, item.tried_by, item.required) == (None, "run", False)
        assert "ran out of time" in item.reason


#: Operators the test kills: sign(2) == 1 fails under either.
KILLED = ("core/ReplaceComparisonOperator_Gt_Lt", "core/ReplaceComparisonOperator_Gt_Eq")


def _mutated(installed, monkeypatch, style, in_place=False, layout="src", shape="package",
             **options):
    """A mutation run of the module in a prepared copy or in place, with
    this environment's Cosmic Ray (the fixture's has only pytest and
    coverage)."""
    tools = Path(sys.executable).parent
    if shutil.which("cosmic-ray", path=str(tools)) is None:
        pytest.skip("needs cosmic-ray beside the interpreter")
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))

    def mutate(adapter, suite, target):
        with adapter.open_mutation_session(**options) as session:
            return session.run(MutationRequest(target=target, suite=suite,
                                               catalogue=Catalogue(operators=KILLED)))
    context = installed(style, layout, shape)
    if in_place:
        root = Path(context.root)
        return mutate(lb.create("python", context), lb.Suite(str(root / "tests"), ("test_mod.py",)),
                      str(root / SOURCE[layout] / SHAPES[shape][0]))
    return _measured(context, mutate, layout, shape)


@pytest.mark.mutation_tool
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_a_mutation_session_mutates_the_copy(installed, monkeypatch, options):
    """The tests import the code Cosmic Ray mutates, the copy or a worker's
    clone, not the original's src, under which every mutant would survive."""
    run = _mutated(installed, monkeypatch, "path", **options)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0
    # The origin run's time is reported apart, and a derived result has none.
    assert run.origin_run_s > 0
    assert run.derive_for_tests([], covered_lines=[]).origin_run_s is None


@pytest.mark.mutation_tool
def test_worker_clones_in_place_import_their_own_source(installed, monkeypatch):
    """Without a prepared copy, each worker's clone imports its own src,
    not the project's unmutated one."""
    run = _mutated(installed, monkeypatch, "path", in_place=True, workers=2,
                   distributor="http")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0


@pytest.mark.mutation_tool
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
@pytest.mark.parametrize("layout", ["src", "root"])
def test_a_mutation_session_of_a_regular_install_mutates_the_copy(
        installed, monkeypatch, layout, options):
    """The tests import the copy, not the installed package."""
    run = _mutated(installed, monkeypatch, "regular", layout=layout, **options)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0


@pytest.mark.mutation_tool
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
@pytest.mark.parametrize("layout", ["src", "root"])
@pytest.mark.parametrize("style", ["first", "meson"])
def test_a_mutation_session_behind_an_import_hook_is_refused_without_a_score(
        installed, monkeypatch, style, layout, options):
    run = _mutated(installed, monkeypatch, style, layout=layout, **options)
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.score() is None
    assert "editable install" in run.reason and "pip install . instead of" in run.reason


@pytest.mark.mutation_tool
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
@pytest.mark.parametrize("layout, shape", [
    ("src", "namespace"), ("lib", "namespace"), ("python", "package"), ("src", "module"),
    ("lib", "module"), ("pysrc", "package")])
def test_a_mutation_session_of_a_regular_install_of_another_layout_mutates_the_copy(
        installed, monkeypatch, layout, shape, options):
    """The tests import the copy in every layout and shape."""
    run = _mutated(installed, monkeypatch, "regular", layout=layout, shape=shape, **options)
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0


def test_the_origin_run_refuses_a_module_imported_from_elsewhere(installed, monkeypatch):
    """With the probe's answer lost, the origin run of the unmutated project
    refuses the session's run before any mutant, since every one would
    survive."""
    from langadapter.python._cosmic_ray import CosmicRayEngine

    monkeypatch.setattr(CosmicRayEngine, "_source_dirs", lambda self, root, env: None)
    run = _mutated(installed, monkeypatch, "path")
    assert run.outcome is Outcome.INFRASTRUCTURE_FAILURE and run.score() is None
    assert "imported pkg.mod from" in run.reason and UNSUPPORTED in run.reason
    assert run.origin_run_s > 0


@pytest.mark.mutation_tool
@pytest.mark.parametrize("mode", ["suite", "local"])
def test_an_older_installed_release_does_not_hide_the_measured_copy(
        installed, site, monkeypatch, mode):
    """An older regular install, whose text differs from the target's, does
    not hide the copy in a source directory of any name."""
    _python, site_dir = site

    def stale(style, layout, shape="package"):
        context = installed(style, layout, shape)
        (site_dir / "pkg" / "mod.py").write_text("# 1.0\n" + TARGET, encoding="utf-8")
        return context

    if mode == "suite":
        run = _covered(stale("regular", "pysrc"), "pysrc")
        assert run.outcome is Outcome.COMPLETED, run.reason
        assert run.coverage.covered == (1, 2, 3)
    else:
        run = _mutated(stale, monkeypatch, "regular", layout="pysrc")
        assert run.outcome is Outcome.COMPLETED, run.reason
        assert run.score() == 100.0


@pytest.mark.mutation_tool
@pytest.mark.parametrize("mode", ["suite", "local"])
def test_a_namespace_package_named_like_an_installed_library_is_measured(
        site, tmp_path, monkeypatch, mode):
    """The opentelemetry instrumentation layout: the target
    src/opentelemetry/instrumentation/instrumented_lib/__init__.py lies below
    namespace directories and the tests also import the installed library
    instrumented_lib. The copy's namespace directory goes first without
    breaking that import, and both runs measure the target."""
    python, site_dir = site
    if mode == "local":
        tools = Path(sys.executable).parent
        if shutil.which("cosmic-ray", path=str(tools)) is None:
            pytest.skip("needs cosmic-ray beside the interpreter")
        monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    root = tmp_path / "proj"
    relative = Path("src", "opentelemetry", "instrumentation", "instrumented_lib", "__init__.py")
    (root / relative).parent.mkdir(parents=True)
    (root / relative).write_text(TARGET, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "conftest.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))\n",
        encoding="utf-8")
    (root / "tests" / "test_mod.py").write_text(
        "import instrumented_lib\n"
        "from opentelemetry.instrumentation.instrumented_lib import sign\n\n\n"
        "def test_positive():\n    assert instrumented_lib.VERSION\n    assert sign(2) == 1\n",
        encoding="utf-8")
    (site_dir / "instrumented_lib").mkdir()
    (site_dir / "instrumented_lib" / "__init__.py").write_text("VERSION = '2.0'\n", encoding="utf-8")
    try:
        owner = prepare_project(lb.ProjectContext(root=str(root),
                                                  interpreter=lb.Interpreter(path=str(python))))
        try:
            with owner.use() as scoped:
                copy = Path(scoped.root)
                adapter = lb.create("python", scoped)
                suite = lb.Suite(str(copy / "tests"), ("test_mod.py",), ("conftest.py",))
                if mode == "suite":
                    run = adapter.tests.run_suite(SuiteRunRequest(
                        suite, timeout_s=120, coverage_target=str(copy / relative)))
                else:
                    with adapter.open_mutation_session() as session:
                        run = session.run(MutationRequest(
                            target=str(copy / relative), suite=suite,
                            catalogue=Catalogue(operators=KILLED)))
        finally:
            owner.close()
    finally:
        shutil.rmtree(site_dir / "instrumented_lib")
    assert run.outcome is Outcome.COMPLETED, run.reason
    if mode == "suite":
        assert run.coverage.covered == (1, 2, 3)
    else:
        assert run.score() == 100.0


@pytest.mark.mutation_tool
@pytest.mark.parametrize("options", [{}, {"workers": 2, "distributor": "http"}],
                         ids=["local", "http"])
def test_another_dirs_package_named_like_an_installed_library_stays_out(
        site, tmp_path, monkeypatch, options):
    """A package examples/helperlib named like the installed library the
    tests use stays off the import path: only the directory holding the run's
    target package goes first."""
    python, site_dir = site
    tools = Path(sys.executable).parent
    if shutil.which("cosmic-ray", path=str(tools)) is None:
        pytest.skip("needs cosmic-ray beside the interpreter")
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ.get("PATH", ""))
    root = tmp_path / "proj"
    files = {"src/pkg/__init__.py": "", "src/pkg/mod.py": TARGET,
             "examples/helperlib/__init__.py": "ORIGIN = 'example'\n",
             "tests/conftest.py": ("import sys\nfrom pathlib import Path\n"
                                   "sys.path.insert(0, str(Path(__file__).resolve().parents[1]"
                                   " / 'src'))\n"),
             "tests/test_mod.py": ("import helperlib\nfrom pkg.mod import sign\n\n\n"
                                   "def test_positive():\n"
                                   "    assert helperlib.ORIGIN == 'installed'\n"
                                   "    assert sign(2) == 1\n")}
    for relative, text in files.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text, encoding="utf-8")
    (site_dir / "helperlib").mkdir()
    (site_dir / "helperlib" / "__init__.py").write_text("ORIGIN = 'installed'\n", encoding="utf-8")
    try:
        owner = prepare_project(lb.ProjectContext(root=str(root),
                                                  interpreter=lb.Interpreter(path=str(python))))
        try:
            with owner.use() as scoped:
                copy = Path(scoped.root)
                adapter = lb.create("python", scoped)
                suite = lb.Suite(str(copy / "tests"), ("test_mod.py",), ("conftest.py",))
                with adapter.open_mutation_session(**options) as session:
                    run = session.run(MutationRequest(
                        target=str(copy / "src" / "pkg" / "mod.py"), suite=suite,
                        catalogue=Catalogue(operators=KILLED)))
        finally:
            owner.close()
    finally:
        shutil.rmtree(site_dir / "helperlib")
    assert run.outcome is Outcome.COMPLETED, run.reason
    assert run.score() == 100.0
