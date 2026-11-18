"""Project discovery and report interpretation use the container's filesystem."""
import json
import os
from pathlib import Path
import subprocess

import pytest

from langadapter.execution import ContainerExecutor, work_dir_tempdir
from langadapter.context import ContainerExecution, Interpreter, ProjectContext
from langadapter.java import _env as jdk
from langadapter.java._build import build_tools, launch_argv, launcher, resolve, resolve_with
from langadapter.outcomes import Outcome

POM = '''<project xmlns="http://maven.apache.org/POM/4.0.0">
<artifactId>example</artifactId><properties><maven.compiler.release>11</maven.compiler.release>
</properties><build><sourceDirectory>/project/src/main/java</sourceDirectory>
<testSourceDirectory>/project/src/test/java</testSourceDirectory>
<outputDirectory>/project/target/classes</outputDirectory>
<testOutputDirectory>/project/target/test-classes</testOutputDirectory></build></project>'''


class ProjectContainer(ContainerExecutor):
    def __init__(self, zone, build="maven"):
        super().__init__("selected", work_dir=str(zone), container_work_dir="/workzone")
        self.calls = []
        self.files = ({"/project/pom.xml": POM, "/project/mvnw": "#!/bin/sh\n"}
                      if build == "maven" else {
                          "/project/build.gradle": "plugins { id 'java' }",
                          "/project/gradlew": "#!/bin/sh\n"})
        self.directories = {"/project/src/main/java", "/project/src/test/java"}

    def run(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv[0] == "test":
            present = (argv[2] in self.files if argv[1] == "-f"
                       else argv[2] in self.directories)
            return subprocess.CompletedProcess(argv, 0 if present else 1, "", "")
        if argv[0] == "cat":
            value = self.files.get(argv[-1])
            return subprocess.CompletedProcess(argv, 0 if value is not None else 1,
                                               value or "", "missing")
        if argv[:4] == launch_argv("/project/mvnw"):
            output = next(a.partition("=")[2] for a in argv if a.startswith("-Doutput="))
            classpath = next(a.partition("=")[2] for a in argv if a.startswith("-Dmdep.outputFile="))
            Path(output).write_text(POM, encoding="utf-8")
            Path(classpath).write_text("/opt/lib/a.jar:/opt/lib/b.jar", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "resolved", "")
        if argv[:4] == launch_argv("/project/gradlew"):
            output = next(a.partition("=")[2] for a in argv if a.startswith("-PlangadapterOut="))
            data = dict(projectDir="/project", mainSources=["/project/src/main/java"],
                        testSources=["/project/absent", "/project/src/test/java"],
                        mainClasses=["/project/build/classes/java/main"],
                        testClasses=["/project/build/classes/java/test"],
                        testClasspath=["/project/build/classes/java/main", "/opt/lib/a.jar"],
                        release=11, testRelease=17, jvmArgs=["-ea"],
                        systemProperties={"sample": "value"})
            (Path(output) / "root.json").write_text(json.dumps(data), encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "resolved", "")
        if argv[:2] == ["sh", "-c"]:
            return subprocess.CompletedProcess(argv, 0, "/opt/tools/selected\n", "")
        raise AssertionError(f"unexpected container command {argv}")


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_project_discovery_uses_container_files_and_posix_wrappers(tmp_path, build):
    executor = ProjectContainer(tmp_path, build)
    assert build_tools("/project", executor) == (build,)
    assert launcher("/project", build, {}, executor) == (
        "/project/mvnw" if build == "maven" else "/project/gradlew")
    assert all(".cmd" not in str(argv) and ".bat" not in str(argv)
               for argv, _ in executor.calls)


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_resolved_model_keeps_container_absolute_paths_and_classpath(tmp_path, build):
    executor = ProjectContainer(tmp_path, build)
    result = resolve("/project", jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                     executor, str(tmp_path / "reports"))
    assert result.outcome is Outcome.COMPLETED, result.reason
    model = result.project
    assert model.root == "/project"
    assert model.main_sources == ("/project/src/main/java",)
    assert model.test_sources == ("/project/src/test/java",)
    assert all("\\" not in path for path in model.run_classpath())
    assert "/opt/lib/a.jar" in model.run_classpath()
    if build == "maven":
        assert "/opt/lib/b.jar" in model.run_classpath()
        assert model.main_classes == ("/project/target/classes",)
    else:
        assert model.test_release == 17
        assert model.test_jvm_args == ("-ea",)
        assert model.test_system_properties == {"sample": "value"}


def test_container_maven_module_uses_ancestor_wrapper_and_reactor_selection(tmp_path):
    executor = ProjectContainer(tmp_path)
    executor.files["/project/pom.xml"] = '''<project xmlns="http://maven.apache.org/POM/4.0.0">
    <packaging>pom</packaging><modules><module>core</module></modules></project>'''
    executor.files["/project/core/pom.xml"] = POM
    assert launcher("/project/core", "maven", {}, executor) == "/project/mvnw"
    result = resolve("/project/core", jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                     executor, str(tmp_path / "reports"))
    assert result.outcome is Outcome.COMPLETED, result.reason
    command, options = next((argv, options) for argv, options in executor.calls
                            if argv[:4] == launch_argv("/project/mvnw"))
    assert command[command.index("-f") + 1] == "/project/pom.xml"
    assert command[command.index("-pl") + 1] == "core"
    assert "-am" in command
    assert options["cwd"] == "/project"


def test_container_missing_wrapper_uses_only_selected_path(tmp_path, monkeypatch):
    executor = ProjectContainer(tmp_path)
    executor.files.pop("/project/mvnw")
    monkeypatch.setenv("PATH", "host-path-is-not-container-path")
    assert launcher("/project", "maven", {"PATH": "/selected/bin"}, executor) == "/opt/tools/selected"
    argv, options = executor.calls[-1]
    assert argv[-1] == "mvn"
    assert options["env"] == {"PATH": "/selected/bin"}


def test_container_reports_refuse_unmounted_host_output_before_discovery(tmp_path):
    zone = tmp_path / "zone"
    zone.mkdir()
    executor = ProjectContainer(zone)
    output = tmp_path / "foreign-output"
    result = resolve("/project", jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                     executor, str(output))
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "mounted host work folder" in result.reason
    assert not output.exists()
    assert not executor.calls


def test_container_discovery_failure_is_not_missing_setup(tmp_path, monkeypatch):
    executor = ProjectContainer(tmp_path)
    monkeypatch.setattr(executor, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 2, "", "container unavailable"))
    result = resolve("/project", jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                     executor, str(tmp_path / "reports"))
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "container unavailable" in result.reason


def test_container_launcher_lookup_failure_is_not_missing_setup(tmp_path, monkeypatch):
    executor = ProjectContainer(tmp_path)
    executor.files.pop("/project/mvnw")
    original = executor.run

    def failed_lookup(argv, **kwargs):
        if argv[0] == "sh":
            return subprocess.CompletedProcess(argv, 2, "", "lookup did not run")
        return original(argv, **kwargs)

    monkeypatch.setattr(executor, "run", failed_lookup)
    result = resolve("/project", jdk._ContainerJdk("/opt/jdk", 21, "21.0.7"),
                     executor, str(tmp_path / "reports"))
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "lookup did not run" in result.reason


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_report_directory_failure_is_an_execution_failure(tmp_path, build):
    blocked = tmp_path / "blocked"
    blocked.write_text("preserved", encoding="ascii")
    executor = ProjectContainer(tmp_path, build)
    result = resolve_with(build, "/project", "unused", {}, executor,
                          str(blocked / "reports"), 60)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert "report directory" in result.reason
    assert blocked.read_text(encoding="ascii") == "preserved"
    assert not executor.calls


@pytest.mark.parametrize("build, tool", [("maven", "Maven"), ("gradle", "Gradle")])
def test_a_build_tool_that_exits_with_an_error_names_its_exit_status(tmp_path, build, tool):
    """The resolution's reason names the build tool and its exit status, and
    the build's output stays with the result."""
    executor = ProjectContainer(tmp_path, build)
    wrapper = "/project/mvnw" if build == "maven" else "/project/gradlew"
    original = executor.run

    def failing(argv, **kwargs):
        if argv[:4] == launch_argv(wrapper):
            return subprocess.CompletedProcess(argv, 3, "build output", "")
        return original(argv, **kwargs)

    executor.run = failing
    result = resolve_with(build, "/project", wrapper, {}, executor,
                          str(tmp_path / "reports"), 60)
    assert result.outcome is Outcome.INFRASTRUCTURE_FAILURE
    assert result.reason == f"{tool} exited 3"
    assert "build output" in result.output


@pytest.mark.parametrize("build", ["maven", "gradle"])
def test_real_docker_build_reports_preserve_container_model(build):
    container = os.environ.get("LANGADAPTER_TEST_JAVA_CONTAINER")
    zone = os.environ.get("LANGADAPTER_TEST_CONTAINER_WORK_DIR")
    named = os.environ.get(f"LANGADAPTER_TEST_CONTAINER_{build.upper()}")
    if not (container and zone and named):
        pytest.skip("needs an explicit offline container build tool and mounted work directory")
    context = ProjectContext(
        root="/workzone", interpreter=Interpreter(path="/opt/jdk/bin/java"),
        execution=ContainerExecution(container, work_dir=zone, container_work_dir="/workzone"))
    executor = ContainerExecutor(container, work_dir=zone, container_work_dir="/workzone",
                                 forward_keys=("JAVA_HOME", "PATH", "GRADLE_USER_HOME"))
    runtime = jdk.read_in_container("/opt/jdk/bin/java", executor)
    assert runtime is not None
    with work_dir_tempdir(context, executor, prefix=f"java_{build}_") as directory:
        root = Path(directory) / "project"
        source = root / "src" / "main" / "java" / "demo"
        source.mkdir(parents=True)
        (source / "Calc.java").write_text(
            "package demo; public class Calc { public static int add(int a, int b) { "
            "return a + b; } }", encoding="ascii")
        (root / "src" / "test" / "java").mkdir(parents=True)
        if build == "maven":
            (root / "pom.xml").write_text('''<project xmlns="http://maven.apache.org/POM/4.0.0">
<modelVersion>4.0.0</modelVersion><groupId>demo</groupId><artifactId>fixture</artifactId>
<version>1.0</version><properties><maven.compiler.release>11</maven.compiler.release>
<project.build.sourceEncoding>UTF-8</project.build.sourceEncoding></properties>
<dependencies><dependency><groupId>org.junit.jupiter</groupId><artifactId>junit-jupiter</artifactId>
<version>5.14.4</version><scope>test</scope></dependency></dependencies>
<build><plugins><plugin><groupId>org.apache.maven.plugins</groupId>
<artifactId>maven-compiler-plugin</artifactId><version>3.15.0</version>
</plugin></plugins></build></project>''', encoding="utf-8")
            wrapper = root / "mvnw"
            wrapper.write_text(f'#!/bin/sh\nexec "{named}" -o -Dmaven.repo.local=/opt/m2 "$@"\n',
                               encoding="utf-8", newline="\n")
        else:
            (root / "settings.gradle").write_text("rootProject.name = 'fixture'", encoding="ascii")
            (root / "build.gradle").write_text(
                "plugins { id 'java' }\ntasks.withType(JavaCompile).configureEach { "
                "options.release = 11 }\n", encoding="ascii")
            wrapper = root / "gradlew"
            wrapper.write_text(f'#!/bin/sh\nexec "{named}" --offline "$@"\n',
                               encoding="utf-8", newline="\n")
        ready = executor.run(["chmod", "+x", str(wrapper)], timeout=60)
        assert ready.returncode == 0, ready.stderr
        result = resolve(str(root), runtime, executor, str(Path(directory) / "reports"),
                         environment={"GRADLE_USER_HOME": str(Path(directory) / "gradle-cache")})
        assert result.outcome is Outcome.COMPLETED, result.reason + "\n" + result.output
        container_root = executor._to_container(str(root))
        assert result.project.root == container_root
        assert result.project.main_sources == (f"{container_root}/src/main/java",)
        assert result.project.test_sources == (f"{container_root}/src/test/java",)
        assert result.project.release == 11
        if build == "maven":
            assert any(path.startswith("/opt/m2/") for path in result.project.test_classpath)
            output = root / "target" / "classes" / "demo" / "Calc.class"
        else:
            output = root / "build" / "classes" / "java" / "main" / "demo" / "Calc.class"
        assert output.is_file()
