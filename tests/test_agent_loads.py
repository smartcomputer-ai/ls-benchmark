"""Harbor can load the adapter through its import path without a source patch."""

from __future__ import annotations

from pathlib import Path

import pytest
from harbor.agents.factory import AgentFactory

from lightspeed_harbor import __version__
from lightspeed_harbor.agent import AGENT_NAME, LightspeedAgent
from lightspeed_harbor.config import ConfigError, HostSettings

IMPORT_PATH = "lightspeed_harbor.agent:LightspeedAgent"


def test_factory_creates_agent_from_import_path(tmp_path: Path, host_settings: HostSettings):
    agent = AgentFactory.create_agent_from_import_path(
        IMPORT_PATH,
        logs_dir=tmp_path,
        model_name="openai/model-snapshot",
        lightspeed_provider_id="provider_1",
        profile_id="inline",
        reasoning_effort="high",
        host_settings=host_settings,
    )
    assert isinstance(agent, LightspeedAgent)
    assert agent.name() == AGENT_NAME
    assert agent.version() == __version__
    assert LightspeedAgent.import_path() == IMPORT_PATH
    assert agent.settings.model_provider == "openai"
    assert agent.settings.model_id == "model-snapshot"
    assert agent.settings.reasoning_effort == "high"
    info = agent.to_agent_info()
    assert info.name == AGENT_NAME
    assert info.model_info is not None
    assert info.model_info.provider == "openai"


def test_host_settings_come_from_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LIGHTSPEED_API_URL", "https://lightspeed.example/rpc")
    monkeypatch.setenv("LIGHTSPEED_API_KEY", "lsk_test")
    monkeypatch.setenv("LIGHTSPEED_HARBOR_REGISTRATION_KEY", "lsrk_test")
    monkeypatch.setenv("LIGHTSPEED_ENVD_GATEWAY_URL", "wss://lightspeed.example/connect")
    monkeypatch.setenv("LIGHTSPEED_HARBOR_ENVD_PATH", "/opt/lightspeed-envd")
    agent = LightspeedAgent(
        logs_dir=tmp_path,
        model_name="openai/model-snapshot",
        lightspeed_provider_id="provider_1",
        profile_id="inline",
        # Harbor's agents[].env wins over the process environment.
        extra_env={"LIGHTSPEED_API_URL": "https://override.example/rpc"},
    )
    assert agent.host.api_url == "https://override.example/rpc"
    assert agent.host.envd_path == Path("/opt/lightspeed-envd")


def test_model_name_is_required(tmp_path: Path, host_settings: HostSettings):
    with pytest.raises(ConfigError, match="model_name"):
        LightspeedAgent(
            logs_dir=tmp_path,
            lightspeed_provider_id="provider_1",
            profile_id="inline",
            host_settings=host_settings,
        )


def test_provider_and_profile_are_required(tmp_path: Path, host_settings: HostSettings):
    with pytest.raises(ConfigError, match="lightspeed_provider_id"):
        LightspeedAgent(
            logs_dir=tmp_path,
            model_name="openai/model-snapshot",
            profile_id="inline",
            host_settings=host_settings,
        )
    with pytest.raises(ConfigError, match="profile_id"):
        LightspeedAgent(
            logs_dir=tmp_path,
            model_name="openai/model-snapshot",
            lightspeed_provider_id="provider_1",
            host_settings=host_settings,
        )


def test_named_profiles_are_not_supported_yet(tmp_path: Path, host_settings: HostSettings):
    with pytest.raises(ConfigError, match="named Lightspeed profiles"):
        LightspeedAgent(
            logs_dir=tmp_path,
            model_name="openai/model-snapshot",
            lightspeed_provider_id="provider_1",
            profile_id="harbor-terminal",
            host_settings=host_settings,
        )


def test_optional_limits_are_validated(tmp_path: Path, host_settings: HostSettings):
    with pytest.raises(ConfigError, match="max_turns"):
        LightspeedAgent(
            logs_dir=tmp_path,
            model_name="openai/model-snapshot",
            lightspeed_provider_id="provider_1",
            profile_id="inline",
            max_turns=0,
            host_settings=host_settings,
        )
