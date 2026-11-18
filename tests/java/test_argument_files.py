"""Every Java tool's argument file reads back as written: a test run's, the
discovery's, PIT's and javac's come from the one writer, _tools.argfile."""
from __future__ import annotations

import os
import subprocess

from langadapter.java import _tools

ECHO = """\
public class Echo {
    public static void main(String[] args) {
        System.out.println(System.getProperty("regex"));
        System.out.println(System.getProperty("place"));
    }
}
"""


def test_a_property_s_backslash_and_a_path_read_back_as_given(jdks, tmp_path):
    """javac and java read each argument as given: a Surefire property such
    as regex=\\d+ keeps its backslash, a path its platform's form."""
    home = jdks[max(jdks)]

    def run(tool, file, arguments):
        listed = _tools.argfile(tmp_path / file, arguments)
        return subprocess.run([str(home / "bin" / (tool + (".exe" if os.name == "nt" else ""))),
                               f"@{listed}"], capture_output=True, text=True, timeout=120, check=False)

    spaced_dir = tmp_path / "directory with space"
    spaced_dir.mkdir()
    source = spaced_dir / "Echo.java"
    source.write_text(ECHO, encoding="utf-8")
    compiled = run("javac", "javac.args", ["-d", str(spaced_dir), str(source)])
    assert compiled.returncode == 0, compiled.stderr
    place = str(spaced_dir / 'a"b')
    done = run("java", "java.args", ["-Dregex=\\d+", f"-Dplace={place}", "-cp", str(spaced_dir), "Echo"])
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines() == ["\\d+", place]
