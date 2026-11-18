"""A macOS JDK bundle (``<name>.jdk``) keeps its home in ``Contents/Home``:
the bundle, its home and its ``java`` all name the same JDK."""
from __future__ import annotations

import os

from langadapter.java import _env as jdks


def test_a_macos_jdk_bundle_is_read_from_its_contents_home(tmp_path):
    home = tmp_path / "temurin-21.jdk" / "Contents" / "Home"
    (home / "bin").mkdir(parents=True)
    suffix = ".exe" if os.name == "nt" else ""
    for tool in ("java", "javac"):
        (home / "bin" / f"{tool}{suffix}").write_text("", encoding="utf-8")
    (home / "release").write_text('JAVA_VERSION="21.0.11"\n', encoding="utf-8")
    found = jdks.read(str(tmp_path / "temurin-21.jdk"))
    assert found is not None and found.home == str(home) and found.feature == 21
    assert jdks.read(str(home)) == found
    assert jdks.read(str(home / "bin" / f"java{suffix}")) == found
    assert jdks.read(str(tmp_path / "temurin-21.jdk" / "Contents")) is None
