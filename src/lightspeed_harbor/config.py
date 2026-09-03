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
ENV_ENVD_CA_FILE = "LIGHTSPEED_HARBOR_ENVD_CA_FILE"
ENV_ENVD_DISCOVERY_URL = "LIGHTSPEED_HARBOR_ENVD_DISCOVERY_URL"
ENV_ENVD_ALLOW_MISMATCH = "LIGHTSPEED_HARBOR_ENVD_ALLOW_MISMATCH"
ENV_UNIVERSE = "LIGHTSPEED_UNIVERSE"
ENV_CAMPAIGN = "LIGHTSPEED_HARBOR_CAMPAIGN"
ENV_SESSION_TTL_SEC = "LIGHTSPEED_HARBOR_SESSION_TTL_SEC"

DISCOVERY_PATH = "/.well-known/lightspeed-envd"
DEFAULT_SESSION_TTL_SEC = 14 * 24 * 3600

REQUIRED_HOST_VARS = (ENV_API_URL, ENV_API_KEY, ENV_REGISTRATION_KEY, ENV_GATEWAY_URL)

# The only profile selector implemented so far: the adapter builds the
# terminal-only session config itself. A committed Lightspeed profile
# (``harbor-terminal``) is a later slice; naming one today is a config error.
PROFILE_INLINE = "inline"

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
    envd_ca_file: Path | None = None
    # Sent as ``x-lightspeed-universe`` for a ``trusted-header`` gateway (the
    # ``./dev.sh`` full profile). ``single`` and ``api-key`` gateways reject it.
    universe: str | None = None
    # Discovery document (P152) with the envd archive that matches the
    # deployment; used when neither a local path nor a pinned release is set.
    envd_discovery_url: str | None = None
    # Development only: accept an envd whose build does not match the server's.
    envd_allow_mismatch: bool = False
    # Session metadata ``campaign`` and automatic deletion after close (P153/P154).
    campaign: str | None = None
    session_ttl_sec: int | None = DEFAULT_SESSION_TTL_SEC

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
        sha256 = (env.get(ENV_ENVD_SHA256) or None) and env[ENV_ENVD_SHA256].strip().lower()
        if envd_path is None and release_url is not None and sha256 is None:
            raise ConfigError(f"{ENV_ENVD_RELEASE_URL} requires {ENV_ENVD_SHA256}")
        if envd_path is not None and release_url is not None:
            raise ConfigError(f"{ENV_ENVD_PATH} and {ENV_ENVD_RELEASE_URL} are mutually exclusive")
        if sha256 is not None and (
            len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256)
        ):
            raise ConfigError(f"{ENV_ENVD_SHA256} must be 64 hex characters")

        ca_file = Path(env[ENV_ENVD_CA_FILE]) if env.get(ENV_ENVD_CA_FILE) else None
        if ca_file is not None and not ca_file.is_file():
            raise ConfigError(f"{ENV_ENVD_CA_FILE} does not name a readable file: {ca_file}")

        discovery = (env.get(ENV_ENVD_DISCOVERY_URL) or "").strip() or _discovery_url(api_url)
        ttl_raw = (env.get(ENV_SESSION_TTL_SEC) or "").strip()
        if ttl_raw:
            if not ttl_raw.isdigit():
                raise ConfigError(
                    f"{ENV_SESSION_TTL_SEC} must be a whole number of seconds (0 disables)"
                )
            session_ttl: int | None = int(ttl_raw) or None
        else:
            session_ttl = DEFAULT_SESSION_TTL_SEC

        return cls(
            api_url=api_url,
            api_key=env[ENV_API_KEY],
            registration_key=env[ENV_REGISTRATION_KEY],
            gateway_url=gateway_url,
            envd_path=envd_path,
            envd_release_url=release_url,
            envd_sha256=sha256,
            envd_version=env.get(ENV_ENVD_VERSION) or None,
            envd_ca_file=ca_file,
            universe=(env.get(ENV_UNIVERSE) or "").strip() or None,
            envd_discovery_url=discovery,
            envd_allow_mismatch=(env.get(ENV_ENVD_ALLOW_MISMATCH) or "").strip()
            in {"1", "true", "yes"},
            campaign=(env.get(ENV_CAMPAIGN) or "").strip() or None,
            session_ttl_sec=session_ttl,
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
            "envd_ca_file": str(self.envd_ca_file) if self.envd_ca_file else None,
            "universe": self.universe,
            "envd_discovery_url": self.envd_discovery_url,
            "envd_allow_mismatch": self.envd_allow_mismatch,
            "campaign": self.campaign,
            "session_ttl_sec": self.session_ttl_sec,
        }

    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in any artifact or log."""
        return tuple(value for value in (self.api_key, self.registration_key) if value)


def _discovery_url(api_url: str) -> str:
    """``https://host/rpc`` -> ``https://host/.well-known/lightspeed-envd``."""
    parts = urlsplit(api_url)
    return f"{parts.scheme}://{parts.netloc}{DISCOVERY_PATH}"


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
    api_kind: str | None = None
    max_turns: int | None = None
    max_output_tokens: int | None = None

    @classmethod
    def resolve(
        cls,
        *,
        model_name: str | None,
        lightspeed_provider_id: str | None,
        profile_id: str | None,
        reasoning_effort: str | None = None,
        api_kind: str | None = None,
        max_turns: int | None = None,
        max_output_tokens: int | None = None,
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
        if profile_id != PROFILE_INLINE:
            raise ConfigError(
                f"kwargs.profile_id must be {PROFILE_INLINE!r}: named Lightspeed profiles are "
                "not supported yet (docs/next-steps.md, path to the first run, step 5)"
            )
        for name, value in (("max_turns", max_turns), ("max_output_tokens", max_output_tokens)):
            if value is not None and (not isinstance(value, int) or value <= 0):
                raise ConfigError(f"kwargs.{name} must be a positive integer")
        return cls(
            model_name=model_name,
            model_provider=provider,
            model_id=model_id,
            lightspeed_provider_id=lightspeed_provider_id,
            profile_id=profile_id,
            reasoning_effort=reasoning_effort or None,
            api_kind=api_kind or None,
            max_turns=max_turns,
            max_output_tokens=max_output_tokens,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "model_name": self.model_name,
            "model_provider": self.model_provider,
            "model_id": self.model_id,
            "lightspeed_provider_id": self.lightspeed_provider_id,
            "profile_id": self.profile_id,
            "reasoning_effort": self.reasoning_effort,
            "api_kind": self.api_kind,
            "max_turns": self.max_turns,
            "max_output_tokens": self.max_output_tokens,
        }
