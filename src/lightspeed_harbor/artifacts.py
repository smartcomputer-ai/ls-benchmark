"""Per-trial artifact export and redaction.

Planned responsibilities (see ``docs/next-steps.md``, slice 3): write bounded,
redacted files under Harbor's ``/logs`` mount so every trial is diagnosable
from retained files alone. No artifact may contain a registration key, a
Lightspeed API key, a model-provider key, or an authorization header.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from harbor.models.trial.paths import EnvironmentPaths

_PATHS = EnvironmentPaths()

ENVD_LOG: PurePosixPath = _PATHS.agent_dir / "lightspeed" / "envd.log"
ARTIFACT_DIR: PurePosixPath = _PATHS.artifacts_dir / "lightspeed"
REGISTRATION_JSON: PurePosixPath = ARTIFACT_DIR / "registration.json"
RUN_JSON: PurePosixPath = ARTIFACT_DIR / "run.json"
PROVENANCE_JSON: PurePosixPath = ARTIFACT_DIR / "provenance.json"
TRAJECTORY_JSON: PurePosixPath = ARTIFACT_DIR / "trajectory.json"
