"""The project's source files and the call statistics that search them."""
import pytest

import langadapter as lb


def _write(root, files):
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_the_source_files_leave_out_tests_ignored_dirs_declared_and_empty_files(tmp_path):
    _write(tmp_path, {"setup.py": "x = 1\n", "src/app.py": "def parse(): pass\n",
                      "src/broken.py": "def (:\n", "src/__init__.py": "",
                      "src/test_helper.py": "y = 1\n", "tests/test_app.py": "z = 1\n",
                      "tests/helper.py": "w = 1\n", ".venv/lib/x.py": "v = 1\n",
                      ".hidden/y.py": "h = 1\n", "build/z.py": "b = 1\n", "notes.txt": "n\n"})
    sources = lb.create("python", lb.ProjectContext(str(tmp_path))).sources
    listing = sources.project_source_files()
    assert (listing.status, listing.value) == ("completed", ("src/app.py", "src/broken.py"))
    assert sources.supports("project_source_files")


def test_a_java_projects_source_files(tmp_path):
    _write(tmp_path, {"src/main/java/App.java": "class App {}\n",
                      "src/main/java/AppTest.java": "class AppTest {}\n",
                      "src/test/java/Other.java": "class Other {}\n",
                      "target/Gen.java": "class Gen {}\n"})
    listing = lb.create("java", lb.ProjectContext(str(tmp_path))).sources.project_source_files()
    assert listing.value == ("src/main/java/App.java",)


def test_a_root_the_host_cannot_read_is_no_empty_listing(tmp_path):
    """A container context's root the host does not have is an
    infrastructure failure, not an empty listing."""
    context = lb.ProjectContext("/langadapter-absent-project", lb.Interpreter(path="/usr/bin/python3"),
                                execution=lb.ContainerExecution("selected", work_dir=str(tmp_path),
                                                                helper_python="/usr/bin/python3"))
    listing = lb.create("python", context).sources.project_source_files()
    assert listing.status == "infrastructure_failure" and listing.value is None
    assert "langadapter-absent-project" in listing.reason


PARSER = "def parse(text):\n    return text\n\n\nclass Reader:\n    def parse(self):\n        return 1\n"


def _counts(scope):
    return {item.definition.name: item.value for item in scope.metrics}


def test_call_sites_are_counted_by_name_in_the_other_source_files(tmp_path):
    _write(tmp_path, {
        "src/parser.py": PARSER,
        "src/a.py": "from parser import parse\nparse(1)\nparse(2)\n",
        "src/b.py": "def run(reader):\n    return reader.parse()\n",
        "src/alias.py": "from parser import parse as p\nhandlers = [parse]\np(3)\n",
        "src/broken.py": "def (:\n    parse(4)\n",
        "src/test_parser.py": "parse(5)\n", "tests/helper.py": "parse(6)\n",
        "build/gen.py": "parse(7)\n"})
    sources = lb.create("python", lb.ProjectContext(str(tmp_path))).sources
    result = sources.call_statistics("src/parser.py")
    assert result.status == "completed"
    metrics = result.value
    functions = {scope.name: _counts(scope) for scope in metrics.functions}
    assert functions == {
        "parse": {"call_site_count_estimate": 3, "calling_file_count_estimate": 2},
        "Reader.parse": {"call_site_count_estimate": 3, "calling_file_count_estimate": 2}}
    assert _counts(metrics.file) == {"call_site_count_estimate": 6, "calling_file_count_estimate": 2}
    value = metrics.file.metrics[0]
    assert (value.definition.definition_version, value.provider) == (
        "name-match-1", "langadapter.call_statistics")
    assert value.reason == "left out, since they do not parse or cannot be read: src/broken.py"
    scopes = {scope.key for scope in sources.metrics(PARSER, names=["function_count"]).value.functions}
    assert {scope.key for scope in metrics.functions} == scopes


def test_a_target_that_does_not_parse_has_no_call_statistics(tmp_path):
    _write(tmp_path, {"src/broken.py": "def (:\n", "src/a.py": "broken()\n"})
    result = lb.create("python", lb.ProjectContext(str(tmp_path))).sources.call_statistics("src/broken.py")
    assert (result.status, result.value) == ("parse_error", None)


def test_a_java_methods_call_sites(tmp_path):
    _write(tmp_path, {
        "src/main/java/Calc.java": "class Calc { int add(int a, int b) { return a + b; } }\n",
        "src/main/java/Use.java": "class Use { int f(Calc c) { return c.add(1, 2) + c.add(3, 4); } }\n",
        "src/test/java/CalcTest.java": "class CalcTest { void t(Calc c) { c.add(5, 6); } }\n"})
    result = lb.create("java", lb.ProjectContext(str(tmp_path))).sources.call_statistics(
        "src/main/java/Calc.java")
    assert [(scope.name, _counts(scope)) for scope in result.value.functions] == [
        ("Calc.add", {"call_site_count_estimate": 2, "calling_file_count_estimate": 1})]


def test_a_target_is_resolved_inside_the_project(tmp_path):
    root = tmp_path / "proj"
    _write(root, {"src/parser.py": "def parse(n):\n    return parse(n - 1) if n else 0\n",
                  "src/a.py": "from parser import parse\nparse(1)\n"})
    _write(tmp_path, {"other/x.py": "def f():\n    return 1\n"})
    sources = lb.create("python", lb.ProjectContext(str(root))).sources
    spelled = sources.call_statistics("src/../src/parser.py")
    assert [(scope.name, _counts(scope)) for scope in spelled.value.functions] == [
        ("parse", {"call_site_count_estimate": 1, "calling_file_count_estimate": 1})]
    for outside in ("../other/x.py", str(tmp_path / "other" / "x.py")):
        with pytest.raises(lb.InvalidRequest, match="target"):
            sources.call_statistics(outside)


def test_the_call_search_alone_reports_no_call_statistics(tmp_path):
    class SearchOnly(lb.LangAdapter):
        language = "search-only"

        def call_statistics(self, source):
            return {}

    adapter = SearchOnly(lb.ProjectContext(str(tmp_path)))
    report = {item.name: item for item in adapter.operation_capabilities()}
    assert not report["sources.call_statistics"].supported
    assert adapter.sources.call_statistics("x.src").status == "unsupported"


def test_an_adapter_that_tells_no_test_file_lists_none(tmp_path):
    adapter = lb.LangAdapter(lb.ProjectContext(str(tmp_path)))
    adapter.language = "partial"
    listing = adapter.sources.project_source_files()
    assert (listing.status, listing.value) == ("unsupported", None)
    assert "is_test_file" in listing.reason
    report = {item.name: item for item in adapter.operation_capabilities()}
    assert not report["sources.project_source_files"].supported
    assert adapter.sources.call_statistics("x.src").status == "unsupported"
    assert not report["sources.call_statistics"].supported
