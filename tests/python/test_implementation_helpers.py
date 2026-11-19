"""Helpers of the Python implementation that its public operations rest on:
the suite root's pytest anchor, the blanking of a collection breaker, POSIX
container paths, module names, and the eviction's coverage shield plugin."""
from __future__ import annotations

import ast
import builtins
import sys
import types
from pathlib import Path

from langadapter.python._plugins import _EVICTION_SHIELD_PLUGIN
from langadapter.python._sources import (
    _blank_collection_breaker, _suite_rootdir, file_to_module_name, _parent_dir,
)


# _suite_rootdir: anchors pytest's search for its ini file.

def test_suite_rootdir_writes_an_anchoring_pytest_ini(tmp_path):
    # The empty pytest.ini ends pytest's ini search here, so a strict parent
    # ini (filterwarnings=error) cannot fail collection.
    suite = tmp_path / "test_x.py"
    suite.write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    root = _suite_rootdir(str(suite))
    assert Path(root) == tmp_path
    anchor = tmp_path / "pytest.ini"
    assert anchor.exists()
    assert "[pytest]" in anchor.read_text(encoding="utf-8")


def test_suite_rootdir_preserves_an_existing_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = -p no:cacheprovider\n", encoding="utf-8")
    suite = tmp_path / "test_x.py"
    suite.write_text("def test_a():\n    assert 1\n", encoding="utf-8")
    _suite_rootdir(str(suite))
    assert "no:cacheprovider" in (tmp_path / "pytest.ini").read_text(
        encoding="utf-8")


def test_suite_rootdir_posix_container_path_writes_no_host_anchor():
    root = _suite_rootdir("/home/swe/repo/xarray/tests/test_x.py",
                          container=True)
    assert root == "/home/swe/repo/xarray/tests"
    assert not (Path("/home/swe/repo/xarray/tests") / "pytest.ini").exists()


def test_parent_dir_keeps_posix_container_paths_posix():
    # Path would rewrite / on a Windows host.
    assert _parent_dir("/home/swe/repo/pkg/m.py") == "/home/swe/repo/pkg"


# _blank_collection_breaker: the structural core of prune_collection_breakers.

def test_blank_breaker_blanks_the_enclosing_top_level_statement():
    src = "import os\nBAD = compute()\n\ndef test_a():\n    assert 1\n"
    out, span = _blank_collection_breaker(src, 2)
    assert span == (2, 2)
    assert "BAD = compute()" not in out
    assert "pass" in out
    assert "def test_a" in out
    ast.parse(out)


def test_blank_breaker_descends_into_a_class_body():
    """A line in a class body blanks only that member."""
    src = ("class T:\n"
           "    DATA = build_at_class_scope()\n"
           "    def test_a(self):\n        assert 1\n")
    out, span = _blank_collection_breaker(src, 2)
    assert span == (2, 2)
    assert "build_at_class_scope" not in out
    assert "    pass" in out                 # blanked at the member indentation
    assert "def test_a" in out
    ast.parse(out)


def test_blank_breaker_comment_line_is_a_noop():
    """A comment-only line encloses no statement, so nothing is blanked."""
    src = "# a separating comment\nx = 1\n\ndef test_a():\n    assert 1\n"
    assert _blank_collection_breaker(src, 1) is None


def test_blank_breaker_decorated_def_blanks_from_the_decorator():
    """A decorated definition is blanked from its decorator on."""
    src = ("@deco\n"
           "def helper():\n    return boom()\n\n"
           "def test_a():\n    assert 1\n")
    out, span = _blank_collection_breaker(src, 3)
    assert span[0] == 1
    assert "@deco" not in out
    assert "def helper" not in out
    assert "def test_a" in out
    ast.parse(out)


def test_blank_breaker_line_outside_any_statement_is_none():
    assert _blank_collection_breaker("x = 1\n", 99) is None


# file_to_module_name

def test_file_to_module_name_basic(tmp_path: Path) -> None:
    """``<root>/pkg/sub/mod.py`` maps to ``pkg.sub.mod``."""
    root = tmp_path / "src"
    (root / "pkg" / "sub").mkdir(parents=True)
    f = root / "pkg" / "sub" / "mod.py"
    f.write_text("")
    assert file_to_module_name(str(f), str(root)) == "pkg.sub.mod"


def test_file_to_module_name_root_level(tmp_path: Path) -> None:
    """A top-level ``mod.py`` maps to ``mod``."""
    root = tmp_path / "src"
    root.mkdir()
    f = root / "mod.py"
    f.write_text("")
    assert file_to_module_name(str(f), str(root)) == "mod"


def test_file_to_module_name_outside_import_root(tmp_path: Path) -> None:
    """A file outside the import root is not a module of it."""
    other = tmp_path / "elsewhere.py"
    other.write_text("")
    assert file_to_module_name(str(other), str(tmp_path / "root")) is None


def test_file_to_module_name_keeps_a_package_named_like_its_root(tmp_path: Path) -> None:
    """A package named like its project directory is part of the module name."""
    root = tmp_path / "tenacity"
    (root / "tenacity").mkdir(parents=True)
    f = root / "tenacity" / "retry.py"
    f.write_text("")
    assert file_to_module_name(str(f), str(root)) == "tenacity.retry"
    assert file_to_module_name(str(root / "tenacity" / "__init__.py"), str(root)) == "tenacity"
    assert file_to_module_name(str(root / "__init__.py"), str(root)) is None
    # A directory named like the root above it is outside it.
    outer = tmp_path / "work" / "tenacity" / "x.py"
    assert file_to_module_name(str(outer), str(root)) is None
    # A container's paths, as POSIX paths on any host.
    assert file_to_module_name("/work/tenacity/tenacity/retry.py", "/work/tenacity",
                               container=True) == "tenacity.retry"
    assert file_to_module_name("/work/tenacity/../tenacity/a/b.py", "/work/tenacity/",
                               container=True) == "a.b"
    assert file_to_module_name("/other/tenacity/retry.py", "/work/tenacity",
                               container=True) is None
    assert file_to_module_name(str(root / "my-pkg" / "mod.py"), str(root)) is None


# The eviction's coverage shield plugin

def test_the_shield_plugin_binds_once_and_opens_no_context():
    """The plugin has one configure-time hook: a per-test hook would add to
    the per-test durations the reduction minimizes, and a test* function
    would open its own coverage context."""
    tree = ast.parse(_EVICTION_SHIELD_PLUGIN)
    functions = [n.name for n in ast.walk(tree)
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    assert functions == ["pytest_configure"]


def test_the_shield_plugin_binds_the_real_builtins_into_coverage_only(
        monkeypatch):
    measured = types.ModuleType("coverage.adapter_fake_part")
    unrelated = types.ModuleType("adapter_fake_unrelated")
    monkeypatch.setitem(sys.modules, "coverage.adapter_fake_part", measured)
    monkeypatch.setitem(sys.modules, "adapter_fake_unrelated", unrelated)
    namespace = {}
    exec(compile(_EVICTION_SHIELD_PLUGIN, "shield", "exec"), namespace)
    measured.len = "defined by the module itself"
    real_divmod = builtins.divmod
    # Replaced after the plugin was loaded, as a test does, and restored at once.
    builtins.divmod = lambda *args: "replaced"
    try:
        namespace["pytest_configure"](None)
    finally:
        builtins.divmod = real_divmod
    assert measured.divmod is real_divmod
    assert measured.len == "defined by the module itself"
    assert "divmod" not in vars(unrelated)
