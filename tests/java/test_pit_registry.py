"""PIT's registry at the pinned VERSION names the mutators and groups _pit
pins (MUTATORS, GROUPS), so a new VERSION shows a difference, and one
pit_mutators name is a group only when PIT's registry calls it one."""
from __future__ import annotations

import os
import subprocess

import pytest

import langadapter as lb
from langadapter.java import _pit
from langadapter.java.adapter import JavaAdapter
from langadapter.mutation import Catalogue

#: Each name PIT's registry knows, the number of mutators it selects and,
#: for one, that mutator's id.
LISTER = """\
import java.util.Collection;
import org.pitest.mutationtest.engine.gregor.MethodMutatorFactory;
import org.pitest.mutationtest.engine.gregor.config.Mutator;

public class ListRegistry {
    public static void main(String[] args) {
        for (String name : new java.util.TreeSet<>(Mutator.allMutatorIds())) {
            Collection<MethodMutatorFactory> found = Mutator.byName(name);
            System.out.println(name + "\\t" + found.size() + "\\t"
                    + (found.size() == 1 ? found.iterator().next().getGloballyUniqueId() : ""));
        }
    }
}
"""


def test_the_pinned_names_are_pit_s_registry(jdks, tmp_path):
    home = jdks[max(jdks)]
    java = home / "bin" / ("java.exe" if os.name == "nt" else "java")
    source = tmp_path / "ListRegistry.java"
    source.write_text(LISTER, encoding="utf-8")
    classpath = os.pathsep.join(str(path) for path in _pit.classpath(os.environ))
    done = subprocess.run([str(java), "-cp", classpath, str(source)], capture_output=True,
                          text=True, timeout=300, check=False)
    assert done.returncode == 0, done.stderr
    rows = [line.split("\t") for line in done.stdout.splitlines()]
    assert {name: ident for name, count, ident in rows if count == "1"} == _pit.MUTATORS
    assert tuple(name for name, count, _ in rows if count != "1") == _pit.GROUPS


@pytest.mark.parametrize("value, expected", [
    ("", Catalogue()),
    ("STRONGER", Catalogue(name="STRONGER")),
    ("REMOVE_CONDITIONALS", Catalogue(name="REMOVE_CONDITIONALS")),
    ("MATH", Catalogue(operators=("MATH",))),
    ("INVERT_NEGS", Catalogue(operators=("INVERT_NEGS",))),
    ("MATH, STRONGER", Catalogue(operators=("MATH", "STRONGER"))),
])
def test_one_pit_name_is_a_group_only_when_pit_calls_it_one(tmp_path, value, expected):
    adapter = JavaAdapter(lb.ProjectContext(str(tmp_path)))
    assert adapter.mutation_catalogue({"pit_mutators": value}) == expected
