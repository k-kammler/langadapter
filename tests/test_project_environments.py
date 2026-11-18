"""An environment inside the project in a prepared copy's values: admitted
when nothing in its site directories leads into the project outside it, refused
with the file and the path otherwise, and left out of the copy."""
import json
import os
from pathlib import Path
import sys

import pytest

import langadapter as lb

LEADS = "leads into the project's sources"


@pytest.fixture
def project(tmp_path):
    """A project with an environment directory inside it and its site directory."""
    root = tmp_path / "project"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "src" / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    environment = root / ".venv"
    site = environment / "lib" / "python3.12" / "site-packages"
    (site / "installed").mkdir(parents=True)
    (site / "installed" / "__init__.py").write_text("", encoding="utf-8")
    (environment / "bin").mkdir()
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\nversion = 3.12.4\n", encoding="utf-8")
    return root, environment, site


def _prepared(tmp_path, root, values):
    context = lb.ProjectContext(str(root), lb.Interpreter(path=sys.executable),
                                environment=lb.Environment(values, inherit=False))
    return lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path))


def _refused(tmp_path, root, values, *texts):
    with pytest.raises(lb.InvalidRequest) as refused:
        _prepared(tmp_path, root, values)
    for text in texts:
        assert str(text) in str(refused.value)
    return refused.value


def test_an_environment_with_installed_packages_only_is_admitted_and_left_out(project, tmp_path):
    """The activated environment and its bin directory on PATH: the copy holds no
    environment directory, and its commands get the values unchanged."""
    root, environment, _ = project
    values = {"VIRTUAL_ENV": str(environment),
              "PATH": os.pathsep.join([str(environment / "bin"), str(tmp_path / "elsewhere")])}
    owner = _prepared(tmp_path, root, values)
    try:
        with owner.use() as context:
            assert not (Path(context.root) / ".venv").exists()
            assert (Path(context.root) / "src" / "pkg" / "__init__.py").is_file()
            assert dict(context.environment) == values
    finally:
        owner.close()


def test_a_path_entry_into_the_project_is_refused(project, tmp_path):
    root, environment, site = project
    (site / "__editable__.pkg-1.0.pth").write_text(str(root / "src") + "\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)},
             LEADS, "__editable__.pkg-1.0.pth", root / "src")


def test_a_relative_path_entry_into_the_project_is_refused(project, tmp_path):
    """A path entry is read as site reads it, relative to the site directory."""
    root, environment, site = project
    (site / "project.pth").write_text("../../../../src\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, "project.pth")


@pytest.mark.parametrize("finder, module, line", [
    ("setuptools", "__editable___pkg_1_0_finder",
     "MAPPING: dict[str, str] = {{'pkg': {target!r}}}\n"),
    ("meson-python", "_pkg_editable_loader",
     "def init():\n    finder = MesonpyMetaFinder('pkg', {{'pkg'}}, {build!r}, ['ninja'], False)\n"),
    ("scikit-build-core", "_pkg_editable",
     "install({{'pkg': {init!r}}}, {{}}, None, ['pkg'], None, False, False, [], [], '')\n"),
    ("editables", "_editable_impl_pkg",
     "from editables.redirector import RedirectingFinder as F\nF.install()\n"
     "F.map_module('pkg', {init!r})\n"),
])
def test_an_import_hook_finder_into_the_project_is_refused(project, tmp_path, finder, module, line):
    """The .pth import line loads a finder module from the site directory, whose
    literals hold the original tree's paths, as each backend writes them."""
    root, environment, site = project
    paths = {"target": str(root / "src" / "pkg"), "build": str(root / "build" / "cp312"),
             "init": str(root / "src" / "pkg" / "__init__.py")}
    (site / f"{module}.py").write_text(line.format(**paths), encoding="utf-8")
    (site / f"__editable__.pkg-1.0-{finder}.pth").write_text(
        f"import {module}; {module}.install()\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, f"{module}.py")


def test_an_egg_link_into_the_project_is_refused(project, tmp_path):
    root, environment, site = project
    (site / "pkg.egg-link").write_text(str(root / "src") + "\n.\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, "pkg.egg-link")


def test_a_site_customization_into_the_project_is_refused(project, tmp_path):
    root, environment, site = project
    (site / "sitecustomize.py").write_text(
        f"import sys\nsys.path.insert(0, {str(root / 'src')!r})\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, "sitecustomize.py")


def test_an_editable_install_record_of_the_project_is_refused(project, tmp_path):
    root, environment, site = project
    record = site / "pkg-1.0.dist-info"
    record.mkdir()
    (record / "direct_url.json").write_text(json.dumps(
        {"url": root.as_uri(), "dir_info": {"editable": True}}), encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, "direct_url.json")


def test_a_regular_install_record_from_the_project_is_admitted(project, tmp_path):
    """A regular install from the project's directory is a copy of it, which the
    runs' origin check handles: only an editable record leads into it."""
    root, environment, site = project
    record = site / "pkg-1.0.dist-info"
    record.mkdir()
    (record / "direct_url.json").write_text(json.dumps(
        {"url": root.as_uri(), "dir_info": {}}), encoding="utf-8")
    _prepared(tmp_path, root, {"VIRTUAL_ENV": str(environment)}).close()


def test_a_package_dir_linked_into_the_site_dir_is_refused(project, tmp_path):
    """flit install --symlink writes no .pth path: the site entry itself is a
    link into the project."""
    root, environment, site = project
    try:
        (site / "pkg").symlink_to(root / "src" / "pkg", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"this system cannot make the link: {error}")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, root / "src" / "pkg")


def test_a_value_whose_path_links_out_of_the_environment_is_refused(project, tmp_path):
    """A directory inside the environment that is a link into the project's
    sources makes the value refer to the project."""
    root, environment, _ = project
    try:
        (environment / "tools").symlink_to(root / "src", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"this system cannot make the link: {error}")
    _refused(tmp_path, root, {"PATH": str(environment / "tools")}, "PATH refers to the original project")


def test_a_linked_pth_file_is_read_by_its_kind(project, tmp_path):
    """A .pth file linked to a file inside the environment is read as a .pth
    file, not only followed as a link."""
    root, environment, site = project
    kept = environment / "kept.pth"
    kept.write_text(str(root / "src") + "\n", encoding="utf-8")
    try:
        (site / "linked.pth").symlink_to(kept)
    except OSError as error:
        pytest.skip(f"this system cannot make the link: {error}")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, "linked.pth", root / "src")


def test_a_pypy_site_dir_is_read(project, tmp_path):
    root, environment, _ = project
    site = environment / "lib" / "pypy3.10" / "site-packages"
    site.mkdir(parents=True)
    (site / "project.pth").write_text(str(root / "src") + "\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, LEADS, "project.pth")


def test_an_environment_with_the_system_site_packages_is_refused(project, tmp_path):
    root, environment, _ = project
    (environment / "pyvenv.cfg").write_text(
        "home = /usr/bin\ninclude-system-site-packages = true\nversion = 3.12.4\n", encoding="utf-8")
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)},
             "includes the base interpreter's packages")


def test_an_unreadable_file_of_the_site_dir_is_refused(project, tmp_path):
    """A file the check cannot read could hold a path into the project."""
    root, environment, site = project
    (site / "unreadable.pth").mkdir()
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, "unreadable.pth")


def test_a_path_that_also_names_another_project_dir_is_refused(project, tmp_path):
    """Every path naming the project must lie in the environment directory: a
    PATH that also names the project's tools directory refers to the project."""
    root, environment, _ = project
    (root / "tools").mkdir()
    _refused(tmp_path, root, {"PATH": os.pathsep.join([str(environment / "bin"), str(root / "tools")])},
             "PATH refers to the original project")


def test_a_dir_without_an_environment_marker_is_refused(project, tmp_path):
    root, environment, _ = project
    (environment / "pyvenv.cfg").unlink()
    _refused(tmp_path, root, {"VIRTUAL_ENV": str(environment)}, "VIRTUAL_ENV refers to the original project")


def test_a_conda_environment_inside_the_project_is_admitted(project, tmp_path):
    root, _, _ = project
    conda = root / "env"
    (conda / "conda-meta").mkdir(parents=True)
    (conda / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    owner = _prepared(tmp_path, root, {"CONDA_PREFIX": str(conda)})
    try:
        with owner.use() as context:
            assert not (Path(context.root) / "env").exists()
    finally:
        owner.close()


def test_a_context_in_a_container_keeps_the_refusal(project, tmp_path):
    """Its values name container paths, which this machine cannot read."""
    root, _, _ = project
    zone = tmp_path / "zone"
    zone.mkdir()
    context = lb.ProjectContext("/subject/project", lb.Interpreter(path="/subject/python"),
        execution=lb.ContainerExecution("borrowed", str(zone), repository_dir="/subject/project",
                                        container_work_dir="/mapped", helper_python="/control/python3"),
        environment=lb.Environment({"VIRTUAL_ENV": "/subject/project/.venv"}, inherit=False))
    with pytest.raises(lb.InvalidRequest, match="VIRTUAL_ENV refers to the original project"):
        lb.prepare_project(context, receipt_parent=str(tmp_path))


def test_an_interpreter_inside_the_project_keeps_its_rule(project, tmp_path):
    """An interpreter selected from inside the project runs in place, without
    the environment check: its editable install is the origin check's."""
    root, environment, site = project
    (site / "__editable__.pkg-1.0.pth").write_text(str(root / "src") + "\n", encoding="utf-8")
    (environment / "bin" / "python").write_bytes(b"")
    context = lb.ProjectContext(str(root), lb.Interpreter(path=str(environment / "bin" / "python")))
    owner = lb.prepare_project(context, parent=str(tmp_path), receipt_parent=str(tmp_path),
                               inputs=lb.ProjectInputSelection(exclude=(".venv",)))
    owner.close()
