# Contributing

This note is for working on the library in its source repository. It is not
part of the installed package.

## Tests and checks

Install the library with its `dev` extra (the C adapter's dependencies,
pytest with its plugins, coverage, ruff and build)
from the repository directory:

```
python -m pip install -e ".[dev]"
```

The tests run with `python -m pytest` in the repository directory, and
`python -m ruff check src tests` passes before each change is committed.

## The documentation site

The guides in `docs/` and the API reference, which mkdocstrings builds from
the library's docstrings, make up a documentation site built with MkDocs.
Install the `docs` extra into an isolated environment, then preview or build
the site from the repository directory:

```
python -m pip install ".[docs]"
mkdocs serve
mkdocs build
```
