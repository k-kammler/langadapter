# How the parts fit

A program such as Testuary reaches a language's tools through the layers
below, and [Words](#words) defines the terms the guides use.

## The layers

```text
  consumers       Testuary, and programs built on it
                  each publishes a profile: what it needs, family by family
      |
      | call LangAdapter's public operations: run a suite, measure
      | coverage, run mutation testing, repair a test file, ...
      v
  LangAdapter     the base class checks each request once for every
  base class      language, answers what an adapter lacks and removes a
                  test by its test_units. The shared loops (templates) that
                  repair a test file that does not load, repair syntax and
                  remove failing imports are there for your hooks to call
      |
      | calls the adapter's hooks
      v
  adapters        Python, Java and C, which come with LangAdapter, and yours
                  each declares its facts and implements its hooks. The
                  tree-sitter units (find tests, imports and the code
                  around a line from a grammar) serve every adapter whose
                  language has a grammar
      |
      | run the language's tools
      v
  the language's  its test runner, coverage tool and mutation tool
  own tools
```

A consumer asks for operations, and reads facts and calls some hooks
itself, such as `run_counts`. It never calls a language's tools directly, so
it needs no code per language. An adapter provides only its language's
specifics and calls the shared loops for the rest.

## Words

The steps of the guide use four words:

- **fact**: something your adapter declares about its language, such as how
  test files are named. Facts have defaults.
- **hook**: a method or attribute of your adapter that LangAdapter or a
  consumer calls or reads, such as `run_suite`.
- **family**: a group of hooks that together give one capability, such as
  coverage or load repair.
- **fallback**: what a consumer does when an optional family is missing.

[Who needs what](who-needs-what.md) defines two more: consumer and profile.

The rest appear in the reference and in messages:

- **operation**: what a consumer asks of LangAdapter, such as running a
  suite. An operation is built on one or more hooks.
- **capability**: whether an operation, or an entry of the capability
  report such as coverage, works for a project: available, unsupported, or
  missing its setup, with the reason.
- **template**: a shared loop in `langadapter.templates` that your hook calls
  with its own steps, such as the loop that repairs a test file that does
  not load.
- **toolkit**: a helper an adapter uses for common work, such as reading
  tests from a tree-sitter grammar.
- **level**: a group of families in a consumer's profile, in the order the
  consumer suggests adding them. Levels belong to a consumer, not to
  LangAdapter.
- **fallbacks**: the record a consumer's run keeps when it applied a fallback.

## Where things live

A newcomer can read LangAdapter's package in this order:

```text
langadapter/
  adapter.py        the base class: facts and hooks
  operations.py     the operation groups, built on the hooks
  hooks.py          every fact, hook and family, with what it enables
  consumers.py      the consumers' profiles, read from installed packages
  check.py          the check: neutral, or for a consumer
  templates.py      the shared loops
  treesitter.py     tree-sitter units
  tutorial/         langadapter tutorial and its example language
  cli.py            the langadapter command and the new-language scaffold
  python/, java/,   the three adapters that come with LangAdapter, java/ with
  c/                the JDK, build, JUnit, JaCoCo and PIT machinery
```

The Python, Java and C adapters are worked examples of most families. None
has every one: `langadapter check python`, `langadapter check java` and
`langadapter check c` show which they leave out.
