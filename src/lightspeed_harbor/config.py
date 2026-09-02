"""Host-side settings and per-agent kwargs for the Lightspeed Harbor adapter.

Everything here fails closed: a missing or inconsistent value raises
``ConfigError`` before Harbor builds a sandbox or a model call is made. Secrets
are read from the host environment only and are never echoed back through
``redacted()``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ENV_API_URL = "LIGHTSPEED_API_URL"
ENV_API_KEY = "LIGHTSPEED_API_KEY"
ENV_REGISTRATION_KEY = "LIGHTSPEED_HARBOR_REGISTRATION_KEY"
ENV_GATEWAY_URL = "LIGHTSPEED_ENVD_GATEWAY_URL"
ENV_ENVD_PATH = "LIGHTSPEED_HARBOR_ENVD_PATH"
ENV_ENVD_RELEASE_URL = "LIGHTSPEED_HARBOR_ENVD_RELEASE_URL"
ENV_ENVD_SHA256 = "LIGHTSPEED_HARBOR_ENVD_SHA256"
ENV_ENVD_VERSION = "LIGHTSPEED_HARBOR_ENVD_VERSION"

REQUIRED_HOST_VARS = (ENV_API_URL, ENV_API_KEY, ENV_REGISTRATION_KEY, ENV_GATEWAY_URL)

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class ConfigError(ValueError):
    """Adapter configuration is missing, malformed, or inconsistent."""


@dataclass(frozen=True)
class HostSettings:
    """Values the adapter process reads on the Harbor host.

    ``api_key`` and ``registration_key`` are secrets. The registration key is
    the only one that ever enters a sandbox, as a mode-0600 file that is
    deleted once the registration receipt appears.
    """

    api_url: str
    api_key: str
    registration_key: str
    gateway_url: str
    envd_path: Path | None = None
    envd_release_url: str | None = None
    envd_sha256: str | None = None
    envd_version: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> HostSettings:
        missing = [name for name in REQUIRED_HOST_VARS if not env.get(name)]
        if missing:
            raise ConfigError(f"missing required host settings: {', '.join(missing)}")

        api_url = env[ENV_API_URL]
        if urlsplit(api_url).scheme not in {"http", "https"}:
            raise ConfigError(f"{ENV_API_URL} must be an http(s) URL")

        gateway_url = env[ENV_GATEWAY_URL]
        _validate_gateway_url(gateway_url)

        envd_path = Path(env[ENV_ENVD_PATH]) if env.get(ENV_ENVD_PATH) else None
        release_url = env.get(ENV_ENVD_RELEASE_URL) or None
        sha256 = env.get(ENV_ENVD_SHA256) or None
        if envd_path is None and (release_url is None or sha256 is None):
            raise ConfigError(
                f"select an envd artifact: set {ENV_ENVD_PATH} for a local binary, "
                f"or both {ENV_ENVD_RELEASE_URL} and {ENV_ENVD_SHA256} for a pinned release"
            )
        if envd_path is not None and release_url is not None:
            raise ConfigError(f"{ENV_ENVD_PATH} and {ENV_ENVD_RELEASE_URL} are mutually exclusive")

        return cls(
            api_url=api_url,
            api_key=env[ENV_API_KEY],
            registration_key=env[ENV_REGISTRATION_KEY],
            gateway_url=gateway_url,
            envd_path=envd_path,
            envd_release_url=release_url,
            envd_sha256=sha256,
            envd_version=env.get(ENV_ENVD_VERSION) or None,
        )

    def redacted(self) -> dict[str, str | None]:
        """Non-secret view for provenance and logs."""
        return {
            "api_url": self.api_url,
            "gateway_url": self.gateway_url,
            "envd_path": str(self.envd_path) if self.envd_path else None,
            "envd_release_url": self.envd_release_url,
            "envd_sha256": self.envd_sha256,
            "envd_version": self.envd_version,
        }


def _validate_gateway_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme == "wss":
        return
    if parts.scheme == "ws" and parts.hostname in _LOOPBACK_HOSTS:
        return
    raise ConfigError(f"{ENV_GATEWAY_URL} must be wss://, or ws:// toward loopback only")


@dataclass(frozen=True)
class AgentSettings:
    """Per-agent values from Harbor's ``model_name`` and ``agents[].kwargs``."""

    model_name: str
    model_provider: str
    model_id: str
    lightspeed_provider_id: str
    profile_id: str
    reasoning_effort: str | None = None

    @classmethod
    def resolve(
        cls,
        *,
        model_name: str | None,
        lightspeed_provider_id: str | None,
        profile_id: str | None,
        reasoning_effort: str | None = None,
    ) -> AgentSettings:
        if not model_name:
            raise ConfigError("model_name is required; the adapter never infers a model")
        provider, sep, model_id = model_name.partition("/")
        if not sep or not provider or not model_id:
            raise ConfigError("model_name must be '<provider>/<immutable-model-id>'")
        if not lightspeed_provider_id:
            raise ConfigError("kwargs.lightspeed_provider_id is required")
        if not profile_id:
            raise ConfigError("kwargs.profile_id is required")
        return cls(
            model_name=model_name,
            model_provider=provider,
            model_id=model_id,
            lightspeed_provider_id=lightspeed_provider_id,
            profile_id=profile_id,
            reasoning_effort=reasoning_effort or None,
        )
