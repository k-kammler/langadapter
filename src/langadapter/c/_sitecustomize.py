"""The sitecustomize of a C extension's runs: the target's finder, first.

The C adapter copies this file as ``sitecustomize.py`` beside the pytest
plugin. Python imports it while it starts, before any other code, so the
plugin's import finder is in place before anything imports the target: a
test runner imports some of CPython's own modules (``random`` imports
``_bisect``) before it loads a plugin. Another ``sitecustomize`` further on
the path still runs, as it would without this one.
"""
import importlib.machinery
import importlib.util
import os
import resource
import sys

import langadapter_c_plugin  # installs the target's finder

# The address space limit c_address_space_limit sets for every test process,
# in bytes, as the C adapter's harness writes it into the plugin's
# configuration.
_LIMIT = (langadapter_c_plugin._configuration_or_none() or {}).get("address_space_limit")
if _LIMIT:
    resource.setrlimit(resource.RLIMIT_AS, (int(_LIMIT), int(_LIMIT)))

_HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.machinery.PathFinder.find_spec(
    "sitecustomize", [entry for entry in sys.path if os.path.abspath(entry or ".") != _HERE])
if _spec is not None and _spec.loader is not None:
    _spec.loader.exec_module(importlib.util.module_from_spec(_spec))
