"""Words the command line, the check, the tutorial and the hook table share:
the kinds of a project setting and a list joined as a sentence lists it."""
from __future__ import annotations

from typing import Sequence

#: The kinds a project setting takes (``ProjectSetting.kind``).
SETTING_KINDS = ("path", "executable", "choice", "text")


def listed(words: Sequence[str], conjunction: str = "and") -> str:
    """``words`` as a sentence lists them, such as ``a``, ``a and b`` or
    ``a, b and c``, with ``conjunction`` before the last."""
    words = list(words)
    if len(words) < 2:
        return "".join(words)
    return f"{', '.join(words[:-1])} {conjunction} {words[-1]}"
