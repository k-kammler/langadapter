"""Membership is explicit and pure, but for Python's bytecode caches, which a
copy always leaves out."""
from dataclasses import FrozenInstanceError

import pytest

from langadapter.errors import InvalidRequest
from langadapter.context import ProjectInputSelection


def test_selection_copies_normalizes_and_exports_detached_inputs():
    excluded, required = [".venv", ".venv/cache", ".git", ".venv"], ["src/main.py", "fixture.txt"]
    value = ProjectInputSelection(excluded, required)
    excluded.clear()
    required.clear()
    assert value.exclude == (".git", ".venv")
    assert value.required == ("fixture.txt", "src/main.py")
    exported = value.to_dict()
    exported["exclude"].append("src")
    assert value.exclude == (".git", ".venv")
    with pytest.raises(FrozenInstanceError):
        value.exclude = ()


@pytest.mark.parametrize("field", ["exclude", "required"])
@pytest.mark.parametrize("value", ["src", None, [None], [""], ["."], ["../src"], ["src/../file"],
    ["./src"], ["src/"], ["src//file"], ["/src"], ["C:/src"], ["a\\b"], ["src/*"],
    ["src/?"], ["src/[ab]"], ["null\0path"]])
def test_ambiguous_path_controls_refuse(field, value):
    with pytest.raises(InvalidRequest):
        ProjectInputSelection(**{field: value})


@pytest.mark.parametrize("excluded", ["src", "src/target.py"])
def test_required_inputs_cannot_be_removed(excluded):
    with pytest.raises(InvalidRequest, match="required input"):
        ProjectInputSelection((excluded,), ("src/target.py",))


def test_native_case_rules_do_not_lowercase_container_paths():
    value = ProjectInputSelection(("Source",), ("source/target.py",))
    assert value.effective(case_sensitive=True) is not value
    with pytest.raises(InvalidRequest, match="required"):
        value.effective(case_sensitive=False)
    assert not value.excludes("source/target.py", case_sensitive=True)
    assert value.excludes("source/target.py", case_sensitive=False)
    assert value.exclude == ("Source",) and value.required == ("source/target.py",)


def test_known_inputs_are_protected_before_any_copy_or_discovery():
    selected = ProjectInputSelection(("target",), ("pom.xml",))
    with pytest.raises(InvalidRequest, match="required"):
        selected.effective(("target/generated/Resource.java",))
    result = selected.effective(("src/Source.java", "mvnw", "pom.xml"))
    assert result.required == ("mvnw", "pom.xml", "src/Source.java")
    assert result.excludes("target/classes/Source.class")
    assert not result.excludes("targets/Resource.java")


def test_the_selection_leaves_out_what_the_copy_leaves_out(tmp_path):
    """excludes answers as the copy does: Python's bytecode caches stay out
    whatever the plan says, and the plan's exclusions, and nothing else."""
    from langadapter import _input_copy

    names = ("pkg/module.py", "pkg/__pycache__/module.cpython-312.pyc", "pkg/old.pyc",
             "pkg/old.pyo", "build/out.txt", "keep.txt")
    source = tmp_path / "project"
    for name in names:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x", encoding="utf-8")
    selection = ProjectInputSelection(("build",))
    copy = tmp_path / "copy"
    _input_copy.tree(source, selection.to_dict(), destination=copy)
    assert {name: selection.excludes(name) for name in names} == {
        name: not (copy / name).exists() for name in names}


def test_names_with_usual_ignore_meanings_are_not_implicitly_removed():
    value = ProjectInputSelection()
    assert all(not value.excludes(name) for name in (".git/config", ".venv/lib/module.py",
                                                   "build/Resource.java", "target/fixture.bin"))


@pytest.mark.parametrize("case", [0, 1, "windows", None])
def test_case_rules_must_be_explicit_boolean(case):
    value = ProjectInputSelection()
    with pytest.raises(InvalidRequest):
        value.effective(case_sensitive=case)
    with pytest.raises(InvalidRequest):
        value.excludes("source.py", case_sensitive=case)


@pytest.mark.parametrize("required", ["pkg/__pycache__/m.pyc", "pkg/m.pyc"])
def test_a_required_path_the_copy_leaves_out_is_refused(required):
    """The copy leaves Python's bytecode caches out, so requiring one raises,
    as excluding a required path does."""
    with pytest.raises(InvalidRequest) as raised:
        ProjectInputSelection(required=(required,))
    assert raised.value.field == "inputs.required"
    with pytest.raises(InvalidRequest):
        ProjectInputSelection().effective((required,))
