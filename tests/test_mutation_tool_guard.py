"""The mutation tool guard (the suite split): which commands start
Cosmic Ray or PIT."""
import sys

import pytest

from tests.conftest import starts_mutation_tool


@pytest.mark.parametrize("command", [
    ["/env/bin/cosmic-ray", "exec", "cr.toml", "session.sqlite"],
    [r"C:\env\Scripts\cosmic-ray.exe", "init", "cr.toml", "session.sqlite"],
    [sys.executable, "-c", "from cosmic_ray.cli import main\nmain()", "exec", "cr.toml", "s"],
    [sys.executable, "-c", "sys.argv = ['cosmic-ray', 'http-worker', '--port', port]"],
    ["docker", "exec", "box", "python3", "/zone/_cr_session_runner.py", "spec.json"],
    ["/jdk/bin/java", "@/scratch/pit.args"],
    ["java", "-cp", "pit.jar", "org.pitest.mutationtest.commandline.MutationCoverageReport"],
])
def test_a_command_that_starts_cosmic_ray_or_pit_is_seen(command):
    assert starts_mutation_tool(command)


@pytest.mark.parametrize("command", [
    [sys.executable, "-c", "import importlib.util", "pytest", "coverage", "cosmic_ray"],
    [sys.executable, "-I", "-S", "-c", "NAMES = ('pytest', 'coverage', 'cosmic-ray')"],
    ["/jdk/bin/java", "-version"],
    ["mvn", "-q", "test-compile"],
])
def test_a_probe_that_only_names_a_tool_is_not(command):
    assert not starts_mutation_tool(command)
