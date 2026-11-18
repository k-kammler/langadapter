"""Registration and lookup of language adapters.

Built-in adapters are registered on import. An outside adapter registers
through the same function and is then treated exactly like a built-in one. A
registration claiming a language name or file extension another adapter holds
is refused unless the caller asks to replace it.

An extension, a file or an installed module a program's settings name, adds
a language or a metric provider with no installation or registration step:
``load_extension`` reads its top-level names ``langadapter_language`` and
``langadapter_metric_provider``. The third name, ``langadapter_toolchain``,
gives the language's toolchain part, which ``langadapter toolchains`` reads
from the loaded module.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import os
import sys
import threading
from dataclasses import dataclass, replace
from pathlib import Path, PurePath
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from langadapter.adapter import LangAdapter
from langadapter.context import ProjectContext
from langadapter.errors import (
    ExtensionFoundOnlyByImport, InvalidRequest, RegistrationConflict,
    UnsupportedLanguage,
)

#: Creates an adapter instance for a validated project context.
AdapterFactory = Callable[[ProjectContext], LangAdapter]


@dataclass(frozen=True)
class Extension:
    """An extension item as found: the ``item`` as named, its ``kind``
    (``file`` or ``module``), the resolved ``file`` (a package's
    ``__init__.py``) and the SHA-256 of its bytes, and for a module of an
    installed distribution that ``distribution`` and its ``version``. After a
    load it also names the ``languages`` and ``metric_providers`` it
    registered and the ``module`` it was imported as."""

    item: str
    kind: str
    file: str
    sha256: str
    distribution: Optional[str] = None
    version: Optional[str] = None
    languages: Tuple[str, ...] = ()
    metric_providers: Tuple[str, ...] = ()
    module: Optional[str] = None


@dataclass(frozen=True)
class Registration:
    """A registered language: its name, file extensions and instance factory,
    and the ``extension`` it was loaded from, None for a built-in language or
    one registered by ``register``."""

    name: str
    file_extensions: Tuple[str, ...]
    factory: AdapterFactory
    extension: Optional[Extension] = None


# Reentrant, so a load registers its language and names its extension in one hold.
_lock = threading.RLock()
_by_name: Dict[str, Registration] = {}
_by_file_extension: Dict[str, str] = {}

def _normalized_file_extension(file_extension: str) -> str:
    text = str(file_extension).strip().lower()
    if not text or text == ".":
        raise InvalidRequest("file_extensions", "a file extension must not be empty")
    return text if text.startswith(".") else f".{text}"


def register(
    name: str,
    file_extensions: Iterable[str],
    factory: AdapterFactory,
    *,
    replace: bool = False,
) -> None:
    """Register ``factory`` for the language ``name`` and its ``file_extensions``. One
    held by another registration raises RegistrationConflict unless ``replace``."""
    language = str(name).strip().lower()
    if not language:
        raise InvalidRequest("name", "a language name must not be empty")
    claimed = tuple(dict.fromkeys(
        _normalized_file_extension(e) for e in file_extensions))
    if not claimed:
        raise InvalidRequest("file_extensions", "name at least one file extension")
    if not callable(factory):
        raise InvalidRequest("factory", "must be callable")
    with _lock:
        if not replace:
            if language in _by_name:
                raise RegistrationConflict(language, language)
            for file_extension in claimed:
                holder = _by_file_extension.get(file_extension)
                if holder is not None:
                    raise RegistrationConflict(file_extension, holder)
        previous = _by_name.pop(language, None)
        if previous is not None:
            for file_extension in previous.file_extensions:
                _by_file_extension.pop(file_extension, None)
        for file_extension in claimed:
            holder = _by_file_extension.get(file_extension)
            if holder is not None and holder != language:
                other = _by_name[holder]
                _by_name[holder] = Registration(
                    other.name,
                    tuple(e for e in other.file_extensions if e != file_extension),
                    other.factory, other.extension)
            _by_file_extension[file_extension] = language
        _by_name[language] = Registration(language, claimed, factory)


def unregister(name: str) -> None:
    """Remove the registration of ``name``, else raise UnsupportedLanguage."""
    language = str(name).strip().lower()
    with _lock:
        registration = _by_name.pop(language, None)
        if registration is None:
            raise UnsupportedLanguage(language, _by_name)
        for file_extension in registration.file_extensions:
            if _by_file_extension.get(file_extension) == language:
                del _by_file_extension[file_extension]


def names() -> Tuple[str, ...]:
    """The registered languages, sorted."""
    with _lock:
        return tuple(sorted(_by_name))


def registration(name: str) -> Registration:
    """The registration of ``name``. UnsupportedLanguage names the registered
    languages."""
    language = str(name).strip().lower()
    with _lock:
        found = _by_name.get(language)
        if found is None:
            raise UnsupportedLanguage(language, _by_name)
    return found


def language_for(path: str) -> str:
    """The language of ``path``'s file extension."""
    file_extension = PurePath(str(path)).suffix.lower()
    with _lock:
        language = _by_file_extension.get(file_extension)
        if language is None:
            raise UnsupportedLanguage(file_extension or str(path), _by_name)
    return language


def create(name: str, context: ProjectContext) -> LangAdapter:
    """An adapter of ``name`` for ``context``, validated first: InvalidRequest names a bad field."""
    if not isinstance(context, ProjectContext):
        raise InvalidRequest("context", "must be a ProjectContext")
    found = registration(name)
    context.validate()
    context._require_execution()
    adapter = found.factory(context)
    if not isinstance(adapter, LangAdapter):
        raise InvalidRequest(
            "factory", f"the {found.name!r} factory returned "
                       f"{type(adapter).__name__}, not a LangAdapter")
    return adapter


_load_lock = threading.RLock()
_loaded: Dict[str, Extension] = {}
_metric_providers: List[Tuple[Any, Extension]] = []


def _is_file_item(item: str, suffix: str) -> bool:
    """Whether the item ``item`` names a file: it ends in ``suffix`` or holds
    a path separator (``/``, and a backslash on Windows). Any other item is
    a module name."""
    return item.endswith(suffix) or "/" in item or (os.sep == "\\" and "\\" in item)


def _module_origin(item: str) -> str:
    """The resolved source file of the module ``item``, found by asking the
    finders of ``sys.meta_path`` for each part in turn, the parent spec's
    search locations in place of an imported parent's ``__path__``, so no
    module runs."""
    parts = item.split(".")
    if not all(part.isidentifier() for part in parts):
        raise InvalidRequest("item", f"{item}: neither a .py file nor a module name")
    search, spec = None, None
    for index in range(len(parts)):
        name = ".".join(parts[:index + 1])
        spec = next((found for finder in sys.meta_path if hasattr(finder, "find_spec")
                     and (found := finder.find_spec(name, search)) is not None), None)
        if spec is None and index:
            raise ExtensionFoundOnlyByImport(
                "item", f"{item}: no module {name} is found without running its package, "
                          f"which load_extension runs")
        if spec is None:
            raise InvalidRequest("item", f"{item}: no module {name} is found in this environment")
        search = spec.submodule_search_locations
        if search is None and index < len(parts) - 1:
            raise InvalidRequest("item", f"{item}: {name} is not a package")
    return _source_file(item, spec)


def _source_file(item: str, spec: Any) -> str:
    if spec is None or not spec.has_location or not str(spec.origin).endswith(".py"):
        raise InvalidRequest("item", f"{item}: the module has no Python source file")
    return str(Path(spec.origin).resolve())


def _module_extension(item: str, origin: str) -> Extension:
    """The ``Extension`` of the module ``item`` at ``origin``, with its
    installed distribution and version where one holds its top-level name."""
    from importlib import metadata
    for distribution in metadata.packages_distributions().get(item.split(".")[0], ()):
        try:
            version = metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
        return Extension(item, "module", origin, _sha256(origin), distribution, version)
    return Extension(item, "module", origin, _sha256(origin))


def _sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def resolve_extension(item: str, *, relative_to: Optional[str] = None) -> Extension:
    """The ``Extension`` of the extension item ``item``, found without running
    any module. An item that ends in ``.py`` or holds a path separator is a
    file, relative to the directory ``relative_to`` or else to the current
    directory. Any other item is a module name, whose file the import system's
    finders show, a dotted name's parent packages unrun. A dotted module
    whose top-level package the finders show, but not the module itself,
    raises ExtensionFoundOnlyByImport, a subclass of InvalidRequest:
    ``load_extension`` imports the package, then finds the module or names
    it missing. Any other item that resolves to no Python source file raises
    a plain InvalidRequest."""
    if not isinstance(item, str) or not item.strip():
        raise InvalidRequest("item", "name an extension file or module")
    if not _is_file_item(item, ".py"):
        return _module_extension(item, _module_origin(item))
    path = Path(item)
    if not path.is_absolute():
        path = Path(os.getcwd() if relative_to is None else relative_to) / path
    path = path.resolve()
    if not path.exists():
        raise InvalidRequest("item", f"{item}: {path} does not exist")
    if path.suffix != ".py" or not path.is_file():
        kind = "directory" if path.is_dir() else "file" if path.is_file() else "special file"
        raise InvalidRequest("item", f"{item}: {path} is a {kind}, not a Python file ending in .py")
    return Extension(item, "file", str(path), _sha256(str(path)))


def _imported(found: Extension, source: bytes) -> Any:
    """The module of ``found``: a module item imported by its name, a file
    run from ``source`` under a private name derived from its path, so it
    never takes the place of an installed module of its stem."""
    if found.kind == "module":
        module = importlib.import_module(found.item)
        # One imported before from another file stays that file's.
        ran = getattr(module, "__file__", None)
        if ran is None or os.path.normcase(str(Path(ran).resolve())) != os.path.normcase(found.file):
            raise ImportError(f"the module imported is {ran}, not {found.file}")
        return module
    name = "_langadapter_extension_" + hashlib.sha256(found.file.encode("utf-8")).hexdigest()[:16]
    module = importlib.util.module_from_spec(importlib.util.spec_from_file_location(name, found.file))
    sys.modules[name] = module
    try:
        exec(compile(source, found.file, "exec", dont_inherit=True), module.__dict__)
    except BaseException:
        del sys.modules[name]
        raise
    return module


def _found(item: str, relative_to: Optional[str]) -> Extension:
    """``item`` resolved, a module the finders show only once its package runs
    found by importing that package."""
    try:
        return resolve_extension(item, relative_to=relative_to)
    except ExtensionFoundOnlyByImport:
        try:
            origin = _source_file(item, importlib.util.find_spec(item))
        except (Exception, SystemExit) as error:
            raise InvalidRequest("item", f"{item}: {type(error).__name__}: {error}") from None
        return _module_extension(item, origin)


def _langadapter_names(found: Extension, module: Any) -> Tuple[Any, Any]:
    """The module's ``langadapter_language`` and ``langadapter_metric_provider``,
    each None where the module has none. InvalidRequest names one that is
    not an adapter class with a language or a valid metric provider."""
    language = getattr(module, "langadapter_language", None)
    provider = getattr(module, "langadapter_metric_provider", None)
    if language is not None and not (
            isinstance(language, type) and issubclass(language, LangAdapter)
            and isinstance(language.language, str) and language.language.strip()):
        raise InvalidRequest("langadapter_language",
                             f"{found.item}: must be a LangAdapter subclass with a language")
    if provider is not None:
        from langadapter.sources import MetricRegistry, _StructuralProvider
        # Beside the providers loaded before and the structural counters every
        # language with them holds, so a metric name one holds is refused
        # here, not at every later adapter's creation.
        check = MetricRegistry()
        for held, _ in _metric_providers:
            check.register(held)
        try:
            check.register(provider)
            taken = sorted(set(_StructuralProvider._names)
                           & {item.name for item in provider.definitions()})
            if taken:
                raise InvalidRequest("definitions", "metric names the structural counters hold: "
                                                    + ", ".join(taken))
        except InvalidRequest as error:
            raise InvalidRequest(error.field, f"{error.message}, from the extension "
                                              f"{found.file}") from None
    return language, provider


def load_extension(item: Union[str, Extension], *, relative_to: Optional[str] = None) -> Extension:
    """Load the extension item ``item``, or an ``Extension`` that
    ``resolve_extension`` returned, and register what LangAdapter reads in
    its module: ``langadapter_language``, an adapter class registered under
    its ``language`` and ``file_extensions``, and ``langadapter_metric_provider``,
    a metric provider that joins the source operations of every adapter
    created afterwards. A module without either registers nothing. A
    module's ``langadapter_toolchain``, the part that installs its
    language's tools, is no registration: ``langadapter toolchains`` reads it
    from the loaded module. Each
    resolved file is imported once per process: an item naming a loaded
    file returns its first ``Extension``. The returned ``Extension`` holds
    the SHA-256 of the file's bytes at the load, the bytes a file item runs.
    A module item runs as the import system loads it, which may be from
    cached bytecode, and one imported before from another file is refused.
    An item that cannot be resolved or imported raises InvalidRequest naming
    it and the cause, as does a metric name a provider loaded before holds,
    and a language another registration holds raises RegistrationConflict.
    Either way nothing of the item stays registered."""
    found = item if isinstance(item, Extension) else _found(item, relative_to)
    key = os.path.normcase(found.file)
    with _load_lock:
        if key in _loaded:
            return _loaded[key]
        source = Path(found.file).read_bytes()
        try:
            module = _imported(found, source)
        except (Exception, SystemExit) as error:
            raise InvalidRequest("item", f"{found.item}: {type(error).__name__}: {error}") from None
        language, provider = _langadapter_names(found, module)
        loaded = replace(
            found, sha256=hashlib.sha256(source).hexdigest(), module=module.__name__,
            languages=(language.language.strip().lower(),) if language is not None else (),
            metric_providers=(provider.name,) if provider is not None else ())
        if language is not None:
            with _lock:
                register(language.language, language.file_extensions, language)
                _by_name[loaded.languages[0]] = replace(_by_name[loaded.languages[0]], extension=loaded)
        if provider is not None:
            _metric_providers.append((provider, loaded))
        _loaded[key] = loaded
        return loaded


def _loaded_metric_providers() -> Tuple[Tuple[Any, Extension], ...]:
    """Each loaded extension's metric provider with its ``Extension``, in load order."""
    with _load_lock:
        return tuple(_metric_providers)
