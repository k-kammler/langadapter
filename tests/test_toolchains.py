"""The toolchain recipe's command: parts found by language and system, the
languages named, the order and the stop at a failing part, the listing and an
extension's part, with fake parts and no network."""
import os
import shlex
import shutil
import sys

import pytest

import langadapter as lb
from langadapter import toolchains
from langadapter.cli import main

#: The system reader itself, which the fixture below replaces.
REAL_CURRENT_SYSTEM = toolchains._current_system
needs_sh = pytest.mark.skipif(shutil.which("sh") is None, reason="needs a POSIX sh")

#: A part that records its arguments and writes its environment file.
RECORDING = """#!/bin/sh
# download checked: {name} 1.0, by its SHA-256
# download archive: {name}-tools from the archive
set -eu
echo "$2" > "$1/{name}.ran"
echo "export {name}_HOME=$1" > "$1/{name}.env"
exit {status}
"""


@pytest.fixture
def system(tmp_path, monkeypatch):
    """A parts directory with alpha, beta and a failing gamma for the system
    testos-1, and that system as the running one."""
    parts = tmp_path / "parts"
    parts.mkdir()
    for name, status in (("alpha", 0), ("beta", 0), ("gamma", 3)):
        (parts / f"{name}.testos-1.sh").write_text(RECORDING.format(name=name, status=status),
                                                   encoding="utf-8")
    (parts / "delta.otheros-2.sh").write_text(RECORDING.format(name="delta", status=0),
                                              encoding="utf-8")
    monkeypatch.setattr(toolchains, "_PARTS", parts)
    monkeypatch.setattr(toolchains, "_current_system", lambda: "testos-1")
    return tmp_path


@pytest.mark.parametrize("text, expected", [
    ('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n', "ubuntu-24.04"),
    ("ID='debian'\nVERSION_ID='12'\n", "debian-12"),
    ("ID=arch\n", None),
])
def test_the_system_is_read_from_os_release(tmp_path, monkeypatch, text, expected):
    release = tmp_path / "os-release"
    release.write_text(text, encoding="utf-8")
    monkeypatch.setattr(toolchains, "_OS_RELEASE", release)
    assert REAL_CURRENT_SYSTEM() == expected
    monkeypatch.setattr(toolchains, "_OS_RELEASE", tmp_path / "absent")
    assert REAL_CURRENT_SYSTEM() is None


def test_parts_are_found_by_language_and_system(system):
    assert sorted(toolchains._parts("testos-1")) == ["alpha", "beta", "gamma"]
    assert sorted(toolchains._parts("otheros-2")) == ["delta"]


def test_a_system_naming_a_directory_names_no_part(system, monkeypatch):
    """The system read from os-release is compared with the parts' names,
    never joined into a path, so an ID such as ../ finds nothing."""
    (system / "outside.sh").write_text(RECORDING.format(name="outside", status=0),
                                       encoding="utf-8")
    # A directory whose name a joined pattern would walk through.
    (system / "parts" / "x.testos-1").mkdir()
    release = system / "os-release"
    release.write_text("ID=x/../..\nVERSION_ID=/outside\n", encoding="utf-8")
    monkeypatch.setattr(toolchains, "_OS_RELEASE", release)
    crafted = REAL_CURRENT_SYSTEM()
    assert toolchains._parts(crafted) == {}
    assert toolchains._parts("testos-1/../../outside") == {}


def test_the_built_in_parts_are_found_for_ubuntu():
    assert {"python", "java", "c"} <= set(toolchains._parts("ubuntu-24.04"))


@needs_sh
def test_the_named_parts_run_in_order_and_name_their_environment_files(system, capsys):
    into = system / "tools"
    assert main(["toolchains", "beta", "alpha", "--into", str(into)]) == 0
    assert (into / "alpha.ran").read_text(encoding="utf-8").strip() == sys.executable
    assert (into / "beta.ran").exists() and not (into / "gamma.ran").exists()
    printed = capsys.readouterr().out.splitlines()
    assert printed[-2:] == [f". {shlex.quote(str(into / 'beta.env'))}",
                            f". {shlex.quote(str(into / 'alpha.env'))}"]


@needs_sh
def test_a_failing_part_stops_the_command_with_its_status(system):
    into = system / "tools"
    assert main(["toolchains", "gamma", "alpha", "--into", str(into)]) == 3
    assert (into / "gamma.ran").exists() and not (into / "alpha.ran").exists()


def test_a_language_without_a_part_names_the_guide(system, capsys):
    assert main(["toolchains", "delta", "--into", str(system / "tools")]) == 2
    error = capsys.readouterr().err
    assert "no part for delta on testos-1" in error and toolchains._GUIDE in error
    assert not (system / "tools").exists()


def test_a_system_without_os_release_names_the_guide(system, monkeypatch, capsys):
    monkeypatch.setattr(toolchains, "_current_system", lambda: None)
    monkeypatch.setattr(sys, "platform", "linux")
    assert main(["toolchains", "alpha", "--into", str(system / "tools")]) == 2
    error = capsys.readouterr().err
    assert "/etc/os-release names no ID and VERSION_ID here." in error
    assert toolchains._GUIDE in error


@pytest.mark.parametrize("arguments", [["all", "--list"], ["python"]])
def test_windows_is_told_where_the_parts_run(arguments, monkeypatch, capsys):
    """On Windows a listing and an install say that the parts run on Ubuntu
    24.04, also in WSL, and where the steps by hand are."""
    monkeypatch.setattr(toolchains, "_current_system", lambda: None)
    monkeypatch.setattr(sys, "platform", "win32")
    assert main(["toolchains", *arguments]) == 2
    assert capsys.readouterr().err == (
        "langadapter toolchains: this system is Windows, and the parts run on Ubuntu 24.04, "
        "also in WSL. Run the command there, or install the tools by hand as the guide's "
        f"section Windows and macOS says: {toolchains._GUIDE}#windows-and-macos\n")


def test_the_listing_names_every_download_and_runs_nothing(system, capsys):
    assert main(["toolchains", "all", "--list", "--into", str(system / "tools")]) == 0
    printed = capsys.readouterr().out
    assert "alpha on testos-1 (built in):" in printed and "gamma on testos-1" in printed
    assert "  checked: beta 1.0, by its SHA-256" in printed
    assert "  archive: beta-tools from the archive" in printed
    assert not (system / "tools").exists()


#: An extension's part, whose check loads the extension the command names.
EXTENSION_PART = """#!/bin/sh
set -eu
printf '%s' "$3" > "$1/lua.extension"
"$2" -m langadapter.cli check lua --extension "$3" > "$1/lua.check"
"""


@needs_sh
def test_an_extension_brings_its_part_in_its_one_file(system, monkeypatch, capsys):
    """The extension's part comes from its one file, runs from a scratch file
    that goes afterwards, and its check finds the language through the
    extension the command passes as the third argument."""
    extension = system / "lua.py"
    extension.write_text(
        "import langadapter as la\n\n\n"
        "class LuaAdapter(la.LangAdapter):\n"
        "    language = 'lua'\n"
        "    file_extensions = ('.lua',)\n\n\n"
        "langadapter_language = LuaAdapter\n"
        "langadapter_toolchain = {'testos-1': " + repr(EXTENSION_PART)
        + "}\n", encoding="utf-8")
    scratch = system / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr("tempfile.tempdir", str(scratch))
    # The part's check imports the LangAdapter under test, as an installed
    # one imports from the environment the command runs in.
    monkeypatch.setenv("PYTHONPATH", os.path.dirname(os.path.dirname(os.path.abspath(lb.__file__))))
    try:
        assert main(["toolchains", "lua", "--list", "--extension", str(extension)]) == 0
        assert f"lua on testos-1 ({extension.resolve()}):" in capsys.readouterr().out
        into = system / "tools"
        assert main(["toolchains", "lua", "--extension", str(extension),
                     "--into", str(into)]) == 0
        assert (into / "lua.extension").read_text(encoding="utf-8") == str(extension.resolve())
        assert "Language: lua" in (into / "lua.check").read_text(encoding="utf-8")
        assert list(scratch.iterdir()) == []
    finally:
        if "lua" in lb.names():
            lb.unregister("lua")


def test_an_install_directory_a_shell_would_read_is_refused(system, capsys):
    """A part writes the install directory into the environment file a shell
    sources, so a directory holding a quote or a dollar sign is refused."""
    into = system / 'tools "$HOME"'
    assert main(["toolchains", "alpha", "--into", str(into)]) == 2
    error = capsys.readouterr().err
    assert 'holds " and $, which a shell' in error and "--into" in error
    assert not into.exists()


#: A stand-in for the interpreter a part downloads through: it writes other
#: bytes than the archive to the download's target, its last argument.
WRONG_DOWNLOAD = """#!/bin/sh
for target; do :; done
printf 'not the archive' > "$target"
"""

#: A stand-in for dpkg-query that reports every package installed except
#: the one the variable MISSING names.
DPKG_QUERY = """#!/bin/sh
for package; do :; done
[ "$package" = "${MISSING:-}" ] && exit 1
echo 'install ok installed'
"""


#: Where the C part unpacks clang's and Mull's packages, below DIR.
C_ROOT = "c/root-18.1.3-1ubuntu1-mull-0.34.2"


def _stand_ins(tmp_path, **scripts):
    """A directory of stand-in commands, by name and text, and the variables
    with that directory first on PATH."""
    stand_in = tmp_path / "bin"
    stand_in.mkdir()
    for name, text in scripts.items():
        (stand_in / name.replace("_", "-")).write_text(text, encoding="utf-8", newline="\n")
        (stand_in / name.replace("_", "-")).chmod(0o755)
    return dict(os.environ, PATH=os.pathsep.join([str(stand_in), os.environ["PATH"]]))


@needs_sh
@pytest.mark.parametrize("language, unpacked", [("python", "python/bin/uv"),
                                                ("java", "java/jdk-21.0.12.1+1"),
                                                ("c", C_ROOT)])
def test_a_download_with_another_checksum_stops_the_part_before_unpacking(
        tmp_path, language, unpacked):
    """A built-in part checks each file it fetches against the checksum it
    holds, and a file with another checksum stops it before tar or dpkg
    runs. The C part's system packages read as installed."""
    import subprocess

    fake = tmp_path / "fake-python"
    fake.write_text(WRONG_DOWNLOAD, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    into = tmp_path / "tools"
    part = toolchains._PARTS / f"{language}.ubuntu-24.04.sh"
    done = subprocess.run(["sh", str(part), into.as_posix(), fake.as_posix(), ""],
                          capture_output=True, text=True,
                          env=_stand_ins(tmp_path, dpkg_query=DPKG_QUERY))
    assert done.returncode != 0
    assert "FAILED" in done.stdout + done.stderr
    assert not (into / unpacked).exists()
    assert not (into / f"{language}.env").exists()


@needs_sh
@pytest.mark.parametrize("language", ["python", "java", "c"])
def test_a_part_refuses_another_architecture_before_anything(tmp_path, language):
    """A built-in part installs x86_64 tools, so on another machine it stops
    with the guide's address before it makes or fetches anything. The
    refusal names the command, not the part's file."""
    import subprocess

    into = tmp_path / "tools"
    part = toolchains._PARTS / f"{language}.ubuntu-24.04.sh"
    done = subprocess.run(["sh", str(part), into.as_posix(), sys.executable, ""],
                          capture_output=True, text=True,
                          env=_stand_ins(tmp_path, uname="#!/bin/sh\necho aarch64\n"))
    assert done.returncode == 2
    assert (f"langadapter toolchains {language} installs x86_64 tools, and this machine is "
            "aarch64") in done.stderr
    assert "ubuntu-24.04.sh" not in done.stderr
    assert toolchains._GUIDE in done.stderr and not into.exists()


@needs_sh
def test_the_c_part_run_as_a_user_names_the_missing_system_packages(tmp_path):
    """Run as a user without GCC 13, the C part stops with the exact
    apt-get lines, the package lists updated first, before it makes or
    fetches anything, and never calls apt-get itself. The refusal names the
    command, not the part's file."""
    import subprocess

    into = tmp_path / "tools"
    part = toolchains._PARTS / "c.ubuntu-24.04.sh"
    env = _stand_ins(tmp_path, dpkg_query=DPKG_QUERY, id="#!/bin/sh\necho 1000\n",
                     apt_get="#!/bin/sh\necho apt-get ran >&2\nexit 9\n")
    done = subprocess.run(["sh", str(part), into.as_posix(), sys.executable, ""],
                          capture_output=True, text=True, env=dict(env, MISSING="gcc-13"))
    assert done.returncode == 1
    assert done.stderr.splitlines()[-1] == "sudo apt-get update && sudo apt-get install gcc-13"
    assert done.stderr.startswith("langadapter toolchains c needs system packages")
    assert "apt-get ran" not in done.stderr and not into.exists()


#: Stand-ins that let a built-in part run through without network: every file
#: reads as checked, dpkg -x records each package in its target directory,
#: tar writes a uv that does nothing, and the Python the command runs in
#: records each module it runs, the check, beside itself and prints an
#: interpreter's path for the Python part's provisioning.
PART_STAND_INS = {
    "dpkg_query": DPKG_QUERY,
    "sha256sum": "#!/bin/sh\ncat > /dev/null\n",
    "sha512sum": "#!/bin/sh\ncat > /dev/null\n",
    "dpkg": '#!/bin/sh\nmkdir -p "$3"\necho "${2##*/}" >> "$3/unpacked"\n',
    "tar": '#!/bin/sh\nprintf "#!/bin/sh\\n" > "$4/uv"\nchmod +x "$4/uv"\n',
    "fake_python": ('#!/bin/sh\nif [ "$1" = "-m" ]; then\n    echo "$*" >> "${0%/*}/checked"\n'
                    '    exit 0\nfi\necho /stand-in/environment/bin/python\n'),
}


def _run_through(tmp_path, language, part=None):
    """Run a built-in part, or ``part`` in its place, through with the
    stand-ins and no langadapter command on PATH: the finished process, the
    install directory and the module runs of the stand-in Python."""
    import subprocess

    env = _stand_ins(tmp_path, **PART_STAND_INS)
    env["PATH"] = os.pathsep.join(entry for entry in env["PATH"].split(os.pathsep)
                                  if entry and not shutil.which("langadapter", path=entry))
    into = tmp_path / "tools"
    part = part or toolchains._PARTS / f"{language}.ubuntu-24.04.sh"
    done = subprocess.run(["sh", str(part), into.as_posix(),
                           (tmp_path / "bin" / "fake-python").as_posix(), ""],
                          capture_output=True, text=True, env=env)
    checked = tmp_path / "bin" / "checked"
    return done, into, checked.read_text(encoding="utf-8").splitlines() if checked.exists() else []


@needs_sh
@pytest.mark.parametrize("language", ["python", "java", "c"])
def test_a_part_checks_with_the_python_the_command_runs_in(tmp_path, language):
    """A part's check runs LangAdapter through the Python the command runs
    in, so it needs no langadapter command on PATH, as when the command
    started with python -m from an environment that is not activated."""
    done, _, ran = _run_through(tmp_path, language)
    assert done.returncode == 0, done.stderr
    assert len(ran) == 1
    assert ran[0].startswith(f"-m langadapter.cli check {language} --example ")
    assert ran[0].endswith(f"/tools/{language}/example")


@needs_sh
def test_the_java_part_keeps_the_checks_downloads_below_the_install_directory(tmp_path):
    """The Java part's check fills LangAdapter's tool cache, which java.env
    names for later runs, and the example's Maven repository, both below
    the install directory rather than the home directory."""
    import re

    done, into, _ = _run_through(tmp_path, "java")
    assert done.returncode == 0, done.stderr
    assert re.search(r'^export LANGADAPTER_JAVA_TOOLS=".*/tools/java/tools"$',
                     (into / "java.env").read_text(encoding="utf-8"), flags=re.MULTILINE)
    config = into / "java" / "example" / ".mvn" / "maven.config"
    assert re.fullmatch(r"-Dmaven\.repo\.local=.*/tools/java/repository\n",
                        config.read_text(encoding="utf-8"))


@needs_sh
def test_a_part_searches_the_commands_own_path(system, monkeypatch):
    """The command puts no directory before its PATH, which every command
    of a part searches, apt-get and dpkg run as root among them, also when
    the command started from a file."""
    import subprocess

    started = system / "bin" / "langadapter"
    started.parent.mkdir()
    started.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [str(started), "toolchains"])
    (system / "parts" / "path.testos-1.sh").write_text('#!/bin/sh\necho "$PATH" > "$1/path"\n',
                                                       encoding="utf-8")
    into = system / "tools"
    assert main(["toolchains", "path", "--into", str(into)]) == 0
    own = subprocess.run(["sh", "-c", 'echo "$PATH"'], capture_output=True, text=True).stdout
    assert (into / "path").read_text(encoding="utf-8") == own


def test_an_install_directory_with_a_colon_is_refused(system, capsys):
    """A part puts the install directory on PATH in its environment file,
    where a colon would split it into two entries."""
    into = system / "tools:bin"
    assert main(["toolchains", "alpha", "--into", str(into)]) == 2
    assert "holds a colon, which a shell" in capsys.readouterr().err
    assert not (system / "tools").exists()


@needs_sh
def test_the_c_part_unpacks_another_pin_into_a_directory_of_its_own(tmp_path):
    """The C part's unpacked packages are named by clang's and Mull's
    versions, so a part with another pin, run on an install of the old one,
    unpacks its packages and its environment file names them."""
    into = tmp_path / "tools"
    text = (toolchains._PARTS / "c.ubuntu-24.04.sh").read_text(encoding="utf-8")
    for version in ("0.34.2", "0.35.0"):
        part = tmp_path / f"c-{version}.sh"
        part.write_text(text.replace("MULL_VERSION=0.34.2", f"MULL_VERSION={version}"),
                        encoding="utf-8", newline="\n")
        if (tmp_path / "bin").exists():
            shutil.rmtree(tmp_path / "bin")
        done, _, _ = _run_through(tmp_path, "c", part)
        assert done.returncode == 0, done.stderr
        root = into / "c" / f"root-18.1.3-1ubuntu1-mull-{version}"
        unpacked = (root / "unpacked").read_text(encoding="utf-8").splitlines()
        assert f"Mull-18-{version}-LLVM-18.1.3-ubuntu-amd64-24.04.deb" in unpacked
        assert len(unpacked) == 7
        assert f'/c/{root.name}/usr/bin:' in (into / "c.env").read_text(encoding="utf-8")


@pytest.mark.parametrize("language", ["python", "java", "c"])
def test_each_pinned_version_is_named_in_a_download_line(language):
    """A part's download lines, which --list prints, name each version the
    part pins."""
    import re

    text = (toolchains._PARTS / f"{language}.ubuntu-24.04.sh").read_text(encoding="utf-8")
    pins = re.findall(r"^[A-Z]+_(?:VERSION|RELEASE)=(\S+)$", text, flags=re.MULTILINE)
    downloads = [line for line in text.splitlines() if line.startswith(toolchains._DOWNLOAD)]
    assert pins and all(any(pin in line for line in downloads) for pin in pins)
