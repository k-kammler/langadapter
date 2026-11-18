"""The Java adapter on the shared contract checks (langadapter.conformance),
with the fixture project built by Maven."""
from __future__ import annotations

import os

import pytest

import langadapter as lb
from langadapter.conformance import AdapterExample, check_adapter

BREAKER = """\
package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class CalcBreakerTest {
    @Test
    void positive() {
        assertEquals(1, Calc.sign(5));
    }

    @Test
    void decompiles() {
        assertEquals(1, Calc.decompile(5));
    }
}
"""


def test_the_java_adapter_keeps_the_contract(java_project, jdks, maven):
    if 21 not in jdks:
        pytest.skip("needs a JDK 21")
    project = java_project("maven")
    java = jdks[21] / "bin" / ("java.exe" if os.name == "nt" else "java")
    context = lb.ProjectContext(root=str(project), interpreter=lb.Interpreter(path=str(java)),
                                environment={"LANGADAPTER_MAVEN": maven})
    example = AdapterExample("java", context, lb.Suite(str(project / "src" / "test" / "java"),
                                                       ("demo/CalcTest.java",)),
                             coverage_target="src/main/java/demo/Calc.java", timeout_s=300,
                             collection_breaker=BREAKER)
    assert check_adapter(example) == ()
