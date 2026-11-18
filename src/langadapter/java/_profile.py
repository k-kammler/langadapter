"""The Java adapter's support declaration and version profile, over the
JVM's shared JDK and JUnit rules (``JvmProfile``)."""
from __future__ import annotations

import re
from pathlib import PurePath
from typing import Optional, Sequence, Tuple
from langadapter.versions import (
    VersionProfile,
    LanguageSupport,
    RuntimeSupport,
    Support,
    VersionSpan,
    Wording,
)


# The JVM toolkit's version profile: every decision that depends on the
# JDK's or on JUnit's version is asked here, not in the operations. A JVM
# language's profile extends it with its own declaration's wording.

#: The JUnit 4 a run uses for a project whose JUnit 4 predates 4.12.
JUNIT4_FLOOR = "junit:junit:4.13.2"
#: The jars by which a test classpath has JUnit 4, 5 or 6.
_JUNIT_JARS = ("junit", "junit-jupiter-api", "junit-platform-engine")


def _jar_version(classpath: Sequence[str], artifact: str) -> Optional[str]:
    """The version of jar ``artifact`` on ``classpath`` by whole file name, or
    None (``junit-4.13.2.jar``, while ``hamcrest-junit-2.0.0.0.jar`` is no
    JUnit)."""
    pattern = re.compile(re.escape(artifact) + r"-(\d.*?)\.jar")
    for entry in classpath:
        match = pattern.fullmatch(PurePath(entry).name)
        if match:
            return match.group(1)
    return None


def release_tuple(version: str) -> Tuple[int, ...]:
    """The leading numbers of a version, for ordering (``4.11`` -> (4, 11))."""
    numbers = []
    for part in version.split("."):
        digits = re.match(r"\d+", part)
        if not digits:
            break
        numbers.append(int(digits.group(0)))
    return tuple(numbers)


class JvmProfile(VersionProfile):
    """The JVM toolkit's version-dependent answers. The class methods answer
    for the versions given (a JUnit version, a test release)."""

    #: The oldest JDK the measuring tools run on: PIT 1.30 needs Java 11.
    minimum_jdk = 11
    #: The helpers' release, so every supported JDK runs them.
    helper_release = 11

    @classmethod
    def runtime_for(cls, release: int) -> int:
        """The oldest JDK feature measuring a project compiled for ``release``."""
        return max(release, cls.minimum_jdk)

    @classmethod
    def preparation_refusal(cls, feature: int) -> Optional[str]:
        """Why a JDK of feature ``feature`` cannot prepare a project, or None."""
        if feature < cls.minimum_jdk:
            return f"Java preparation requires JDK {cls.minimum_jdk} or newer, not {feature}"
        return None

    @classmethod
    def run_refusal(cls, feature: int, test_release: int, platform: str) -> Optional[str]:
        """Why a JDK of feature version ``feature`` cannot run a suite
        compiled for ``test_release`` on JUnit Platform ``platform``, or None."""
        minimum = cls.minimum_runtime(test_release, platform)
        if feature < minimum:
            return (f"the project's test release and JUnit Platform need JDK {minimum} or "
                    f"newer, not {feature}")
        return None

    @classmethod
    def minimum_runtime(cls, test_release: int, platform: str) -> int:
        """The oldest JDK that runs a suite compiled for ``test_release`` on
        the JUnit Platform ``platform``: JUnit 6 needs Java 17."""
        return max(cls.runtime_for(test_release),
                   17 if release_tuple(platform) >= (6,) else cls.minimum_jdk)

    @staticmethod
    def platform_of_jupiter(jupiter: str) -> str:
        """Jupiter ``jupiter``'s Platform version: 1.x.y for 5.x.y, else equal."""
        major, _, rest = jupiter.partition(".")
        return f"1.{rest}" if major == "5" else jupiter

    @staticmethod
    def family(platform: str) -> str:
        """The Jupiter and Vintage version that belongs to ``platform``."""
        major, _, rest = platform.partition(".")
        return f"5.{rest}" if major == "1" else platform

    @staticmethod
    def junit4_substitute(junit4: Optional[str]) -> Optional[str]:
        """The JUnit 4 a run uses in place of ``junit4``, or None: Vintage needs
        4.12, so an older one runs as 4.13.2, which runs its tests unchanged."""
        if junit4 and release_tuple(junit4) < (4, 12):
            return JUNIT4_FLOOR
        return None

    @staticmethod
    def framework_name(jupiter: Optional[str], junit4: bool) -> str:
        """``JUnit 4``, ``JUnit 5`` or ``JUnit 6`` by the test classpath's
        Jupiter version, else its JUnit 4, else JUnit 5."""
        if jupiter:
            return f"JUnit {jupiter.split('.')[0]}"
        return "JUnit 4" if junit4 else "JUnit 5"

    @staticmethod
    def framework_refusal(classpath: Sequence[str]) -> str:
        """Why no suite on the test ``classpath`` runs, or "": TestNG without
        JUnit, whose tests the launcher does not find."""
        testng = _jar_version(classpath, "testng")
        if testng is None or any(_jar_version(classpath, artifact) for artifact in _JUNIT_JARS):
            return ""
        return (f"TestNG is not supported: the test classpath has TestNG {testng} and no JUnit, "
                "and Java operations run JUnit 4, 5 and 6 tests")


#: What the Java adapter supports: PIT 1.30 and the release-11 helpers need
#: JDK 11 (the warmed helper targets 8). The capability report says "Java 21.0.11".
SUPPORT = LanguageSupport(
    runtime=RuntimeSupport(
        "JDK", tested=tuple(VersionSpan((n,), (n,)) for n in (11, 17, 21, 25, 26, 27)),
        expected=(VersionSpan((12,), (16,)), VersionSpan((18,), (20,)), VersionSpan((22,), (24,))),
        reason="PIT 1.30 and the helpers, compiled for release 11, need JDK 11"),
    wording={"java": Wording(capability="Java {text}", setup="{text}", facts="{text}")})
#: The unit Java's prune_collection_breakers removes.
COLLECTION_UNIT = ("the smallest declaration around a line javac reports an error on, or "
                   "around the line of a static initialization that throws: an import, or "
                   "a member of a type (a method, constructor, field, initializer or nested "
                   "type)")


class JavaProfile(JvmProfile):
    """The Java adapter's version answers: the JVM toolkit's, plus the
    context JDK's classification against the Java declaration."""

    def refusal(self) -> str:
        """Why no Java operation runs on this JDK, or "" when they run."""
        if self.support is not Support.UNSUPPORTED:
            return ""
        return (f"JDK {self.runtime.text} is not supported: Java operations need JDK "
                f"{self.declaration.runtime.describe()}, since {self.declaration.runtime.reason}")

    def note(self) -> str:
        """The setup check's note on a JDK outside the tested ones, or ""."""
        tested = ", ".join(span.text() for span in self.declaration.runtime.tested)
        if self.support is Support.EXPECTED:
            return f"JDK {self.runtime.text} is expected to work but is not among the tested JDKs ({tested})"
        if self.support is Support.UNTESTED and self.runtime is not None and self.runtime.numbers:
            return f"JDK {self.runtime.text} is not among the tested JDKs ({tested})"
        return ""

    @staticmethod
    def timeout_marker(framework: str) -> str:
        """The framework's syntax for a per-test timeout."""
        return "@Test(timeout = {seconds}000)" if framework == "JUnit 4" else "@Timeout({seconds})"
