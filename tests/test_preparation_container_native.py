"""Optional public factory/executor checks in an explicitly borrowed container."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

import langadapter as lb
from langadapter._owned_directory import OwnershipRefused
from langadapter.execution import container_path, executor_for


@pytest.fixture
def selected_container(tmp_path):
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    zone = os.environ.get("LANGADAPTER_TEST_CONTAINER_WORK_DIR")
    python = os.environ.get("LANGADAPTER_TEST_CONTAINER_HELPER_PYTHON")
    if not (os.environ.get("LANGADAPTER_TEST_PREPARED_CONTAINER") == "1" and container and zone and python):
        pytest.skip("Needs opt-in borrowed container, mapped work directory and selected container Python")
    host = Path(tempfile.mkdtemp(prefix="public-container-fixture-", dir=zone))
    source = host / "source"
    source.mkdir()
    (source / "target.py").write_bytes(b"value = 1\n")
    (source / "pytest.ini").write_bytes(b"[pytest]\n")
    (source / "resource.txt").write_bytes(b"original resource\n")
    execution = lb.ContainerExecution(container, str(Path(zone).absolute()),
        container_work_dir="/workzone", helper_python=python)
    root = container_path(execution, str(source))
    context = lb.ProjectContext(root, lb.Interpreter(path=python), execution=execution,
                                test_framework=lb.TestFrameworkConfig(path="pytest.ini"))
    return context, host, source, tmp_path


def test_real_public_container_factory_restores_inputs_and_keeps_durable_results(selected_container):
    context, host, source, receipts = selected_container
    prepared = lb.prepare_project(context, receipt_parent=str(receipts))
    old = None
    proof = host / "proof.json"
    try:
        with prepared.use() as scoped:
            old = executor_for(scoped)
            native = old.run([context.interpreter.path, "-I", "-"], cwd=scoped.root,
                input=("from pathlib import Path\nimport json,sys\n"
                       "root=Path.cwd()\nassert (root/'target.py').read_bytes()==b'value = 1\\n'\n"
                       "assert (root/'resource.txt').read_bytes()==b'original resource\\n'\n"
                       "(root/'target.py').unlink()\n(root/'resource.txt').write_bytes(b'changed')\n"
                       "(root/'pytest.ini').write_bytes(b'changed configuration')\n"
                       f"Path({container_path(context.execution, str(proof))!r}).write_text("
                       "json.dumps({'root':str(root),'value':0}),encoding='ascii')\n"
                       "sys.stdout.buffer.write(b'native\\r\\n')\n"
                       "sys.stderr.buffer.write(b'diagnostic\\n')\n"), timeout=15)
            assert native.returncode == 0 and native.stdout == "native\n"
            assert native.stderr == "diagnostic\n"
            assert json.loads(proof.read_text()) == {"root": scoped.root, "value": 0}
        with prepared.use() as scoped:
            with pytest.raises(OwnershipRefused):
                old.run([context.interpreter.path, "--version"], timeout=1)
            native = executor_for(scoped).run([context.interpreter.path, "-I", "-"], cwd=scoped.root,
                input=("from pathlib import Path\nroot=Path.cwd()\n"
                       "assert (root/'target.py').read_bytes()==b'value = 1\\n'\n"
                       "assert (root/'resource.txt').read_bytes()==b'original resource\\n'\n"
                       "assert (root/'pytest.ini').read_bytes()==b'[pytest]\\n'\nprint('restored')\n"),
                timeout=15)
            assert native.returncode == 0 and native.stdout == "restored\n"
        result = dict(json.loads(proof.read_text()))
        prepared.close()
        prepared.close()
        assert json.loads(proof.read_text()) == result
        assert (source / "target.py").read_bytes() == b"value = 1\n"
        assert (source / "resource.txt").read_bytes() == b"original resource\n"
        assert (source / "pytest.ini").read_bytes() == b"[pytest]\n"
    except BaseException as original:
        try:
            prepared.close()
        except BaseException as cleanup:
            if hasattr(original, "add_note"):
                original.add_note("Prepared container cleanup remains unresolved: " + str(cleanup))
        raise


def test_real_public_container_timeout_retains_original_error_and_recoverable_owner(selected_container):
    context, host, source, receipts = selected_container
    prepared = lb.prepare_project(context, receipt_parent=str(receipts))
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        with prepared.use() as scoped:
            executor_for(scoped).run([context.interpreter.path, "-I", "-u", "-c",
                "import os,time;os.write(1,b'waiting\\n');os.write(2,b'diagnostic\\n');time.sleep(60)"],
                cwd=scoped.root, timeout=1)
    assert caught.value._langadapter_prepared_project is prepared
    assert caught.value.output == b"waiting\n" and caught.value.stderr == b"diagnostic\n"
    assert Path(prepared._prepared_copy.owned.receipt).is_file()
    assert prepared._use_scope is not None and prepared._use_record is not None
    prepared._use_scope.require_quiescent()
    owner = caught.value._langadapter_command
    assert owner._closed and owner.returncode == -15
    assert owner.helper._langadapter_command_disposed is True
    prepared.close()
    assert not Path(prepared._prepared_copy.owned.receipt).exists()
    assert (source / "target.py").read_bytes() == b"value = 1\n"
    (host / "recovered.json").write_text(json.dumps({"native_exit": owner.returncode}), encoding="ascii")
