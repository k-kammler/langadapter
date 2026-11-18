# Extension points

LangAdapter takes these additions without a change to the library. Each has
one guide.

| Extension point | What you write | How a program finds it | Guide |
| --- | --- | --- | --- |
| A language | one file, `<language>.py`, with its adapter class and `langadapter_language = <Class>`, or a module of an installed package that names its class the same way | `--extension ITEM` of `langadapter check` and `langadapter toolchains`, `langadapter.load_extension`, or a consumer's setting, each taking the file or the module's name | [Add a language](add-a-language.md) |
| A metric provider | one file with a provider object and `langadapter_metric_provider = <object>` | `--extension FILE` of `langadapter check`, `langadapter.load_extension`, or a consumer's setting | [A metric provider](metric-provider.md) |
| A metric provider on one adapter | `adapter.sources.register_metric_provider(provider)` in a consumer's own code | that adapter object only | [A metric provider](metric-provider.md#the-contract) |
| A language's own metric providers | the adapter class's `metric_providers()` | every adapter of the language | [A metric provider](metric-provider.md#the-contract) |
| A toolchain part | in the language's file, `langadapter_toolchain = {"<system>": "<shell text>"}` | `langadapter toolchains LANGUAGE --extension FILE` | [Install the toolchains](../guide/toolchains.md#a-language-from-an-extension) |
| A consumer's requirements | `langadapter_profile.json` in the consumer's package | the consumer's module name, or the file, as `--for` takes them | [Who needs what](who-needs-what.md#publish-your-packages-requirements) |
| A language registered in code | `langadapter.register(name, file_extensions, factory)` in a consumer's own code | the consumer that calls it | [Below](#a-language-registered-in-code) |
| A faster per-suite mutation run | a mutation session's `per_suite_fast(request)` | the session | [Add a language](add-a-language.md#run-mutation-testing) and [The conformance check](conformance.md#what-check_per_suite-checks) |
| A mutation engine | the adapter's `native_mutation_engines`, `open_mutation_session(engine, ...)`, which returns a session, and `mutation_operators(engine)` | the adapter | [Add a language](add-a-language.md#run-mutation-testing) |

A file names its additions with the three top-level names
`langadapter_language`, `langadapter_metric_provider` and
`langadapter_toolchain`, and LangAdapter reads them without any installation
or registration step. A toolchain part belongs to the language the same file
names. LangAdapter has no settings file of its own: a setting that names an
extension file is a consumer's, such as Testuary's, and the consumer loads
the file with `langadapter.load_extension`.

## A language registered in code

A consumer that ships its language in its own package can register it from
its own code instead of a file:

```python
import langadapter as la

la.register("toy", [".toy"], ToyAdapter)
adapter = la.create("toy", la.ProjectContext("path/to/project"))
la.unregister("toy")
```

The factory is what `create` calls with the project's context, usually the
adapter class. A language name or file extension another registration holds
raises `RegistrationConflict`, unless the call passes `replace=True`.
`langadapter.unregister(name)` removes the registration again. The
[`register`](https://k-kammler.github.io/langadapter/reference/core/#langadapter.register)
entry gives its arguments.
