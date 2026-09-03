"""``LightspeedAgent``: Harbor's external ``BaseAgent`` for the Lightspeed agent system.

The agent loop runs in hosted Lightspeed, not in the task container. This
class runs in Harbor's orchestrator process and uses the Harbor environment
only to upload and start ``lightspeed-envd`` before registration. Everything
after that goes through the Lightspeed API and the registered environment.

Job configuration:

.. code-block:: yaml

    agents:
      - import_path: lightspeed_harbor.agent:LightspeedAgent
        model_name: openai/<immutable-model-id>
        kwargs:
          lightspeed_provider_id: <provider-id>
          profile_id: inline
          reasoning_effort: <effort>
          # optional: api_kind, max_turns, max_output_tokens,
          # registration_timeout_sec, poll_interval_sec, cleanup_timeout_sec
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import platform as host_platform
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import harbor
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from lightspeed_harbor import __version__, artifacts, envd
from lightspeed_harbor.client import RUN_TERMINAL_STATUSES, LightspeedClient, LightspeedError
from lightspeed_harbor.config import AgentSettings, HostSettings
from lightspeed_harbor.errors import (
    FAILURE_AGENT_EXECUTION,
    FAILURE_ARTIFACT_ONLY,
    AdapterError,
    HarnessSetupError,
)

AGENT_NAME = "lightspeed"
_EVENT_PAGE_LIMIT = 500
_EVENT_MAX_PAGES = 40
_MODEL_LIST_ATTEMPTS = 4
_MODEL_LIST_BACKOFF_SEC = 2.0


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def build_session_config(
    settings: AgentSettings, model: dict[str, str], registration_key_id: str | None
) -> dict[str, Any]:
    """The terminal-only inline profile: one model route, the environment tool
    surface scoped to the campaign key, no MCP, web, sub-agents, VFS, or skills."""
    environments: dict[str, Any] = {"selectionTools": False}
    if registration_key_id:
        environments["registrationKeys"] = [registration_key_id]
    config: dict[str, Any] = {
        "model": {
            "providerId": model["providerId"],
            "model": model["model"],
            "apiKind": model["apiKind"],
        },
        "features": {"environments": environments},
    }
    generation: dict[str, Any] = {}
    if settings.reasoning_effort:
        generation["reasoningEffort"] = settings.reasoning_effort
    if settings.max_output_tokens:
        generation["maxOutputTokens"] = settings.max_output_tokens
    if generation:
        config["generation"] = generation
    if settings.max_turns:
        config["limits"] = {"maxTurns": settings.max_turns}
    return config


@dataclass
class _RunState:
    """Everything one trial learns, for cleanup ordering and the artifacts."""

    started_at: str = field(default_factory=_utcnow)
    finished_at: str | None = None
    client: LightspeedClient | None = None
    receipt: envd.Receipt | None = None
    key_deleted: bool = False
    environment_view: dict[str, Any] | None = None
    registration_key_id: str | None = None
    initialize: dict[str, Any] | None = None
    model: dict[str, str] | None = None
    session_config: dict[str, Any] | None = None
    session_id: str | None = None
    run_id: str | None = None
    status: str | None = None
    usage: dict[str, Any] | None = None
    run_view: dict[str, Any] | None = None
    error: str | None = None
    failure_class: str | None = None
    cancelled: bool = False
    timings: dict[str, float] = field(default_factory=dict)
    cleanup: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    instruction_sha256: str | None = None
    instruction_bytes: int | None = None


class LightspeedAgent(BaseAgent):
    # Raw Lightspeed events are exported until a faithful ATIF mapping exists.
    SUPPORTS_ATIF = False

    def __init__(
        self,
        logs_dir: Path,
        model_name: str | None = None,
        *,
        lightspeed_provider_id: str | None = None,
        profile_id: str | None = None,
        reasoning_effort: str | None = None,
        api_kind: str | None = None,
        max_turns: int | None = None,
        max_output_tokens: int | None = None,
        registration_timeout_sec: float = 90.0,
        poll_interval_sec: float = 2.0,
        cleanup_timeout_sec: float = 60.0,
        host_settings: HostSettings | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(logs_dir=logs_dir, model_name=model_name, **kwargs)
        # Validate at construction so a bad configuration stops the job before
        # Harbor builds any sandbox. Harbor's ``agents[].env`` (``extra_env``)
        # takes precedence over the process environment, matching BaseAgent.
        self.settings = AgentSettings.resolve(
            model_name=model_name,
            lightspeed_provider_id=lightspeed_provider_id,
            profile_id=profile_id,
            reasoning_effort=reasoning_effort,
            api_kind=api_kind,
            max_turns=max_turns,
            max_output_tokens=max_output_tokens,
        )
        self.host = host_settings or HostSettings.from_env({**os.environ, **self._extra_env})
        self.paths = envd.SandboxPaths()
        self.registration_timeout_sec = float(registration_timeout_sec)
        self.poll_interval_sec = float(poll_interval_sec)
        self.cleanup_timeout_sec = float(cleanup_timeout_sec)
        self._platform: str | None = None
        self._artifact: envd.EnvdArtifact | None = None
        self._envd_version: str | None = None

    @staticmethod
    def name() -> str:
        return AGENT_NAME

    def version(self) -> str | None:
        return __version__

    # --- setup -------------------------------------------------------------

    async def setup(self, environment: BaseEnvironment) -> None:
        """Upload and verify ``envd`` in the sandbox. Does not start it and creates
        no durable remote state, so a failure here costs no model call."""
        self._platform = await envd.detect_platform(environment)
        target = envd.SUPPORTED_TARGETS[self._platform]
        self._artifact = await envd.resolve_artifact(self.host, target=target)
        self._envd_version = await envd.install(
            environment, self._artifact, self.paths, ca_file=self.host.envd_ca_file
        )
        if self.host.envd_version and self.host.envd_version not in self._envd_version:
            raise HarnessSetupError(
                f"envd reports {self._envd_version!r}, expected version {self.host.envd_version!r}"
            )
        self.logger.info(
            "lightspeed-envd %s (%s, sha256 %s) installed at %s",
            self._envd_version,
            self._platform,
            self._artifact.sha256[:12],
            self.paths.binary,
        )

    # --- run ---------------------------------------------------------------

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Perform exactly one Harbor trial through hosted Lightspeed.

        Register ``envd``, delete the key file, start a session on the receipt's
        exact environment, run the unmodified instruction, wait for a terminal
        status, project usage into ``context``, and clean up in ``finally``
        without touching the sandbox filesystem the verifier will inspect.
        """
        if self._artifact is None:
            raise HarnessSetupError("LightspeedAgent.setup() did not run before run()")
        state = _RunState()
        state.instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()
        state.instruction_bytes = len(instruction.encode())
        try:
            await self._execute(instruction, environment, context, state)
        except asyncio.CancelledError:
            state.cancelled = True
            state.error = "cancelled by Harbor (agent timeout or job cancellation)"
            state.failure_class = FAILURE_AGENT_EXECUTION
            raise
        except BaseException as exc:
            state.error = f"{type(exc).__name__}: {exc}"
            state.failure_class = (
                exc.failure_class if isinstance(exc, AdapterError) else FAILURE_AGENT_EXECUTION
            )
            raise
        finally:
            state.finished_at = _utcnow()
            await self._cleanup(environment, state)
            self._finish_context(context, state)
            self._write_artifacts(state)

    async def _execute(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
        state: _RunState,
    ) -> None:
        clock = time.monotonic()
        await envd.write_key_file(environment, self.paths, self.host.registration_key)
        state.metadata = envd.correlation_metadata(
            context_id=getattr(self, "context_id", None),
            session_id=getattr(self, "session_id", None),
        )
        command = envd.start_command(
            self.paths,
            gateway_url=self.host.gateway_url,
            cwd=await self._workdir(environment),
            metadata=state.metadata,
            display_name=getattr(self, "session_id", None),
            with_ca_file=self.host.envd_ca_file is not None,
        )
        await envd.start(environment, command)
        state.receipt = await envd.wait_for_receipt(
            environment, self.paths, timeout_sec=self.registration_timeout_sec
        )
        await envd.delete_key_file(environment, self.paths)
        state.key_deleted = True
        state.timings["registration_sec"] = round(time.monotonic() - clock, 3)

        client = self._make_client()
        await client.__aenter__()
        state.client = client
        state.initialize = await client.initialize()
        state.environment_view = (await client.environments_read(state.receipt.environment_id))[
            "environment"
        ]
        state.registration_key_id = self._validate_environment(
            state.environment_view, state.receipt
        )
        state.model = await self._resolve_model(client)
        state.session_config = build_session_config(
            self.settings, state.model, state.registration_key_id
        )
        session = (
            await client.session_start(
                display_name=getattr(self, "session_id", None), config=state.session_config
            )
        )["session"]
        state.session_id = session["id"]
        await client.session_environments_activate(state.session_id, state.receipt.environment_id)
        context_id = getattr(self, "context_id", None)
        submission_id = str(context_id) if context_id else str(uuid.uuid4())
        run = (
            await client.session_runs_start(
                state.session_id, instruction, submission_id=submission_id
            )
        )["run"]
        state.run_id = run["id"]
        state.status = run.get("status")
        state.timings["run_accepted_sec"] = round(time.monotonic() - clock, 3)

        state.run_view = await self._wait_for_run(client, state, context)
        state.timings["run_terminal_sec"] = round(time.monotonic() - clock, 3)
        if state.status != "completed":
            message = await self._failure_message(client, state)
            state.error = f"lightspeed run {state.status}" + (f": {message}" if message else "")
            state.failure_class = FAILURE_AGENT_EXECUTION
            # The verifier still runs on whatever the agent left behind, exactly as
            # it does for a Codex process that exits unhappily.
            self.logger.warning(
                "Lightspeed run %s ended %s: %s", state.run_id, state.status, message
            )

    # --- pieces of the run -------------------------------------------------

    def _make_client(self) -> LightspeedClient:
        return LightspeedClient(self.host.api_url, self.host.api_key, universe=self.host.universe)

    @staticmethod
    async def _workdir(environment: BaseEnvironment) -> str:
        """The directory Harbor runs agent commands in: the task's explicit
        ``workdir``, otherwise whatever the sandbox starts commands in (its
        image ``WORKDIR``), which is also where the Codex arm begins."""
        config = getattr(environment, "task_env_config", None)
        explicit = getattr(config, "workdir", None)
        if explicit:
            return str(explicit)
        result = await environment.exec("pwd", timeout_sec=30)
        cwd = (
            (result.stdout or "").strip().splitlines()[-1] if (result.stdout or "").strip() else ""
        )
        if result.return_code != 0 or not cwd.startswith("/"):
            raise HarnessSetupError(
                f"could not determine the sandbox working directory: {result.stderr!r}"
            )
        return cwd

    @staticmethod
    def _validate_environment(view: dict[str, Any], receipt: envd.Receipt) -> str:
        """The receipt names a live registered environment of ours; return its key id."""
        source = view.get("source") or {}
        if source.get("type") != "registered":
            raise HarnessSetupError(
                f"environment {receipt.environment_id} is not a registered environment"
            )
        if source.get("daemonId") != receipt.daemon_id:
            raise HarnessSetupError("environment daemon id does not match the registration receipt")
        if source.get("identityMode") != receipt.identity_mode:
            raise HarnessSetupError("environment identity mode does not match the receipt")
        incarnation = view.get("incarnation") or {}
        incarnation_id = incarnation.get("incarnationId") or incarnation.get("id")
        if incarnation_id and incarnation_id != receipt.incarnation_id:
            raise HarnessSetupError("environment incarnation does not match the receipt")
        status = view.get("status")
        if status != "ready":
            raise HarnessSetupError(
                f"environment {receipt.environment_id} is {status!r}, not ready"
            )
        key_id = source.get("registrationKeyId")
        if not key_id:
            raise HarnessSetupError("environment carries no registration key id")
        return str(key_id)

    async def _resolve_model(self, client: LightspeedClient) -> dict[str, str]:
        """Map Harbor's ``model_name`` to one explicit Lightspeed model route. No fallback.

        ``models/list`` queries the provider live, so a transient provider error
        (an HTTP 500 from the model catalog) is retried a few times: it happens
        before any model call and cannot be influenced by the task.
        """
        settings = self.settings
        listing: dict[str, Any] = {}
        for attempt in range(1, _MODEL_LIST_ATTEMPTS + 1):
            listing = await client.models_list()
            provider = next(
                (
                    p
                    for p in listing.get("providers", [])
                    if p.get("providerId") == settings.lightspeed_provider_id
                ),
                None,
            )
            if provider is None or not provider.get("error") or attempt == _MODEL_LIST_ATTEMPTS:
                break
            self.logger.warning(
                "models/list: provider %s reported %r (attempt %d/%d), retrying",
                settings.lightspeed_provider_id,
                provider.get("error"),
                attempt,
                _MODEL_LIST_ATTEMPTS,
            )
            await asyncio.sleep(_MODEL_LIST_BACKOFF_SEC * attempt)
        providers = {p.get("providerId"): p for p in listing.get("providers", [])}
        provider = providers.get(settings.lightspeed_provider_id)
        if provider is None:
            raise HarnessSetupError(
                f"Lightspeed provider {settings.lightspeed_provider_id!r} is not configured; "
                f"known providers: {sorted(p for p in providers if p)}"
            )
        credential = provider.get("credential")
        if credential is False or (
            isinstance(credential, str) and credential.lower() in {"missing", "absent", "none"}
        ):
            raise HarnessSetupError(
                f"Lightspeed provider {settings.lightspeed_provider_id!r} has no usable credential"
            )
        routes = [
            m
            for m in listing.get("models", [])
            if m.get("providerId") == settings.lightspeed_provider_id
            and m.get("model") == settings.model_id
        ]
        if not routes:
            available = sorted(
                {
                    m.get("model", "")
                    for m in listing.get("models", [])
                    if m.get("providerId") == settings.lightspeed_provider_id
                }
            )
            error = provider.get("error")
            raise HarnessSetupError(
                f"model {settings.model_id!r} is not exposed by provider "
                f"{settings.lightspeed_provider_id!r}"
                + (f" (provider error: {error})" if error else "")
                + f"; available: {available[:40]}"
            )
        kinds = sorted({str(m.get("apiKind")) for m in routes})
        if settings.api_kind:
            if settings.api_kind not in kinds:
                raise HarnessSetupError(
                    f"kwargs.api_kind {settings.api_kind!r} is not offered for "
                    f"{settings.model_id!r}; offered: {kinds}"
                )
            api_kind = settings.api_kind
        elif len(kinds) == 1:
            api_kind = kinds[0]
        else:
            raise HarnessSetupError(
                f"model {settings.model_id!r} has several API kinds {kinds}; "
                "set kwargs.api_kind explicitly"
            )
        return {
            "providerId": settings.lightspeed_provider_id,
            "model": settings.model_id,
            "apiKind": api_kind,
        }

    async def _wait_for_run(
        self, client: LightspeedClient, state: _RunState, context: AgentContext
    ) -> dict[str, Any]:
        """Poll the bounded session view until the run is terminal, projecting usage
        progressively so a Harbor timeout still leaves data, then read the run once."""
        assert state.session_id and state.run_id
        while True:
            view = (await client.session_read(state.session_id))["session"]
            summary = self._find_run(view, state.run_id)
            if summary is None:
                summary = (await client.session_runs_read(state.session_id, state.run_id))["run"]
            state.status = summary.get("status")
            self._project_usage(context, summary.get("usage"), state)
            if state.status in RUN_TERMINAL_STATUSES:
                break
            await asyncio.sleep(self.poll_interval_sec)
        run = (await client.session_runs_read(state.session_id, state.run_id))["run"]
        state.status = run.get("status", state.status)
        self._project_usage(context, run.get("usage"), state)
        return run

    @staticmethod
    def _find_run(session_view: dict[str, Any], run_id: str) -> dict[str, Any] | None:
        active = session_view.get("activeRun")
        if isinstance(active, dict) and active.get("id") == run_id:
            return active
        for summary in session_view.get("runs") or []:
            if isinstance(summary, dict) and summary.get("id") == run_id:
                return summary
        return None

    @staticmethod
    def _project_usage(
        context: AgentContext, usage: dict[str, Any] | None, state: _RunState
    ) -> None:
        if not usage:
            return
        state.usage = usage
        context.n_input_tokens = usage.get("inputTokens")
        context.n_cache_tokens = usage.get("cachedInputTokens")
        context.n_output_tokens = usage.get("outputTokens")

    async def _failure_message(self, client: LightspeedClient, state: _RunState) -> str | None:
        """Best-effort: the ``runFailed`` event message for this run."""
        if not state.session_id or not state.run_id:
            return None
        after: int | None = None
        try:
            for _ in range(_EVENT_MAX_PAGES):
                page = await client.session_events_read(
                    state.session_id, after_seq=after, limit=_EVENT_PAGE_LIMIT
                )
                for event in page.get("events") or []:
                    kind = event.get("kind") or {}
                    if kind.get("type") == "runFailed" and kind.get("runId") == state.run_id:
                        return str(kind.get("message") or "")
                cursor = page.get("nextCursor") or {}
                if page.get("complete") or "seq" not in cursor:
                    break
                after = int(cursor["seq"])
        except (LightspeedError, ValueError, TypeError) as exc:
            self.logger.debug("could not read failure message: %s", exc)
        return None

    # --- cleanup and artifacts --------------------------------------------

    async def _cleanup(self, environment: BaseEnvironment, state: _RunState) -> None:
        """Bounded, best-effort teardown. Errors are recorded, never raised, so they
        cannot replace the trial's real outcome or touch the sandbox filesystem."""
        client = state.client
        steps: list[tuple[str, Any]] = []
        if (
            client
            and state.session_id
            and state.run_id
            and state.status not in RUN_TERMINAL_STATUSES
        ):
            steps.append(
                ("runs/cancel", lambda: client.session_runs_cancel(state.session_id, state.run_id))
            )
        if client and state.session_id:
            steps.append(
                ("session/close", lambda: client.session_close(state.session_id, force=True))
            )
        steps.append(("envd/stop", lambda: envd.stop(environment, self.paths)))
        if client and state.receipt:
            steps.append(
                (
                    "environments/close",
                    lambda: client.environments_close(state.receipt.environment_id),
                )
            )
        if not state.key_deleted:
            steps.append(("key/delete", lambda: envd.delete_key_file(environment, self.paths)))
        try:
            async with asyncio.timeout(self.cleanup_timeout_sec):
                for name, step in steps:
                    try:
                        await step()
                        state.cleanup[name] = "ok"
                    except LightspeedError as exc:
                        state.cleanup[name] = "ok (already gone)" if exc.not_found else str(exc)
                    except Exception as exc:  # noqa: BLE001 - recorded, never raised
                        state.cleanup[name] = f"{type(exc).__name__}: {exc}"
        except TimeoutError:
            for name, _ in steps:
                state.cleanup.setdefault(name, "skipped: cleanup timeout")
        finally:
            if client is not None:
                try:
                    await client.aclose()
                except Exception as exc:  # noqa: BLE001
                    state.cleanup["client/close"] = f"{type(exc).__name__}: {exc}"
        problems = {k: v for k, v in state.cleanup.items() if not v.startswith("ok")}
        if problems:
            self.logger.warning("Lightspeed cleanup left work behind: %s", problems)

    def _finish_context(self, context: AgentContext, state: _RunState) -> None:
        usage = state.usage or {}
        server = (state.initialize or {}).get("serverInfo")
        context.metadata = {
            **(context.metadata or {}),
            "lightspeed": {
                "session_id": state.session_id,
                "run_id": state.run_id,
                "environment_id": state.receipt.environment_id if state.receipt else None,
                "status": state.status,
                "failure_class": state.failure_class,
                "error": state.error,
                "cancelled": state.cancelled,
                "reasoning_tokens": usage.get("reasoningTokens"),
                "total_tokens": usage.get("totalTokens"),
                "timings": state.timings,
                "cleanup": state.cleanup,
                "server": server,
                "envd_version": self._envd_version,
            },
        }

    def _write_artifacts(self, state: _RunState) -> None:
        directory = Path(self.logs_dir) / artifacts.ARTIFACT_SUBDIR
        secrets = self.host.secrets()
        registration = {
            "receipt": state.receipt.as_dict() if state.receipt else None,
            "metadata": state.metadata,
            "registration_key_id": state.registration_key_id,
            "environment": _environment_summary(state.environment_view),
            "gateway_url": self.host.gateway_url,
            "envd": {
                "version": self._envd_version,
                "platform": self._platform,
                "artifact": self._artifact.as_dict() if self._artifact else None,
                "log": str(artifacts.ENVD_LOG),
            },
        }
        run_view = state.run_view or {}
        run = {
            "session_id": state.session_id,
            "run_id": state.run_id,
            "status": state.status,
            "cancelled": state.cancelled,
            "error": state.error,
            "failure_class": state.failure_class,
            "usage": state.usage,
            "timings": state.timings,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
            "run_started_at_ms": run_view.get("startedAtMs"),
            "run_completed_at_ms": run_view.get("completedAtMs"),
            "entries": len(run_view.get("entries") or []),
            "tool_batches": len(run_view.get("toolBatches") or []),
            "model": state.model,
            "session_config": state.session_config,
            "instruction_sha256": state.instruction_sha256,
            "instruction_bytes": state.instruction_bytes,
            "cleanup": state.cleanup,
        }
        provenance = {
            "adapter": {"name": AGENT_NAME, "version": __version__},
            "harbor_version": harbor.__version__,
            "python": host_platform.python_version(),
            "host_settings": self.host.redacted(),
            "agent_settings": self.settings.as_dict(),
            "lightspeed": state.initialize,
            "envd": {
                "version": self._envd_version,
                "platform": self._platform,
                "artifact": self._artifact.as_dict() if self._artifact else None,
            },
            "harbor_session_id": getattr(self, "session_id", None),
            "harbor_context_id": str(getattr(self, "context_id", None) or "") or None,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
        }
        for name, payload in (
            (artifacts.REGISTRATION_JSON, registration),
            (artifacts.RUN_JSON, run),
            (artifacts.PROVENANCE_JSON, provenance),
        ):
            try:
                artifacts.write_json(directory, name, payload, secrets=secrets)
            except Exception as exc:  # noqa: BLE001 - artifact-only failure keeps the score
                self.logger.error("could not write %s (%s): %s", name, FAILURE_ARTIFACT_ONLY, exc)


def _environment_summary(view: dict[str, Any] | None) -> dict[str, Any] | None:
    if not view:
        return None
    return {
        "environmentId": view.get("environmentId"),
        "status": view.get("status"),
        "source": view.get("source"),
        "displayName": view.get("displayName"),
        "lastSeenAtMs": view.get("lastSeenAtMs"),
    }
