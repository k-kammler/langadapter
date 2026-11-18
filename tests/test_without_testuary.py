"""The package imports no Testuary module: one instance per language and
configured project, without Testuary."""
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"


def test_importing_the_package_imports_no_testuary_module():
    probe = (
        "import sys; import langadapter; "
        "import langadapter.registry, langadapter.context; "
        "loaded = [m for m in sys.modules if m == 'testuary' "
        "or m.startswith('testuary.')]; "
        "print(','.join(loaded))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        env={"PYTHONPATH": str(SOURCE), "SYSTEMROOT": _systemroot()},
        timeout=60, check=True)
    assert completed.stdout.strip() == ""


def test_no_source_file_names_testuary():
    offenders = [
        str(path.relative_to(SOURCE)) for path in SOURCE.rglob("*.py")
        if "testuary" in path.read_text(encoding="utf-8").lower()
    ]
    assert offenders == []


def _systemroot() -> str:
    import os
    return os.environ.get("SYSTEMROOT", "")
