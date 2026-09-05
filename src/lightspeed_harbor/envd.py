"""``lightspeed-envd`` artifact selection, upload, start, receipt, and stop.

The daemon is the only Lightspeed component that runs inside the Harbor
sandbox. This module owns everything about it:

- picking the artifact for the sandbox platform (probed with ``uname -m``, not
  assumed from the host) and verifying its SHA-256 on the host;
- uploading it and checking ``--version`` as the task user;
- building the start command with the ``LIGHTSPEED_ENVD_*`` variables from
  ``docs/lightspeed-contract.md``. The registration key travels only through
  ``LIGHTSPEED_ENVD_REGISTRATION_KEY_FILE``; it never appears in argv, the
  daemon's environment, logs, receipt, or artifacts;
- waiting for the registration receipt and terminating the process group.

Environment objects are duck-typed: ``exec(command, cwd=, env=, timeout_sec=,
user=)`` returning ``return_code``/``stdout``/``stderr``, ``upload_file``, and
``default_user``. That is the subset of Harbor's ``BaseEnvironment`` the
adapter uses, and what the test fakes implement.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shlex
import tarfile
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol
from uuid import UUID

import httpx

from lightspeed_harbor.config import ENV_ENVD_PATH, HostSettings
from lightspeed_harbor.errors import HarnessSetupError

ENVD_BINARY_NAME = "lightspeed-envd"

# Harbor platform string -> Rust target triple of the artifact to upload.
# amd64 is the static musl build the Lightspeed release publishes, which runs
# on any image regardless of its glibc; arm64 is only built locally
# (scripts/build-envd-linux.sh) for Apple silicon Docker daemons.
SUPPORTED_TARGETS: Mapping[str, str] = {
    "linux/amd64": "x86_64-unknown-linux-musl",
    "linux/arm64": "aarch64-unknown-linux-musl",
}

_UNAME_PLATFORMS: Mapping[str, str] = {
    "x86_64": "linux/amd64",
    "amd64": "linux/amd64",
    "aarch64": "linux/arm64",
    "arm64": "linux/arm64",
}

RECEIPT_FIELDS = ("environmentId", "incarnationId", "daemonId", "connectionId", "identityMode")

# Bounds enforced by the Lightspeed gateway at the registration handshake.
METADATA_MAX_ENTRIES = 32
METADATA_MAX_KEY_BYTES = 64
METADATA_MAX_VALUE_BYTES = 256
METADATA_RESERVED_PREFIX = "lightspeed."

_ALIVE = "__LIGHTSPEED_HARBOR_ALIVE__"
_DEAD = "__LIGHTSPEED_HARBOR_DEAD__"
# `envd --version` right after upload: attempts before an empty answer is fatal.
_VERSION_PROBE_ATTEMPTS = 3
_VERSION_PROBE_DELAY_SEC = 1.0


class ExecResultLike(Protocol):
    return_code: int
    stdout: str | None
    stderr: str | None


class EnvironmentLike(Protocol):
    default_user: str | int | None

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResultLike: ...

    async def upload_file(self, source_path: Path | str, target_path: str) -> None: ...


@dataclass(frozen=True)
class SandboxPaths:
    """Where the adapter keeps its files inside one trial sandbox."""

    root: PurePosixPath = PurePosixPath("/tmp/lightspeed-harbor")
    log_dir: PurePosixPath = PurePosixPath("/logs/agent/lightspeed")

    @property
    def binary(self) -> PurePosixPath:
        return self.root / "bin" / ENVD_BINARY_NAME

    @property
    def state_dir(self) -> PurePosixPath:
        return self.root / "state"

    @property
    def key_file(self) -> PurePosixPath:
        return self.root / "registration.key"

    @property
    def receipt_file(self) -> PurePosixPath:
        return self.root / "receipt.json"

    @property
    def pid_file(self) -> PurePosixPath:
        return self.root / "envd.pid"

    @property
    def ca_file(self) -> PurePosixPath:
        return self.root / "gateway-ca.pem"

    @property
    def log_file(self) -> PurePosixPath:
        return self.log_dir / "envd.log"


@dataclass(frozen=True)
class EnvdArtifact:
    """A verified ``lightspeed-envd`` binary on the host, ready to upload."""

    path: Path
    sha256: str
    source: str  # "local", "release", or "discovery"
    target: str | None = None
    archive_sha256: str | None = None
    release_url: str | None = None
    git_sha: str | None = None
    version: str | None = None
    channel: str | None = None
    protocol_version: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "sha256": self.sha256,
            "source": self.source,
            "target": self.target,
            "archive_sha256": self.archive_sha256,
            "release_url": self.release_url,
            "git_sha": self.git_sha,
            "version": self.version,
            "channel": self.channel,
            "protocol_version": self.protocol_version,
        }


@dataclass(frozen=True)
class Receipt:
    environment_id: str
    incarnation_id: str
    daemon_id: str
    connection_id: str
    identity_mode: str
    raw: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "environmentId": self.environment_id,
            "incarnationId": self.incarnation_id,
            "daemonId": self.daemon_id,
            "connectionId": self.connection_id,
            "identityMode": self.identity_mode,
        }


# --- host side -------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def platform_from_uname(machine: str) -> str:
    """Map ``uname -m`` output to Harbor's platform string; unsupported fails closed."""
    key = machine.strip().lower()
    try:
        return _UNAME_PLATFORMS[key]
    except KeyError:
        raise HarnessSetupError(
            f"unsupported sandbox architecture {machine.strip()!r}; "
            f"supported: {', '.join(sorted(SUPPORTED_TARGETS))}"
        ) from None


async def detect_platform(environment: EnvironmentLike) -> str:
    result = await environment.exec("uname -m", timeout_sec=30)
    if result.return_code != 0 or not (result.stdout or "").strip():
        raise HarnessSetupError(f"could not probe sandbox architecture: {result.stderr!r}")
    return platform_from_uname(result.stdout or "")


def default_cache_dir() -> Path:
    return Path.home() / ".cache" / "ls-benchmark" / "envd"


async def resolve_artifact(
    host: HostSettings,
    *,
    target: str,
    cache_dir: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EnvdArtifact:
    """Return the verified binary for ``target``.

    Precedence: a local override (``LIGHTSPEED_HARBOR_ENVD_PATH``), then a
    pinned release URL plus checksum, then the deployment's discovery document
    (P152), which names the archive built from the deployed commit.
    """
    if host.envd_path is not None:
        if not host.envd_path.is_file():
            raise HarnessSetupError(f"envd binary not found: {host.envd_path}")
        digest = sha256_file(host.envd_path)
        if host.envd_sha256 is not None and digest != host.envd_sha256:
            raise HarnessSetupError(
                f"envd binary {host.envd_path} has SHA-256 {digest}, expected {host.envd_sha256}"
            )
        return EnvdArtifact(path=host.envd_path, sha256=digest, source="local", target=target)

    cache_dir = cache_dir or default_cache_dir()
    if host.envd_release_url is not None:
        assert host.envd_sha256 is not None
        binary = await _cached_download(
            host.envd_release_url, host.envd_sha256, cache_dir, transport=transport
        )
        return EnvdArtifact(
            path=binary,
            sha256=sha256_file(binary),
            source="release",
            target=target,
            archive_sha256=host.envd_sha256,
            release_url=host.envd_release_url,
        )

    if not host.envd_discovery_url:
        raise HarnessSetupError("no envd artifact configured and no discovery URL derived")
    document = await _fetch_discovery(host.envd_discovery_url, transport=transport)
    artifacts = document.get("artifacts") or {}
    entry = artifacts.get(target) if isinstance(artifacts, dict) else None
    if not isinstance(entry, dict) or not entry.get("url") or not entry.get("sha256"):
        raise HarnessSetupError(
            f"discovery document {host.envd_discovery_url} has no artifact for {target} "
            f"(available: {sorted(artifacts) if isinstance(artifacts, dict) else []}); "
            f"set {ENV_ENVD_PATH} for a locally built binary"
        )
    expected = str(entry["sha256"]).strip().lower()
    binary = await _cached_download(str(entry["url"]), expected, cache_dir, transport=transport)
    return EnvdArtifact(
        path=binary,
        sha256=sha256_file(binary),
        source="discovery",
        target=target,
        archive_sha256=expected,
        release_url=str(entry["url"]),
        git_sha=(str(document.get("gitSha")) if document.get("gitSha") else None),
        version=(str(document.get("version")) if document.get("version") else None),
        channel=(str(document.get("channel")) if document.get("channel") else None),
        protocol_version=(
            int(document["protocolVersion"])
            if isinstance(document.get("protocolVersion"), int)
            else None
        ),
    )


async def _fetch_discovery(
    url: str, *, transport: httpx.AsyncBaseTransport | None = None
) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=30.0, transport=transport
        ) as http:
            response = await http.get(url)
    except httpx.HTTPError as exc:
        raise HarnessSetupError(f"envd discovery failed: {url}: {exc}") from exc
    if response.status_code >= 400:
        raise HarnessSetupError(
            f"envd discovery failed: {url} returned HTTP {response.status_code}"
        )
    try:
        document = response.json()
    except ValueError as exc:
        raise HarnessSetupError(f"envd discovery document at {url} is not JSON") from exc
    if not isinstance(document, dict):
        raise HarnessSetupError(f"envd discovery document at {url} is not an object")
    return document


async def _cached_download(
    url: str,
    expected_sha256: str,
    cache_dir: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Path:
    slot = cache_dir / expected_sha256
    binary = slot / ENVD_BINARY_NAME
    if not binary.is_file():
        await _download_release(url, expected_sha256, slot, transport=transport)
    return binary


async def _download_release(
    url: str,
    expected_sha256: str,
    slot: Path,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> None:
    slot.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ls-benchmark-envd-") as temp:
        download = Path(temp) / "artifact"
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=120.0, transport=transport
        ) as http:
            async with http.stream("GET", url) as response:
                if response.status_code >= 400:
                    raise HarnessSetupError(
                        f"envd release download failed: HTTP {response.status_code}"
                    )
                with download.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)
        digest = sha256_file(download)
        if digest != expected_sha256:
            raise HarnessSetupError(
                f"envd release {url} has SHA-256 {digest}, expected {expected_sha256}"
            )
        _install_downloaded(download, slot / ENVD_BINARY_NAME)


def _install_downloaded(download: Path, binary: Path) -> None:
    if tarfile.is_tarfile(download):
        with tarfile.open(download) as archive:
            members = [
                m
                for m in archive.getmembers()
                if m.isfile() and PurePosixPath(m.name).name in {ENVD_BINARY_NAME, "envd"}
            ]
            if len(members) != 1:
                raise HarnessSetupError(
                    f"envd archive must contain exactly one {ENVD_BINARY_NAME} binary, "
                    f"found {[m.name for m in members]}"
                )
            with archive.extractfile(members[0]) as source, binary.open("wb") as sink:  # type: ignore[union-attr]
                sink.write(source.read())
    else:
        binary.write_bytes(download.read_bytes())
    binary.chmod(0o755)


# --- sandbox side ----------------------------------------------------------


def _q(value: str | PurePosixPath) -> str:
    return shlex.quote(str(value))


async def _exec_checked(
    environment: EnvironmentLike,
    command: str,
    *,
    what: str,
    user: str | int | None = None,
    timeout_sec: int = 60,
) -> ExecResultLike:
    result = await environment.exec(command, timeout_sec=timeout_sec, user=user)
    if result.return_code != 0:
        detail = (result.stderr or result.stdout or "").strip()[-500:]
        raise HarnessSetupError(f"{what} failed (exit {result.return_code}): {detail}")
    return result


async def install(
    environment: EnvironmentLike,
    artifact: EnvdArtifact,
    paths: SandboxPaths,
    *,
    ca_file: Path | None = None,
) -> str:
    """Upload the binary (and optional CA bundle), fix ownership, and return ``--version``."""
    await _exec_checked(
        environment,
        f"umask 077 && mkdir -p {_q(paths.root / 'bin')} {_q(paths.state_dir)}",
        what="create envd directories",
    )
    await environment.upload_file(artifact.path, str(paths.binary))
    if ca_file is not None:
        await environment.upload_file(ca_file, str(paths.ca_file))
    await _fix_ownership(environment, paths, [paths.binary] + ([paths.ca_file] if ca_file else []))
    await _exec_checked(environment, f"chmod 0755 {_q(paths.binary)}", what="mark envd executable")
    # A sandbox exec occasionally returns exit 0 with empty output right after
    # the upload (seen twice in one five-trial job on the Docker backend while
    # five sandboxes started at once). The probe is idempotent, so ask again
    # before treating silence as the wrong build.
    last: ExecResultLike | None = None
    for attempt in range(_VERSION_PROBE_ATTEMPTS):
        if attempt:
            await asyncio.sleep(_VERSION_PROBE_DELAY_SEC)
        last = await _exec_checked(
            environment, f"{_q(paths.binary)} --version", what="run envd --version", timeout_sec=30
        )
        version = (last.stdout or "").strip() or (last.stderr or "").strip()
        if version:
            return version
    detail = ((last.stderr if last else "") or "").strip()[-200:]
    raise HarnessSetupError(
        f"envd --version printed nothing in {_VERSION_PROBE_ATTEMPTS} attempts"
        + (f" (stderr: {detail})" if detail else "")
    )


async def _fix_ownership(
    environment: EnvironmentLike, paths: SandboxPaths, files: list[PurePosixPath]
) -> None:
    # upload_file copies as root on every Harbor backend; hand the files to the task user.
    if environment.default_user is None:
        return
    owner = _q(str(environment.default_user))
    targets = " ".join(_q(f) for f in files)
    await _exec_checked(
        environment,
        f"chown {owner} {_q(paths.root)} {targets}",
        what="chown envd files",
        user="root",
    )


async def write_key_file(
    environment: EnvironmentLike, paths: SandboxPaths, registration_key: str
) -> None:
    """Materialize the registration key as a mode-0600 file owned by the task user."""
    with tempfile.TemporaryDirectory(prefix="ls-benchmark-key-") as temp:
        local = Path(temp) / "registration.key"
        local.touch(mode=0o600)
        local.write_text(registration_key)
        await environment.upload_file(local, str(paths.key_file))
    owner = _q(str(environment.default_user)) if environment.default_user is not None else None
    chown = f"chown {owner} {_q(paths.key_file)} && " if owner else ""
    await _exec_checked(
        environment,
        f"{chown}chmod 0600 {_q(paths.key_file)}",
        what="protect registration key file",
        user="root",
    )


async def delete_key_file(environment: EnvironmentLike, paths: SandboxPaths) -> None:
    await _exec_checked(
        environment,
        f"rm -f {_q(paths.key_file)} && test ! -e {_q(paths.key_file)}",
        what="delete registration key file",
    )


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


def start_command(
    paths: SandboxPaths,
    *,
    gateway_url: str,
    cwd: str,
    metadata: Mapping[str, str],
    display_name: str | None = None,
    fs_root: str = "/",
    with_ca_file: bool = False,
) -> str:
    """Shell that starts the daemon detached, in its own process group, logging to
    ``paths.log_file`` and recording its pid. Contains no secret: the key is
    referenced only by file path."""
    variables: dict[str, str] = {
        "LIGHTSPEED_ENVD_GATEWAY_URL": gateway_url,
        "LIGHTSPEED_ENVD_REGISTRATION_KEY_FILE": str(paths.key_file),
        "LIGHTSPEED_ENVD_REGISTRATION_RECEIPT": str(paths.receipt_file),
        "LIGHTSPEED_ENVD_REGISTRATION_METADATA": json.dumps(dict(metadata), separators=(",", ":")),
        "LIGHTSPEED_ENVD_CWD": cwd,
        "LIGHTSPEED_ENVD_FS_ROOT": fs_root,
        "LIGHTSPEED_ENVD_STATE_DIR": str(paths.state_dir),
    }
    if display_name:
        variables["LIGHTSPEED_ENVD_REGISTRATION_NAME"] = display_name
    if with_ca_file:
        variables["LIGHTSPEED_ENVD_CA_FILE"] = str(paths.ca_file)
    exports = " ".join(f"{name}={_q(value)}" for name, value in variables.items())
    binary, log, pid = _q(paths.binary), _q(paths.log_file), _q(paths.pid_file)
    return (
        "set -e\n"
        f"mkdir -p {_q(paths.log_dir)}\n"
        f"cd {_q(cwd)}\n"
        f"rm -f {_q(paths.receipt_file)} {pid}\n"
        f"export {exports}\n"
        "if command -v setsid >/dev/null 2>&1; then\n"
        f"  setsid nohup {binary} >>{log} 2>&1 </dev/null &\n"
        "else\n"
        f"  nohup {binary} >>{log} 2>&1 </dev/null &\n"
        "fi\n"
        f"echo $! > {pid}\n"
    )


async def start(environment: EnvironmentLike, command: str) -> None:
    await _exec_checked(environment, command, what="start envd", timeout_sec=60)


def parse_receipt(text: str, *, expected_identity_mode: str | None = "ephemeral") -> Receipt:
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise HarnessSetupError(f"registration receipt is not JSON: {text[:200]!r}") from exc
    if not isinstance(raw, dict):
        raise HarnessSetupError("registration receipt is not a JSON object")
    missing = [
        field for field in RECEIPT_FIELDS if not isinstance(raw.get(field), str) or not raw[field]
    ]
    if missing:
        raise HarnessSetupError(f"registration receipt lacks fields: {', '.join(missing)}")
    if expected_identity_mode is not None and raw["identityMode"] != expected_identity_mode:
        raise HarnessSetupError(
            f"registration key admitted a {raw['identityMode']!r} environment; "
            f"the benchmark key must be {expected_identity_mode!r}"
        )
    return Receipt(
        environment_id=raw["environmentId"],
        incarnation_id=raw["incarnationId"],
        daemon_id=raw["daemonId"],
        connection_id=raw["connectionId"],
        identity_mode=raw["identityMode"],
        raw=raw,
    )


def _probe_command(paths: SandboxPaths) -> str:
    receipt, pid = _q(paths.receipt_file), _q(paths.pid_file)
    return (
        f"if [ -s {receipt} ]; then cat {receipt}; "
        f'elif kill -0 "$(cat {pid} 2>/dev/null)" 2>/dev/null; then echo {_ALIVE}; '
        f"else echo {_DEAD}; fi"
    )


async def wait_for_receipt(
    environment: EnvironmentLike,
    paths: SandboxPaths,
    *,
    timeout_sec: float = 90.0,
    poll_sec: float = 1.0,
    expected_identity_mode: str | None = "ephemeral",
) -> Receipt:
    """Poll until the daemon writes its receipt. A daemon exit before the receipt is
    a terminal rejection (bad key, closed environment, protocol mismatch)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    probe = _probe_command(paths)
    while True:
        result = await environment.exec(probe, timeout_sec=30)
        output = (result.stdout or "").strip()
        if output and output != _ALIVE and output != _DEAD and result.return_code == 0:
            return parse_receipt(output, expected_identity_mode=expected_identity_mode)
        if output == _DEAD:
            tail = await read_log_tail(environment, paths)
            raise HarnessSetupError(
                "envd exited before writing a registration receipt "
                f"(terminal rejection or startup failure):\n{tail}"
            )
        if loop.time() >= deadline:
            tail = await read_log_tail(environment, paths)
            raise HarnessSetupError(
                f"no registration receipt after {timeout_sec:.0f}s "
                f"(gateway unreachable or registration deferred):\n{tail}"
            )
        await asyncio.sleep(poll_sec)


async def read_log_tail(environment: EnvironmentLike, paths: SandboxPaths, lines: int = 40) -> str:
    result = await environment.exec(
        f"tail -n {int(lines)} {_q(paths.log_file)} 2>/dev/null || true", timeout_sec=30
    )
    return (result.stdout or "").strip()


def stop_command(paths: SandboxPaths, *, grace_sec: float = 5.0) -> str:
    """Terminate the daemon's process group, then kill it. Never fails: the sandbox
    must stay intact for the verifier whatever state the daemon is in."""
    pid = _q(paths.pid_file)
    ticks = max(1, int(grace_sec / 0.25))
    return (
        f'pid="$(cat {pid} 2>/dev/null)" || exit 0\n'
        '[ -n "$pid" ] || exit 0\n'
        'kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || exit 0\n'
        f"i=0; while [ $i -lt {ticks} ]; do "
        'kill -0 "$pid" 2>/dev/null || exit 0; sleep 0.25; i=$((i+1)); done\n'
        'kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true\n'
        "exit 0\n"
    )


async def stop(
    environment: EnvironmentLike, paths: SandboxPaths, *, grace_sec: float = 5.0
) -> None:
    await environment.exec(
        stop_command(paths, grace_sec=grace_sec), timeout_sec=int(grace_sec) + 30
    )
