"""Registration, lookup and instance creation: built-in and outside
adapters, one instance per language and project."""
import sys

import pytest

import langadapter as lb


class ToyAdapter(lb.LangAdapter):
    """An adapter for a made-up language, registered from outside."""

    language = "toy"
    file_extensions = (".toy",)


class OtherAdapter(lb.LangAdapter):
    language = "other"
    file_extensions = (".oth",)


@pytest.fixture
def context(tmp_path):
    return lb.ProjectContext(
        root=str(tmp_path), interpreter=lb.Interpreter(path=sys.executable))


@pytest.fixture
def toy():
    lb.register("toy", [".toy", "TOYX"], ToyAdapter)
    yield
    for name in ("toy", "other"):
        if name in lb.names():
            lb.unregister(name)


def test_an_outside_adapter_is_found_by_name_and_extension(toy, context):
    assert "toy" in lb.names()
    assert lb.language_for("src/game.toy") == "toy"
    assert lb.language_for("GAME.TOYX") == "toy"
    adapter = lb.create("toy", context)
    assert isinstance(adapter, ToyAdapter)
    assert adapter.context is context
    assert lb.registration("TOY").file_extensions == (".toy", ".toyx")


def test_a_conflicting_extension_is_refused_naming_its_holder(toy):
    with pytest.raises(lb.RegistrationConflict) as refused:
        lb.register("other", [".oth", ".toy"], OtherAdapter)
    assert refused.value.claimed == ".toy"
    assert refused.value.holder == "toy"
    assert "other" not in lb.names()


def test_a_conflicting_name_is_refused(toy):
    with pytest.raises(lb.RegistrationConflict) as refused:
        lb.register("toy", [".t2"], ToyAdapter)
    assert refused.value.holder == "toy"
    assert lb.registration("toy").file_extensions == (".toy", ".toyx")


def test_replace_takes_over_what_another_registration_holds(toy):
    lb.register("other", [".oth", ".toy"], OtherAdapter, replace=True)
    assert lb.language_for("a.toy") == "other"
    assert lb.registration("toy").file_extensions == (".toyx",)
    lb.register("toy", [".toy"], ToyAdapter, replace=True)
    assert lb.language_for("a.toy") == "toy"
    assert lb.registration("toy").file_extensions == (".toy",)
    assert lb.registration("other").file_extensions == (".oth",)
    with pytest.raises(lb.UnsupportedLanguage):
        lb.language_for("a.toyx")


def test_an_unknown_language_lists_the_registered_ones(toy, context):
    with pytest.raises(lb.UnsupportedLanguage) as refused:
        lb.create("cobol", context)
    assert refused.value.requested == "cobol"
    assert "toy" in refused.value.registered
    assert "toy" in str(refused.value)
    with pytest.raises(lb.UnsupportedLanguage) as unknown_file:
        lb.language_for("program.cbl")
    assert unknown_file.value.requested == ".cbl"


def test_an_invalid_context_is_refused_at_creation_naming_its_field(
        toy, tmp_path):
    missing = lb.ProjectContext(
        root=str(tmp_path / "absent"),
        interpreter=lb.Interpreter(path=sys.executable))
    with pytest.raises(lb.InvalidRequest) as refused:
        lb.create("toy", missing)
    assert refused.value.field == "root"


def test_a_factory_must_return_an_adapter(context):
    lb.register("toy", [".toy"], lambda ctx: object())
    try:
        with pytest.raises(lb.InvalidRequest) as refused:
            lb.create("toy", context)
        assert refused.value.field == "factory"
    finally:
        lb.unregister("toy")


@pytest.mark.parametrize("name, file_extensions", [
    ("", [".x"]), ("x", []), ("x", [""]), ("x", ["."]),
])
def test_a_registration_without_a_name_or_extension_is_refused(
        name, file_extensions):
    with pytest.raises(lb.InvalidRequest):
        lb.register(name, file_extensions, ToyAdapter)


def test_instances_for_different_contexts_share_no_state(toy, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a = lb.create("toy", lb.ProjectContext(
        root=str(first), interpreter=lb.Interpreter(path=sys.executable)))
    b = lb.create("toy", lb.ProjectContext(
        root=str(second), interpreter=lb.Interpreter(path=sys.executable)))
    assert a is not b
    assert a.context.root != b.context.root
