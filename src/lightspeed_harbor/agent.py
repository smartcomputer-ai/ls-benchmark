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
          # optional: api_kind, max_turns, max_output_tokens, jobs (default
          # true), instructions (bundled prompt name, a file path, or "none";
          # default "harbor-terminal"), registration_timeout_sec,
          # poll_interval_sec, cleanup_timeout_sec, keep_environment_for_verifier
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform as host_platform
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import resources
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
_EVENT_MAX_PAGES = 400
_EVENT_MAX_BYTES = 8 * 1024 * 1024
_TOOL_ERROR_STATUSES = frozenset({"failed", "unavailable"})
_RUN_TERMINAL_KINDS = frozenset({"runCompleted", "runFailed", "runCancelled"})
_MODEL_LIST_ATTEMPTS = 4
_MODEL_LIST_BACKOFF_SEC = 2.0


def _utcnow() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


DEFAULT_INSTRUCTIONS = "harbor-terminal"


def load_instructions(spec: str | None) -> tuple[str, str] | None:
    """Resolve the harness prompt: ``None``/``"none"`` for no prompt, a bundled
    prompt name (``harbor-terminal``), or a path to a text file. Returns
    ``(text, source)``."""
    if not spec or spec.lower() == "none":
        return None
    path = Path(spec)
    if path.suffix and path.is_file():
        return path.read_text(), str(path)
    bundled = resources.files("lightspeed_harbor").joinpath("prompts", f"{spec}.md")
    if bundled.is_file():
        return bundled.read_text(), f"bundled:{spec}"
    raise HarnessSetupError(f"instructions {spec!r} is neither a bundled prompt nor a file")


def build_session_config(
    settings: AgentSettings,
    model: dict[str, str],
    registration_key_id: str | None,
    *,
    jobs: bool = True,
) -> dict[str, Any]:
    """The terminal-only inline profile: one model route, the environment tool
    surface (process tools and, by default, durable jobs) scoped to the campaign
    key, no MCP, web, sub-agents, VFS, or skills."""
    environments: dict[str, Any] = {"selectionTools": False, "jobs": bool(jobs)}
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


_SLEEP_COMMAND = re.compile(r"(?:^|[;&|(]\s*)sleep\s")


def _call_command(call: dict[str, Any]) -> str | None:
    """The shell text of one tool call from its inline arguments: Codex's
    ``cmd``, Claude Code's ``command``, or the canonical ``argv``."""
    raw = call.get("arguments")
    if not isinstance(raw, str):
        return None
    try:
        args = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    for key in ("cmd", "command"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    argv = args.get("argv")
    if isinstance(argv, list) and all(isinstance(part, str) for part in argv):
        return " ".join(argv)
    return None


def compute_measures(events: list[dict[str, Any]], run_id: str | None) -> dict[str, Any]:
    """Secondary measures for one run from the raw session events: model calls,
    turns, tool calls and errors, tool output bytes and truncations, commands
    that poll with ``sleep``, time to the first model request and first tool
    call, model versus tool time, and the engine's failure kind. Events of
    other runs are ignored; unknown shapes are skipped rather than guessed."""
    run_started: int | None = None
    gen_requested: dict[str, int] = {}
    batch_started: dict[str, int] = {}
    m: dict[str, Any] = {
        "model_calls": 0,
        "turns": 0,
        "tool_batches": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "tool_output_bytes": 0,
        "tool_output_truncations": 0,
        "sleep_commands": 0,
        "model_time_ms": 0,
        "tool_time_ms": 0,
        "time_to_first_model_request_ms": None,
        "time_to_first_tool_call_ms": None,
        "run_duration_ms": None,
        "terminal_event": None,
        "failure_kind": None,
    }
    for event in events:
        kind = event.get("kind") if isinstance(event, dict) else None
        if not isinstance(kind, dict):
            continue
        if run_id and kind.get("runId") not in (None, run_id):
            continue
        kind_type = kind.get("type")
        at = event.get("observedAtMs")
        if not isinstance(at, int):
            continue
        if kind_type == "runStarted":
            run_started = at
        elif kind_type == "turnStarted":
            m["turns"] += 1
        elif kind_type == "turnGenerationRequested":
            gen_requested[str(kind.get("turnId"))] = at
            if run_started is not None and m["time_to_first_model_request_ms"] is None:
                m["time_to_first_model_request_ms"] = at - run_started
        elif kind_type == "turnGenerationCompleted":
            m["model_calls"] += 1
            started = gen_requested.pop(str(kind.get("turnId")), None)
            if started is not None:
                m["model_time_ms"] += max(0, at - started)
        elif kind_type == "toolBatchStarted":
            m["tool_batches"] += 1
            batch_started[str(kind.get("batchId"))] = at
            for call in kind.get("calls") or []:
                if not isinstance(call, dict):
                    continue
                command = _call_command(call)
                if command is not None and _SLEEP_COMMAND.search(command):
                    m["sleep_commands"] += 1
        elif kind_type == "toolCallStarted":
            m["tool_calls"] += 1
            if run_started is not None and m["time_to_first_tool_call_ms"] is None:
                m["time_to_first_tool_call_ms"] = at - run_started
        elif kind_type == "toolCallCompleted":
            if kind.get("status") in _TOOL_ERROR_STATUSES:
                m["tool_errors"] += 1
            output_bytes = kind.get("outputBytes")
            if isinstance(output_bytes, int):
                m["tool_output_bytes"] += output_bytes
            if kind.get("truncated") is True:
                m["tool_output_truncations"] += 1
        elif kind_type == "toolBatchCompleted":
            started = batch_started.pop(str(kind.get("batchId")), None)
            if started is not None:
                m["tool_time_ms"] += max(0, at - started)
        elif kind_type in _RUN_TERMINAL_KINDS:
            m["terminal_event"] = kind_type
            if kind_type == "runFailed" and isinstance(kind.get("kind"), str):
                m["failure_kind"] = kind["kind"]
            if run_started is not None:
                m["run_duration_ms"] = at - run_started
    return m


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
    events: list[dict[str, Any]] | None = None
    events_complete: bool = False
    events_truncated: bool = False
    measures: dict[str, Any] = field(default_factory=dict)


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
        jobs: bool = True,
        instructions: str | None = DEFAULT_INSTRUCTIONS,
        registration_timeout_sec: float = 90.0,
        poll_interval_sec: float = 2.0,
        cleanup_timeout_sec: float = 60.0,
        keep_environment_for_verifier: bool = True,
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
        # Processes the agent left running (a server the verifier must reach)
        # have envd's pipes as stdio; stopping envd or closing the environment
        # before the verifier kills them with EPIPE. Leave both alive: Harbor
        # destroys the sandbox after verification and the registration key's
        # ephemeral grace closes the environment once the daemon is gone.
        self.keep_environment_for_verifier = bool(keep_environment_for_verifier)
        self.jobs = bool(jobs)
        # The harness prompt (Lightspeed's own instructions for this tool
        # surface). It never sees the task; its digest goes into provenance.
        self.instructions = load_instructions(instructions)
        self._platform: str | None = None
        self._artifact: envd.EnvdArtifact | None = None
        self._envd_version: str | None = None
        self._server_info: dict[str, Any] | None = None
        # Test hook: transport for artifact discovery and download.
        self._artifact_transport: Any = None

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
        # The server says which build it is (P152); the daemon must be that build.
        async with self._make_client() as client:
            self._server_info = (await client.initialize()).get("serverInfo") or {}
        server_sha = str(self._server_info.get("gitSha") or "") or None
        self._artifact = await envd.resolve_artifact(
            self.host, target=target, transport=self._artifact_transport
        )
        if (
            server_sha
            and self._artifact.git_sha
            and self._artifact.git_sha != server_sha
            and not self.host.envd_allow_mismatch
        ):
            raise HarnessSetupError(
                f"envd artifact is built from {self._artifact.git_sha[:12]} but the server "
                f"runs {server_sha[:12]}; refusing a mismatched daemon"
            )
        self._envd_version = await envd.install(
            environment, self._artifact, self.paths, ca_file=self.host.envd_ca_file
        )
        if self.host.envd_version and self.host.envd_version not in self._envd_version:
            raise HarnessSetupError(
                f"envd reports {self._envd_version!r}, expected version {self.host.envd_version!r}"
            )
        if (
            server_sha
            and server_sha not in self._envd_version
            and not self.host.envd_allow_mismatch
        ):
            raise HarnessSetupError(
                f"envd in the sandbox reports {self._envd_version!r}, which is not the server's "
                f"build {server_sha[:12]}; set LIGHTSPEED_HARBOR_ENVD_ALLOW_MISMATCH=1 only for "
                "development"
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
            await self._cleanup(environment, state, context)
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
        harbor_session = getattr(self, "session_id", None)
        task_name = (
            harbor_session.split("__")[0] if harbor_session and "__" in harbor_session else None
        )
        state.metadata = envd.correlation_metadata(
            context_id=getattr(self, "context_id", None),
            session_id=harbor_session,
            task_name=task_name,
            extra={"campaign": self.host.campaign} if self.host.campaign else None,
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
            self.settings, state.model, state.registration_key_id, jobs=self.jobs
        )
        profile: dict[str, Any] | None = None
        session_kwargs: dict[str, Any] = {"config": state.session_config}
        if self.instructions is not None:
            profile = {
                "kind": "inline",
                "profile": {
                    "config": state.session_config,
                    "instructions": {"type": "text", "text": self.instructions[0]},
                },
            }
            session_kwargs = {"profile": profile}
        session = (
            await client.session_start(
                display_name=getattr(self, "session_id", None),
                # The same correlation map the registered environment carries,
                # so one metadata filter finds a trial's session and sandbox.
                metadata=state.metadata,
                **session_kwargs,
                # Evaluation sessions collect themselves (P154); None keeps them.
                delete_after_close_ms=(
                    self.host.session_ttl_sec * 1000 if self.host.session_ttl_sec else None
                ),
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
        await self._export_events(client, state)
        if state.status != "completed":
            message = self._failure_message(state)
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

    async def _export_events(self, client: LightspeedClient, state: _RunState) -> None:
        """Page the whole session event log into ``state.events`` (bounded by pages
        and bytes) and derive the secondary measures. Never raises: an export
        problem is artifact-only."""
        if state.events is not None or not state.session_id:
            return
        events: list[dict[str, Any]] = []
        size = 0
        after: int | None = None
        complete = False
        truncated = False
        try:
            for _ in range(_EVENT_MAX_PAGES):
                page = await client.session_events_read(
                    state.session_id, after_seq=after, limit=_EVENT_PAGE_LIMIT
                )
                batch = [e for e in (page.get("events") or []) if isinstance(e, dict)]
                events.extend(batch)
                size += len(json.dumps(batch))
                cursor = page.get("nextCursor") or {}
                if page.get("complete") or "seq" not in cursor or not batch:
                    complete = bool(page.get("complete", not batch))
                    break
                after = int(cursor["seq"])
                if size >= _EVENT_MAX_BYTES:
                    truncated = True
                    break
            else:
                truncated = True
        except (LightspeedError, ValueError, TypeError) as exc:
            self.logger.warning("event export incomplete: %s", exc)
            truncated = True
        state.events = events
        state.events_complete = complete and not truncated
        state.events_truncated = truncated
        state.measures = compute_measures(events, state.run_id)

    @staticmethod
    def _failure_message(state: _RunState) -> str | None:
        """The ``runFailed`` message for this run, from the exported events."""
        for event in state.events or []:
            kind = event.get("kind") or {}
            if kind.get("type") == "runFailed" and kind.get("runId") == state.run_id:
                return str(kind.get("message") or "")
        return None

    # --- cleanup and artifacts --------------------------------------------

    async def _cleanup(
        self, environment: BaseEnvironment, state: _RunState, context: AgentContext
    ) -> None:
        """Bounded, best-effort teardown. Errors are recorded, never raised, so they
        cannot replace the trial's real outcome or touch the sandbox filesystem."""
        client = state.client

        async def cancel_run() -> None:
            # The cancel response is the final run view: keep its status, usage,
            # entries, and tool batches so a timed-out trial is recorded fully.
            assert client and state.session_id and state.run_id
            view = (await client.session_runs_cancel(state.session_id, state.run_id)).get("run")
            if isinstance(view, dict):
                state.run_view = view
                state.status = view.get("status", state.status)
                self._project_usage(context, view.get("usage"), state)

        steps: list[tuple[str, Any]] = []
        if (
            client
            and state.session_id
            and state.run_id
            and state.status not in RUN_TERMINAL_STATUSES
        ):
            steps.append(("runs/cancel", cancel_run))
        if client and state.session_id and state.events is None:
            steps.append(("events/export", lambda: self._export_events(client, state)))
        if client and state.session_id:
            steps.append(
                ("session/close", lambda: client.session_close(state.session_id, force=True))
            )
        # A trial that never registered, or a caller that opted out, still tears
        # the daemon and the environment down here; otherwise both outlive the
        # agent phase on purpose (see __init__).
        teardown = not self.keep_environment_for_verifier or state.receipt is None
        if teardown:
            steps.append(("envd/stop", lambda: envd.stop(environment, self.paths)))
        if client and state.receipt and teardown:
            steps.append(
                (
                    "environments/close",
                    lambda: client.environments_close(state.receipt.environment_id),
                )
            )
        if state.receipt and not teardown:
            state.cleanup["envd/stop"] = "skipped: kept for the verifier"
            state.cleanup["environments/close"] = "skipped: ephemeral grace closes it"
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
                "measures": state.measures,
                "events_exported": len(state.events) if state.events is not None else None,
                "cleanup": state.cleanup,
                "server": server,
                "envd_version": self._envd_version,
            },
        }

    def _instructions_record(self) -> dict[str, Any] | None:
        if self.instructions is None:
            return None
        text, source = self.instructions
        return {
            "source": source,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "bytes": len(text.encode()),
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
            "measures": state.measures,
            "cleanup": state.cleanup,
        }
        events = {
            "session_id": state.session_id,
            "run_id": state.run_id,
            "complete": state.events_complete,
            "truncated": state.events_truncated,
            "count": len(state.events or []),
            "events": state.events or [],
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
            "instructions": self._instructions_record(),
            "harbor_session_id": getattr(self, "session_id", None),
            "harbor_context_id": str(getattr(self, "context_id", None) or "") or None,
            "started_at": state.started_at,
            "finished_at": state.finished_at,
        }
        for name, payload in (
            (artifacts.REGISTRATION_JSON, registration),
            (artifacts.RUN_JSON, run),
            (artifacts.PROVENANCE_JSON, provenance),
            (artifacts.EVENTS_JSON, events),
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
