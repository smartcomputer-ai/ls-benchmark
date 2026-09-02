"""Adapter error types carrying the P149 failure class.

Every failure raised by the adapter names the boundary where it occurred so
``run.json`` and Harbor's exception record agree on the classification.
"""

from __future__ import annotations

FAILURE_HARNESS_SETUP = "harness_setup"
FAILURE_AGENT_EXECUTION = "agent_execution"
FAILURE_ARTIFACT_ONLY = "artifact_only"


class AdapterError(RuntimeError):
    """Base class for adapter failures; ``failure_class`` is one of the constants above."""

    failure_class = FAILURE_AGENT_EXECUTION

    def __init__(self, message: str, *, failure_class: str | None = None) -> None:
        super().__init__(message)
        if failure_class is not None:
            self.failure_class = failure_class


class HarnessSetupError(AdapterError):
    """The Lightspeed arm could not be set up: bad ``envd`` artifact, registration
    rejected, contract mismatch. Counts as this agent's failure, never retried."""

    failure_class = FAILURE_HARNESS_SETUP


class AgentExecutionError(AdapterError):
    """The hosted run could not be started or observed after registration."""

    failure_class = FAILURE_AGENT_EXECUTION
