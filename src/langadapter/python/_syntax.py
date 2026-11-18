"""The calling process's Python parser, raising SyntaxError for every
rejected source (Python 3.10 and 3.11 raise ValueError for a NUL byte), and
the tests pytest collects from a parsed module."""
from __future__ import annotations

import ast
import warnings
from typing import Any, List, Optional, Tuple

_NUL = chr(0)
_NUL_MESSAGE = "source code string cannot contain null bytes"
#: The base classes whose subclasses unittest, and pytest through it,
#: collects whatever their names.
_TEST_CASES = frozenset({"TestCase", "IsolatedAsyncioTestCase"})
#: A pytest fixture's decorator, called or not.
_FIXTURES = (("pytest", "fixture"), ("fixture",))


def parse(source: str, filename: str = "<unknown>", mode: str = "exec") -> Any:
    """``ast.parse(source, filename, mode)``. A NUL byte raises SyntaxError."""
    if _NUL in source:
        raise SyntaxError(_NUL_MESSAGE)
    return ast.parse(source, filename, mode)


def parse_quietly(source: str) -> ast.Module:
    """``parse(source)`` without SyntaxWarning or DeprecationWarning reaching
    the caller's warning stream: an invalid escape gives the first from
    Python 3.12 and the second before it."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        warnings.simplefilter("ignore", DeprecationWarning)
        return parse(source)


def compile_module(source: str, filename: str) -> Any:
    """``compile(source, filename, "exec")``. A NUL byte raises SyntaxError."""
    if _NUL in source:
        raise SyntaxError(_NUL_MESSAGE)
    return compile(source, filename, "exec")


def _dotted(node: ast.AST) -> Tuple[str, ...]:
    """The dotted name ``node`` calls or names (``pytest.fixture(...)``
    gives ``("pytest", "fixture")``), else ()."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: List[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return (node.id, *reversed(parts)) if isinstance(node, ast.Name) else ()


def _test_function(node: ast.AST) -> bool:
    return (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test")
            and not any(_dotted(decorator) in _FIXTURES for decorator in node.decorator_list))


def _marked_no_test(node: ast.ClassDef) -> bool:
    """Whether the class body sets ``__test__ = False``, which pytest
    honors."""
    return any(isinstance(item, ast.Assign) and isinstance(item.value, ast.Constant)
               and item.value.value is False
               and any(isinstance(target, ast.Name) and target.id == "__test__"
                       for target in item.targets)
               for item in node.body)


def _test_class(node: ast.ClassDef) -> bool:
    if _marked_no_test(node):
        return False
    if any(_dotted(base)[-1:] and _dotted(base)[-1] in _TEST_CASES for base in node.bases):
        return True
    return node.name.startswith("Test") and not any(
        isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name in ("__init__", "__new__")
        for item in node.body)


def collected_tests(tree: ast.Module) -> List[Tuple[str, Optional[ast.ClassDef], ast.AST]]:
    """The tests pytest's default collection finds in a module, as ``(name,
    class, function)``: its ``test*`` functions (class None), and the
    ``test*`` methods (name ``Class.method``) of its classes named ``Test*``
    without an ``__init__`` or ``__new__`` or based on unittest's
    ``TestCase`` or ``IsolatedAsyncioTestCase``, a class setting
    ``__test__ = False`` none. A function marked as a pytest fixture is not
    a test. Every definition counts, a shadowed one too. The rule reads the
    module's own text only, so it does not count the methods a class
    inherits, tests imported into the module, ``Test*`` classes nested in
    another or a ``TestCase`` reached through another base."""
    tests: List[Tuple[str, Optional[ast.ClassDef], ast.AST]] = []
    for node in tree.body:
        if _test_function(node):
            tests.append((node.name, None, node))
        elif isinstance(node, ast.ClassDef) and _test_class(node):
            tests.extend((f"{node.name}.{item.name}", node, item)
                         for item in node.body if _test_function(item))
    return tests
