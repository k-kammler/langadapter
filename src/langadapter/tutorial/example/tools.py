"""The Toy's own tools: what node, pytest or javac are for a real language.

A Toy source file holds one function per line::

    double(x) = x * 2
    sign(x) = if x > 0 then 1 else if x < 0 then -1 else 0

and a Toy test file one test per line::

    test doubles: double(2) == 4

Blank lines and lines that start with ``#`` are ignored. A test file line
that is neither a test nor ignored makes loading fail at that line.

The tools run a test file against a source file and report each test's
outcome, the lines the run evaluated and the branches it took (a
conditional's ``then`` part is branch 1, its ``else`` part branch 2), and
make mutants: each ``+``, ``-`` and ``*`` of a source line, swapped for
another operator, one mutant at a time.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

FUNCTION = re.compile(r"^(?P<name>[a-z_][a-z0-9_]*)\((?P<params>[^)]*)\)\s*=\s*(?P<body>.+)$")
TEST = re.compile(r"^test (?P<name>[A-Za-z_][A-Za-z0-9_]*):\s*(?P<left>.+?)\s*==\s*(?P<right>.+)$")
#: Each mutation operator: what it swaps an operator for.
SWAPS = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Add}
SYMBOL = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*"}


class LoadError(Exception):
    """A file that cannot be loaded, at a line, and with ``file`` the file's
    name."""

    def __init__(self, line: int, message: str, file: str = "") -> None:
        super().__init__(f"{file}:{line} does not load: {message}" if file
                         else f"line {line}: {message}")
        self.line = line
        self.message = message
        self.file = file


@dataclass(frozen=True)
class Function:
    name: str
    line: int
    params: Tuple[str, ...]
    body: ast.expr


@dataclass(frozen=True)
class Test:
    name: str
    line: int
    left: ast.expr
    right: ast.expr


@dataclass
class Trace:
    """The source lines a run evaluated and the branches it took."""

    lines: Set[int] = field(default_factory=set)
    branches: Set[Tuple[int, int]] = field(default_factory=set)


def _expression(text: str, line: int) -> ast.expr:
    # "if c then a else b", which runs to the end of its line, is Python's
    # "(a if c else b)". The last one is translated first, so that one in
    # an else part ends up inside the one around it.
    python = text
    while True:
        match = re.search(r"^(.*)\bif\s+(.+?)\s+then\s+(.+?)\s+else\s+(.+)$", python)
        if not match:
            break
        python = (match.group(1)
                  + f"({match.group(3)} if {match.group(2)} else {match.group(4)})")
    try:
        return ast.parse(python, mode="eval").body
    except (SyntaxError, ValueError) as exc:
        raise LoadError(line, f"not a Toy expression: {text}") from exc


def _lines(text: str):
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if line and not line.startswith("#"):
            yield number, line


def load_source(text: str) -> Dict[str, Function]:
    """The functions of a source file. LoadError names a line that is not
    a function."""
    functions: Dict[str, Function] = {}
    for number, line in _lines(text):
        match = FUNCTION.match(line)
        if match is None:
            raise LoadError(number, f"not a function: {line}")
        params = tuple(p.strip() for p in match["params"].split(",") if p.strip())
        functions[match["name"]] = Function(match["name"], number, params,
                                            _expression(match["body"], number))
    return functions


def load_tests(text: str) -> List[Test]:
    """The tests of a test file. LoadError names a line that is not a test."""
    tests = []
    for number, line in _lines(text):
        match = TEST.match(line)
        if match is None:
            raise LoadError(number, f"not a test: {line}")
        tests.append(Test(match["name"], number, _expression(match["left"], number),
                          _expression(match["right"], number)))
    return tests


class _Evaluator:
    def __init__(self, functions: Dict[str, Function], trace: Trace) -> None:
        self.functions = functions
        self.trace = trace

    def value(self, node: ast.expr, scope: Dict[str, int], line: int, depth: int = 0) -> int:
        if depth > 200:
            raise RecursionError("the Toy calls nest too deep")
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.Name):
            if node.id not in scope:
                raise NameError(f"no value named {node.id}")
            return scope[node.id]
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self.value(node.operand, scope, line, depth)
        if isinstance(node, ast.BinOp) and type(node.op) in (ast.Add, ast.Sub, ast.Mult,
                                                             ast.FloorDiv):
            left = self.value(node.left, scope, line, depth)
            right = self.value(node.right, scope, line, depth)
            # Only the node's own operation is computed: a division by zero
            # is an error of a division, never of a sum with a 0.
            op = type(node.op)
            if op is ast.FloorDiv:
                return left // right if right else _divide_by_zero()
            return {ast.Add: left + right, ast.Sub: left - right, ast.Mult: left * right}[op]
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left = self.value(node.left, scope, line, depth)
            right = self.value(node.comparators[0], scope, line, depth)
            op = type(node.ops[0])
            return int({ast.Lt: left < right, ast.Gt: left > right, ast.LtE: left <= right,
                        ast.GtE: left >= right, ast.Eq: left == right,
                        ast.NotEq: left != right}[op])
        if isinstance(node, ast.IfExp):
            taken = 1 if self.value(node.test, scope, line, depth) else 2
            self.trace.branches.add((line, taken))
            return self.value(node.body if taken == 1 else node.orelse, scope, line, depth)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            function = self.functions.get(node.func.id)
            if function is None:
                raise NameError(f"no function named {node.func.id}")
            args = [self.value(a, scope, line, depth) for a in node.args]
            if len(args) != len(function.params):
                raise TypeError(f"{function.name} takes {len(function.params)} arguments")
            self.trace.lines.add(function.line)
            return self.value(function.body, dict(zip(function.params, args)), function.line,
                              depth + 1)
        raise ValueError(f"not Toy arithmetic: {ast.unparse(node)}")


def _divide_by_zero() -> int:
    raise ZeroDivisionError("division by zero")


def run(tests: Sequence[Test], functions: Dict[str, Function]
        ) -> Tuple[List[Tuple[Test, str, str]], Trace]:
    """Each test's outcome (passed, failed or error) and message, and the
    trace of the run."""
    trace = Trace()
    evaluator = _Evaluator(functions, trace)
    results = []
    for test in tests:
        try:
            left = evaluator.value(test.left, {}, test.line)
            right = evaluator.value(test.right, {}, test.line)
        except (ArithmeticError, LookupError, NameError, TypeError, ValueError,
                RecursionError) as exc:
            results.append((test, "error", str(exc)))
            continue
        results.append((test, "passed", "") if left == right
                       else (test, "failed", f"{left} != {right}"))
    return results, trace


def branches(functions: Dict[str, Function]) -> List[Tuple[int, int]]:
    """Every branch of the source: two for each conditional."""
    found = []
    for function in functions.values():
        for node in ast.walk(function.body):
            if isinstance(node, ast.IfExp):
                found += [(function.line, 1), (function.line, 2)]
    return sorted(set(found))


@dataclass(frozen=True)
class Mutant:
    """One mutant: its id, the line and column of the operator, the change
    as text, and the functions with the change made."""

    id: str
    line: int
    column: int
    change: str
    functions: Dict[str, Function] = field(compare=False, repr=False)


class _Swap(ast.NodeTransformer):
    def __init__(self, target: int) -> None:
        self.target = target
        self.seen = -1

    def visit_BinOp(self, node: ast.BinOp) -> ast.AST:
        self.generic_visit(node)
        if type(node.op) in SWAPS:
            self.seen += 1
            if self.seen == self.target:
                return ast.BinOp(node.left, SWAPS[type(node.op)](), node.right)
        return node


def mutants(functions: Dict[str, Function]) -> List[Mutant]:
    """Every mutant of the source: each swappable operator, swapped."""
    made: List[Mutant] = []
    for function in sorted(functions.values(), key=lambda f: f.line):
        operators = [n for n in ast.walk(function.body)
                     if isinstance(n, ast.BinOp) and type(n.op) in SWAPS]
        for index, node in enumerate(operators):
            swap = _Swap(index)
            changed = swap.visit(ast.parse(ast.unparse(function.body), mode="eval")).body
            before = SYMBOL[type(node.op)]
            after = SYMBOL[SWAPS[type(node.op)]]
            mutated = dict(functions)
            mutated[function.name] = Function(function.name, function.line, function.params,
                                              changed)
            made.append(Mutant(f"{function.line}.{index + 1}", function.line,
                               node.col_offset, f"line {function.line}: {before} to {after} "
                               f"in {function.name}", mutated))
    return made


def mutant_status(mutant: Mutant, tests: Sequence[Test],
                  covered: Optional[Set[int]] = None) -> Tuple[str, List[str]]:
    """``killed`` with the tests that fail under the mutant, ``survived``,
    or ``no coverage`` when no test reaches its line."""
    if covered is not None and mutant.line not in covered:
        return "no coverage", []
    results, _ = run(tests, mutant.functions)
    killers = [test.name for test, outcome, _ in results if outcome != "passed"]
    return ("killed", killers) if killers else ("survived", [])
