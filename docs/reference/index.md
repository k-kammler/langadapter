# Reference

The reference is generated from the modules below and their docstrings.
Modules whose names start with an underscore are internal and not listed.
As files in the repository, the module pages hold only the module names
the site renders into their texts.
A name `langadapter` itself exports, such as `langadapter.Outcome`, has its
entry on the Core page, and the page of the module that defines it does
not render it again.
What users need from `langadapter.suites`, `langadapter.context`,
`langadapter.operations`, `langadapter.preparation` and
`langadapter.cancellation` is on the Core page. The packages
`langadapter.python`, `langadapter.java` and `langadapter.c` hold their
adapters' modules, which the Python, Java and C pages render. `langadapter.tutorial.language` and `langadapter.tutorial.example`
hold the Toy language that `langadapter tutorial` writes out and explains step
by step.
[Tested environments](tested-environments.md) lists the platforms, Python
versions and language tools this release was tested with.

| Page | Modules |
| --- | --- |
| [Core](core.md) | `langadapter`: contexts, suites, registration, setup checks and the operation groups (`SourceOperations`, `TestOperations`, `MutationOperations`), plus `langadapter.errors` and `langadapter.execution` |
| [Runs](runs.md) | `langadapter.runs`: requests and results of test operations, warmed suite timing included |
| [Outcomes and identity](outcomes.md) | `langadapter.outcomes`: outcomes, identities and observation records |
| [Setup and capabilities](setup.md) | `langadapter.setup`, `langadapter.capabilities` |
| [Mutation](mutation.md) | `langadapter.mutation`: requests, runs, sessions, per-suite runs, resolutions, evidence, outcomes and kill matrices |
| [Sources](sources.md) | `langadapter.sources`: source operations, pools and structural metrics |
| [Python](python.md) | the Python adapter, with Cosmic Ray settings and kill attribution (`langadapter.python.cosmic_ray`), pytest options, the directory of an interpreter environment inside the project, bytecode and suite setup (`langadapter.python.project`), and source analysis and repair (`langadapter.python.source_analysis`) |
| [Java](java.md) | the Java adapter and its source analysis |
| [C](c.md) | the C adapter (`langadapter.c.adapter`): `CAdapter` and its runner options `CRunOptions` |
| [Extending](extending.md) | `langadapter.adapter`, `langadapter.registry`, `langadapter.conformance`, `langadapter.check`, `langadapter.consumers`, `langadapter.versions`, `langadapter.vocabulary`, `langadapter.templates`, `langadapter.treesitter`, the hook table's module (`langadapter.hooks`), the command line (`langadapter.cli`) and the tutorial (`langadapter.tutorial`) |
| [Machine resources](resources.md) | `langadapter.resources`: the processors a process may run on, and a line of memory use and load for a progress log |
| [Timing placement](placement.md) | `langadapter.placement`: one benchmark at a time in a process, and the launcher of a timing process |
| [Hooks and facts](hooks.md) | `langadapter.hooks`: every fact and hook an adapter can provide, with what it enables and what to write |
| [Commands](cli.md) | `langadapter.cli.parser`: every command of `langadapter` with its arguments and options |
