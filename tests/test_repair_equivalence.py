"""The repair loops on broken inputs give the recorded results exactly.

Load repair, syntax repair and import pruning for Python and Java, with
scripted collection, compilation, import probe, run and validator results:
the statuses, removed labels and their order, messages, round limits and
sources of 122 cases. repair_equivalence.txt holds the expected outputs, one
line per case, recorded with Python 3.12. The two cases whose text comes
from the interpreter take it from the running Python.
"""
import json
import subprocess
import sys
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

import langadapter as lb

EXPECTED = dict(line.split(" ", 1) for line in
                (Path(__file__).with_name("repair_equivalence.txt")
                 .read_text(encoding="utf-8").splitlines()))


def _outputs(project):
    out = {}

    def emit(name, value):
        out[name] = json.dumps(value, sort_keys=True, default=str)

    # ---------------------------------------------------------------- Python ----
    from langadapter.python.adapter import PythonAdapter

    adapter = PythonAdapter(lb.ProjectContext(root=str(project),
                                              interpreter=lb.Interpreter(path=sys.executable)))
    original = adapter._executor()

    PY_SOURCE = ("import pytest\n"                      # 1
                 "\n"                                   # 2
                 "A = 1 / 0\n"                          # 3
                 "B = {}['x']\n"                        # 4
                 "C = int('z')\n"                       # 5
                 "D = [][1]\n"                          # 6
                 "E = None.x\n"                         # 7
                 "\n"                                   # 8
                 "\n"                                   # 9
                 "class TestK:\n"                       # 10
                 "    F = 1 / 0\n"                      # 11
                 "\n"                                   # 12
                 "    def test_k(self):\n"              # 13
                 "        assert 1\n"                   # 14
                 "\n"                                   # 15
                 "\n"                                   # 16
                 "def test_a():\n"                      # 17
                 "    assert 1\n")                      # 18


    def fail_at(line):
        return ("collect", 2, f"ERROR collecting candidate.py\ncandidate.py:{line}: in <module>\n")


    SCRIPTS = {
        "py_one_removal": [fail_at(3), ("collect", 0, "")],
        "py_two_removals_in_order": [fail_at(4), fail_at(3), ("collect", 0, "")],
        "py_class_member": [fail_at(11), ("collect", 0, "")],
        "py_no_location_first": [("collect", 2, "ERROR collecting candidate.py\nsomething\n")],
        "py_no_location_round3": [fail_at(3), fail_at(4),
                                  ("collect", 2, "ERROR collecting candidate.py\nnothing\n")],
        "py_not_blankable_round1": [fail_at(999)],
        "py_not_blankable_round3": [fail_at(3), fail_at(4), fail_at(999)],
        "py_not_blankable_round5": [fail_at(3), fail_at(4), fail_at(5), fail_at(6), fail_at(999)],
        "py_exhausted": [fail_at(3), fail_at(4), fail_at(5), fail_at(6), fail_at(7)],
        "py_exhausted_comment_line": [fail_at(3), fail_at(4), fail_at(5), fail_at(6), fail_at(2)],
        "py_loads_at_round5": [fail_at(3), fail_at(4), fail_at(5), fail_at(6), ("collect", 0, "")],
        "py_timeout_round2": [fail_at(3), ("timeout",)],
        "py_crash_round2": [fail_at(3), ("collect", -11, "Segmentation fault")],
        "py_no_tests_round1": [("collect", 5, "no tests ran")],
    }


    class Scripted:
        def __init__(self, script):
            self.script, self.calls = list(script), 0

        def run(self, command, **kwargs):
            if "--collect-only" not in command:
                return original.run(command, **kwargs)
            self.calls += 1
            if not self.script:
                raise AssertionError("more collections than scripted")
            step = self.script.pop(0)
            if step[0] == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))
            return CompletedProcess(command, step[1], step[2], "")


    for name, script in SCRIPTS.items():
        executor = Scripted(script)
        adapter._executor = lambda e=executor: e
        report = {}
        fixed, removed = adapter._prune_collection_breakers(PY_SOURCE, str(project), timeout=60,
                                                        report=report)
        raw = {"fixed_is_input": fixed == PY_SOURCE, "fixed": fixed, "removed": removed,
               "report": report, "collections": executor.calls,
               "report_keys": list(report)}
        executor2 = Scripted(SCRIPTS[name])
        adapter._executor = lambda e=executor2: e
        result = adapter.prune_collection_breakers(PY_SOURCE, timeout_s=60)
        raw["public"] = {"status": result.status, "detail": result.detail,
                         "removed": list(result.removed), "same": result.source == PY_SOURCE,
                         "source": result.source}
        emit(name, raw)
    adapter._executor = lambda: original

    # Python salvage_syntax through the module with scripted validators.
    from langadapter.python.source_analysis import salvage_syntax
    from langadapter.sources import SourceValidation


    def verdicts(*items):
        queue = list(items)

        def validate(text):
            status, message, line = queue.pop(0) if queue else ("syntax_error", "again", 1)
            return SourceValidation(status, message, line, 0, "compiler")
        return validate


    PY_SALVAGE = {
        "pys_indent_then_valid": ("def f():\n\nx = 1\n",
                                  [("syntax_error", "expected an indented block after function "
                                    "definition on line 1", 3), ("valid", "", 0)]),
        "pys_opener_beyond": ("x\n", [("syntax_error", "expected an indented block after if "
                                       "statement on line 9", 1)]),
        "pys_line_zero": ("x\n", [("syntax_error", "bad", 0)]),
        "pys_line_beyond_round3": ("a\nb\nc\n", [("syntax_error", "bad", 1), ("syntax_error", "bad", 2),
                                                 ("syntax_error", "bad", 7)]),
        "pys_exhausted": ("a\n" * 40, []),
        "pys_exhausted_distinct": ("a\n" * 40, [("syntax_error", "bad", n) for n in range(1, 41)]),
        "pys_validator_round2": ("a\nb\n", [("syntax_error", "bad", 1), ("timeout", "slow", 0)]),
        "pys_crlf_blank": ("a\r\nb\r\n", [("syntax_error", "bad", 2), ("valid", "", 0)]),
    }
    for name, (source, script) in PY_SALVAGE.items():
        result = salvage_syntax(source, verdicts(*script))
        emit(name, {"type": type(result).__module__ + "." + type(result).__name__,
                    "source": result.source, "status": result.status, "repairs": result.repairs,
                    "validation": [result.validation.status, result.validation.message,
                                   result.validation.line]})

    # ------------------------------------------------------------------ Java ----
    from langadapter.java import _sources as js

    JAVA = ("package demo;\n"                                   # 1
            "\n"                                                # 2
            "import org.junit.jupiter.api.Test;\n"              # 3
            "import org.example.Missing;\n"                     # 4
            "\n"                                                # 5
            "class CalcTest {\n"                                # 6
            "    static int broken = Missing.value();\n"        # 7
            "    int a = 1, b = 2;\n"                           # 8
            "\n"                                                # 9
            "    @Test\n"                                       # 10
            "    void one() {\n"                                # 11
            "        undefined();\n"                            # 12
            "    }\n"                                           # 13
            "\n"                                                # 14
            "    @Test\n"                                       # 15
            "    void two() {\n"                                # 16
            "        alsoUndefined();\n"                        # 17
            "    }\n"                                           # 18
            "\n"                                                # 19
            "    void helper(int x) { nope(); }\n"              # 20
            "\n"                                                # 21
            "    static class Inner {\n"                        # 22
            "        void deep() { worse(); }\n"                # 23
            "    }\n"                                           # 24
            "\n"                                                # 25
            "    @Test\n"                                       # 26
            "    void three() {\n"                              # 27
            "    }\n"                                           # 28
            "}\n")                                              # 29


    def _marker_lines(text, markers):
        """The 1-based lines of ``text`` holding each marker (an int is a line)."""
        lines = text.splitlines()
        found = []
        for marker in markers:
            if isinstance(marker, int):
                found.append(marker)
                continue
            found += [n for n, line in enumerate(lines, 1) if marker in line][:1]
        return found


    def java_ops(script, runs=()):
        ops = object.__new__(js.JavaSources)
        state = {"compile": list(script), "runs": list(runs), "compilations": 0, "run_checks": 0}

        def compilation(text):
            state["compilations"] += 1
            step = state["compile"].pop(0) if state["compile"] else (False, [7], "again")
            if step is None:
                return None
            compiles, markers, diagnostics = step
            return compiles, _marker_lines(text, markers), diagnostics

        def run_check(text, timeout_s):
            state["run_checks"] += 1
            return state["runs"].pop(0) if state["runs"] else ("runs", "", [])

        ops.__dict__["_refusal"] = lambda source, label: None
        ops.__dict__["_compilation"] = compilation
        ops.__dict__["_run_check"] = run_check
        return ops, state


    JAVA_CASES = {
        "jv_one_method": [(False, ["undefined();"], "CalcTest.java:12: error"), (True, [], "")],
        "jv_import_and_field": [(False, ["import org.example", "static int broken"], "errs"),
                                (True, [], "")],
        "jv_two_rounds": [(False, ["undefined();"], "e1"), (False, ["alsoUndefined"], "e2"),
                          (True, [], "")],
        "jv_nested_member": [(False, ["worse"], "e"), (True, [], "")],
        "jv_header_line": [(False, ["class CalcTest"], "header")],
        "jv_package_line_round3": [(False, ["undefined();"], "e1"), (False, ["nope"], "e2"),
                                   (False, ["package demo"], "pkg")],
        "jv_no_lines": [(False, [], "no lines")],
        "jv_javac_cannot_run_round2": [(False, ["undefined();"], "e1"), None],
        "jv_exhausted_mappable": [(False, ["undefined();"], "r1"), (False, ["alsoUndefined"], "r2"),
                                  (False, ["nope"], "r3"), (False, ["worse"], "r4"),
                                  (False, ["int a = 1"], "r5"), (False, ["static int broken"], "r6")],
        "jv_exhausted_unmappable_last": [(False, ["undefined();"], "r1"),
                                         (False, ["alsoUndefined"], "r2"), (False, ["nope"], "r3"),
                                         (False, ["worse"], "r4"), (False, ["int a = 1"], "r5"),
                                         (False, ["package demo"], "r6")],
        "jv_loads_at_round6": [(False, ["undefined();"], "r1"), (False, ["alsoUndefined"], "r2"),
                               (False, ["nope"], "r3"), (False, ["worse"], "r4"),
                               (False, ["int a = 1"], "r5"), (True, [], "")],
        "jv_no_test_left": [(False, ["undefined();", "alsoUndefined", "void three"], "all tests")],
        "jv_no_test_left_round2": [(False, ["undefined();"], "a"),
                                   (False, ["alsoUndefined", "void three"], "b")],
    }
    for name, script in JAVA_CASES.items():
        ops, state = java_ops(script)
        result = js.JavaSources.prune_collection_breakers(ops, JAVA, timeout_s=10)
        emit(name, {"status": result.status, "detail": result.detail, "removed": list(result.removed),
                    "same": result.source == JAVA, "source": result.source,
                    "compilations": state["compilations"], "run_checks": state["run_checks"]})

    # A class that compiles and whose static initialization could throw: the run check.
    JAVA_STATIC = JAVA.replace("    static int broken = Missing.value();\n",
                               "    static int broken = compute();\n"
                               "    static int compute() { return 1 / 0; }\n")
    RUN_CASES = {
        "jv_static_init_fails_then_runs": ([(True, [], ""), (True, [], "")],
                                           [("failed", "init fails", [7]), ("runs", "", [])]),
        "jv_static_init_fails_header": ([(True, [], "")], [("failed", "init fails", [6])]),
        "jv_static_run_infrastructure": ([(True, [], "")], [("infrastructure", "timeout", [])]),
        "jv_static_collection_failed": ([(True, [], "")], [("failed", "does not compile", [])]),
    }
    for name, (script, runs) in RUN_CASES.items():
        ops, state = java_ops(script, runs)
        result = js.JavaSources.prune_collection_breakers(ops, JAVA_STATIC, timeout_s=10)
        emit(name, {"status": result.status, "detail": result.detail, "removed": list(result.removed),
                    "same": result.source == JAVA_STATIC, "source": result.source,
                    "compilations": state["compilations"], "run_checks": state["run_checks"]})

    # Java salvage_syntax with a scripted validator.
    JAVA_SALVAGE = {
        "jvs_two_blanks": ("a\nb\nc\n", [("syntax_error", "bad", 1), ("syntax_error", "bad", 3),
                                        ("valid", "", 0)]),
        "jvs_braces": ("class A {\n  void f() {", [("syntax_error", "reached end of file while "
                                                    "parsing", 2)] * 2 + [("valid", "", 0)]),
        "jvs_blank_line_error": ("a\n\nc\n", [("syntax_error", "bad", 1), ("syntax_error", "bad", 2)]),
        "jvs_line_beyond": ("a\n", [("syntax_error", "bad", 5)]),
        "jvs_line_none": ("a\n", [("syntax_error", "bad", None)]),
        "jvs_exhausted": ("a\n" * 40, [("syntax_error", "bad", n) for n in range(1, 41)]),
        "jvs_beyond_round7": ("a\n" * 40, [("syntax_error", "bad", n) for n in range(1, 7)]
                              + [("syntax_error", "bad", 99)]),
        "jvs_exhausted_eof": ("class A {", [("syntax_error", "reached end of file", 1)] * 30),
        "jvs_validator": ("a\nb\n", [("syntax_error", "bad", 1), ("infrastructure_failure", "no jvm", 0)]),
        "jvs_no_newline_last": ("a\nb", [("syntax_error", "bad", 2), ("valid", "", 0)]),
        "jvs_crlf": ("a\r\nb\r\nc", [("syntax_error", "bad", 2), ("syntax_error", "bad", 3),
                                     ("valid", "", 0)]),
    }
    for name, (source, script) in JAVA_SALVAGE.items():
        queue = list(script)
        ops = object.__new__(js.JavaSources)

        def validate(text, q=queue):
            status, message, line = q.pop(0) if q else ("syntax_error", "again", 1)
            return SimpleNamespace(valid=status == "valid", status=status, message=message, line=line)

        ops.__dict__["validate_source"] = validate
        result = js.JavaSources.salvage_syntax(ops, source)
        emit(name, {"status": result.status, "detail": result.detail, "source": result.source,
                    "removed": list(result.removed), "parser": result.parser})

    return out


def import_cases(project):
    out = {}

    def emit(name, value):
        out[name] = json.dumps(value, sort_keys=True, default=str)

    from langadapter.python.adapter import PythonAdapter

    adapter = PythonAdapter(lb.ProjectContext(root=str(project),
                                              interpreter=lb.Interpreter(path=sys.executable)))
    original = adapter._executor()

    source = ("import os\n"
              "import missingmod, json\n"
              "from os import path, nothere\n"
              "from os import (\n"
              "    sep as s,\n"
              "    gone as g,\n"
              ")\n"
              "from missing2 import x\n"
              "from . import rel\n"
              "from os import *\n"
              "\n"
              "def test_a():\n"
              "    assert os\n")

    def answer(modules, names):
        return json.dumps({"modules": modules, "names": names})

    all_ok = answer({"os": True, "missingmod": True, "json": True},
                    {"os:path": True, "os:nothere": True, "os:sep": True, "os:gone": True,
                     "missing2:x": True})
    some_bad = answer({"os": True, "missingmod": False, "json": True},
                      {"os:path": True, "os:nothere": False, "os:sep": True, "os:gone": False,
                       "missing2:x": False})
    all_bad = answer({"os": False, "missingmod": False, "json": False},
                     {"os:path": False, "os:nothere": False, "os:sep": False, "os:gone": False,
                      "missing2:x": False})

    class Scripted:
        def __init__(self, stdout, fail=False):
            self.stdout, self.fail, self.calls = stdout, fail, 0

        def run(self, command, **kwargs):
            if "-c" not in command or "find_spec" not in " ".join(map(str, command)):
                return original.run(command, **kwargs)
            self.calls += 1
            if self.fail:
                raise OSError("the interpreter could not start")
            return CompletedProcess(command, 0, self.stdout + "\n", "")

    cases = {
        "pyi_all_resolve": (source, Scripted(all_ok)),
        "pyi_some_fail": (source, Scripted(some_bad)),
        "pyi_all_fail": (source, Scripted(all_bad)),
        "pyi_probe_fails": (source, Scripted("", fail=True)),
        "pyi_probe_garbage": (source, Scripted("not json")),
        "pyi_parse_error": ("import os\ndef f(:\n", Scripted(all_ok)),
        "pyi_nothing_to_check": ("from . import rel\nfrom os import *\nx = 1\n", Scripted(all_ok)),
        "pyi_crlf": (source.replace("\n", "\r\n"), Scripted(some_bad)),
    }
    for name, (text, executor) in cases.items():
        adapter._executor = lambda e=executor: e
        report = {}
        pruned, dropped = adapter._prune_unresolvable_imports(text, str(project), report=report)
        raw = {"pruned": pruned, "dropped": list(dropped), "report": report,
               "probes": executor.calls}
        executor.calls = 0
        result = adapter.prune_unresolvable_imports(text)
        raw["public"] = {"status": result.status, "detail": result.detail,
                         "removed": list(result.removed), "source": result.source,
                         "probes": executor.calls}
        emit(name, raw)
    adapter._executor = lambda: original

    from langadapter.java import _sources as js

    java = ("package demo;\n"                           # 1
            "\n"                                        # 2
            "import org.junit.jupiter.api.Test;\n"      # 3
            "import org.example.Missing;\n"             # 4
            "import static org.example.Gone.value;\n"   # 5
            "import org.example.Other;\n"               # 6
            "import org.example.Fourth;\n"              # 7
            "import org.example.Fifth;\n"               # 8
            "import org.example.Sixth;\n"               # 9
            "\n"                                        # 10
            "class CalcTest {\n"                        # 11
            "    @Test\n"                               # 12
            "    void one() { undefined(); }\n"         # 13
            "}\n")                                      # 14

    def marker_lines(text, markers):
        lines = text.splitlines()
        found = []
        for marker in markers:
            if isinstance(marker, int):
                found.append(marker)
                continue
            found += [n for n, line in enumerate(lines, 1) if marker in line][:1]
        return found

    def java_ops(script):
        ops = object.__new__(js.JavaSources)
        state = {"script": list(script), "compiles": 0}

        def compile_errors(text):
            state["compiles"] += 1
            step = state["script"].pop(0) if state["script"] else ["undefined"]
            return None if step is None else marker_lines(text, step)

        ops.__dict__["_refusal"] = lambda source, label: None
        ops.__dict__["_compile_errors"] = compile_errors
        return ops, state

    java_cases = {
        "jvi_one_round": [["Missing;", "undefined"], ["undefined"]],
        "jvi_two_rounds": [["Missing;"], ["Gone.value"], ["undefined"]],
        "jvi_no_import_errors": [["undefined", 2]],
        "jvi_no_errors": [[]],
        "jvi_duplicate_lines": [["Missing;", "Missing;", "Other;"], []],
        "jvi_javac_cannot_run": [None],
        "jvi_javac_cannot_run_round2": [["Missing;"], None],
        "jvi_five_rounds": [["Missing;"], ["Gone.value"], ["Other;"], ["Fourth;"], ["Fifth;"],
                            ["Sixth;"]],
        "jvi_all_at_once": [["Test;", "Missing;", "Gone.value", "Other;", "Fourth;", "Fifth;",
                             "Sixth;"], ["undefined"]],
    }
    for name, script in java_cases.items():
        ops, state = java_ops(script)
        result = js.JavaSources.prune_unresolvable_imports(ops, java)
        emit(name, {"status": result.status, "detail": result.detail,
                    "removed": list(result.removed), "source": result.source,
                    "compiles": state["compiles"]})
    return out


def review_cases(project):
    """Python and Java import pruning on broken inputs, and Java load repair
    beside a top-level method."""
    from langadapter.java import _sources as js

    out = {}
    project.mkdir(parents=True, exist_ok=True)
    (project / "localpkg").mkdir(exist_ok=True)
    (project / "localpkg" / "__init__.py").write_text("HERE = 1\n", encoding="utf-8")
    (project / "localpkg" / "sub.py").write_text("X = 2\n", encoding="utf-8")
    (project / "boom.py").write_text("raise RuntimeError('import fails')\n", encoding="utf-8")


    def emit(name, value):
        out["r2_" + name] = json.dumps(value, sort_keys=True, default=str)


    from langadapter.python.adapter import PythonAdapter

    adapter = PythonAdapter(lb.ProjectContext(root=str(project),
                                              interpreter=lb.Interpreter(path=sys.executable)))
    original = adapter._executor()


    class Counting:
        def __init__(self, answer=None, exc=None):
            self.answer, self.exc, self.calls = answer, exc, 0

        def run(self, command, **kwargs):
            if "-c" in command and "find_spec" in " ".join(map(str, command)):
                self.calls += 1
                if self.exc is not None:
                    raise self.exc
                if self.answer is not None:
                    return CompletedProcess(command, 0, self.answer, "")
            return original.run(command, **kwargs)


    REAL = {
        "real_plain_missing": "import os\nimport missing_mod_xyz\n\ndef test_a():\n    assert os\n",
        "real_plain_multi": "import os, missing_mod_xyz, json as j\n\ndef test_a():\n    pass\n",
        "real_dotted": "import os.path\nimport os.nothere_xyz\nimport localpkg.sub\nimport localpkg.gone\n",
        "real_alias": "import missing_mod_xyz as m\nfrom os import path as p, nothere as n\n",
        "real_from_mixed": "from os import path, sep, nothere_xyz, getcwd\nx = 1\n",
        "real_from_all_bad": "from os import nothere1, nothere2\nfrom missing_mod_xyz import a, b\n",
        "real_from_submodule": "from localpkg import sub, HERE, gone\nfrom localpkg import *\n",
        "real_star_missing": "from missing_mod_xyz import *\nimport os\n",
        "real_paren_multiline": ("from os import (\n    path,  # keep\n    nothere_xyz,\n"
                                 "    sep as s,\n)\nimport json\n"),
        "real_same_line_code": "from os import nothere_xyz; x = 1\nimport missing_mod_xyz; y = 2\n",
        "real_duplicates": ("import missing_mod_xyz\nimport missing_mod_xyz\n"
                            "from os import nothere, nothere\n"),
        "real_future": FUTURE,
        "real_nested_not_top": ("import os\ntry:\n    import missing_a\nexcept ImportError:\n"
                                "    pass\nif True:\n    import missing_b\ndef f():\n"
                                "    import missing_c\n"),
        "real_no_trailing_newline": "import os\nfrom os import path, nothere_xyz",
        "real_crlf_mixed": ("import os\r\nfrom os import path, nothere_xyz\r\n"
                            "import missing_mod_xyz\r\nx = 1\r\n"),
        "real_import_raises": "import boom\nfrom boom import anything\nimport os\n",
        "real_relative_only": "from . import a\nfrom .. import b\nx = 1\n",
        "real_nothing": "x = 1\n",
        "real_parse_error": "import missing_mod_xyz\ndef f(:\n    pass\n",
        "real_parse_error_later": "import os\nfrom os import nothere\nclass A:\n  def f(self)\n",
        "real_decorated_and_errors": ("import pytest\nimport missing_mod_xyz\n"
                                      "from os import nothere_xyz\nA = 1 / 0\n\n"
                                      "@pytest.fixture\ndef fx():\n    return undefined_name\n"),
        "real_empty": "",
        "real_bom": "\ufeffimport missing_mod_xyz\nimport os\n",
        "real_unicode": "import os\nfrom os import nothere_\u00e9\n# \u00fc\n",
    }
    SCRIPTED = {
        "scr_exc_oserror": Counting(exc=OSError("cannot start")),
        "scr_timeout": Counting(exc=subprocess.TimeoutExpired(["python"], 120)),
        "scr_garbage": Counting(answer="hello\n"),
        "scr_empty_stdout": Counting(answer=""),
        "scr_json_dict_missing_keys": Counting(answer='{"modules": {}}\n'),
        "scr_json_scalar_int": Counting(answer="1\n"),
        "scr_json_scalar_str": Counting(answer='"x"\n'),
        "scr_json_pairs_list": Counting(answer='[["modules", {}], ["names", {}]]\n'),
        "scr_all_false": Counting(answer='{"modules": {}, "names": {}}\n'),
        "scr_extra_noise_before": Counting(answer='noise\n{"modules": {"os": true}, "names": {}}\n'),
    }
    SCRIPT_SOURCE = "import os\nimport missing_mod_xyz\nfrom os import path, nothere\n"

    for name, source in REAL.items():
        executor = Counting()
        adapter._executor = lambda e=executor: e
        report = {}
        try:
            pruned, dropped = adapter._prune_unresolvable_imports(source, str(project), report=report)
            raw = {"pruned": pruned, "dropped": list(dropped), "report": report,
                   "report_keys": list(report), "probes": executor.calls}
        except Exception as exc:
            raw = {"raised": f"{type(exc).__name__}: {exc}", "probes": executor.calls}
        executor.calls = 0
        try:
            result = adapter.prune_unresolvable_imports(source)
            raw["public"] = {"status": result.status, "detail": result.detail,
                             "removed": list(result.removed), "source": result.source,
                             "parser": result.parser, "probes": executor.calls}
        except Exception as exc:
            raw["public"] = {"raised": f"{type(exc).__name__}: {exc}"}
        emit(name, raw)

    for name, executor in SCRIPTED.items():
        adapter._executor = lambda e=executor: e
        report = {}
        try:
            pruned, dropped = adapter._prune_unresolvable_imports(SCRIPT_SOURCE, str(project),
                                                              report=report)
            raw = {"pruned": pruned, "dropped": list(dropped), "report": report,
                   "report_keys": list(report), "probes": executor.calls}
        except Exception as exc:
            raw = {"raised": f"{type(exc).__name__}: {exc}", "probes": executor.calls}
        try:
            result = adapter.prune_unresolvable_imports(SCRIPT_SOURCE)
            raw["public"] = {"status": result.status, "detail": result.detail,
                             "removed": list(result.removed), "source": result.source}
        except Exception as exc:
            raw["public"] = {"raised": f"{type(exc).__name__}: {exc}"}
        emit(name, raw)
    adapter._executor = lambda: original

    # ------------------------------------------------------------------ Java ----

    JAVA = ("package demo;\n"                                   # 1
            "\n"                                                # 2
            "import org.junit.jupiter.api.Test;\n"              # 3
            "import org.example.Missing;\n"                     # 4
            "import static org.example.Gone.value;\n"           # 5
            "import static org.example.Star.*;\n"               # 6
            "import org.wild.*;\n"                              # 7
            "import a.B; import c.D;\n"                         # 8
            "// a comment between imports\n"                    # 9
            "import org.example.Last;\n"                        # 10
            "\n"                                                # 11
            "class CalcTest {\n"                                # 12
            "    @Test\n"                                       # 13
            "    void one() { undefined(); }\n"                 # 14
            "}\n")                                              # 15


    def marker_lines(text, markers):
        lines = text.splitlines()
        found = []
        for marker in markers:
            if isinstance(marker, int):
                found.append(marker)
                continue
            found += [n for n, line in enumerate(lines, 1) if marker in line][:1]
        return found


    def java_ops(script, refusal=None):
        ops = object.__new__(js.JavaSources)
        state = {"script": list(script), "compiles": 0, "texts": []}

        def compile_errors(text):
            state["compiles"] += 1
            state["texts"].append(text)
            step = state["script"].pop(0) if state["script"] else ["undefined"]
            return None if step is None else marker_lines(text, step)

        ops.__dict__["_refusal"] = lambda source, label: refusal
        ops.__dict__["_compile_errors"] = compile_errors
        return ops, state


    JAVA_CASES = {
        "jv_none": (JAVA, [[]]),
        "jv_other_errors_only": (JAVA, [["undefined", "class CalcTest", 1]]),
        "jv_mixed_round1_then_other": (JAVA, [["Missing;", "undefined", "Gone.value"],
                                              ["undefined"]]),
        "jv_static_star_wild": (JAVA, [["Star.*", "org.wild"], []]),
        "jv_same_line_two_imports": (JAVA, [["import a.B"], []]),
        "jv_unsorted_duplicates": (JAVA, [[10, 4, 10, 4, 3], []]),
        "jv_rounds_1_to_5": (JAVA, [["Missing;"], ["Gone.value"], ["Star.*"], ["org.wild"],
                                    ["Last;"], ["Test;"]]),
        "jv_rounds_4_then_clean": (JAVA, [["Missing;"], ["Gone.value"], ["Star.*"], ["org.wild"],
                                          ["undefined"]]),
        "jv_javac_fails_round1": (JAVA, [None]),
        "jv_javac_fails_round3": (JAVA, [["Missing;"], ["Gone.value"], None]),
        "jv_javac_fails_round5": (JAVA, [["Missing;"], ["Gone.value"], ["Star.*"], ["org.wild"],
                                         None]),
        "jv_line_beyond": (JAVA, [[99, "Missing;"], []]),
        "jv_crlf": (JAVA.replace("\n", "\r\n"), [["Missing;", "Last;"], ["Gone.value"], []]),
        "jv_cr_only": (JAVA.replace("\n", "\r"), [[4, 10], []]),
        "jv_broken_tree": (JAVA.replace("class CalcTest {", "class CalcTest {{"),
                           [["Missing;", "undefined"], ["Gone.value"], []]),
        "jv_no_imports": ("package demo;\nclass A { void f() { x(); } }\n", [["x()"]]),
        "jv_all_imports_round1": (JAVA, [["Test;", "Missing;", "Gone.value", "Star.*", "org.wild",
                                          "import a.B", "Last;"], ["undefined"]]),
    }
    for name, (source, script) in JAVA_CASES.items():
        ops, state = java_ops(script)
        try:
            result = js.JavaSources.prune_unresolvable_imports(ops, source)
            emit(name, {"status": result.status, "detail": result.detail,
                        "removed": list(result.removed), "source": result.source,
                        "same": result.source == source, "parser": result.parser,
                        "compiles": state["compiles"], "texts": state["texts"]})
        except Exception as exc:
            emit(name, {"raised": f"{type(exc).__name__}: {exc}", "compiles": state["compiles"]})

    # A refusal is passed through.
    from langadapter.sources import SourceResult

    refused = SourceResult("refused", JAVA, "javac", detail="too large")
    ops, state = java_ops([[]], refusal=refused)
    result = js.JavaSources.prune_unresolvable_imports(ops, JAVA)
    emit("jv_refusal", {"same_object": result is refused, "compiles": state["compiles"]})
    SOURCE = ("package demo;\n"                 # 1
              "\n"                              # 2
              "import org.junit.jupiter.api.Test;\n"  # 3
              "\n"                              # 4
              "class CalcTest {\n"              # 5
              "    @Test\n"                     # 6
              "    void one() {\n"              # 7
              "    }\n"                         # 8
              "}\n"                             # 9
              "\n"                              # 10
              "void two() {\n"                  # 11
              "    int x = 1;\n"                # 12
              "}\n")                            # 13


    def ops_for(script):
        ops = object.__new__(js.JavaSources)
        state = {"script": list(script), "compilations": 0}

        def compilation(text):
            state["compilations"] += 1
            return state["script"].pop(0) if state["script"] else (True, [], "")

        ops.__dict__["_refusal"] = lambda source, label: None
        ops.__dict__["_compilation"] = compilation
        ops.__dict__["_run_check"] = lambda text, timeout_s: ("runs", "", [])
        return ops, state


    CASES = {
        "release17_lines_11_13": [(False, [11, 13], "CalcTest.java:11:1: compiler.err.expected4")],
        "release17_line_11_only": [(False, [11], "CalcTest.java:11:1: compiler.err.expected4")],
        "release21_lines_11_1": [(False, [11, 1], "preview disabled; package")],
    }
    for name, script in CASES.items():
        ops, state = ops_for(script)
        result = js.JavaSources.prune_collection_breakers(ops, SOURCE, timeout_s=10)
        out["r2_top_" + name] = json.dumps({"status": result.status, "detail": result.detail,
                                             "removed": list(result.removed),
                                             "same": result.source == SOURCE,
                                             "compilations": state["compilations"]}, sort_keys=True)
    return out


@pytest.fixture(scope="module")
def outputs(tmp_path_factory):
    found = _outputs(tmp_path_factory.mktemp("repair-equivalence"))
    found.update(import_cases(tmp_path_factory.mktemp("import-pruning")))
    found.update(review_cases(tmp_path_factory.mktemp("review-cases")))
    return found


#: r2_real_future's message holds the interpreter's own syntax error, whose
#: column differs between CPython patch releases (3.12.10, which recorded
#: the outputs, reports 37, and 3.12.3 reports 1).
FUTURE = "from __future__ import annotations, nothere_feature\nimport os\n"


def _expected(case):
    if case == "r2_scr_json_scalar_str":
        # The repair reports the interpreter's TypeError as it is, and Python
        # 3.11 added ", not 'str'" to it.
        text, key = "", "key"
        try:
            text[key]
        except TypeError as error:
            message = str(error)
        return EXPECTED[case].replace("string indices must be integers, not 'str'", message)
    if case != "r2_real_future":
        return EXPECTED[case]
    try:
        compile(FUTURE, "<case>", "exec")
    except SyntaxError as error:
        column = error.offset
    return EXPECTED[case].replace("(line 1, column 37)", f"(line 1, column {column})")


@pytest.mark.parametrize("case", sorted(EXPECTED))
def test_a_repair_gives_the_recorded_result(outputs, case):
    assert outputs[case] == _expected(case)


def test_every_case_is_compared(outputs):
    assert sorted(outputs) == sorted(EXPECTED) and len(EXPECTED) == 122
