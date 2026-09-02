from __future__ import annotations

import pytest

from lightspeed_harbor.config import ConfigError, HostSettings

BASE = {
    "LIGHTSPEED_API_URL": "https://lightspeed.example/rpc",
    "LIGHTSPEED_API_KEY": "lsk_test",
    "LIGHTSPEED_HARBOR_REGISTRATION_KEY": "lsrk_test",
    "LIGHTSPEED_ENVD_GATEWAY_URL": "wss://lightspeed.example/environment-gateway/connect",
    "LIGHTSPEED_HARBOR_ENVD_PATH": "/opt/lightspeed-envd",
}


def test_missing_required_names_are_listed():
    with pytest.raises(ConfigError) as excinfo:
        HostSettings.from_env({})
    message = str(excinfo.value)
    for name in (
        "LIGHTSPEED_API_URL",
        "LIGHTSPEED_API_KEY",
        "LIGHTSPEED_HARBOR_REGISTRATION_KEY",
        "LIGHTSPEED_ENVD_GATEWAY_URL",
    ):
        assert name in message


def test_plain_ws_is_loopback_only():
    with pytest.raises(ConfigError, match="wss"):
        HostSettings.from_env({**BASE, "LIGHTSPEED_ENVD_GATEWAY_URL": "ws://gateway.example/c"})
    settings = HostSettings.from_env(
        {**BASE, "LIGHTSPEED_ENVD_GATEWAY_URL": "ws://127.0.0.1:18080/environment-gateway/connect"}
    )
    assert settings.gateway_url.startswith("ws://127.0.0.1")


def test_envd_artifact_selection_is_required_and_exclusive():
    without_artifact = {k: v for k, v in BASE.items() if k != "LIGHTSPEED_HARBOR_ENVD_PATH"}
    with pytest.raises(ConfigError, match="envd artifact"):
        HostSettings.from_env(without_artifact)
    with pytest.raises(ConfigError, match="mutually exclusive"):
        HostSettings.from_env(
            {
                **BASE,
                "LIGHTSPEED_HARBOR_ENVD_RELEASE_URL": "https://releases.example/envd",
                "LIGHTSPEED_HARBOR_ENVD_SHA256": "0" * 64,
            }
        )
    release = HostSettings.from_env(
        {
            **without_artifact,
            "LIGHTSPEED_HARBOR_ENVD_RELEASE_URL": "https://releases.example/envd",
            "LIGHTSPEED_HARBOR_ENVD_SHA256": "0" * 64,
        }
    )
    assert release.envd_path is None
    assert release.envd_sha256 == "0" * 64


def test_redacted_view_has_no_secrets():
    settings = HostSettings.from_env(BASE)
    redacted = settings.redacted()
    assert "api_key" not in redacted
    assert "registration_key" not in redacted
    assert "lsk_test" not in str(redacted)
    assert "lsrk_test" not in str(redacted)
