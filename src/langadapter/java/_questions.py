"""Java's setup questions (``JavaAdapter.setup_questions``): the JDK home,
then the build tool's launcher when the project has no wrapper, or a note
when the runs refuse its build files (none, or both systems')."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple, Union

from langadapter.adapter import SetupNote, SetupQuestion
from langadapter.java._build import (
    BOTH_BUILDS, BOTH_BUILDS_ACTION, BUILD_FILES, build_systems, wrapper_name,
)
from langadapter.java._profile import SUPPORT

#: The oldest JDK the release runs a Java project on: the support
#: declaration's oldest tested or expected JDK, below which it refuses.
OLDEST_JDK = int(SUPPORT.runtime.oldest())


def jdk_version(probe: Any, home: str) -> Optional[int]:
    """The feature version the JDK at ``home`` names in its release file,
    or None."""
    text = probe.read_text(Path(home) / "release")
    match = re.search(r'JAVA_VERSION="(?:1\.)?(\d+)', text or "")
    return int(match.group(1)) if match else None


def default_jdk(probe: Any) -> Tuple[str, str]:
    """The JDK home to propose and what was looked at: JAVA_HOME, else the
    JDK of the java on PATH when it is ``OLDEST_JDK`` or later."""
    home = probe.environment("JAVA_HOME")
    if home:
        return home, "from JAVA_HOME"
    java = probe.which("java")
    if not java:
        return "", "JAVA_HOME is not set and no java is on PATH"
    found = str(Path(java).resolve().parent.parent)
    version = jdk_version(probe, found)
    if version is not None and version >= OLDEST_JDK:
        return found, f"the java on PATH, JDK {version}"
    what = f"JDK {version}" if version is not None else "not a JDK this check can read"
    return "", f"JAVA_HOME is not set and the java on PATH is {what}"


def jdk_candidates(probe: Any) -> List[str]:
    """The JDK homes offered: JAVA_HOME, then the JDK of the java on PATH,
    each once."""
    found = [probe.environment("JAVA_HOME")]
    java = probe.which("java")
    if java:
        found.append(str(Path(java).resolve().parent.parent))
    return list(dict.fromkeys(path for path in found if path))


def problem(setting: str, value: str, project_dir: Path, probe: Any) -> Optional[str]:
    """Why the setup refuses ``value`` for ``setting``: a JDK, its home or its
    java executable as the runs read it (``_env.read``), must name its
    version in its release file, the project's JDK ``OLDEST_JDK`` or later,
    and a build tool's launcher file must exist. A blank JDK, which uses
    JAVA_HOME or the project's JDK, is not checked."""
    if setting in ("java_home", "build_java_home"):
        if not value.strip():
            return None
        home = Path(value).parent.parent if probe.is_file(Path(value)) else Path(value)
        version = jdk_version(probe, str(home))
        if setting == "build_java_home":
            return None if version is not None else "no JDK whose release file names a version"
        return None if (version or 0) >= OLDEST_JDK else f"a JDK {OLDEST_JDK} or later is needed"
    if setting in ("maven", "gradle"):
        return None if probe.is_file(Path(value)) else "no such file"
    return None


def questions(project_dir: Path, probe: Any) -> Iterator[Union[SetupQuestion, SetupNote]]:
    """The JDK, then the build tool as the project's files call for it,
    read as the runs read them (``build_systems``)."""
    default, looked = default_jdk(probe)
    yield SetupQuestion("java.jdk", "The JDK home (a directory with bin/java)", setting="java_home",
                        default=default, reason=looked,
                        choices=lambda: tuple(jdk_candidates(probe)), open=True,
                        check=lambda answer: problem("java_home", answer, project_dir, probe))
    tools = build_systems([name for _, names in BUILD_FILES for name in names
                           if probe.is_file(project_dir / name)])
    # The runs' rule: the selected tool's own wrapper in the platform's form.
    wrapper = len(tools) == 1 and probe.is_file(
        project_dir / wrapper_name(tools[0], windows=os.name == "nt"))
    if not tools:
        yield SetupNote(("  The project has no Maven or Gradle build file (pom.xml, "
                         "build.gradle): a Java run needs one.",),
                        (f"a pom.xml or build.gradle in {project_dir}",))
    elif len(tools) > 1:
        yield SetupNote((f"  A Java run refuses the project: {BOTH_BUILDS}.",),
                        (BOTH_BUILDS_ACTION,))
    elif not wrapper:
        tool = tools[0]
        yield SetupQuestion(
            "java.build", f"The {tool} executable (the project has no {tool} wrapper)",
            setting=tool, default=probe.which("mvn" if tool == "maven" else "gradle"),
            check=lambda answer: problem(tool, answer, project_dir, probe))
