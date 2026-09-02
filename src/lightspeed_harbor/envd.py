"""``lightspeed-envd`` artifact selection, upload, start, and receipt handling.

Planned responsibilities (see ``docs/next-steps.md``, slice 2): pick the pinned
artifact for the sandbox platform, verify SHA-256 on the host, upload through
``BaseEnvironment.upload_file``, start the daemon as the task user with the
``LIGHTSPEED_ENVD_*`` variables listed in ``docs/lightspeed-contract.md``, wait
for the registration receipt, and terminate the process group on cleanup.

The registration key is passed only through ``LIGHTSPEED_ENVD_REGISTRATION_KEY_FILE``.
It never appears in argv, the daemon's environment, logs, or artifacts.
"""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

ENVD_BINARY_NAME = "lightspeed-envd"

# Harbor platform string -> Rust target triple of the artifact to upload.
SUPPORTED_TARGETS: Mapping[str, str] = {
    "linux/amd64": "x86_64-unknown-linux-gnu",
}

RECEIPT_FIELDS = ("environmentId", "incarnationId", "daemonId", "connectionId", "identityMode")

# Bounds enforced by the Lightspeed gateway at the registration handshake.
METADATA_MAX_ENTRIES = 32
METADATA_MAX_KEY_BYTES = 64
METADATA_MAX_VALUE_BYTES = 256
METADATA_RESERVED_PREFIX = "lightspeed."


def correlation_metadata(
    *,
    context_id: UUID | None,
    session_id: str | None,
    task_name: str | None = None,
    attempt: int | None = None,
    job_id: str | None = None,
    trial_id: str | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Bounded, non-secret correlation metadata for the registration handshake.

    Harbor's ``context_id`` is the principal join key. Fields a Harbor version
    does not expose through ``BaseAgent`` are omitted rather than discovered
    through internal state. The values are diagnostic only and never act as
    identity or authority on the Lightspeed side.
    """
    candidates: dict[str, str | None] = {
        "source": "harbor",
        "agent": "lightspeed",
        "harborContextId": str(context_id) if context_id else None,
        "harborSessionId": session_id,
        "harborJobId": job_id,
        "harborTrialId": trial_id,
        "harborTaskName": task_name,
        "harborAttempt": str(attempt) if attempt is not None else None,
    }
    metadata = {key: value for key, value in candidates.items() if value}
    metadata.update({key: value for key, value in (extra or {}).items() if value})
    for key, value in metadata.items():
        if key.startswith(METADATA_RESERVED_PREFIX):
            raise ValueError(f"metadata key {key!r} uses the reserved prefix")
        if len(key.encode()) > METADATA_MAX_KEY_BYTES:
            raise ValueError(f"metadata key {key!r} exceeds {METADATA_MAX_KEY_BYTES} bytes")
        if len(value.encode()) > METADATA_MAX_VALUE_BYTES:
            metadata[key] = value.encode()[:METADATA_MAX_VALUE_BYTES].decode(errors="ignore")
    if len(metadata) > METADATA_MAX_ENTRIES:
        raise ValueError(f"metadata exceeds {METADATA_MAX_ENTRIES} entries")
    return metadata
