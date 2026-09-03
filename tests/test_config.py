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


def test_envd_artifact_precedence_and_discovery_default():
    without_path = {k: v for k, v in BASE.items() if k != "LIGHTSPEED_HARBOR_ENVD_PATH"}
    # Nothing configured: the deployment's discovery document, derived from the API URL.
    discovered = HostSettings.from_env(without_path)
    assert discovered.envd_path is None
    assert discovered.envd_discovery_url == "https://lightspeed.example/.well-known/lightspeed-envd"
    assert (
        HostSettings.from_env(
            {
                **without_path,
                "LIGHTSPEED_HARBOR_ENVD_DISCOVERY_URL": "https://mirror.example/envd.json",
            }
        ).envd_discovery_url
        == "https://mirror.example/envd.json"
    )
    with pytest.raises(ConfigError, match="requires LIGHTSPEED_HARBOR_ENVD_SHA256"):
        HostSettings.from_env(
            {**without_path, "LIGHTSPEED_HARBOR_ENVD_RELEASE_URL": "https://releases.example/envd"}
        )
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
            **without_path,
            "LIGHTSPEED_HARBOR_ENVD_RELEASE_URL": "https://releases.example/envd",
            "LIGHTSPEED_HARBOR_ENVD_SHA256": "0" * 64,
        }
    )
    assert release.envd_path is None
    assert release.envd_sha256 == "0" * 64


def test_campaign_and_session_ttl():
    settings = HostSettings.from_env(BASE)
    assert settings.campaign is None
    assert settings.session_ttl_sec == 14 * 24 * 3600
    custom = HostSettings.from_env(
        {**BASE, "LIGHTSPEED_HARBOR_CAMPAIGN": " tb2 ", "LIGHTSPEED_HARBOR_SESSION_TTL_SEC": "0"}
    )
    assert custom.campaign == "tb2"
    assert custom.session_ttl_sec is None
    with pytest.raises(ConfigError, match="whole number"):
        HostSettings.from_env({**BASE, "LIGHTSPEED_HARBOR_SESSION_TTL_SEC": "3d"})


def test_redacted_view_has_no_secrets():
    settings = HostSettings.from_env(BASE)
    redacted = settings.redacted()
    assert "api_key" not in redacted
    assert "registration_key" not in redacted
    assert "lsk_test" not in str(redacted)
    assert "lsrk_test" not in str(redacted)


def test_universe_is_optional_and_trimmed():
    assert HostSettings.from_env(BASE).universe is None
    settings = HostSettings.from_env(
        {**BASE, "LIGHTSPEED_UNIVERSE": " 00000000-0000-0000-0000-000000000001 "}
    )
    assert settings.universe == "00000000-0000-0000-0000-000000000001"
    assert settings.redacted()["universe"] == settings.universe
