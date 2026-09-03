"""Per-trial artifact export and redaction.

Artifacts are written on the Harbor host under the agent's ``logs_dir``
(``<trial>/agent/lightspeed-adapter/``), so they exist even when the sandbox
is unreachable. The daemon log is the one file written inside the sandbox
(``/logs/agent/lightspeed/envd.log``); Harbor syncs it with the agent logs.
The two directories are deliberately different: on a Linux Docker host the
sandbox creates ``lightspeed/`` as root inside the bind-mounted logs
directory, and the host-side process could not write into it.

No artifact may contain a registration key, a Lightspeed API key, a
model-provider key, or an authorization header; ``write_json`` refuses to
write one that does.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

from harbor.models.trial.paths import EnvironmentPaths

_PATHS = EnvironmentPaths()

SANDBOX_SUBDIR = "lightspeed"  # inside the sandbox, created by the task user
ARTIFACT_SUBDIR = "lightspeed-adapter"  # on the Harbor host, created by the adapter
ENVD_LOG: PurePosixPath = _PATHS.agent_dir / SANDBOX_SUBDIR / "envd.log"
REGISTRATION_JSON = "registration.json"
RUN_JSON = "run.json"
PROVENANCE_JSON = "provenance.json"

_HEADER_MARKERS = ("authorization: bearer", '"authorization"')

# A placeholder such as ``LIGHTSPEED_API_KEY=local`` (single-mode gateways
# ignore the header) would match ordinary words; real keys are far longer.
MIN_SECRET_LENGTH = 12


class RedactionError(RuntimeError):
    """An artifact would have contained a secret; nothing was written."""


def assert_no_secrets(text: str, secrets: Iterable[str]) -> None:
    lowered = text.lower()
    for marker in _HEADER_MARKERS:
        if marker in lowered:
            raise RedactionError("artifact contains an authorization header")
    for secret in secrets:
        if len(secret) >= MIN_SECRET_LENGTH and secret in text:
            raise RedactionError("artifact contains a configured secret")


def write_json(
    directory: Path, name: str, payload: dict[str, Any], *, secrets: Iterable[str]
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str)
    assert_no_secrets(text, secrets)
    path = directory / name
    path.write_text(text + "\n")
    return path
