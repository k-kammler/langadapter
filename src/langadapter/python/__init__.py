"""The Python adapter: source analysis, test execution, coverage and the
Cosmic Ray engine for Python projects."""
from typing import Dict, Optional


def _pinned_tools(version: Optional[str]) -> Dict[str, str]:
    """The tool versions LangAdapter pins for a project Python of
    ``version`` ("3.14", "3.12.3", or None or "unknown" for an unknown
    version), which provisioning installs and the setup's install advice
    names."""
    from langadapter.python._profile import tools_of
    from langadapter.versions import described

    return dict(tools_of(described("python", version, "context")).versions)
