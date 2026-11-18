"""Seed a project copy's compiled modules from a store, or harvest them into it.

Run by the project's own interpreter, whose compiled modules these are, with
the standard library and, to check pytest's rewritten test modules, pytest:
``<python> -I -c <this file's text> seed|harvest <store> <root> ...``, with a
store directory and a root for each part of the copy. It prints how many
modules it wrote as JSON.

A store entry is the code of one compiled module, at
``<store>/<directory below root>/<pyc name>/<magic number>-<source sha256>``. The
pyc name holds the interpreter's cache tag (and pytest's version for a
rewritten test module), so no other interpreter's entry is ever written into a
copy. A harvested module is stored only when its code equals a fresh compile
(or pytest rewrite) of the source beside it: a module compiled from other
source text with the same size and time stamp never enters the store.
"""
import hashlib
import importlib.util
import json
import marshal
import os
import sys

MAGIC = importlib.util.MAGIC_NUMBER
TAG = sys.implementation.cache_tag
PLAIN = (f"{TAG}.opt-{sys.flags.optimize}.pyc" if sys.flags.optimize else f"{TAG}.pyc")


def kind(name):
    """(module stem, rewritten by pytest) for a pyc name of this interpreter,
    else None."""
    stem, dot, rest = name.partition(".")
    if not dot or TAG is None:
        return None
    if rest == PLAIN:
        return stem, False
    if rest.startswith(TAG + "-pytest-") and rest.endswith(".pyc"):
        return stem, True
    return None


def stamp(source):
    """The time stamp and size words a timestamp pyc of ``source`` carries."""
    status = os.stat(source)
    return ((int(status.st_mtime) & 0xFFFFFFFF).to_bytes(4, "little")
            + (status.st_size & 0xFFFFFFFF).to_bytes(4, "little"))


def digest(path):
    with open(path, "rb") as stream:
        return hashlib.sha256(stream.read()).hexdigest()


def write(path, data):
    """Write ``path`` whole or not at all: concurrent writers of one entry
    write the same bytes, and a reader never sees a partial file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.tmp"
    try:
        with open(temporary, "wb") as stream:
            stream.write(data)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def located(code, filename):
    """``code`` as compiled at ``filename``: pytest keeps the path a test
    module was rewritten at, where the import machinery corrects it."""
    return code.replace(co_filename=filename, co_consts=tuple(
        located(value, filename) if isinstance(value, type(code)) else value
        for value in code.co_consts))


def rewritten(source):
    """pytest's rewrite of ``source``, None when pytest cannot give it."""
    try:
        from pathlib import Path

        from _pytest.assertion.rewrite import _rewrite_test
        return _rewrite_test(Path(source), None)[1]
    except Exception:
        return None


def harvest(store, root):
    stored = 0
    for current_dir, names, _ in os.walk(root):
        if "__pycache__" not in names:
            names[:] = [name for name in names if not name.startswith(".")]
            continue
        names[:] = [name for name in names if name != "__pycache__" and not name.startswith(".")]
        relative = os.path.relpath(current_dir, root)
        parts = [] if relative == os.curdir else relative.split(os.sep)
        cache = os.path.join(current_dir, "__pycache__")
        for name in sorted(os.listdir(cache)):
            found = kind(name)
            if found is None:
                continue
            stem, pytest = found
            source = os.path.join(current_dir, stem + ".py")
            try:
                with open(os.path.join(cache, name), "rb") as stream:
                    data = stream.read()
                if (len(data) < 16 or data[:4] != MAGIC or data[4:8] != bytes(4)
                        or data[8:16] != stamp(source)):
                    continue
                with open(source, "rb") as stream:
                    text = stream.read()
                entry = os.path.join(store, *parts, name,
                                     f"{MAGIC.hex()}-{hashlib.sha256(text).hexdigest()}")
                if os.path.exists(entry):
                    continue
                expected = (rewritten(source) if pytest
                            else compile(text, source, "exec", dont_inherit=True))
                if expected is None or marshal.loads(data[16:]) != expected:
                    continue
                write(entry, data[16:])
                stored += 1
            except (OSError, ValueError, EOFError, TypeError, SyntaxError):
                continue
    return stored


def seed(store, root):
    seeded = 0
    sums = {}
    for current_dir, names, _ in os.walk(store):
        relative = os.path.relpath(current_dir, store)
        parts = [] if relative == os.curdir else relative.split(os.sep)
        for name in [name for name in names if name.endswith(".pyc")]:
            names.remove(name)
            found = kind(name)
            if found is None:
                continue
            stem, pytest = found
            source = os.path.join(root, *parts, stem + ".py")
            target = os.path.join(root, *parts, "__pycache__", name)
            try:
                if not os.path.isfile(source) or os.path.exists(target):
                    continue
                if source not in sums:
                    sums[source] = digest(source)
                entry = os.path.join(current_dir, name, f"{MAGIC.hex()}-{sums[source]}")
                if not os.path.isfile(entry):
                    continue
                with open(entry, "rb") as stream:
                    body = stream.read()
                if pytest:
                    body = marshal.dumps(located(marshal.loads(body), source))
                write(target, MAGIC + bytes(4) + stamp(source) + body)
                seeded += 1
            except (OSError, ValueError, EOFError, TypeError, AttributeError):
                continue
    return seeded


if __name__ == "__main__":
    operation, *pairs = sys.argv[1:]
    step = seed if operation == "seed" else harvest
    count = sum(step(store, root) for store, root in zip(pairs[::2], pairs[1::2]))
    print(json.dumps({operation: count}))
