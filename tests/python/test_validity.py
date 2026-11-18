from __future__ import annotations

import hashlib
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor

import pytest

from langadapter.sources import TargetIdentity
from langadapter.python._validity import (
    POLICY_VERSION,
    analyze_python_generated_test_validity,
    screen_python_generated_test_source,
)


TARGET_SOURCE = """\
def add(left, right):
    return left + right


class Calculator:
    def double(self, value):
        return value * 2
"""


@pytest.fixture
def identity() -> TargetIdentity:
    return TargetIdentity(
        source_path="/workspace/pkg/target.py",
        project_root="/workspace",
        module_name="pkg.target",
        language="python",
        policy_version=POLICY_VERSION,
        target_source_sha256=hashlib.sha256(TARGET_SOURCE.encode()).hexdigest(),
        target_source=TARGET_SOURCE,
    )


def rules(source: str, identity: TargetIdentity) -> set[str]:
    return {
        finding.rule_id
        for finding in analyze_python_generated_test_validity(source, identity)
    }


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            """\
import inspect
import pkg.target as target
def test_source():
    assert "return" in inspect.getsource(target.add)
""",
            "target_source_inspection",
        ),
        (
            """\
from inspect import getsourcelines as lines
from pkg import target
def test_source():
    assert lines(target.add)
""",
            "target_source_inspection",
        ),
        (
            """\
from pathlib import Path
import pkg.target as target
def test_source():
    assert Path(target.__file__).read_text()
""",
            "target_source_read",
        ),
        (
            """\
import pkg.target as target
def test_source():
    assert target.__spec__.origin.endswith("target.py")
""",
            "target_source_path",
        ),
        (
            """\
import dis
import pkg.target as target
def test_code():
    assert list(dis.get_instructions(target.add))
""",
            "target_code_inspection",
        ),
        (
            """\
import typing
import pkg.target as target
def test_stub():
    return typing.get_overloads(target.add)[0](1, 2)
""",
            "overload_stub_execution",
        ),
        (
            """\
import typing
import pkg.target as target
def test_private_stub():
    registry = typing._overload_registry[target.add.__module__]
    return registry[target.add.__qualname__][0](1, 2)
""",
            "overload_stub_execution",
        ),
        (
            """\
import pkg.target as target
def test_synthetic():
    exec(compile("x = 1", target.__file__, "exec"), target.__dict__)
""",
            "synthetic_target_attribution",
        ),
        (
            """\
import importlib
def test_resolved():
    module = importlib.import_module("pkg.target")
    assert open(module.__file__).read()
""",
            "target_source_read",
        ),
        (
            """\
import importlib.util
def test_resolved():
    spec = importlib.util.find_spec("pkg.target")
    assert open(spec.origin).read()
""",
            "target_source_read",
        ),
        (
            """\
import sys
def test_resolved():
    module = sys.modules["pkg.target"]
    assert module.__file__
""",
            "target_source_path",
        ),
        (
            """\
import sys
def test_resolved():
    module = sys.modules.get("pkg.target")
    assert module.__file__
""",
            "target_source_path",
        ),
        (
            """\
import pkg.target as target
def test_loader():
    assert target.__loader__.get_source(target.__name__)
""",
            "target_source_inspection",
        ),
        (
            """\
import pkg.target as target
def test_loader():
    assert target.__spec__.loader.get_source(target.__name__)
""",
            "target_source_inspection",
        ),
        (
            """\
import importlib
def test_keyword_import():
    module = importlib.import_module(name="pkg.target")
    assert module.__file__
""",
            "target_source_path",
        ),
        (
            """\
import pkg.target as target
def test_keyword_open():
    return open(file=target.__file__).read()
""",
            "target_source_read",
        ),
        (
            """\
from builtins import open as read
import pkg.target as target
def test_alias():
    return read(file=target.__file__).read()
""",
            "target_source_read",
        ),
        (
            """\
import pkg.target as target
def test_keyword_compile():
    return compile(source="x = 1", filename=target.__file__, mode="exec")
""",
            "synthetic_target_attribution",
        ),
        (
            """\
import operator
import pkg.target as target
def test_computed():
    return operator.attrgetter("__file__")(target)
""",
            "target_source_path",
        ),
        (
            """\
def test_path():
    return open("/workspace/pkg/target.py").read()
""",
            "target_source_read",
        ),
        (
            """\
import inspect
import pkg.target as target
class TestSource:
    text = inspect.getsource(target.add)
    def test_source(self):
        assert "return" in self.text
""",
            "target_source_inspection",
        ),
        (
            """\
import inspect
import pytest
import pkg.target as target
@pytest.mark.parametrize("text", [inspect.getsource(target.add)])
def test_source(text):
    assert "return" in text
""",
            "target_source_inspection",
        ),
        (
            """\
import inspect
import pkg.target as target
def test_source(text=inspect.getsource(target.add)):
    assert "return" in text
""",
            "target_source_inspection",
        ),
        (
            """\
import inspect
import pkg.target as target
def test_source():
    match 1:
        case 1:
            assert "return" in inspect.getsource(target.add)
""",
            "target_source_inspection",
        ),
        pytest.param(
            """\
import inspect
import pkg.target as target
def test_source():
    try:
        pass
    except* ValueError:
        assert "return" in inspect.getsource(target.add)
""",
            "target_source_inspection",
            marks=pytest.mark.skipif(sys.version_info < (3, 11), reason="except* is Python 3.11's"),
        ),
        (
            """\
import inspect
import pkg.target as target
def test_source():
    read = lambda: inspect.getsource(target.add)
    assert "return" in read()
""",
            "target_source_inspection",
        ),
        (
            """\
import inspect
from . import target
def test_source():
    assert "return" in inspect.getsource(target.add)
""",
            "target_source_inspection",
        ),
    ],
)
def test_rejects_target_implementation_routes(
    source: str, expected: str, identity: TargetIdentity,
) -> None:
    assert expected in rules(source, identity)


def test_propagates_target_path_through_local_helper(identity: TargetIdentity) -> None:
    source = """\
import pkg.target as target

def read(path):
    with open(path) as handle:
        return handle.read()

def test_source():
    assert read(target.__file__)
"""
    findings = analyze_python_generated_test_validity(source, identity)
    assert "target_source_read" in {finding.rule_id for finding in findings}
    helper = next(finding for finding in findings if finding.rule_id == "target_source_read")
    assert helper.enclosing_test is None


@pytest.mark.parametrize(
    "source",
    [
        """\
import inspect
import pkg.target as target
def test_dynamic(name):
    inspect.getsource(getattr(target, name))
""",
        """\
import dis
import pkg.target as target
def test_dynamic(name):
    dis.Bytecode(getattr(target, name))
""",
        """\
import pkgutil
def test_resource():
    assert pkgutil.get_data("pkg", "target.py")
""",
        """\
from importlib import resources
def test_resource():
    assert resources.read_text("pkg", "target.py")
""",
        """\
from importlib import resources
def test_resource():
    assert resources.files("pkg").joinpath("target.py").read_text()
""",
    ],
)
def test_rejects_ambiguous_and_resource_routes(
    source: str, identity: TargetIdentity,
) -> None:
    assert analyze_python_generated_test_validity(source, identity)


def test_dynamic_runtime_attribute_without_source_flow_is_allowed(
    identity: TargetIdentity,
) -> None:
    source = """\
import pkg.target as target
def test_runtime_metadata(name):
    assert getattr(target, name) is not None
"""
    assert analyze_python_generated_test_validity(source, identity) == ()


def test_does_not_reject_helper_with_only_test_owned_paths(identity: TargetIdentity) -> None:
    source = """\
def read(path):
    with open(path) as handle:
        return handle.read()

def test_own_file(tmp_path):
    path = tmp_path / "data.txt"
    path.write_text("ok")
    assert read(path) == "ok"
"""
    assert analyze_python_generated_test_validity(source, identity) == ()


def test_a_finding_names_the_collected_test_that_holds_it(identity: TargetIdentity) -> None:
    """The enclosing test is the one pytest collects: a function nested in
    a test names that test, a method of a class pytest does not collect
    names none, and a unittest method counts whatever its class's name."""
    source = """\
import inspect
import unittest
import pkg.target as target

def testfoo():
    def test_inner():
        return inspect.getsource(target.add)
    assert test_inner()

class Helper:
    def test_y(self):
        return inspect.getsource(target.add)

class CalcTests(unittest.TestCase):
    def testRead(self):
        self.assertTrue(inspect.getsource(target.add))
"""
    assert [finding.enclosing_test for finding in
            analyze_python_generated_test_validity(source, identity)] == [
        "testfoo", None, "CalcTests.testRead"]


def test_rejects_embedded_complete_target_source(identity: TargetIdentity) -> None:
    source = f"""\
def test_copy():
    code = {TARGET_SOURCE!r}
    namespace = {{}}
    exec(compile(code, "test_owned.py", "exec"), namespace)
"""
    assert "copied_target_execution" in rules(source, identity)


def test_accepts_unrelated_executable_literal(identity: TargetIdentity) -> None:
    source = """\
def test_literal():
    namespace = {}
    exec("def owned(value):\\n    return value + 1", namespace)
    assert namespace["owned"](1) == 2
"""
    assert analyze_python_generated_test_validity(source, identity) == ()


def test_accepts_runtime_metadata_and_test_module_file(identity: TargetIdentity) -> None:
    source = """\
import inspect
import pkg.target as target

def test_metadata():
    assert list(inspect.signature(target.add).parameters) == ["left", "right"]
    assert target.add.__annotations__ == {}
    assert __file__.endswith("test_target.py")
"""
    assert analyze_python_generated_test_validity(source, identity) == ()


def test_accepts_the_targets_namespace_as_runtime_metadata(identity: TargetIdentity) -> None:
    """A module's or class's namespace is runtime metadata, which is
    allowed, unlike the target's source text, bytecode and copied source."""
    source = """\
import pkg.target as target

def test_namespace():
    assert "double" in target.Calculator.__dict__
    assert "add" in vars(target) and vars(target.Calculator)["double"]
    assert target.__dict__["add"](1, 2) == 3
"""
    assert analyze_python_generated_test_validity(source, identity) == ()


@pytest.mark.parametrize(("source", "expected"), [
    ("""\
import pkg.target as target
def test_loader():
    assert vars(target)["__loader__"].get_source(target.__name__)
""", "target_source_inspection"),
    ("""\
import pkg.target as target
def test_code():
    assert target.__dict__["add"].__code__.co_code
""", "target_code_inspection"),
    ("""\
import pkg.target as target
def test_path():
    assert open(vars(target)["__file__"]).read()
""", "target_source_read"),
], ids=["loader", "code", "file"])
def test_source_text_and_bytecode_reached_through_the_namespace_stay_forbidden(
        source: str, expected: str, identity: TargetIdentity) -> None:
    assert expected in rules(source, identity)


def test_importing_similarly_named_module_is_not_target(identity: TargetIdentity) -> None:
    source = """\
import pkg.target_helpers as target
def test_helper_source():
    assert target.__file__
"""
    assert analyze_python_generated_test_validity(source, identity) == ()


def test_parse_failure_is_not_silently_accepted(identity: TargetIdentity) -> None:
    with pytest.raises(SyntaxError):
        analyze_python_generated_test_validity("def test_bad(:\n", identity)


def test_analysis_does_not_emit_compiler_syntax_warnings(
    identity: TargetIdentity,
) -> None:
    source = "def test_pattern():\n    assert '\\*'\n"
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert analyze_python_generated_test_validity(source, identity) == ()
        assert screen_python_generated_test_source(source, identity) == ()
    assert caught == []


def test_analysis_is_deterministic_under_concurrent_calls(
    identity: TargetIdentity,
) -> None:
    source = """\
import inspect
import pkg.target as target
def test_source():
    assert inspect.getsource(target.add)
"""
    expected = analyze_python_generated_test_validity(source, identity)
    with ThreadPoolExecutor(max_workers=8) as pool:
        observed = list(pool.map(
            lambda _index: analyze_python_generated_test_validity(
                source, identity,
            ),
            range(64),
        ))
    assert all(item == expected for item in observed)


def test_broad_screen_reports_review_signals(identity: TargetIdentity) -> None:
    source = """\
import os
import traceback

def test_review():
    os.walk(".")
    traceback.format_exc()
    open("other.py").read()
    assert "pkg.target" == "pkg.target"
"""
    observed = {flag.rule_id for flag in screen_python_generated_test_source(source, identity)}
    assert observed == {
        "python_source_read",
        "runtime_source_rendering",
        "source_discovery",
        "target_literal",
    }


def test_broad_screen_ignores_inert_target_docstrings(
    identity: TargetIdentity,
) -> None:
    source = '''\
"""Tests for pkg.target."""

def test_ok():
    """pkg.target behavior."""
    assert True

"""Tests for pkg.target."""
'''

    assert screen_python_generated_test_source(source, identity) == ()


def test_broad_screen_takes_an_import_path_for_an_import(identity: TargetIdentity) -> None:
    """An importlib import path names the target as a patch target does, so
    it raises no flag. A path or a file name stays a signal."""
    source = """\
import importlib
from unittest import mock

target = importlib.import_module("pkg.target")


def test_patched(monkeypatch):
    with mock.patch("pkg.target.add", return_value=3):
        monkeypatch.setattr("pkg.target.Calculator.double", lambda self, v: v)
        assert target.add(1, 1) == 3
    assert "pkg/target.py"
"""
    flags = screen_python_generated_test_source(source, identity)
    assert [(flag.rule_id, flag.start_line) for flag in flags] == [("target_literal", 11)]


def test_broad_screen_keeps_target_literals_used_by_code(
    identity: TargetIdentity,
) -> None:
    source = '''\
TARGET = "pkg.target"

def test_target_name():
    assert TARGET == "pkg.target"
'''

    flags = screen_python_generated_test_source(source, identity)

    assert {flag.rule_id for flag in flags} == {"target_literal"}


#: A test source for each finding the policy can give, with every finding
#: it gives that source: (rule, operation, diagnostic).
_FINDINGS = [
    (
        """\
import dis
import pkg.target as target
def test_dynamic(name):
    dis.Bytecode(getattr(target, name))
""",
        {
            ("ambiguous_target_attribute", "dis.Bytecode",
             "inspects a selected or target-derived code object"),
        },
    ),
    (
        """\
import pkg.target as target
def test_dynamic(name='add'):
    assert getattr(target, name).__code__
""",
        {
            ("ambiguous_target_attribute", "getattr(...).__code__",
             "an unresolved target attribute flows into an implementation access"),
        },
    ),
    (
        """\
import inspect
import pkg.target as target
def test_dynamic(name):
    inspect.getsource(getattr(target, name))
""",
        {
            ("ambiguous_target_attribute", "inspect.getsource",
             "reads implementation source from a selected or target-derived value"),
        },
    ),
    (
        "def test_compile():\n    assert compile('def add(left, right):\\n    return left + right\\n', 'x', 'exec')\n",
        {
            ("copied_target_execution", "compile",
             "compiles a copied implementation span from the selected target"),
        },
    ),
    (
        """\
import inspect
import pkg.target as target
def test_compile():
    assert compile(inspect.getsource(target.add), 'x', 'exec')
""",
        {
            ("copied_target_execution", "compile",
             "compiles implementation source obtained from the selected target"),
            ("target_source_inspection", "inspect.getsource",
             "reads implementation source from a selected or target-derived value"),
        },
    ),
    (
        "def test_exec():\n    exec('def add(left, right):\\n    return left + right\\n')\n",
        {
            ("copied_target_execution", "exec",
             "executes a copied implementation span from the selected target"),
        },
    ),
    (
        """\
import pkg.target as target
def test_synthetic():
    exec(compile("x = 1", target.__file__, "exec"), target.__dict__)
""",
        {
            ("copied_target_execution", "exec",
             "executes or evaluates copied implementation code from the selected target"),
            ("synthetic_target_attribution", "compile filename",
             "attributes synthetic code to the selected target's source path"),
            ("target_source_path", "target.__file__",
             "accesses the selected target module's source path"),
        },
    ),
    (
        """\
import typing
import pkg.target as target
def test_stub():
    return typing.get_overloads(target.add)[0](1, 2)
""",
        {
            ("overload_stub_execution", "typing.get_overloads",
             "invokes an overload stub to manufacture execution evidence"),
        },
    ),
    (
        """\
import dis
import pkg.target as target
def test_code():
    assert list(dis.get_instructions(target.add))
""",
        {
            ("target_code_inspection", "dis.get_instructions",
             "inspects a selected or target-derived code object"),
        },
    ),
    (
        """\
import pkg.target as target
def test_code():
    assert target.__dict__["add"].__code__.co_code
""",
        {
            ("target_code_inspection", "target.__code__",
             "inspects the selected target's code object"),
        },
    ),
    (
        """\
import ast
import inspect
import pkg.target as target
def test_parse():
    assert ast.parse(inspect.getsource(target.add))
""",
        {
            ("target_source_analysis", "ast.parse",
             "analyzes source text obtained from the selected target"),
            ("target_source_inspection", "inspect.getsource",
             "reads implementation source from a selected or target-derived value"),
        },
    ),
    (
        """\
import inspect
import pkg.target as target
def test_source():
    assert inspect.getsource(target.add)
""",
        {
            ("target_source_inspection", "inspect.getsource",
             "reads implementation source from a selected or target-derived value"),
        },
    ),
    (
        """\
from inspect import getsourcelines as lines
from pkg import target
def test_source():
    assert lines(target.add)
""",
        {
            ("target_source_inspection", "inspect.getsourcelines",
             "reads implementation source from a selected or target-derived value"),
        },
    ),
    (
        """\
import pkg.target as target
def test_loader():
    assert target.__loader__.get_source(target.__name__)
""",
        {
            ("target_source_inspection", "loader.get_source",
             "reads implementation source from the selected target"),
        },
    ),
    (
        """\
import pkg.target as target
def test_path():
    assert open(vars(target)["__file__"]).read()
""",
        {
            ("target_source_path", "target.__file__",
             "accesses the selected target module's source path"),
            ("target_source_read", "open",
             "reads the selected target's implementation file"),
        },
    ),
    (
        """\
import pkg.target as target
def test_source():
    assert target.__spec__.origin.endswith("target.py")
""",
        {
            ("target_source_path", "target.__spec__.origin",
             "accesses the selected target module's source path"),
        },
    ),
    (
        """\
from importlib import resources
def test_resource():
    assert resources.read_text("pkg", "target.py")
""",
        {
            ("target_source_read", "importlib.resources.read_text",
             "reads the selected target's implementation resource"),
        },
    ),
    (
        """\
import linecache
import pkg.target as target
def test_lines():
    assert linecache.getlines(target.__file__)
""",
        {
            ("target_source_path", "target.__file__",
             "accesses the selected target module's source path"),
            ("target_source_read", "linecache",
             "reads the selected target's implementation file"),
        },
    ),
    (
        """\
def test_path():
    return open("/workspace/pkg/target.py").read()
""",
        {
            ("target_source_read", "open",
             "reads the selected target's implementation file"),
        },
    ),
    (
        """\
from pathlib import Path
import pkg.target as target
def test_source():
    assert Path(target.__file__).read_text()
""",
        {
            ("target_source_path", "target.__file__",
             "accesses the selected target module's source path"),
            ("target_source_read", "path read",
             "reads the selected target's implementation file"),
        },
    ),
    (
        """\
import pkgutil
def test_resource():
    assert pkgutil.get_data("pkg", "target.py")
""",
        {
            ("target_source_read", "pkgutil.get_data",
             "reads the selected target's implementation resource"),
        },
    ),
]


@pytest.mark.parametrize(("source", "expected"), _FINDINGS, ids=[
    "+".join(sorted(operation for _, operation, _ in expected)) for _, expected in _FINDINGS])
def test_each_finding_names_its_rule_operation_and_diagnostic(
    source: str, expected: set, identity: TargetIdentity,
) -> None:
    """Each finding's rule, operation and diagnostic, as the policy states
    them, for every place the policy records a finding."""
    assert {(finding.rule_id, finding.operation, finding.diagnostic)
            for finding in analyze_python_generated_test_validity(source, identity)} == expected


@pytest.mark.parametrize("spelling", ["pkg/target.py", "./pkg/target.py", "pkg/../pkg/target.py"])
def test_a_read_of_the_target_by_a_path_relative_to_the_project_is_refused(identity, spelling):
    """A test runs in the project root, so a path relative to it that names
    the target file reads it as the absolute path does: refused, where it
    was a review flag only."""
    for read in (f'open("{spelling}").read()', f'Path("{spelling}").read_text()'):
        source = f"from pathlib import Path\n\ndef test_source():\n    assert {read}\n"
        assert "target_source_read" in rules(source, identity), read


def test_a_relative_read_of_another_file_stays_allowed(identity):
    source = (
        "from pathlib import Path\n\n"
        "def test_data():\n"
        '    assert Path("data/target.json").read_text()\n'
        '    assert open("pkg/other.py").read()\n'
        '    assert (Path(".") / "notes.txt").read_text()\n'
        '    assert open("target.py").read()\n')
    assert "target_source_read" not in rules(source, identity)
