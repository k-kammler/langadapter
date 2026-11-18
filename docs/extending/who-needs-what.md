# Who needs what

A language for LangAdapter can do many things: run tests, measure coverage,
run mutation testing, repair a test file that does not load, and more. Each
group of these is a family. Not every program needs every family. This page
explains how a program says what it needs, and how LangAdapter shows you
what your language still lacks for the programs you use.
[What Testuary requires](consumer-requirements.md) states, for Testuary,
where each family is used and what Testuary does without it.

## Two words

A **consumer** is a program that uses LangAdapter, such as Testuary.

A consumer's **profile** is a small file in the consumer's package that says,
family by family, what it needs from a language. A profile names families,
never single methods, so it stays valid when a family's methods change.

## Three tiers

A profile gives each family one of three tiers:

- **Required**: the consumer cannot run without it. Your language must
  provide it before the consumer works with it.
- **Optional**: the consumer uses it when it is there. The profile says what
  it improves, for example the quality of the results or the time a run
  takes, and what the consumer does without it.
- **Not used**: the consumer never calls it. It can still be useful in
  general, and the check says what for. A family a profile does not name is
  not used by that consumer.

## How requirements add up

A consumer publishes only its own needs. If it builds on another consumer,
that one publishes its own needs too. LangAdapter follows the packages you
have installed from the consumer you name to everything it depends on, reads
each profile it finds, and combines them family by family. The strictest
tier wins: required over optional, optional over not used. Here `examplegen` is
a made-up program built on Testuary:

```text
  you run: langadapter check <language> --extension <file> --for examplegen

  examplegen      profile: what examplegen itself needs
    | depends on
  Testuary        profile: what Testuary itself needs
    | depends on
  LangAdapter     combines both profiles, family by family:
                  the strictest tier wins

  result, for each family:
    run tests         required   (required by Testuary, via examplegen)
    load repair       optional   (optional for Testuary, via examplegen)
    metric providers  not used
```

Every requirement shows where it comes from, as in "required by Testuary,
via examplegen". When two profiles give a family different tiers, the check lists
both sources, and the level the family is listed under shows the tier that
wins. LangAdapter reads the profiles from the installed
packages' files and never imports a consumer to do so.

A consumer that is not installed has no profile here. Then the check says
so and suggests installing it, for example pip install <consumer>, rather
than guessing.

## One installation serves every consumer

You install your language's package once in a Python environment, and every
consumer there can use it: each one loads it from the module its own settings
name, such as Testuary's `[main] extensions`. Testuary's settings can also
name your file in place of the module, such as `extensions = javascript.py`,
and its [Run settings](https://k-kammler.github.io/testuary/guide/configuration/#run-settings)
say where a relative path starts. Installing the package registers
nothing by itself. Another environment needs its own installation, as with
any Python package.

The check shows each consumer's needs separately:

```text
langadapter check javascript --extension langadapter_javascript --for testuary
langadapter check javascript --extension langadapter_javascript --for examplegen
```

Without `--for`, `langadapter check javascript --extension langadapter_javascript` lists every family in its
usual order, and adds one line per installed consumer with its version and
state, such as "Testuary 0.2.0: level 0 of 3 complete, 2 required families
missing."

## A worked example

Three packages form a chain: `examplereport` uses `examplegen`, and
`examplegen` uses Testuary. The first two are made up, and the example
shortens Testuary's profile to two of its levels.

- Testuary's profile, so shortened, requires running tests, coverage and
  mutation, and uses load repair when it is there.
- `examplegen` adds one need of its own: it requires timing.
- `examplereport` adds one need of its own: it uses structural metrics when they
  are there.

Neither `examplegen` nor `examplereport` repeats Testuary's needs.
`langadapter check python --for examplereport` combines all three. LangAdapter's
Python adapter has every family the three use, so its report includes these
lines:

```text
Testuary level 1: Run, coverage, mutation
  hook run_suite (Run the tests): present, required by Testuary, via examplegen, via examplereport. Guide: add-a-language.md#run-the-tests
  hook open_mutation_session (Run mutation testing): present, required by Testuary, via examplegen, via examplereport. Guide: add-a-language.md#run-mutation-testing
examplegen level 1: Timing
  hook benchmark (Time a suite): present, required by examplegen, via examplereport. Guide: add-a-language.md#timing
examplereport level 1: Metrics
  hook structural_metrics (Structural metrics): present, optional for examplereport. Guide: add-a-language.md#structural-metrics
Testuary level 2: Load repair
  hook prune_collection_breakers (Repair loading): present, optional for Testuary, via examplegen, via examplereport. Guide: add-a-language.md#repair-loading
Not used:
  Merge test files (merge_sources): not needed by examplereport. Useful for combining suites into one. Guide: add-a-language.md#merge-test-files
```

Each level carries the name of the program whose level it is, and each line
says which program needs the hook and through which packages, in the form
[The check](add-a-language.md#the-check) describes.

A chain of any length works the same way.

## Publish your package's requirements

This section is for authors of consumers: programs that use LangAdapter and
need families from a language.

Write the profile as a JSON file inside your package,
`mytool/langadapter_profile.json`:

```json
{
  "schema": 1,
  "consumer": "mytool",
  "title": "mytool",
  "levels": [
    {"level": 1, "title": "Timing",
     "families": [
       {"family": "timing", "tier": "required"}]},
    {"level": 2, "title": "Faster runs",
     "families": [
       {"family": "per-test evidence", "tier": "optional",
        "improves": "measures only the tests a change affects",
        "fallback": "every run measures the whole suite"}]}
  ]
}
```

- `schema` is the version of this format. A profile in a newer format than
  the installed LangAdapter knows is reported, not guessed.
- `title` is your program's name in the check's lines, by default `consumer`.
- `tutorial`, optional, gives the closing step of `langadapter tutorial --for`:
  a `title`, its `text` as a list of paragraphs, the `command` as a list of
  words and `install`, how to install the command.
- `levels` groups your families in the order a language author should add
  them. Levels are yours to define.
- Each family's name, as `family` takes it, is on
  [Hooks and facts](../reference/hooks.md) under its heading.
- Each family has a `tier`. An optional family also says what it `improves`
  and the `fallback` your program uses without it. Write both as short
  lower-case phrases without a final period: `improves` follows "where it",
  as in "where it measures only the tests a change affects", and `fallback`
  follows "Without it,".
- LangAdapter reads `schema`, `consumer`, `title`, `levels` and `tutorial`,
  and ignores any other key.

Save the file as `langadapter_profile.json` in your package's directory, beside
its `__init__.py`, and ship it as package data. Your program's name as a
consumer is its module's name, such as `mytool`, and the profile's
`consumer` field must say the same. LangAdapter finds the file without
importing your program and never loads it as code: in the file list of the
installed distribution, in the source directory of an editable install, and
otherwise in the directory Python would import your package from, a package of
your distribution's own name or one its `top_level.txt` lists.

Name only your own needs. The needs of the consumers you depend on come
from their profiles, through your package's dependencies.

Before you publish, check a profile file directly:

```text
langadapter check javascript --extension javascript.py --for mytool/langadapter_profile.json
```

A profile read this way has no installed package, so the needs of its
dependencies are not added, and the report's notes say so. After the facts,
the report lists your levels and the families your profile does not use.
`Ready:` then says whether everything the profile requires is there, as
[The check](add-a-language.md#the-check) describes. For a language without
timing it reads, shortened:

```text
For mytool:
mytool level 1: Timing
  hook benchmark (Time a suite): missing, required by mytool. Run a suite repeatedly and return its times, warmed where the runtime warms up. Guide: add-a-language.md#timing
mytool level 2: Faster runs
  hook per_test_evidence (Collect per-test evidence): present, optional for mytool. Guide: add-a-language.md#per-test-evidence
Not used:
  Merge test files (merge_sources): not needed by mytool. Useful for combining suites into one. Guide: add-a-language.md#merge-test-files
Ready: no.
Notes:
  the requirement profile mytool/langadapter_profile.json is used alone: the requirements of the tools it builds on are not added, since it is not an installed package
Summary: 20 present, 29 default used, 19 missing (1 required by mytool), 2 not checked.
```
