"""Every helper the Java adapter compiles targets the profile's helper
release."""
import ast
from pathlib import Path

import langadapter
from langadapter.java._profile import JvmProfile

_ROOT = Path(langadapter.__file__).parent
_HELPER = {"str(JavaProfile.helper_release)", "str(JvmProfile.helper_release)"}


def _compiles():
    """javac's ``--release`` argument in every argv literal, and the test
    release of every project copy made with ``replace``, by file."""
    releases, copies = [], []
    for path in sorted((_ROOT / "java").glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.List, ast.Tuple)):
                for first, second in zip(node.elts, node.elts[1:]):
                    if isinstance(first, ast.Constant) and first.value == "--release":
                        releases.append((path.name, ast.unparse(second)))
            elif isinstance(node, ast.Call) and getattr(node.func, "id", None) == "replace":
                copies.extend((path.name, ast.unparse(keyword.value))
                              for keyword in node.keywords if keyword.arg == "test_release")
    return releases, copies


def test_every_helper_compile_names_helper_release():
    """Helper javac calls name helper_release, and the warmed-timing helper
    gets it as its test release. Only the project's own tests compile at
    the project's release."""
    releases, copies = _compiles()
    helpers = [entry for entry in releases if entry[0] != "_build.py"]
    assert helpers and all(release in _HELPER for _, release in helpers), helpers
    assert [entry for entry in releases if entry[0] == "_build.py"] == [
        ("_build.py", "str(project.release_of_tests())")]
    assert copies == [("_warmed_fork.py", "JvmProfile.helper_release")]
    assert JvmProfile.helper_release == 11
