"""Test execution, coverage, mutation and timing observations for a
configured project.

A caller describes the project in a ``ProjectContext`` and creates an
adapter for its language with ``create``. Results of executing operations
carry an ``Outcome``, source operations a ``SourceStatus``. A value the
adapter did not observe is absent, never zero.
"""
from langadapter.adapter import LangAdapter
from langadapter.capabilities import (
    Availability, Capability, CapabilityReport, LanguageFacts, OperationCapability, ToolVersion,
)
from langadapter.context import (
    ContainerExecution, Environment, Interpreter, LocalExecution, ProjectContext,
    ProvisioningRequest, TestFrameworkConfig, ProjectInputSelection,
)
from langadapter.errors import (
    ExtensionFoundOnlyByImport, InvalidRequest, LangAdapterError, OperationCancelled,
    OwnershipRefused,
    recovery_owners,
    RegistrationConflict, SourceReadError, UnsupportedLanguage,
)
from langadapter.cancellation import CancelScope
from langadapter.outcomes import (
    ExecutionIdentity, FileIdentity, InstrumentIdentity, ObservationIdentity, ToolchainIdentity,
    Outcome,
)
from langadapter.setup import (
    ProjectSetupItem, ProjectSetupReport, ProjectSetupRequest, inspect_project, verify_project,
)
from langadapter.sources import (
    MetricDefinition, MetricProvider, MetricScope, MetricValue, SourceMetrics, SourceObservation,
    SourceStatus,
)
from langadapter.registry import (
    Extension, Registration, create, language_for, load_extension, names, register,
    registration, resolve_extension, unregister,
)
from langadapter.suites import (
    BranchItem, CrashItem, EvidenceItem, KillItem, LineItem, Suite, TestId,
    parse_token,
)
from langadapter.operations import SourceOperations, TestOperations, MutationOperations
from langadapter.preparation import PreparedProject, prepare_project

# The built-in adapters, registered when the package is imported.
from langadapter.c.adapter import CAdapter as _CAdapter
from langadapter.java.adapter import JavaAdapter as _JavaAdapter
from langadapter.python.adapter import PythonAdapter as _PythonAdapter

register(_PythonAdapter.language, _PythonAdapter.file_extensions, _PythonAdapter)
register(_JavaAdapter.language, _JavaAdapter.file_extensions, _JavaAdapter)
register(_CAdapter.language, _CAdapter.file_extensions, _CAdapter)

__version__ = "0.1.1"

__all__ = [
    "__version__", "Availability", "LangAdapter", "BranchItem", "CancelScope", "Capability",
    "CapabilityReport", "ContainerExecution", "CrashItem", "create",
    "Environment",
    "EvidenceItem", "ExecutionIdentity", "Extension", "ExtensionFoundOnlyByImport", "FileIdentity",
    "InstrumentIdentity",
    "Interpreter", "InvalidRequest", "KillItem", "language_for", "load_extension",
    "LangAdapterError", "LanguageFacts", "LineItem", "LocalExecution",
    "MetricDefinition", "MetricProvider", "MetricScope", "MetricValue", "SourceMetrics",
    "names", "ObservationIdentity", "OperationCancelled", "OperationCapability", "Outcome",
    "OwnershipRefused", "recovery_owners",
    "parse_token", "ProjectContext",
    "ProvisioningRequest", "register", "Registration", "registration",
    "ProjectInputSelection",
    "ProjectSetupItem", "ProjectSetupReport", "ProjectSetupRequest", "resolve_extension",
    "inspect_project", "verify_project",
    "PreparedProject", "prepare_project",
    "RegistrationConflict", "SourceObservation", "SourceOperations", "SourceReadError", "SourceStatus", "Suite", "TestFrameworkConfig", "TestId",
    "MutationOperations", "TestOperations", "ToolchainIdentity", "ToolVersion", "unregister", "UnsupportedLanguage",
]
