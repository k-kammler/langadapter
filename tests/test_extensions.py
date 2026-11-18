"""Extensions named by a file or a module: resolved without running code,
loaded once per file, registering what LangAdapter reads in them all or
none, with the file's SHA-256."""
import hashlib
import importlib
import sys
from importlib import metadata

import pytest

import langadapter as lb
from langadapter import registry

LANGUAGE = '''import langadapter as lb


class {cls}(lb.LangAdapter):
    language = "{language}"
    file_extensions = ("{suffix}",)


langadapter_language = {cls}
'''

PROVIDER = '''import langadapter as lb


class Provider:
    name = "{name}"

    def definitions(self):
        return (lb.MetricDefinition("{metric}", "count", "1", ("file",)),)

    def measure(self, source, *, language, parser):
        raise AssertionError("not measured here")


langadapter_metric_provider = Provider()
'''


pytestmark = pytest.mark.usefixtures("registry_kept")


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _language(path, language, suffix, cls="ToyAdapter"):
    return _write(path, LANGUAGE.format(cls=cls, language=language, suffix=suffix))


def _context(tmp_path):
    return lb.ProjectContext(root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable))


def test_a_first_language_in_one_file(tmp_path):
    toy = _language(tmp_path / "toy.py", "toy", ".toy")
    loaded = lb.load_extension(str(toy))
    assert (loaded.item, loaded.kind, loaded.file) == (str(toy), "file", str(toy.resolve()))
    assert loaded.sha256 == hashlib.sha256(toy.read_bytes()).hexdigest()
    assert (loaded.languages, loaded.metric_providers) == (("toy",), ())
    assert (loaded.distribution, loaded.version) == (None, None)
    assert lb.language_for("game.toy") == "toy"
    assert lb.registration("toy").extension is loaded
    assert lb.registration("python").extension is None
    # The module is the file's own, under a private name.
    assert loaded.module.startswith("_langadapter_extension_") and loaded.module != "toy"


def test_a_relative_file_resolves_against_its_dir_else_the_current_one(tmp_path, monkeypatch):
    toy = _language(tmp_path / "settings" / "toy.py", "toy", ".toy")
    assert lb.resolve_extension("toy.py", relative_to=str(tmp_path / "settings")).file == str(
        toy.resolve())
    monkeypatch.chdir(tmp_path)
    assert lb.resolve_extension("settings/toy.py").file == str(toy.resolve())
    with pytest.raises(lb.InvalidRequest, match="toy.py"):
        lb.resolve_extension("toy.py")


def test_a_module_item_is_found_as_python_finds_it(tmp_path, monkeypatch):
    _language(tmp_path / "la_ext_module.py", "toy", ".toy")
    monkeypatch.syspath_prepend(str(tmp_path))
    loaded = lb.load_extension("la_ext_module")
    assert (loaded.kind, loaded.file, loaded.module) == (
        "module", str((tmp_path / "la_ext_module.py").resolve()), "la_ext_module")
    assert lb.registration("toy").extension is loaded
    assert loaded.distribution is None


def test_an_installed_module_names_its_distribution_and_version():
    found = lb.resolve_extension("pytest")
    assert (found.kind, found.distribution, found.version) == (
        "module", "pytest", metadata.version("pytest"))
    assert found.file.endswith("__init__.py")


def test_a_resolution_runs_no_module(tmp_path, monkeypatch):
    """A dotted name's parent package and the module itself stay unrun, and
    a file's SHA-256 follows its bytes."""
    _write(tmp_path / "la_ext_pkg" / "__init__.py", "raise RuntimeError('the package ran')\n")
    sub = _write(tmp_path / "la_ext_pkg" / "web.py", "raise RuntimeError('the module ran')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    found = lb.resolve_extension("la_ext_pkg.web")
    assert (found.kind, found.file) == ("module", str(sub.resolve()))
    assert "la_ext_pkg" not in sys.modules and "la_ext_pkg.web" not in sys.modules
    toy = _write(tmp_path / "toy.py", "raise RuntimeError('the file ran')\n")
    first = lb.resolve_extension(str(toy))
    toy.write_text("raise RuntimeError('the file changed')\n", encoding="utf-8")
    assert lb.resolve_extension(str(toy)).sha256 != first.sha256


@pytest.mark.parametrize("make, cause", [
    (lambda path: None, "does not exist"),
    (lambda path: path.mkdir(), "is a directory, not a Python file"),
])
def test_a_file_item_that_names_no_python_file_says_what_it_names(tmp_path, make, cause):
    """A missing file is named missing, and an existing path that is no
    Python source file is named as what it is."""
    item = tmp_path / "ext.py"
    make(item)
    with pytest.raises(lb.InvalidRequest, match=cause) as refused:
        lb.resolve_extension(str(item))
    assert str(item) in str(refused.value)
    _write(tmp_path / "notes.txt", "a text file\n")
    with pytest.raises(lb.InvalidRequest, match="is a file, not a Python file"):
        lb.resolve_extension(str(tmp_path / "notes.txt"))


def test_a_package_that_changes_its_search_path_loads_by_load_extension(tmp_path, monkeypatch):
    """Resolving a module its package's code makes findable raises
    ExtensionFoundOnlyByImport without running the package, a missing item
    raises a plain InvalidRequest, and load_extension loads the module."""
    elsewhere = tmp_path / "elsewhere"
    _language(elsewhere / "lang.py", "toy", ".toy")
    _write(tmp_path / "la_ext_moving" / "__init__.py", f"__path__.append({str(elsewhere)!r})\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    with pytest.raises(lb.ExtensionFoundOnlyByImport, match="load_extension") as raised:
        lb.resolve_extension("la_ext_moving.lang")
    assert isinstance(raised.value, lb.InvalidRequest) and raised.value.field == "item"
    assert "la_ext_moving" not in sys.modules
    for missing in ("la_ext_absent", str(tmp_path / "absent.py")):
        with pytest.raises(lb.InvalidRequest) as raised:
            lb.resolve_extension(missing)
        assert not isinstance(raised.value, lb.ExtensionFoundOnlyByImport)
    with pytest.raises(lb.ExtensionFoundOnlyByImport):
        lb.resolve_extension("la_ext_moving.absent")
    with pytest.raises(lb.InvalidRequest) as raised:
        lb.load_extension("la_ext_moving.absent")
    assert not isinstance(raised.value, lb.ExtensionFoundOnlyByImport)
    loaded = lb.load_extension("la_ext_moving.lang")
    assert loaded.file == str((elsewhere / "lang.py").resolve())
    assert lb.registration("toy").extension is loaded


def test_the_same_file_twice_is_imported_once(tmp_path, monkeypatch):
    counter = tmp_path / "runs.txt"
    source = LANGUAGE.format(cls="ToyAdapter", language="toy", suffix=".toy")
    toy = _write(tmp_path / "la_ext_once.py",
                 source + f"with open({str(counter)!r}, 'a') as runs:\n    runs.write('x')\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.chdir(tmp_path)
    first = lb.load_extension(str(toy))
    assert lb.load_extension("la_ext_once.py") is first
    assert lb.load_extension("la_ext_once") is first
    assert counter.read_text() == "x"


def test_two_files_of_one_name_load_as_two_modules(tmp_path):
    one = lb.load_extension(str(_language(tmp_path / "a" / "ext.py", "toya", ".ta")))
    two = lb.load_extension(str(_language(tmp_path / "b" / "ext.py", "toyb", ".tb")))
    assert one.file != two.file and one.module != two.module
    assert lb.registration("toya").extension is one and lb.registration("toyb").extension is two


def test_a_conflict_leaves_nothing_of_the_item_registered(tmp_path):
    item = _write(tmp_path / "taken.py",
                  LANGUAGE.format(cls="TakenAdapter", language="python", suffix=".taken")
                  + PROVIDER.format(name="taken.provider", metric="taken_count"))
    with pytest.raises(lb.RegistrationConflict, match="python"):
        lb.load_extension(str(item))
    assert lb.registration("python").extension is None
    assert ".taken" not in registry._by_file_extension
    assert registry._loaded_metric_providers() == ()
    adapter = lb.create("python", _context(tmp_path))
    assert "taken_count" not in {item.name for item in adapter.sources.metric_definitions()}


def test_a_module_for_another_program_registers_nothing(tmp_path):
    item = _write(tmp_path / "goal.py", "other_program_hook = object()\n")
    before = lb.names()
    loaded = lb.load_extension(str(item))
    assert (loaded.languages, loaded.metric_providers) == ((), ())
    assert loaded.sha256 == hashlib.sha256(item.read_bytes()).hexdigest()
    assert lb.names() == before


@pytest.mark.parametrize("text, cause", [
    (None, "does not exist"),
    ("raise RuntimeError('boom')\n", "RuntimeError: boom"),
    ("langadapter_language = 3\n", "must be a LangAdapter subclass"),
])
def test_an_item_that_cannot_be_loaded_is_refused_naming_it(tmp_path, text, cause):
    item = tmp_path / "broken.py"
    if text is not None:
        _write(item, text)
    before = (lb.names(), dict(registry._loaded))
    with pytest.raises(lb.InvalidRequest, match=cause) as refused:
        lb.load_extension(str(item))
    assert str(item) in str(refused.value)
    assert (lb.names(), registry._loaded) == before
    with pytest.raises(lb.InvalidRequest, match="la_ext_missing"):
        lb.load_extension("la_ext_missing")


def test_a_metric_provider_joins_every_adapter_created_after_the_load(tmp_path):
    lb.load_extension(str(_write(tmp_path / "count.py",
                                 PROVIDER.format(name="count.provider", metric="toy_count"))))
    adapter = lb.create("python", _context(tmp_path))
    assert "toy_count" in {item.name for item in adapter.sources.metric_definitions()}
    # A provider whose metric name one loaded before holds is refused at its
    # load, and the adapters created afterwards work.
    again = _write(tmp_path / "again.py", PROVIDER.format(name="again.provider", metric="toy_count"))
    with pytest.raises(lb.InvalidRequest, match=r"toy_count.*again\.py"):
        lb.load_extension(str(again))
    assert [extension.file for _, extension in registry._loaded_metric_providers()] == [
        str((tmp_path / "count.py").resolve())]
    # So is one named like a structural counter that Python's adapter holds.
    counter = _write(tmp_path / "counter.py", PROVIDER.format(name="counter.provider", metric="loop_count"))
    with pytest.raises(lb.InvalidRequest, match="loop_count.*counter"):
        lb.load_extension(str(counter))
    later = lb.create("python", _context(tmp_path))
    assert "toy_count" in {item.name for item in later.sources.metric_definitions()}


def test_a_module_imported_before_from_another_file_is_refused(tmp_path, monkeypatch):
    """The finders show one file, the process runs another: the load names
    both and registers nothing, so no record names a file that did not run."""
    _language(tmp_path / "one" / "la_ext_moved.py", "toya", ".ta")
    _language(tmp_path / "two" / "la_ext_moved.py", "toyb", ".tb")
    monkeypatch.syspath_prepend(str(tmp_path / "one"))
    importlib.import_module("la_ext_moved")
    monkeypatch.syspath_prepend(str(tmp_path / "two"))
    with pytest.raises(lb.InvalidRequest, match="the module imported is") as refused:
        lb.load_extension("la_ext_moved")
    assert str(tmp_path / "one") in str(refused.value)
    assert "toya" not in lb.names() and "toyb" not in lb.names()
