"""The test vocabulary: a language's words for its tests.

``LangAdapter.test_vocabulary()`` maps the entries below to texts for the
adapter's project (Java's words differ by JUnit version). A caller inserts
them where it describes the language's tests and builds plain sentences from
the other facts for entries a language leaves out. Every entry is optional.
Where or whether one is shown is the caller's choice. A language changes its
texts only on purpose, since they shape what is written from them.
"""
from __future__ import annotations

from typing import Any, Mapping, Tuple

#: Each entry's name and what its text says, with an example from Java.
ENTRIES: Tuple[Tuple[str, str], ...] = (
    ("valid_source", "Source the language accepts, as a phrase, such as valid Java."),
    ("real_source", "The language's real source, as a phrase, such as real Java."),
    ("test_declaration", "How a test is written, in code, such as ``@Test void name()``."),
    ("test_declaration_full",
     "A test as a noun phrase with its import, such as ``@Test`` method "
     "(``org.junit.jupiter.api.Test``)."),
    ("collection_rule", "Which tests the framework runs, as a sentence."),
    ("fixture_term", "The word for shared setup, such as lifecycle methods."),
    ("parametrize_term",
     "The word for a test repeated over inputs, such as ``@ParameterizedTest``."),
    ("single_file_rule", "The rule for writing one test file, as sentences."),
    ("test_file_form",
     "What a complete test file holds beyond its tests' own entries, as a list, or empty "
     "where the language's other entries say it."),
    ("test_file",
     "A test file's name for a target, with a placeholder, such as "
     "``<TargetClass>Test.java``."),
    ("import_rule",
     "How a test reaches its target, as a verb phrase, such as uses the target class "
     "directly."),
    ("load_error", "The word for a test file that does not load, such as compilation error."),
    ("fake_builtin_rule",
     "How a test fakes a built-in function, as rule lines, or empty where the language "
     "has none."),
    ("exception_assertion",
     "The word for asserting that a call fails, such as ``assertThrows``."),
    ("timeout_marker",
     "A per-test timeout in code, with {seconds} in place of the value, such as "
     "@Timeout({seconds})."),
    ("parse_error_hints", "The usual causes of a parse error, as a list in words."),
    ("test_dependencies",
     "What a test may use, as lines: the test toolkit and the project's own code."),
    ("validity_rule",
     "What a test may and may not do with its target under the language's validity "
     "policy, as sentences, or empty where the language has no such policy."),
    ("stands_alone_advice",
     "What to do when a test file passes in the measured run of the suite but not "
     "on its own, as a numbered list."),
    ("load_error_patterns",
     "The usual causes of a test file that does not load and their fixes, as a "
     "list."),
    ("coverage_counts_inconsistent_cause",
     "The usual cause of coverage counts that cannot be read, as a sentence, or empty "
     "where the language's coverage reports no such counts or no usual cause is known."),
    ("coverage_counts_inconsistent_fix",
     "What to change in a test whose coverage counts cannot be read, as a sentence, or "
     "empty where the language's coverage reports no such counts or no usual cause is "
     "known."),
)
#: The entry names, in the order above.
NAMES: Tuple[str, ...] = tuple(name for name, _ in ENTRIES)


def problems(vocabulary: Mapping[str, Any]) -> Tuple[str, ...]:
    """Why ``vocabulary`` is not a test vocabulary: unknown entries, non-string texts."""
    found = []
    for name, text in vocabulary.items():
        if name not in NAMES:
            found.append(f"{name!r} is not a test vocabulary entry")
        elif not isinstance(text, str):
            found.append(f"the entry {name} is {type(text).__name__}, not text")
    return tuple(found)


__all__ = ["ENTRIES", "NAMES", "problems"]
