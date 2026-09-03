from __future__ import annotations

import io
import re
import tarfile
from pathlib import Path
from uuid import UUID

import pytest

from lightspeed_harbor.config import HostSettings
from lightspeed_harbor.envd import (
    METADATA_MAX_VALUE_BYTES,
    SandboxPaths,
    _install_downloaded,
    correlation_metadata,
    parse_receipt,
    platform_from_uname,
    resolve_artifact,
    sha256_file,
    start_command,
    stop_command,
)
from lightspeed_harbor.errors import HarnessSetupError
from tests.fakes import RECEIPT

# --- correlation metadata (unchanged contract) -------------------------------


def test_correlation_metadata_uses_context_id_and_omits_unknown_fields():
    context_id = UUID("594025f3-7d65-4655-8576-4bee95002eae")
    metadata = correlation_metadata(
        context_id=context_id, session_id="hello-world__bZZeEkw__agent", attempt=2
    )
    assert metadata["source"] == "harbor"
    assert metadata["agent"] == "lightspeed"
    assert metadata["harborContextId"] == str(context_id)
    assert metadata["harborSessionId"] == "hello-world__bZZeEkw__agent"
    assert metadata["harborAttempt"] == "2"
    assert "harborJobId" not in metadata
    assert "harborTrialId" not in metadata
    assert "harborTaskName" not in metadata


def test_correlation_metadata_bounds_values():
    metadata = correlation_metadata(context_id=None, session_id=None, task_name="x" * 1000)
    assert len(metadata["harborTaskName"].encode()) == METADATA_MAX_VALUE_BYTES


def test_correlation_metadata_never_contains_secrets():
    metadata = correlation_metadata(context_id=None, session_id="s")
    assert not any(key.lower().endswith("key") for key in metadata)


def test_reserved_prefix_is_rejected():
    with pytest.raises(ValueError, match="reserved"):
        correlation_metadata(context_id=None, session_id="s", extra={"lightspeed.internal": "x"})


def test_entry_limit_is_enforced():
    extra = {f"k{i}": "v" for i in range(40)}
    with pytest.raises(ValueError, match="entries"):
        correlation_metadata(context_id=None, session_id="s", extra=extra)


# --- platform and artifact ---------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "platform"),
    [("x86_64\n", "linux/amd64"), ("aarch64", "linux/arm64"), ("arm64", "linux/arm64")],
)
def test_platform_from_uname(machine: str, platform: str):
    assert platform_from_uname(machine) == platform


def test_unsupported_platform_fails_before_any_model_call():
    with pytest.raises(HarnessSetupError, match="unsupported sandbox architecture"):
        platform_from_uname("riscv64")


def _host(tmp_path: Path, **overrides) -> HostSettings:
    values = dict(
        api_url="https://ls.example/rpc",
        api_key="lsk_test",
        registration_key="lsrk_test",
        gateway_url="wss://ls.example/environment-gateway/connect",
    )
    values.update(overrides)
    return HostSettings(**values)


async def test_local_artifact_digest_is_computed_and_checked(tmp_path: Path):
    binary = tmp_path / "lightspeed-envd"
    binary.write_bytes(b"#!/bin/sh\necho envd\n")
    digest = sha256_file(binary)
    artifact = await resolve_artifact(
        _host(tmp_path, envd_path=binary), target="x86_64-unknown-linux-musl"
    )
    assert artifact.sha256 == digest
    assert artifact.source == "local"
    with pytest.raises(HarnessSetupError, match="SHA-256"):
        await resolve_artifact(
            _host(tmp_path, envd_path=binary, envd_sha256="0" * 64),
            target="x86_64-unknown-linux-musl",
        )


async def test_missing_local_artifact_fails(tmp_path: Path):
    with pytest.raises(HarnessSetupError, match="not found"):
        await resolve_artifact(
            _host(tmp_path, envd_path=tmp_path / "nope"), target="x86_64-unknown-linux-musl"
        )


def test_release_archive_extracts_single_binary(tmp_path: Path):
    archive = tmp_path / "lightspeed-envd-0.1.0-x86_64-unknown-linux-musl.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        data = b"ELF-envd"
        info = tarfile.TarInfo("lightspeed-envd")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    target = tmp_path / "out" / "lightspeed-envd"
    target.parent.mkdir()
    _install_downloaded(archive, target)
    assert target.read_bytes() == b"ELF-envd"
    assert target.stat().st_mode & 0o111


def test_release_archive_with_two_binaries_is_rejected(tmp_path: Path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("a/lightspeed-envd", "b/envd"):
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(HarnessSetupError, match="exactly one"):
        _install_downloaded(archive, tmp_path / "lightspeed-envd")


# --- receipt -----------------------------------------------------------------


def test_receipt_parses_and_requires_all_ids():
    receipt = parse_receipt(__import__("json").dumps(RECEIPT))
    assert receipt.environment_id == "environment_1"
    assert receipt.identity_mode == "ephemeral"
    with pytest.raises(HarnessSetupError, match="lacks fields: connectionId"):
        parse_receipt(
            '{"environmentId":"e","incarnationId":"i","daemonId":"d","identityMode":"ephemeral"}'
        )
    with pytest.raises(HarnessSetupError, match="not JSON"):
        parse_receipt("registering with wss://...")


def test_receipt_identity_mode_must_match_key_policy():
    with pytest.raises(HarnessSetupError, match="must be 'ephemeral'"):
        parse_receipt(__import__("json").dumps({**RECEIPT, "identityMode": "persistent"}))


# --- start and stop commands ------------------------------------------------


def test_start_command_passes_key_by_file_only():
    paths = SandboxPaths()
    command = start_command(
        paths,
        gateway_url="wss://ls.example/environment-gateway/connect",
        cwd="/app",
        metadata={"source": "harbor", "harborContextId": "ctx"},
        display_name="task__x__agent",
        with_ca_file=True,
    )
    assert f"LIGHTSPEED_ENVD_REGISTRATION_KEY_FILE={paths.key_file}" in command
    assert re.search(r"LIGHTSPEED_ENVD_REGISTRATION_KEY=", command) is None
    assert "lsrk_" not in command
    assert f"LIGHTSPEED_ENVD_REGISTRATION_RECEIPT={paths.receipt_file}" in command
    assert "LIGHTSPEED_ENVD_FS_ROOT=/" in command
    assert "LIGHTSPEED_ENVD_CWD=/app" in command
    assert f"LIGHTSPEED_ENVD_CA_FILE={paths.ca_file}" in command
    assert '\'{"source":"harbor","harborContextId":"ctx"}\'' in command
    assert "setsid nohup" in command
    assert str(paths.log_file) in command
    assert f"echo $! > {paths.pid_file}" in command


def test_stop_command_is_harmless_without_a_daemon():
    command = stop_command(SandboxPaths(), grace_sec=2)
    assert command.startswith('pid="$(cat /tmp/lightspeed-harbor/envd.pid 2>/dev/null)" || exit 0')
    assert 'kill -TERM -- "-$pid"' in command
    assert 'kill -KILL -- "-$pid"' in command
    assert command.rstrip().endswith("exit 0")
