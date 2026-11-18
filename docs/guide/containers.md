# Containers

A Python or Java project can run inside a Linux container that you start
and run yourself. The library never creates, stops or removes the
container. C projects run locally only: the C adapter's operations run
nowhere but on the computer itself ([C projects](c.md)). Container execution
was tested for Python projects in Linux Docker containers, from Ubuntu 24.04
under WSL and not from Windows itself. Java projects in containers are not
tested. [Tested environments](../reference/tested-environments.md#containers)
gives the details.

## What running in a container needs

- a running container, and the program whose `exec` reaches it, which
  `ContainerExecution`'s `runner` names (`docker` by default)
- the project inside the container: the context's root is the project's path
  there, an absolute POSIX path, such as a directory the container mounts,
  and the interpreter's and the helper Python's paths are paths in the
  container too
- a writable work directory on the host, an absolute path, mounted into the
  container and outside the project directory
- a helper Python that you select explicitly: a Python 3.10 or newer in the
  container, which supervises every command. The project's own interpreter
  can serve, since the helper runs isolated and without site packages
- commands in the container that run as the host user, unless the work directory
  lies on a file system that presents every file as the host user's, such as
  a Windows drive mounted in WSL

To track and end the processes a command starts, the helper Python also needs
procfs child accounting and permission to make itself a subreaper. These are
checked in the container before the project's command starts. Where the
helper's Python and the kernel provide process descriptors (pidfds), the
helper signals through them. Otherwise it runs the command in a process group
and session of its own, and an observation's identity records the mode as
`process_group`. In that mode the command itself cannot start a new session
(`os.setsid()` fails with a permission error), which it can under pidfd
supervision. No helper Python is installed automatically.

The host reads the helper's reports and removes the copies of the project. It
can do neither for files that another user owns, so a container that runs as
root with its work directory on a Linux file system fails at its first command. Start
such a container as the host user, for example with the `docker run` option
`--user "$(id -u):$(id -g)"`.

```python
from langadapter import ContainerExecution, Interpreter, ProjectContext, prepare_project
from langadapter.execution import executor_for

context = ProjectContext(
    "/projects/example",  # in the container: the project's root
    Interpreter(path="/tools/python"),  # in the container: the project's interpreter
    execution=ContainerExecution(
        "existing-container",  # the running container's name
        "/host/workzone",  # on the host: the work directory, absolute
        container_work_dir="/workzone",  # in the container: where it is mounted
        helper_python="/control/python3",  # in the container: the helper Python
    ),
)
prepared = prepare_project(context)
with prepared.use() as scoped:
    result = executor_for(scoped).run(
        ["/tools/python", "-c", "print('ready')"], cwd=scoped.root, timeout=10
    )
prepared.close()
```

In a container built as [Install the toolchains](toolchains.md#in-a-container)
shows, Ubuntu's `/usr/bin/python3` can serve as the helper Python, and a
Python project's interpreter can be the python part's environment, whose
path [What each part installs](toolchains.md#what-each-part-installs)
gives, with `/opt/toolchains` as `DIR`.

`adapter.probe_capabilities()` starts the project's interpreter in the
container through the helper before any run, and reports missing setup when
the runner program, the container, the helper or a tool is missing.

A warmed Java benchmark runs on the project's own context, not inside a
prepared copy's use, as [Warmed Java timing](timing.md#warmed-java-timing)
explains.

## How commands run

The selected executor confirms that the helper Python is ready, that every
process the command started has ended and that the runner on the host has
been shut down. If any of these cannot be confirmed, cleanup is refused.
Commands never fall back to the host. With a helper Python selected, they
never run unsupervised. Without one, the executor runs each command through
the container runner's own `exec` without that supervision, and a context with
captured environment variables, Java operations and `prepare_project` refuse
to run that way.

Java uses the same supervised executor with the selected JDK and build
launcher in the container. The tool cache stays on the host, and each run
copies the jars it needs into the work directory. Maven or Gradle resolves the project as it
does locally, and JUnit, JaCoCo and PIT keep their own measurement
definitions. The helper Python does not instrument Java and does not run each
test in its own JVM. Without a helper Python, Java operations in a container
report missing setup instead of running on the host.

`operation_capabilities()` lists, without running a tool, where each
implemented operation can run: locally or in a container. For Java, an
absent helper Python is known missing setup, not missing Java support. Other
readiness stays unknown until it is checked. Project
resolution and each operation can still refuse missing tools, an unsupported
project or unsupported request controls. Suites on the host must be readable
through the declared mount. Tool paths in the container stay container paths
and are never replaced by host paths.

The setup check does not search a container for an interpreter. Select the
container's interpreter explicitly.
