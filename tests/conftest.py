from __future__ import annotations

import pytest

from lightspeed_harbor.config import HostSettings


@pytest.fixture
def host_settings() -> HostSettings:
    return HostSettings(
        api_url="https://lightspeed.example/rpc",
        api_key="lsk_test",
        registration_key="lsrk_test",
        gateway_url="wss://lightspeed.example/environment-gateway/connect",
        envd_release_url="https://releases.example/envd",
        envd_sha256="0" * 64,
    )
