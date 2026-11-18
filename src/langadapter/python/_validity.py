"""Validity analysis of generated Python tests: policy violations and review flags."""

from __future__ import annotations

import ast
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from langadapter.sources import (
    SuiteAuditFlag, SuiteValidityFinding, TargetIdentity,
)
from langadapter.python import _syntax


#: The policy's version. A test may read the target's namespace (``__dict__``,
#: ``vars``) as runtime metadata, but not its source text, bytecode or copied
#: source, also when reached through the namespace. Every statement the test
#: module runs is read: class bodies, decorators and defaults, ``match``
#: cases, ``except*`` handlers and lambda bodies, and a relative import names
#: the target when its dotted path ends the target's module name.
POLICY_VERSION = "python-generated-test-validity-v4"
#: The ``try`` statements: ``except*`` (``ast.TryStar``) from Python 3.11.
_TRY_KINDS = (ast.Try, getattr(ast, "TryStar", ast.Try))


def _parameters(arguments: ast.arguments) -> List[str]:
    """The names a function's or a lambda's ``arguments`` bind."""
    return [argument.arg for argument in (
        *arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs,
        arguments.vararg, arguments.kwarg) if argument is not None]


_SOURCE_APIS = {
    "inspect.getsource",
    "inspect.getsourcelines",
    "inspect.findsource",
    "inspect.getsourcefile",
    "inspect.getfile",
}
_IMPORT_APIS = {
    "importlib.import_module",
    "importlib.util.find_spec",
}
_READ_METHODS = {"read_text", "read_bytes", "open"}
_READ_APIS = {"open", "io.open"}
_HASH_OR_PARSE_APIS = {
    "ast.parse",
    "hashlib.md5",
    "hashlib.sha1",
    "hashlib.sha224",
    "hashlib.sha256",
    "hashlib.sha384",
    "hashlib.sha512",
    "tokenize.generate_tokens",
    "tokenize.tokenize",
}
_CODE_APIS = {
    "dis.Bytecode",
    "dis.code_info",
    "dis.dis",
    "dis.get_instructions",
    "dis.show_code",
}
_OVERLOAD_APIS = {"typing.get_overloads"}
_BUILTINS = {"compile", "exec", "eval", "getattr", "vars", "open"}


@dataclass(frozen=True)
class _Value:
    tags: FrozenSet[str] = frozenset()
    literals: FrozenSet[str] = frozenset()

    def with_tags(self, *tags: str) -> "_Value":
        return _Value(self.tags.union(tags), self.literals)

    @staticmethod
    def merge(values: Iterable["_Value"]) -> "_Value":
        tags: Set[str] = set()
        literals: Set[str] = set()
        for value in values:
            tags.update(value.tags)
            literals.update(value.literals)
        return _Value(frozenset(tags), frozenset(literals))


_UNKNOWN = _Value()


def _norm_path(value: str) -> str:
    return os.path.normcase(os.path.normpath(value.replace("\\", os.sep)))


def _norm_source(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return textwrap.dedent(value).strip("\n")


def _node_span(node: ast.AST) -> Tuple[int, int, int, int]:
    return (
        int(getattr(node, "lineno", 0) or 0),
        int(getattr(node, "col_offset", 0) or 0),
        int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
        int(getattr(node, "end_col_offset", getattr(node, "col_offset", 0)) or 0),
    )


class _Analyzer:
    def __init__(self, source: str, identity: TargetIdentity) -> None:
        self.source = source
        self.identity = identity
        self.tree = _syntax.parse_quietly(source)
        self.tests = {id(function): name
                      for name, _, function in _syntax.collected_tests(self.tree)}
        self.parents: Dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self.tree):
            for child in ast.iter_child_nodes(parent):
                self.parents[child] = parent
        self.findings: List[SuiteValidityFinding] = []
        self._finding_keys: Set[Tuple[str, int, int, Optional[str]]] = set()
        self.functions: Dict[str, ast.AST] = {}
        self.function_keys: Dict[int, str] = {}
        self._active_calls: Set[Tuple[str, Tuple[Tuple[str, ...], ...]]] = set()
        self.target_module = identity.module_name
        self.target_path = _norm_path(identity.source_path)
        self.project_root = identity.project_root
        self.target_basename = Path(identity.source_path).name
        self.target_package = identity.module_name.rpartition(".")[0]
        self.target_source_norm = _norm_source(identity.target_source)
        self.target_definition_sources = self._target_definition_sources(identity.target_source)
        self.globals: Dict[str, _Value] = {}
        self._register_functions(self.tree.body, prefix="")

    @staticmethod
    def _target_definition_sources(source: str) -> FrozenSet[str]:
        if not source:
            return frozenset()
        try:
            tree = _syntax.parse_quietly(source)
        except SyntaxError:
            return frozenset()
        values: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                segment = ast.get_source_segment(source, node)
                if segment:
                    values.add(_norm_source(segment))
        return frozenset(values)

    def _register_functions(self, body: Sequence[ast.stmt], *, prefix: str) -> None:
        del prefix  # Keys use source coordinates and are scope-independent.
        module = ast.Module(body=list(body), type_ignores=[])
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            key = f"{node.name}@{node.lineno}:{node.col_offset}"
            self.functions[key] = node
            self.function_keys[id(node)] = key

    def _test_id(self, node: ast.AST) -> Optional[str]:
        """The test (``_syntax.collected_tests``) whose definition holds
        ``node``, else None."""
        current: Optional[ast.AST] = node
        while current is not None:
            if id(current) in self.tests:
                return self.tests[id(current)]
            current = self.parents.get(current)
        return None

    def _add(self, node: ast.AST, rule: str, operation: str, diagnostic: str) -> None:
        test_id = self._test_id(node)
        start_line, start_col, end_line, end_col = _node_span(node)
        key = (rule, start_line, start_col, test_id)
        if key in self._finding_keys:
            return
        self._finding_keys.add(key)
        self.findings.append(SuiteValidityFinding(
            rule_id=rule,
            policy_version=self.identity.policy_version,
            operation=operation,
            diagnostic=diagnostic,
            start_line=start_line,
            start_col=start_col,
            end_line=end_line,
            end_col=end_col,
            enclosing_test=test_id,
        ))

    def _literal_value(self, value: object) -> _Value:
        if not isinstance(value, str):
            return _UNKNOWN
        tags: Set[str] = set()
        if value == self.target_module:
            tags.add("target_module_name")
        # A test runs in the project root, so a path relative to it names a
        # file as its absolute path does.
        if self.target_path in (_norm_path(value),
                                _norm_path(os.path.join(self.project_root, value))):
            tags.add("target_path")
        return _Value(frozenset(tags), frozenset({value}))

    def _module_value(self, module: str) -> _Value:
        tags = {f"module:{module}"}
        if module == self.target_module:
            tags.add("target_module")
        elif self.target_module.startswith(f"{module}."):
            tags.add(f"target_module_prefix:{module}")
        return _Value(frozenset(tags))

    @staticmethod
    def _api_names(value: _Value) -> Set[str]:
        names = {tag[4:] for tag in value.tags if tag.startswith("api:")}
        names.update(
            name.removeprefix("builtins.")
            for name in tuple(names)
            if name.startswith("builtins.")
            and name.removeprefix("builtins.") in _BUILTINS
        )
        return names

    @staticmethod
    def _module_names(value: _Value) -> Set[str]:
        return {tag[7:] for tag in value.tags if tag.startswith("module:")}

    @staticmethod
    def _has(value: _Value, *tags: str) -> bool:
        return any(tag in value.tags for tag in tags)

    def _attribute(self, node: ast.AST, base: _Value, attr: str) -> _Value:
        values: List[_Value] = []
        for module in self._module_names(base):
            qualified = f"{module}.{attr}"
            values.append(self._module_value(qualified))
            values.append(_Value(frozenset({f"api:{qualified}"})))
        for api in self._api_names(base):
            values.append(_Value(frozenset({f"api:{api}.{attr}"})))

        if "target_module" in base.tags or "target_symbol" in base.tags:
            if attr == "__file__":
                self._add(
                    node, "target_source_path", "target.__file__",
                    "accesses the selected target module's source path",
                )
                values.append(_Value(frozenset({"target_path"})))
            elif attr == "__spec__":
                values.append(_Value(frozenset({"target_spec"})))
            elif attr == "__loader__":
                values.append(_Value(frozenset({"target_loader"})))
            elif attr == "__name__":
                values.append(_Value(
                    frozenset({"target_module_name"}),
                    frozenset({self.target_module}),
                ))
            elif attr == "__code__":
                self._add(
                    node, "target_code_inspection", "target.__code__",
                    "inspects the selected target's code object",
                )
                values.append(_Value(frozenset({"target_code"})))
            elif attr == "__dict__":
                # Runtime metadata: a read from it is judged as the attribute
                # it names (the subscript below).
                values.append(_Value(frozenset({"target_namespace"})))
            else:
                values.append(_Value(frozenset({"target_symbol"})))
        if "target_spec" in base.tags and attr == "origin":
            self._add(
                node, "target_source_path", "target.__spec__.origin",
                "accesses the selected target module's source path",
            )
            values.append(_Value(frozenset({"target_path"})))
        if "target_spec" in base.tags and attr == "loader":
            values.append(_Value(frozenset({"target_loader"})))
        if "target_path" in base.tags and attr in {"parent", "parents"}:
            values.append(_Value(frozenset({"target_path_ancestor"})))
        if ("target_path" in base.tags or "target_path_ancestor" in base.tags) and attr in _READ_METHODS:
            values.append(_Value(frozenset({f"bound_read:{attr}"})))
        if "target_package_path" in base.tags and attr in {"joinpath", "__truediv__"}:
            values.append(_Value(frozenset({f"target_resource_join:{attr}"})))
        if "target_loader" in base.tags and attr == "get_source":
            values.append(_Value(frozenset({"api:target_loader.get_source"})))
        if "overload_registry_entry" in base.tags and attr in {"values", "items"}:
            values.append(_Value(frozenset({"bound_overload_registry_values"})))
        if "unknown_target" in base.tags:
            if attr in {"__file__", "__code__", "__loader__", "__spec__"}:
                self._add(
                    node, "ambiguous_target_attribute", f"getattr(...).{attr}",
                    "an unresolved target attribute flows into an implementation access",
                )
            values.append(_Value(frozenset({"unknown_target"})))
        return _Value.merge(values) if values else _UNKNOWN

    def _assign(self, target: ast.AST, value: _Value, env: Dict[str, _Value]) -> None:
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._assign(item, value, env)

    def _eval(self, node: Optional[ast.AST], env: Dict[str, _Value]) -> _Value:
        if node is None:
            return _UNKNOWN
        if isinstance(node, ast.Constant):
            return self._literal_value(node.value)
        if isinstance(node, ast.Name):
            if node.id in _BUILTINS:
                return _Value(frozenset({f"api:{node.id}"}))
            return env.get(node.id, self.globals.get(node.id, _UNKNOWN))
        if isinstance(node, ast.Attribute):
            return self._attribute(node, self._eval(node.value, env), node.attr)
        if isinstance(node, ast.Subscript):
            base = self._eval(node.value, env)
            index = self._eval(node.slice, env)
            if ("typing._overload_registry" in self._api_names(base)
                    and self._has(index, "target_symbol", "target_module")):
                return _Value(frozenset({"overload_registry_entry"}))
            if "overload_registry_entry" in base.tags:
                return _Value(frozenset({"overload_collection"}))
            if "overload_collection" in base.tags:
                return _Value(frozenset({"overload_stub"}))
            if (any(name == "sys.modules" for name in self._api_names(base))
                    and "target_module_name" in index.tags):
                return _Value(frozenset({"target_module"}))
            if "target_namespace" in base.tags:
                names = sorted(index.literals)
                if names:
                    return _Value.merge(
                        self._attribute(node, _Value(frozenset({"target_symbol"})), name)
                        for name in names)
                return _Value(frozenset({"target_symbol"}))
            return _UNKNOWN
        if isinstance(node, ast.Call):
            return self._call(node, env)
        if isinstance(node, ast.BinOp):
            left = self._eval(node.left, env)
            right = self._eval(node.right, env)
            merged = _Value.merge((left, right))
            if isinstance(node.op, (ast.Add, ast.Div)):
                if self._has(merged, "target_path", "target_path_ancestor"):
                    return merged.with_tags("target_path")
                if "target_source" in merged.tags:
                    return merged.with_tags("target_source")
            return merged
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return _Value.merge(self._eval(item, env) for item in node.elts)
        if isinstance(node, ast.Dict):
            return _Value.merge(
                self._eval(item, env) for item in (*node.keys, *node.values)
                if item is not None
            )
        if isinstance(node, ast.IfExp):
            self._eval(node.test, env)
            return _Value.merge((self._eval(node.body, env), self._eval(node.orelse, env)))
        if isinstance(node, ast.JoinedStr):
            return _Value.merge(
                self._eval(item.value, env)
                for item in node.values if isinstance(item, ast.FormattedValue)
            )
        if isinstance(node, ast.UnaryOp):
            return self._eval(node.operand, env)
        if isinstance(node, ast.BoolOp):
            return _Value.merge(self._eval(item, env) for item in node.values)
        if isinstance(node, ast.Compare):
            return _Value.merge(
                [self._eval(node.left, env)]
                + [self._eval(item, env) for item in node.comparators]
            )
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            local = dict(env)
            for generator in node.generators:
                item = self._eval(generator.iter, local)
                if "overload_collection" in item.tags:
                    item = _Value(frozenset({"overload_stub"}))
                self._assign(generator.target, item, local)
                for condition in generator.ifs:
                    self._eval(condition, local)
            return self._eval(node.elt, local)
        if isinstance(node, ast.Lambda):
            for default in (*node.args.defaults, *node.args.kw_defaults):
                self._eval(default, env)
            local = {**env, **dict.fromkeys(_parameters(node.args), _UNKNOWN)}
            self._eval(node.body, local)
            return _UNKNOWN
        return _Value.merge(self._eval(child, env) for child in ast.iter_child_nodes(node))

    def _call(self, node: ast.Call, env: Dict[str, _Value]) -> _Value:
        func = self._eval(node.func, env)
        args = [self._eval(arg, env) for arg in node.args]
        kwargs = {kw.arg: self._eval(kw.value, env) for kw in node.keywords if kw.arg}
        apis = self._api_names(func)

        def argument(index: int, *names: str) -> _Value:
            if index < len(args):
                return args[index]
            for name in names:
                if name in kwargs:
                    return kwargs[name]
            return _UNKNOWN

        for tag in func.tags:
            if tag.startswith("local_func:"):
                return self._call_local(tag[11:], args, kwargs)
        if "overload_stub" in func.tags:
            self._add(
                node, "overload_stub_execution", "typing.get_overloads",
                "invokes an overload stub to manufacture execution evidence",
            )
            return _UNKNOWN
        if "bound_overload_registry_values" in func.tags:
            return _Value(frozenset({"overload_collection"}))

        if apis & _SOURCE_APIS:
            subject = argument(0, "object")
            if self._has(
                subject, "target_module", "target_symbol", "target_code",
                "unknown_target",
            ):
                api = sorted(apis & _SOURCE_APIS)[0]
                self._add(
                    node,
                    (
                        "ambiguous_target_attribute"
                        if "unknown_target" in subject.tags
                        else "target_source_inspection"
                    ),
                    api,
                    "reads implementation source from a selected or target-derived value",
                )
                return _Value(frozenset({"target_source"}))
        if apis & _IMPORT_APIS:
            subject = argument(0, "name")
            if "target_module_name" in subject.tags:
                if "importlib.util.find_spec" in apis:
                    return _Value(frozenset({"target_spec"}))
                return _Value(frozenset({"target_module"}))
        if "pkgutil.get_loader" in apis:
            subject = argument(0, "fullname")
            if "target_module_name" in subject.tags or "target_module" in subject.tags:
                return _Value(frozenset({"target_loader"}))
        if "pkgutil.get_data" in apis:
            module_value = argument(0, "package")
            resource_value = argument(1, "resource")
            if (
                self.target_package in module_value.literals
                and self.target_basename in resource_value.literals
            ):
                self._add(
                    node, "target_source_read", "pkgutil.get_data",
                    "reads the selected target's implementation resource",
                )
                return _Value(frozenset({"target_source"}))
        if "importlib.resources.files" in apis:
            subject = argument(0, "anchor", "package")
            if self._has(subject, "target_module_name", "target_module"):
                return _Value(frozenset({"target_path"}))
            if self.target_package in subject.literals:
                return _Value(frozenset({"target_package_path"}))
        if apis & {
            "importlib.resources.open_binary",
            "importlib.resources.open_text",
            "importlib.resources.read_binary",
            "importlib.resources.read_text",
        }:
            module_value = argument(0, "package")
            resource_value = argument(1, "resource")
            if (
                self.target_package in module_value.literals
                and self.target_basename in resource_value.literals
            ):
                api = sorted(apis & {
                    "importlib.resources.open_binary",
                    "importlib.resources.open_text",
                    "importlib.resources.read_binary",
                    "importlib.resources.read_text",
                })[0]
                self._add(
                    node, "target_source_read", api,
                    "reads the selected target's implementation resource",
                )
                return _Value(frozenset({"target_source"}))
        if any(tag.startswith("target_resource_join:") for tag in func.tags):
            resource_value = argument(0, "pathsegments")
            if self.target_basename in resource_value.literals:
                return _Value(frozenset({"target_path"}))
        if "target_loader.get_source" in apis:
            subject = argument(0, "fullname")
            if "target_module_name" in subject.tags:
                self._add(
                    node, "target_source_inspection", "loader.get_source",
                    "reads implementation source from the selected target",
                )
                return _Value(frozenset({"target_source"}))

        if apis & _READ_APIS:
            subject = argument(0, "file")
            if self._has(subject, "target_path", "target_path_ancestor"):
                self._add(
                    node, "target_source_read", sorted(apis & _READ_APIS)[0],
                    "reads the selected target's implementation file",
                )
                return _Value(frozenset({"target_source"}))
        if any(tag.startswith("bound_read:") for tag in func.tags):
            self._add(
                node, "target_source_read", "path read",
                "reads the selected target's implementation file",
            )
            return _Value(frozenset({"target_source"}))
        if "linecache.getlines" in apis or "linecache.getline" in apis:
            subject = argument(0, "filename")
            if self._has(subject, "target_path", "target_path_ancestor"):
                self._add(
                    node, "target_source_read", "linecache",
                    "reads the selected target's implementation file",
                )
                return _Value(frozenset({"target_source"}))

        if apis & _HASH_OR_PARSE_APIS:
            subject = argument(0, "source", "data", "string")
            if "target_source" in subject.tags:
                api = sorted(apis & _HASH_OR_PARSE_APIS)[0]
                self._add(
                    node, "target_source_analysis", api,
                    "analyzes source text obtained from the selected target",
                )
        if apis & _CODE_APIS:
            subject = argument(0, "x", "object")
            if self._has(subject, "target_symbol", "target_code", "unknown_target"):
                api = sorted(apis & _CODE_APIS)[0]
                self._add(
                    node,
                    (
                        "ambiguous_target_attribute"
                        if "unknown_target" in subject.tags
                        else "target_code_inspection"
                    ),
                    api,
                    "inspects a selected or target-derived code object",
                )
        if apis & _OVERLOAD_APIS:
            subject = argument(0, "func")
            if "target_symbol" in subject.tags:
                return _Value(frozenset({"overload_collection"}))

        if "getattr" in apis:
            subject = argument(0, "object")
            name_value = argument(1, "name")
            if self._has(subject, "target_module", "target_symbol"):
                names = sorted(name_value.literals)
                if not names:
                    return _Value(frozenset({"unknown_target"}))
                return _Value.merge(self._attribute(node, subject, name) for name in names)
        if "operator.attrgetter" in apis:
            names = sorted(argument(0).literals)
            if names:
                return _Value(frozenset({f"attrgetter:{name}" for name in names}))
        for tag in func.tags:
            if tag.startswith("attrgetter:") and args:
                return self._attribute(node, args[0], tag[11:])
        if "vars" in apis and self._has(argument(0, "object"), "target_module", "target_symbol"):
            return _Value(frozenset({"target_namespace"}))

        if "pathlib.Path" in apis or "pathlib.PurePath" in apis:
            return argument(0)
        if "sys.modules.get" in apis:
            subject = argument(0, "key")
            if "target_module_name" in subject.tags:
                return _Value(frozenset({"target_module"}))
        if "compile" in apis:
            source_value = argument(0, "source")
            filename_value = argument(1, "filename")
            if "target_source" in source_value.tags:
                self._add(
                    node, "copied_target_execution", "compile",
                    "compiles implementation source obtained from the selected target",
                )
            for literal in source_value.literals:
                normalized = _norm_source(literal)
                if normalized and (
                    normalized == self.target_source_norm
                    or normalized in self.target_definition_sources
                ):
                    self._add(
                        node, "copied_target_execution", "compile",
                        "compiles a copied implementation span from the selected target",
                    )
            if self._has(filename_value, "target_path", "target_path_ancestor"):
                self._add(
                    node, "synthetic_target_attribution", "compile filename",
                    "attributes synthetic code to the selected target's source path",
                )
            return _Value(frozenset({"target_code"}) if (
                "target_source" in source_value.tags
                or self._has(filename_value, "target_path", "target_path_ancestor")
            ) else frozenset())
        if apis & {"exec", "eval"}:
            subject = argument(0, "source", "object")
            if self._has(subject, "target_source", "target_code"):
                self._add(
                    node, "copied_target_execution", sorted(apis & {"exec", "eval"})[0],
                    "executes or evaluates copied implementation code from the selected target",
                )
            for literal in subject.literals:
                normalized = _norm_source(literal)
                if normalized and (
                    normalized == self.target_source_norm
                    or normalized in self.target_definition_sources
                ):
                    self._add(
                        node, "copied_target_execution", sorted(apis & {"exec", "eval"})[0],
                        "executes a copied implementation span from the selected target",
                    )
        return _UNKNOWN

    def _call_local(
        self, key: str, args: Sequence[_Value], kwargs: Mapping[str, _Value],
    ) -> _Value:
        node = self.functions.get(key)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _UNKNOWN
        signature = tuple(tuple(sorted(value.tags)) for value in args)
        active_key = (key, signature)
        if active_key in self._active_calls:
            return _UNKNOWN
        self._active_calls.add(active_key)
        try:
            env = dict(self.globals)
            parameters = list(node.args.posonlyargs) + list(node.args.args)
            for index, parameter in enumerate(parameters):
                if index < len(args):
                    env[parameter.arg] = args[index]
                elif parameter.arg in kwargs:
                    env[parameter.arg] = kwargs[parameter.arg]
                else:
                    env[parameter.arg] = _UNKNOWN
            if node.args.vararg:
                env[node.args.vararg.arg] = _Value.merge(args[len(parameters):])
            returns: List[_Value] = []
            self._exec(node.body, env, returns)
            return _Value.merge(returns)
        finally:
            self._active_calls.remove(active_key)

    def _import(self, node: ast.stmt, env: Dict[str, _Value]) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                module = alias.name if alias.asname else alias.name.split(".")[0]
                env[local] = self._module_value(module)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                qualified = f"{module}.{alias.name}".strip(".")
                if self._names_target(qualified, node.level):
                    env[local] = _Value(frozenset({"target_module"}))
                elif self._names_target(module, node.level):
                    env[local] = _Value(frozenset({"target_symbol"}))
                elif not node.level and self.target_module.startswith(f"{qualified}."):
                    env[local] = self._module_value(qualified)
                else:
                    env[local] = _Value(frozenset({f"api:{qualified}"}))

    def _names_target(self, name: str, level: int) -> bool:
        """Whether an import of ``name`` names the target module: by its
        whole name, or, for a relative import (``level`` dots, the test
        module's own package unknown), as the end of it."""
        return name == self.target_module or (
            level > 0 and bool(name) and self.target_module.endswith("." + name))

    def _branches(self, branches: Sequence[Sequence[ast.stmt]], env: Dict[str, _Value],
                  returns: List[_Value]) -> None:
        """Run each of ``branches`` on a copy of ``env`` and merge what they
        bind into it."""
        envs = []
        for branch in branches:
            local = dict(env)
            self._exec(branch, local, returns)
            envs.append(local)
        for name in set().union(*(item.keys() for item in envs)):
            env[name] = _Value.merge(item.get(name, _UNKNOWN) for item in envs)

    def _exec(
        self, body: Sequence[ast.stmt], env: Dict[str, _Value],
        returns: Optional[List[_Value]] = None,
    ) -> None:
        returns = returns if returns is not None else []
        for stmt in body:
            if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                self._import(stmt, env)
            elif isinstance(stmt, ast.Assign):
                value = self._eval(stmt.value, env)
                for target in stmt.targets:
                    self._assign(target, value, env)
            elif isinstance(stmt, ast.AnnAssign):
                self._assign(stmt.target, self._eval(stmt.value, env), env)
                self._eval(stmt.annotation, env)
            elif isinstance(stmt, ast.AugAssign):
                value = _Value.merge((self._eval(stmt.target, env), self._eval(stmt.value, env)))
                self._assign(stmt.target, value, env)
            elif isinstance(stmt, ast.Expr):
                self._eval(stmt.value, env)
            elif isinstance(stmt, ast.Assert):
                self._eval(stmt.test, env)
                self._eval(stmt.msg, env)
            elif isinstance(stmt, ast.Return):
                returns.append(self._eval(stmt.value, env))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = self.function_keys[id(stmt)]
                env[stmt.name] = _Value(frozenset({f"local_func:{key}"}))
                for decorator in stmt.decorator_list:
                    self._eval(decorator, env)
                for default in (*stmt.args.defaults, *stmt.args.kw_defaults):
                    self._eval(default, env)
            elif isinstance(stmt, ast.ClassDef):
                for expression in (*stmt.decorator_list, *stmt.bases,
                                   *(keyword.value for keyword in stmt.keywords)):
                    self._eval(expression, env)
                self._exec(stmt.body, dict(env), returns)
            elif isinstance(stmt, ast.If):
                self._eval(stmt.test, env)
                self._branches([stmt.body, stmt.orelse], env, returns)
            elif isinstance(stmt, ast.Match):
                self._eval(stmt.subject, env)
                for case in stmt.cases:
                    self._eval(case.guard, env)
                # A subject no case matches runs none of them.
                self._branches([*(case.body for case in stmt.cases), []], env, returns)
            elif isinstance(stmt, (ast.For, ast.AsyncFor)):
                item = self._eval(stmt.iter, env)
                if "overload_collection" in item.tags:
                    item = _Value(frozenset({"overload_stub"}))
                local = dict(env)
                self._assign(stmt.target, item, local)
                self._exec(stmt.body, local, returns)
                self._exec(stmt.orelse, local, returns)
                env.update(local)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                local = dict(env)
                for item in stmt.items:
                    value = self._eval(item.context_expr, local)
                    if item.optional_vars is not None:
                        self._assign(item.optional_vars, value, local)
                self._exec(stmt.body, local, returns)
                env.update(local)
            elif isinstance(stmt, _TRY_KINDS):
                self._branches([stmt.body, stmt.orelse, stmt.finalbody,
                                *(handler.body for handler in stmt.handlers)], env, returns)
            elif isinstance(stmt, ast.While):
                self._eval(stmt.test, env)
                self._exec(stmt.body, env, returns)
                self._exec(stmt.orelse, env, returns)
            elif isinstance(stmt, (ast.Raise, ast.Delete)):
                for child in ast.iter_child_nodes(stmt):
                    self._eval(child, env)

    def run(self) -> Tuple[SuiteValidityFinding, ...]:
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                key = self.function_keys[id(node)]
                self.globals[node.name] = _Value(frozenset({f"local_func:{key}"}))
        # The whole module: its definitions' decorators, defaults and class
        # bodies run when it is imported.
        self._exec(self.tree.body, self.globals)
        for node in self.functions.values():
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            env = {**self.globals, **dict.fromkeys(_parameters(node.args), _UNKNOWN)}
            self._exec(node.body, env, [])
        return tuple(sorted(
            self.findings,
            key=lambda item: (item.start_line, item.start_col, item.rule_id),
        ))


def analyze_python_generated_test_validity(
    source: str, identity: TargetIdentity,
) -> Tuple[SuiteValidityFinding, ...]:
    """The source's policy violations, deterministic and in source order."""
    return _Analyzer(source, identity).run()


def screen_python_generated_test_source(
    source: str, identity: TargetIdentity,
) -> Tuple[SuiteAuditFlag, ...]:
    """The heuristic review flags: literals naming the target, file discovery,
    runtime source rendering, reads of a ``.py`` path and execution of a long
    source literal. Unparseable source gives one ``unparseable_source`` flag."""
    try:
        tree = _syntax.parse_quietly(source)
    except SyntaxError as exc:
        return (SuiteAuditFlag(
            rule_id="unparseable_source",
            diagnostic=str(exc),
            start_line=int(exc.lineno or 0),
            start_col=int(exc.offset or 0),
            end_line=int(exc.lineno or 0),
            end_col=int(exc.offset or 0),
        ),)

    flags: List[SuiteAuditFlag] = []
    seen: Set[Tuple[str, int, int]] = set()
    target_needles = {
        identity.module_name,
        Path(identity.source_path).name,
        identity.source_path,
    }
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    # The target's module name, or a dotted path below it, as the first argument
    # of an import or patch call is how a test imports or patches it: no flag.
    imports = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        called = _call_name(node)
        first = node.args[0]
        if (called.split(".")[-1] in _IMPORT_PATH_CALLS
                or called.endswith(_IMPORT_PATH_SUFFIXES)) and (
                isinstance(first, ast.Constant) and isinstance(first.value, str)
                and (first.value == identity.module_name
                     or first.value.startswith(identity.module_name + "."))):
            imports.add(first)

    def add(node: ast.AST, rule: str, diagnostic: str) -> None:
        start_line, start_col, end_line, end_col = _node_span(node)
        key = (rule, start_line, start_col)
        if key in seen:
            return
        seen.add(key)
        flags.append(SuiteAuditFlag(
            rule_id=rule,
            diagnostic=diagnostic,
            start_line=start_line,
            start_col=start_col,
            end_line=end_line,
            end_col=end_col,
        ))

    for node in ast.walk(tree):
        # A standalone string expression (a merged suite's repeated module
        # docstring) is inert and shows no source access.
        if (
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            and not isinstance(parents.get(node), ast.Expr)
            and node not in imports
            and any(needle and needle in node.value for needle in target_needles)
        ):
            add(node, "target_literal", "literal names the selected target")
        if not isinstance(node, ast.Call):
            continue
        name = _call_name(node)
        if name in {"glob.glob", "glob.iglob", "os.walk", "os.listdir"} or name.endswith(".rglob"):
            add(node, "source_discovery", "discovers files or directories")
        if name.startswith("traceback.format_") or name.startswith("linecache."):
            add(node, "runtime_source_rendering", "may expose source text at runtime")
        if name in {"open", "io.open"} or name.endswith((".read_text", ".read_bytes", ".open")):
            for arg in node.args[:1]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.endswith(".py"):
                    add(node, "python_source_read", "reads a Python source path")
        if name in {"compile", "exec", "eval"} and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and len(arg.value) >= 200:
                add(node, "sizable_executable_literal", "executes a sizable source literal")
    return tuple(sorted(flags, key=lambda item: (item.start_line, item.start_col, item.rule_id)))


#: Calls whose first argument is an import path: an import, a patch.
_IMPORT_PATH_CALLS = frozenset({"import_module", "__import__", "patch"})
_IMPORT_PATH_SUFFIXES = ("patch.dict", "monkeypatch.setattr", "monkeypatch.delattr")


def _call_name(node: ast.Call) -> str:
    """A call's dotted name as written (``mock.patch``), or empty."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if not isinstance(node.func, ast.Attribute):
        return ""
    parts = [node.func.attr]
    base = node.func.value
    while isinstance(base, ast.Attribute):
        parts.append(base.attr)
        base = base.value
    if isinstance(base, ast.Name):
        parts.append(base.id)
    return ".".join(reversed(parts))
