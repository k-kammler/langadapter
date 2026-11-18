"""Standard-library selected copying, also executable by the container's helper Python."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import stat
import sys


class LinkRefused(OSError):
    """A selected project input is a link, which a copy never follows."""


def ordinary(path):
    identity = path.lstat()
    if (stat.S_ISLNK(identity.st_mode) or getattr(identity, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise LinkRefused(f"selected project input cannot be a link: {path}")
    if not (stat.S_ISDIR(identity.st_mode) or stat.S_ISREG(identity.st_mode)):
        raise OSError(f"selected project input must be an ordinary entry: {path}")
    return identity


def derived(name):
    """Whether an entry is Python's bytecode cache, which a copy leaves out:
    pytest's rewritten test modules keep the original file's name, so a copied
    cache would make the copy's tests report under the original's paths."""
    return name == "__pycache__" or name.endswith((".pyc", ".pyo"))


def tree(source, plan, *, destination=None, case_sensitive=True, checkpoint=None):
    """Apply the already validated explicit plan, rechecking each selected entry."""
    def check():
        if checkpoint is not None:
            checkpoint()

    check()
    source, destination = Path(source), None if destination is None else Path(destination)
    key = (lambda value: value) if case_sensitive else str.casefold
    excluded = tuple(key(value) for value in plan["exclude"])
    if not stat.S_ISDIR(ordinary(source).st_mode):
        raise OSError(f"selected project source is not a directory: {source}")
    for required in plan["required"]:
        selected = source
        for part in required.split("/"):
            check()
            selected = selected / part
            ordinary(selected)

    def walk(directory, target, relative):
        check()
        if not stat.S_ISDIR(ordinary(directory).st_mode):
            raise OSError(f"selected directory changed before enumeration: {directory}")
        if target is not None:
            target.mkdir()
        with os.scandir(directory) as entries:
            for entry in entries:
                check()
                name = f"{relative}/{entry.name}" if relative else entry.name
                named = key(name)
                if any(named == value or named.startswith(value + "/") for value in excluded):
                    continue
                if derived(entry.name):
                    continue
                src, dst = directory / entry.name, None if target is None else target / entry.name
                identity = ordinary(src)
                if stat.S_ISDIR(identity.st_mode):
                    walk(src, dst, name)
                elif dst is not None:
                    shutil.copy2(src, dst, follow_symlinks=False)
                    check()
                    ordinary(dst)
        if target is not None:
            shutil.copystat(directory, target, follow_symlinks=False)
        check()

    walk(source, destination, "")


if __name__ == "__main__":
    selected = json.loads(sys.stdin.read())
    if sys.argv[1] not in ("inspect", "copy"):
        raise ValueError("unknown selected-copy operation")
    tree(sys.argv[2], selected, destination=sys.argv[3] if sys.argv[1] == "copy" else None)
