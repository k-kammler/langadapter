# Commands

This page is generated from `langadapter.cli.parser`, the parser of the `langadapter`
command. `langadapter <command> --help` prints the same texts.

| Command | What |
| --- | --- |
| [`langadapter check`](#check) | Check a language adapter, for every consumer or for one (--for). |
| [`langadapter new-language`](#new-language) | Write a new language's file, with every hook commented out. |
| [`langadapter tutorial`](#tutorial) | Build an adapter for a tiny example language, step by step. |
| [`langadapter toolchains`](#toolchains) | Install the tools of the named languages, each proved by the check. |

| Option | What |
| --- | --- |
| `langadapter --version` | Print the installed LangAdapter version and exit. |

## `langadapter check` {#check}

Check a language adapter: which facts and hooks it provides, in the order of the reference page Hooks and facts, or by the levels of a consumer.

| Argument | What |
| --- | --- |
| `[language]` | A registered language. Without it, list the languages and consumers. |
| `--extension ITEM` | Load ITEM first, a file such as toy.py or an installed module that adds a language or a metric provider (repeatable). |
| `--for ITEM` | Check what a consumer needs: ITEM names it by its module, or names its requirement profile, a .json file (repeatable). |
| `--example DIR` | Run the language's suite on the test files in the directory DIR, found by its test file rule. |

## `langadapter new-language` {#new-language}

Write a new language as one file, LANGUAGE.py: its adapter with every fact and hook in the order of the reference page Hooks and facts, and the line that names it to LangAdapter. With --for, the hooks the consumer requires are marked TODO with its levels.

| Argument | What |
| --- | --- |
| `language` | The language's name, such as lua. |
| `--file-extension FILE_EXTENSION` | Its source file extension, such as .lua (required unless --diff or --update-record). |
| `--for ITEM` | Write it for a consumer: ITEM names it by its module, or names its requirement profile, a .json file (repeatable). |
| `--test-pattern PATTERN` | The test file pattern, such as \*\_test.lua, with one \* for the source file's stem. It gives the adapter's test\_file\_name (default test\_\*&lt;file extension>). |
| `--runtime PROGRAM` | The program that runs the tests, such as lua. Without it, the adapter marks it TODO. |
| `--grammar NAME` | A tree-sitter grammar for the toolkit, such as tree-sitter-lua. |
| `--runtime-name NAME` | The runtime's name in version messages (default: the --runtime value). |
| `--tested-runtime RANGE` | The tested runtime versions, such as 5.3-5.4. Without it, versions stay undeclared. |
| `--expected-runtime RANGE` | Versions expected to work but not tested (needs --tested-runtime). |
| `--output-dir DIR` | The directory to write LANGUAGE.py in (default: the current directory). With --diff or --update-record, the shared package's directory. |
| `--diff` | Compare the consumer versions recorded in a shared package's pyproject.toml with what the installed consumers require now, and write nothing. |
| `--update-record` | Record the installed consumers' versions in a shared package's pyproject.toml. |

## `langadapter tutorial` {#tutorial}

Build an adapter for the Toy, a tiny example language, step by step: run its tests, find and remove tests, measure coverage, run mutation testing, repair loading and declare versions, each step checked.

| Argument | What |
| --- | --- |
| `--for ITEM` | Also show what a consumer needs, from its installed requirement profile: ITEM names it by its module (repeatable). |
| `--output-dir OUTPUT_DIR` | A new or empty directory for the tutorial's files. |
| `--auto` | Play every step without questions, paced for watching. |
| `--speed SPEED` | With --auto: a factor of the pace, 1 by default (2 is twice as fast). |

Without --output-dir the files go to ./langadapter-tutorial, which must be new or empty.

## `langadapter toolchains` {#toolchains}

Install the tools of the named languages for this system, each language's part ending with langadapter check on a small example. The parts run on Ubuntu 24.04, also in WSL and in a container.

| Argument | What |
| --- | --- |
| `LANGUAGE [LANGUAGE ...]` | python, java, c, a language an extension adds, or all. |
| `--into DIR` | Install below DIR, by default the user's data directory (langadapter/toolchains there). |
| `--list` | List each part and what it downloads, and run none. |
| `--extension ITEM` | Load ITEM first, a file or an installed module that adds a language (repeatable). |

## `python -m langadapter.hooks --json` {#hooks-json}

This command belongs to the module `langadapter.hooks`, not to the `langadapter` command above, and this page alone describes it. It prints the hook table as JSON: its schema version, every family with its title, order, what it makes possible, what it is useful for and its guide, and every fact and hook with its fields, the table [Hooks and facts](hooks.md) renders. Other arguments, `--help` among them, print the usage and exit with status 2.
