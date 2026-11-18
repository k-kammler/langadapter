"""Preparation budgets preserve partial ownership and ordinary defaults."""
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from langadapter import ProjectContext, ProjectInputSelection, prepare_project
from langadapter import _input_copy, _prepared_copy, preparation
from langadapter.errors import InvalidRequest


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(now=0.0)
    selected = SimpleNamespace(monotonic=lambda: state.now)
    monkeypatch.setattr(preparation, "time", selected)
    monkeypatch.setattr(_prepared_copy, "time", selected)
    return state


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "module.py").write_bytes(b"value = 1\n")
    return ProjectContext(str(root))


@pytest.mark.parametrize("timeout", [0, -1, True, False, "1", float("inf"), float("nan"),
                                     pytest.param(10**1000, id="unrepresentable")])
def test_invalid_timeout_is_refused_before_validation_or_allocation(monkeypatch, timeout):
    def refused(*args, **kwargs):
        pytest.fail("invalid preparation budget reached context validation")

    monkeypatch.setattr(ProjectContext, "validate", refused)
    with pytest.raises(InvalidRequest, match="timeout_s"):
        prepare_project(ProjectContext("unselected"), timeout_s=timeout)


@pytest.mark.parametrize("timeout", [None, 2, 2.5])
def test_optional_budget_keeps_default_bytes_and_close(project, tmp_path, clock, timeout):
    owner = prepare_project(project, parent=str(tmp_path), receipt_parent=str(tmp_path), timeout_s=timeout)
    root = Path(owner.root)
    assert (root / "module.py").read_bytes() == b"value = 1\n"
    assert owner.inputs is None
    owner.close()
    assert not root.exists()
    assert Path(project.root, "module.py").read_bytes() == b"value = 1\n"


@pytest.mark.parametrize("selected", [False, True])
def test_slow_preflight_times_out_without_allocating(project, tmp_path, monkeypatch, clock, selected):
    real = _prepared_copy._ordinary if not selected else _input_copy.ordinary
    Path(project.root, "other.py").write_bytes(b"other = 2\n")
    files = []

    def slow(path):
        result = real(path)
        if Path(path).suffix == ".py":
            files.append(Path(path).name)
            clock.now = 3
        return result

    monkeypatch.setattr(_prepared_copy if not selected else _input_copy,
                        "_ordinary" if not selected else "ordinary", slow)
    before = tuple(sorted(path.name for path in tmp_path.iterdir()))
    with pytest.raises(subprocess.TimeoutExpired) as failed:
        prepare_project(project, parent=str(tmp_path), receipt_parent=str(tmp_path), timeout_s=2,
                        inputs=ProjectInputSelection(required=("module.py",)) if selected else None)
    assert not hasattr(failed.value, "_langadapter_prepared_project")
    assert not hasattr(failed.value, "_langadapter_prepared_copy")
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before
    assert len(files) == 1


@pytest.mark.parametrize("selected", [False, True])
def test_slow_copy_preserves_timeout_and_recoverable_actual_owner(project, tmp_path, monkeypatch, clock, selected):
    original = _prepared_copy.shutil.copy2
    copied = []

    def slow(src, dst, **kwargs):
        result = original(src, dst, **kwargs)
        copied.append((src, dst))
        clock.now = 3
        return result

    monkeypatch.setattr(_prepared_copy.shutil, "copy2", slow)
    with pytest.raises(subprocess.TimeoutExpired) as failed:
        prepare_project(project, parent=str(tmp_path), receipt_parent=str(tmp_path), timeout_s=2,
                        inputs=ProjectInputSelection(required=("module.py",)) if selected else None)
    error = failed.value
    owner = error._langadapter_prepared_project
    prepared_copy = owner._prepared_copy
    try:
        assert len(copied) == 1
        assert not prepared_copy.root.exists()
        assert error._langadapter_prepared_copy is prepared_copy
        prepared_copy.owned.verify()
        prepared_copy.baseline.verify()
        assert (prepared_copy.inputs / "module.py").read_bytes() == b"value = 1\n"
        assert Path(project.root, "module.py").read_bytes() == b"value = 1\n"
    finally:
        owner.close()
        owner.close()
    assert not prepared_copy.owned.root.exists()
    assert not prepared_copy.baseline.root.exists()


def test_timeout_after_first_allocation_preserves_the_partial_directory(project, tmp_path, monkeypatch, clock):
    original = _prepared_copy.OwnedDirectory.create.__func__

    def allocate(cls, **kwargs):
        owner = original(cls, **kwargs)
        clock.now = 3
        return owner

    monkeypatch.setattr(_prepared_copy.OwnedDirectory, "create", classmethod(allocate))
    with pytest.raises(subprocess.TimeoutExpired) as failed:
        prepare_project(project, parent=str(tmp_path), receipt_parent=str(tmp_path), timeout_s=2)
    error = failed.value
    directories = error._langadapter_owned_directories
    assert len(directories) == 1
    directories[0].verify()
    assert error._langadapter_prepared_project is not None
    error._langadapter_prepared_project.close()
    assert not directories[0].root.exists()


def test_factory_completion_cannot_return_a_spent_owner(project, tmp_path, monkeypatch, clock):
    original = preparation._verify_independent

    def slow(prepared_copy, scope):
        original(prepared_copy, scope)
        clock.now = 3

    monkeypatch.setattr(preparation, "_verify_independent", slow)
    with pytest.raises(subprocess.TimeoutExpired) as failed:
        prepare_project(project, parent=str(tmp_path), receipt_parent=str(tmp_path), timeout_s=2)
    owner = failed.value._langadapter_prepared_project
    assert owner._incomplete
    assert (Path(owner.root) / "module.py").read_bytes() == b"value = 1\n"
    owner.close()
    assert not Path(owner.root).exists()


def test_timeout_after_admission_validation_does_not_allocate(project, tmp_path, monkeypatch, clock):
    original = ProjectContext.validate

    def slow(context):
        original(context)
        clock.now = 3

    monkeypatch.setattr(ProjectContext, "validate", slow)
    before = tuple(sorted(path.name for path in tmp_path.iterdir()))
    with pytest.raises(subprocess.TimeoutExpired):
        prepare_project(project, parent=str(tmp_path), receipt_parent=str(tmp_path), timeout_s=2)
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before


def test_remaining_native_bound_is_not_restarted(clock):
    assert _prepared_copy._timeout(None, "ordinary", 120) == 120
    assert _prepared_copy._timeout(10, "first", 120) == 10
    clock.now = 9
    assert _prepared_copy._timeout(10, "second", 600) == 1
    clock.now = 10
    with pytest.raises(subprocess.TimeoutExpired):
        _prepared_copy._timeout(10, "spent", 600)
