"""Environment values that name the project being prepared. A value is
admitted only when every path in it that names the project lies inside an
environment directory of the project (one holding ``pyvenv.cfg`` or
``conda-meta``), and nothing in that environment's site directories leads into
the project outside that directory. The check reads the site directories without
running the environment. An admitted directory is left out of the copy, as the
runtime it is. Any other value that names the project is refused."""
from __future__ import annotations

import ast
import glob
import json
import os
import re
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple
from urllib.parse import urlparse
from urllib.request import url2pathname

from langadapter.context import ProjectInputSelection
from langadapter.errors import InvalidRequest

#: Where the caller's shell is (PWD) and was before its last cd (OLDPWD):
#: the caller's place, not the project's. A value inside the project does
#: not stop the preparation.
_SHELL_LOCATION = ("PWD", "OLDPWD")
#: The characters that end a path in an environment value.
_PATH_END = re.compile(r"[\s\"'," + re.escape(os.pathsep) + "]")


def admitted(environment, patterns, roots: Sequence[str], container: bool) -> Tuple[str, ...]:
    """The environment directories that ``environment``'s values name, each
    checked, after refusing a value that names the project (``patterns``
    find its root, ``roots`` spells it) outside such a directory."""
    env_dirs: list = []
    for name, value in environment.items():
        if (name.upper() if os.name == "nt" else name) in _SHELL_LOCATION:
            continue
        text = value.replace("\\", "/")
        text = text.casefold() if os.name == "nt" else text
        starts = [found.start() for pattern in patterns for found in pattern.finditer(text)]
        if not starts:
            continue
        named = {None} if container else {
            _environment_dir(_PATH_END.split(text[start:], 1)[0], roots) for start in starts}
        if None in named:
            raise InvalidRequest("environment",
                                 f"{name} refers to the original project; configure the copied root explicitly")
        for env_dir in sorted(named):
            _check(name, env_dir, roots)
            if env_dir not in env_dirs:
                env_dirs.append(env_dir)
    return tuple(env_dirs)


def left_out(inputs: Optional[ProjectInputSelection], env_dirs: Sequence[str],
             source: Path) -> ProjectInputSelection:
    """``inputs`` with the admitted environment ``env_dirs`` left out of the copy."""
    excluded = tuple(Path(os.path.relpath(os.path.realpath(env_dir), source)).as_posix()
                     for env_dir in env_dirs)
    if inputs is None:
        return ProjectInputSelection(exclude=excluded)
    return ProjectInputSelection(exclude=(*inputs.exclude, *excluded), required=inputs.required)


def _environment_dir(path: str, roots: Sequence[str]) -> Optional[str]:
    """The environment directory inside the project that ``path`` lies in, else
    None, also when ``path`` resolves into the project outside that directory."""
    tops = {os.path.normcase(os.path.abspath(root)) for root in roots}
    current = os.path.abspath(path)
    while os.path.normcase(current) not in tops and os.path.dirname(current) != current:
        if (os.path.isfile(os.path.join(current, "pyvenv.cfg"))
                or os.path.isdir(os.path.join(current, "conda-meta"))):
            return None if _leads_into(path, roots, current) else current
        current = os.path.dirname(current)
    return None


def _check(name: str, env_dir: str, roots: Sequence[str]) -> None:
    """Refuse the environment at ``env_dir`` when it includes the base
    interpreter's packages, or when a file of its site directories names a path
    inside the project outside it, or cannot be read."""
    said = f"{name} names the environment {env_dir} inside the project, "
    if _read(said, os.path.join(env_dir, "pyvenv.cfg"), _includes_system_site):
        raise InvalidRequest("environment", said + (
            "which includes the base interpreter's packages. This check does not read them. "
            "Create it without --system-site-packages, or outside the project"))
    for site in _site_dirs(env_dir):
        for entry in sorted(os.scandir(site), key=lambda entry: entry.name):
            for file, path in _read(said, entry.path, lambda _: list(_entry_paths(site, entry))):
                if _leads_into(path, roots, env_dir):
                    raise InvalidRequest("environment", said + (
                        f"whose {file} leads into the project's sources at {path}. Use an "
                        "environment outside the project, or install the project without editable mode"))


def _read(said: str, file: str, reader):
    """``reader(file)``, or a refusal naming ``file`` when it cannot be read,
    since an unread file could hold a path into the project."""
    try:
        return reader(file)
    except (OSError, UnicodeError, SyntaxError, ValueError, IndexError) as error:
        raise InvalidRequest("environment", said + (
            f"whose {file} cannot be read ({type(error).__name__}), so it could hold a path "
            "into the project")) from error


def _includes_system_site(configuration: str) -> bool:
    """Whether the virtual environment's pyvenv.cfg includes the system site packages."""
    if not os.path.isfile(configuration):
        return False
    for line in _text(configuration).splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "include-system-site-packages":
            return value.strip().lower() == "true"
    return False


def _site_dirs(env_dir: str) -> Tuple[str, ...]:
    """Every implementation's site directories: lib*/*/site-packages, and Windows' Lib/site-packages."""
    found = (glob.glob(os.path.join(glob.escape(env_dir), "lib*", "*", "site-packages"))
             + glob.glob(os.path.join(glob.escape(env_dir), "Lib", "site-packages")))
    return tuple({os.path.normcase(path): path for path in sorted(found)}.values())


def _entry_paths(site: str, entry: os.DirEntry) -> Iterator[Tuple[str, str]]:
    """Each path an entry of the site directory names, with the file that names it."""
    path = entry.path
    if entry.is_symlink() or getattr(os.path, "isjunction", lambda _: False)(path):
        yield path, os.path.realpath(path)
    # A linked file is read by its kind too.
    if entry.name.endswith(".pth"):
        yield from _pth_paths(site, path)
    elif entry.name.endswith(".egg-link"):
        yield path, os.path.join(site, _text(path).splitlines()[0].strip())
    elif entry.name in ("sitecustomize.py", "usercustomize.py"):
        yield from ((path, literal) for literal in _literals(_text(path)))
    elif entry.name.endswith(".dist-info") and os.path.isfile(os.path.join(path, "direct_url.json")):
        yield from _editable_record(os.path.join(path, "direct_url.json"))


def _pth_paths(site: str, pth: str) -> Iterator[Tuple[str, str]]:
    """A .pth file's path entries, and the literals of its import lines and
    of the modules they load from the site directory (editable installs'
    import-hook finders)."""
    for line in _text(pth).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("import ", "import\t")):
            yield pth, os.path.join(site, line)
            continue
        yield from ((pth, literal) for literal in _literals(line))
        for module in _imported(line):
            parts = module.split(".")
            for file in (os.path.join(site, *parts) + ".py", os.path.join(site, *parts, "__init__.py")):
                if os.path.isfile(file):
                    yield from ((file, literal) for literal in _literals(_text(file)))


def _editable_record(record: str) -> Iterator[Tuple[str, str]]:
    """The directory an editable install's direct_url.json names."""
    data = json.loads(_text(record))
    url = str(data.get("url", ""))
    if (data.get("dir_info") or {}).get("editable") and url.startswith("file:"):
        yield record, url2pathname(urlparse(url).path)


def _imported(line: str) -> Iterator[str]:
    for node in ast.walk(ast.parse(line)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module


def _literals(text: str) -> Iterator[str]:
    """The absolute paths among ``text``'s string literals, read without running it."""
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and os.path.isabs(node.value):
            yield node.value


def _leads_into(path: str, roots: Sequence[str], env_dir: str) -> bool:
    """Whether ``path``, as written or resolved, lies inside the project and
    outside the environment ``env_dir``."""
    tops = [os.path.normcase(os.path.abspath(root)) for root in roots]
    own = {os.path.normcase(os.path.abspath(env_dir)), os.path.normcase(os.path.realpath(env_dir))}
    for form in {os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.realpath(path))}:
        if (any(_below(form, top) for top in tops)
                and not any(_below(form, environment) for environment in own)):
            return True
    return False


def _below(path: str, base_dir: str) -> bool:
    return path == base_dir or path.startswith(base_dir.rstrip(os.sep) + os.sep)


def _text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")
