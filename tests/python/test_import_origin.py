"""The import origin record of a measured run and its refusal: the module
named by the target must execute from the target file in the prepared
copy, recorded when the import system finds it."""
import importlib
import importlib.machinery
import importlib.util
import sys
import types

import pytest

from langadapter.python._plugins import (
    UNSUPPORTED, origin_environment, origin_plugin_text, origin_refusal,
    read_origin,
)

SUFFIX = importlib.machinery.EXTENSION_SUFFIXES[0]


@pytest.fixture
def isolated(monkeypatch):
    """The plugin's finder and the imported fixtures stay in the test."""
    monkeypatch.setattr(sys, "meta_path", list(sys.meta_path))
    monkeypatch.setattr(sys, "path", list(sys.path))
    before = set(sys.modules)
    importlib.invalidate_caches()
    yield
    for name in set(sys.modules) - before:
        del sys.modules[name]
    importlib.invalidate_caches()


def _plugin(monkeypatch, **variables):
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    plugin = types.ModuleType("origin_fixture")
    exec(origin_plugin_text(), plugin.__dict__)
    return plugin


def _files(root, *paths, text=""):
    for path in paths:
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")


def _watching(tmp_path, monkeypatch, target):
    return _plugin(monkeypatch, **origin_environment(str(target), str(tmp_path / "proj"), str(tmp_path / "origin.json")))


def _record(tmp_path, plugin):
    plugin.pytest_unconfigure(None)
    return read_origin(tmp_path / "origin.json")


def _watch(plugin):
    watch, = [finder for finder in sys.meta_path if type(finder).__name__ == "_Watch"]
    assert sys.meta_path[0] is watch
    return watch


@pytest.mark.parametrize("files, target, names", [
    (("src/company/pkg/__init__.py", "src/company/pkg/mod.py"), "src/company/pkg/mod.py",
     ["pkg.mod", "company.pkg.mod", "src.company.pkg.mod"]),
    (("pkg/__init__.py",), "pkg/__init__.py", ["pkg"]),
    (("mod.py",), "mod.py", ["mod"]),
    (("my-src/mod.py",), "my-src/mod.py", ["mod"]),
], ids=["namespace", "package", "root-module", "dir-no-name"])
def test_the_names_a_module_is_imported_by(tmp_path, monkeypatch, files, target, names):
    _files(tmp_path, *files)
    plugin = _plugin(monkeypatch)
    assert list(plugin._names(str(tmp_path / target), str(tmp_path))) == names


def test_a_module_executed_from_the_copy_is_measured(tmp_path, monkeypatch, isolated):
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py")
    target = tmp_path / "proj/src/pkg/mod.py"
    plugin = _watching(tmp_path, monkeypatch, target)
    monkeypatch.syspath_prepend(str(tmp_path / "proj/src"))
    module = importlib.import_module("pkg.mod")
    # The import system's own spec, unchanged.
    assert type(module.__spec__.loader) is importlib.machinery.SourceFileLoader
    _watch(plugin)
    record = _record(tmp_path, plugin)
    event, = record["seen"]["pkg.mod"]
    assert event["copy"] is True and event["counted"] is True
    assert origin_refusal(record) is None


def test_a_stale_install_is_refused(tmp_path, monkeypatch, isolated):
    """An older installed release executed in place of the target."""
    _files(tmp_path, "proj/pysrc/pkg/__init__.py", "proj/pysrc/pkg/mod.py", "site/pkg/__init__.py")
    _files(tmp_path, "site/pkg/mod.py", text="# 1.0\n")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/pysrc/pkg/mod.py")
    plugin._SITE = [plugin._norm(str(tmp_path / "site"))]
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    importlib.import_module("pkg.mod")
    reason = origin_refusal(_record(tmp_path, plugin))
    assert "imported pkg.mod from the installed copy" in reason
    assert str(tmp_path / "site") in reason and UNSUPPORTED in reason


def test_an_import_hook_is_refused_with_a_regular_install(tmp_path, monkeypatch, isolated):
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py",
           "original/src/pkg/__init__.py", "original/src/pkg/mod.py")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")

    class Hook:
        def find_spec(self, name, path=None, target=None):
            if name in ("pkg", "pkg.mod"):
                relative = "pkg/__init__.py" if name == "pkg" else "pkg/mod.py"
                return importlib.util.spec_from_file_location(
                    name, tmp_path / "original/src" / relative)
            return None

    sys.meta_path.insert(1, Hook())
    importlib.import_module("pkg.mod")
    reason = origin_refusal(_record(tmp_path, plugin))
    assert "imported pkg.mod from" in reason and "original" in reason
    assert "pip install . instead of pip install -e ." in reason


def test_a_hook_that_maps_only_the_package_is_a_hook(tmp_path, monkeypatch, isolated):
    """setuptools' editable finder maps the package, and the path finder
    then finds its modules in the original tree."""
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py",
           "original/src/pkg/__init__.py", "original/src/pkg/mod.py")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")

    class Hook:
        def find_spec(self, name, path=None, target=None):
            if name == "pkg":
                return importlib.util.spec_from_file_location(
                    name, tmp_path / "original/src/pkg/__init__.py",
                    submodule_search_locations=[str(tmp_path / "original/src/pkg")])
            return None

    sys.meta_path.append(Hook())
    importlib.import_module("pkg.mod")
    record = _record(tmp_path, plugin)
    assert record["seen"]["pkg.mod"][0]["hook"] is True
    assert "pip install . instead of pip install -e ." in origin_refusal(record)


def test_an_import_hook_of_a_project_with_compiled_modules_is_unsupported(
        tmp_path, monkeypatch, isolated):
    """The hook's advice leads nowhere for a compiled project."""
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py",
           "original/src/pkg/mod.py", f"build/pkg/_ext{SUFFIX}")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")

    class Hook:
        def find_spec(self, name, path=None, target=None):
            if name == "pkg.mod":
                return importlib.util.spec_from_file_location(
                    name, tmp_path / "original/src/pkg/mod.py")
            if name == "pkg._ext":
                return importlib.machinery.ModuleSpec(
                    name, None, origin=str(tmp_path / f"build/pkg/_ext{SUFFIX}"))
            return None

    sys.meta_path.insert(1, Hook())
    watch = _watch(plugin)
    assert watch.find_spec("pkg.mod") is not None and watch.find_spec("pkg._ext") is not None
    reason = origin_refusal(_record(tmp_path, plugin))
    assert "compiled modules (pkg._ext)" in reason and UNSUPPORTED in reason
    assert "pip install" not in reason


def test_a_compiled_module_beside_the_target_is_refused(tmp_path, monkeypatch, isolated):
    """A build_ext --inplace extension shadows the source."""
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py",
           f"proj/src/pkg/mod{SUFFIX}")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")
    monkeypatch.syspath_prepend(str(tmp_path / "proj/src"))
    importlib.import_module("pkg")
    spec = _watch(plugin).find_spec("pkg.mod", sys.modules["pkg"].__path__)
    assert spec.origin.endswith(SUFFIX)
    reason = origin_refusal(_record(tmp_path, plugin))
    assert "a compiled module beside the module under test" in reason
    assert UNSUPPORTED in reason and "remove the compiled module" in reason


def test_a_compiled_module_the_copy_lacks_refuses_a_run_that_could_not_collect(
        tmp_path, monkeypatch, isolated):
    """A regular install holds pkg._ext and the copy does not."""
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py",
           f"site/pkg/_ext{SUFFIX}")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    monkeypatch.syspath_prepend(str(tmp_path / "proj/src"))
    importlib.import_module("pkg")
    with pytest.raises(ImportError):
        importlib.import_module("pkg._ext")
    record = _record(tmp_path, plugin)
    assert record["missing"] == {"pkg._ext": str((tmp_path / f"site/pkg/_ext{SUFFIX}").resolve())}
    reason = origin_refusal(record, collected=False)
    assert "needs the compiled module pkg._ext" in reason and UNSUPPORTED in reason
    # A run that collected its tests ran without it, as a fallback does.
    assert origin_refusal(record) is None


def test_a_module_never_imported_is_no_refusal(tmp_path, monkeypatch, isolated):
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")
    record = _record(tmp_path, plugin)
    assert record["seen"] == {} and origin_refusal(record) is None


def test_a_standard_library_module_of_the_same_name_is_not_the_project(
        tmp_path, monkeypatch, isolated):
    import json

    _files(tmp_path, "proj/src/json.py")
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/json.py")
    assert sys.modules["json"] is json
    record = _record(tmp_path, plugin)
    assert record["seen"] == {} and origin_refusal(record) is None


def test_a_missing_record_is_unconfirmed(tmp_path):
    assert read_origin(tmp_path / "absent.json") is None
    assert "could not be confirmed" in origin_refusal(None)


def test_a_library_named_like_a_deeper_package_is_another_module(tmp_path, monkeypatch, isolated):
    """The target opentelemetry.instrumentation.instrumented_lib and an
    installed library instrumented_lib: a name from a directory two levels
    into the project is not the target's, whether or not the tests import
    the target."""
    _files(tmp_path, "proj/src/opentelemetry/instrumentation/instrumented_lib/__init__.py",
           "site/instrumented_lib/__init__.py")
    target = tmp_path / "proj/src/opentelemetry/instrumentation/instrumented_lib/__init__.py"
    plugin = _watching(tmp_path, monkeypatch, target)
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    importlib.import_module("instrumented_lib")
    record = _record(tmp_path, plugin)
    assert record["seen"]["instrumented_lib"][0]["counted"] is False
    assert origin_refusal(record) is None
    monkeypatch.syspath_prepend(str(tmp_path / "proj/src"))
    importlib.import_module("opentelemetry.instrumentation.instrumented_lib")
    record = _record(tmp_path, plugin)
    assert record["seen"]["opentelemetry.instrumentation.instrumented_lib"][0]["copy"] is True
    assert origin_refusal(record) is None


def test_a_module_replacing_itself_with_a_wrapper_counts_as_executed(
        tmp_path, monkeypatch, isolated):
    """The module puts a delegating wrapper in sys.modules."""
    _files(tmp_path, "proj/src/pkg/__init__.py")
    _files(tmp_path, "proj/src/pkg/mod.py", text=(
        "import sys\n\n\nclass _Wrapper:\n    def __init__(self, wrapped):\n"
        "        self._wrapped = wrapped\n\n\n"
        "sys.modules[__name__] = _Wrapper(sys.modules[__name__])\n"))
    plugin = _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")
    monkeypatch.syspath_prepend(str(tmp_path / "proj/src"))
    importlib.import_module("pkg.mod")
    assert not isinstance(sys.modules["pkg.mod"], types.ModuleType)
    record = _record(tmp_path, plugin)
    assert record["seen"]["pkg.mod"][0]["copy"] is True and origin_refusal(record) is None


def test_a_lazily_loaded_target_is_not_executed_by_the_record(tmp_path, monkeypatch, isolated):
    _files(tmp_path, "proj/src/pkg/__init__.py")
    target = tmp_path / "proj/src/pkg/mod.py"
    marker = tmp_path / "executed"
    _files(tmp_path, "proj/src/pkg/mod.py", text=f"open({str(marker)!r}, 'w').close()\n")
    plugin = _watching(tmp_path, monkeypatch, target)
    monkeypatch.syspath_prepend(str(tmp_path / "proj/src"))
    spec = importlib.util.find_spec("pkg.mod")
    spec.loader = importlib.util.LazyLoader(spec.loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pkg.mod"] = module
    spec.loader.exec_module(module)
    record = _record(tmp_path, plugin)
    assert not marker.exists() and record["seen"]["pkg.mod"][0]["copy"] is True


def test_a_module_loaded_from_its_file_is_read_from_its_spec(tmp_path, monkeypatch, isolated):
    """Loaded by path under the target's name, without a finder."""
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py")
    target = tmp_path / "proj/src/pkg/mod.py"
    plugin = _watching(tmp_path, monkeypatch, target)
    spec = importlib.util.spec_from_file_location("pkg.mod", target)
    module = importlib.util.module_from_spec(spec)
    sys.modules["pkg.mod"] = module
    spec.loader.exec_module(module)
    plugin.pytest_collection_finish(None)
    record = _record(tmp_path, plugin)
    assert record["seen"]["pkg.mod"][0]["copy"] is True and origin_refusal(record) is None


def test_the_longest_name_decides(tmp_path):
    """The module executed under the target's longest name is the target's."""
    record = {"target": "t", "names": [], "seen": {
        "pkg.mod": [{"file": "/elsewhere/pkg/mod.py", "counted": True, "hook": True}],
        "src.pkg.mod": [{"file": "/venv/site-packages/src/pkg/mod.py", "counted": True,
                         "installed": True}]}}
    assert "imported src.pkg.mod from the installed copy" in origin_refusal(record)


def test_a_finders_error_reaches_the_import_once(tmp_path, monkeypatch, isolated):
    """A project hook that raises for the target's name reports its own
    error and is asked once, as without the finder."""
    _files(tmp_path, "proj/src/pkg/__init__.py", "proj/src/pkg/mod.py")
    _watching(tmp_path, monkeypatch, tmp_path / "proj/src/pkg/mod.py")
    asked = []

    class Hook:
        def find_spec(self, name, path=None, target=None):
            if name == "pkg":
                asked.append(name)
                raise RuntimeError("the project's hook failed")
            return None

    sys.meta_path.insert(1, Hook())
    with pytest.raises(RuntimeError, match="the project's hook failed"):
        importlib.import_module("pkg.mod")
    assert asked == ["pkg"]


def test_a_run_without_a_module_behind_a_hook_to_a_compiled_module_gets_the_hook_text():
    """A run that names no module names no module under test in its refusal:
    an editable install's hook that loads a compiled module gets the hook's
    text and its advice, whatever name sorts first."""
    hooked = {"copy": False, "counted": True, "installed": False, "beside": False, "hook": True}
    record = {"target": "", "names": ["pkg"], "missing": {}, "compiled": {}, "seen": {
        "pkg._ext": [{**hooked, "file": "/build/pkg/_ext" + SUFFIX, "compiled": True}],
        "pkg.mod": [{**hooked, "file": "/original/pkg/mod.py", "compiled": False}]}}
    reason = origin_refusal(record, named=False)
    assert "pkg._ext" in reason and "import hook of an editable install" in reason
    assert "module under test is" not in reason and "pip install . instead of" in reason


def test_a_compiled_fallback_that_ran_from_the_copy_keeps_its_outcome():
    """With the compiled module missing and the target run from the copy,
    a collection failure of another test file stays the run's outcome."""
    record = {"target": "t", "names": ["pkg.mod"], "missing": {"pkg._ext": "/site/pkg/_ext.so"},
              "seen": {"pkg.mod": [{"file": "t", "copy": True, "counted": True}]}}
    assert origin_refusal(record, collected=False) is None
    del record["seen"]["pkg.mod"]
    assert "needs the compiled module pkg._ext" in origin_refusal(record, collected=False)
