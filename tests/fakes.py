"""Fakes for the adapter's two boundaries: Harbor's environment and the Lightspeed API.

``FakeEnvironment`` records every ``exec`` and upload and simulates the envd
lifecycle (probe, receipt, stop) from the command strings the adapter builds.
``FakeLightspeed`` is an ``httpx.MockTransport`` JSON-RPC server that records
calls and answers with contract-shaped documents.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

ALIVE = "__LIGHTSPEED_HARBOR_ALIVE__"
GIT_SHA = "2b016737ea6144c7656e3fba8982d4eacdb831da"
DEAD = "__LIGHTSPEED_HARBOR_DEAD__"

RECEIPT = {
    "environmentId": "environment_1",
    "incarnationId": "incarnation_1",
    "daemonId": "daemon_1",
    "connectionId": "connection_1",
    "identityMode": "ephemeral",
}


@dataclass
class FakeExecResult:
    return_code: int
    stdout: str | None = ""
    stderr: str | None = ""


@dataclass
class FakeEnvironment:
    default_user: str | int | None = "agent"
    workdir: str | None = "/app"  # task.toml [environment].workdir; None = image default
    image_workdir: str = "/workspace"  # what `pwd` answers when no workdir is declared
    uname: str = "x86_64"
    version_output: str = f"lightspeed-envd 0.1.0 (git {GIT_SHA}, x86_64-unknown-linux-musl)"
    # How many `--version` probes answer with exit 0 and no output first (the
    # Docker backend does that occasionally right after the upload).
    silent_version_probes: int = 0
    version_probes: int = 0
    receipt: dict[str, Any] | None = field(default_factory=lambda: dict(RECEIPT))
    receipt_after_polls: int = 1
    envd_dies: bool = False
    log_tail: str = "lightspeed-envd registration rejected: key revoked; not retrying"
    calls: list[dict[str, Any]] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    started: bool = False
    stopped: bool = False
    polls: int = 0

    def __post_init__(self) -> None:
        self.task_env_config = SimpleNamespace(workdir=self.workdir)

    def commands(self, *, user: str | None = None) -> list[str]:
        return [c["command"] for c in self.calls if user is None or c["user"] == user]

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> FakeExecResult:
        self.calls.append({"command": command, "cwd": cwd, "env": env, "user": user})
        if command == "uname -m":
            return FakeExecResult(0, self.uname + "\n")
        if command == "pwd":
            return FakeExecResult(0, self.image_workdir + "\n")
        if command.endswith("--version"):
            self.version_probes += 1
            if self.version_probes <= self.silent_version_probes:
                return FakeExecResult(0, "")
            return FakeExecResult(0, self.version_output + "\n")
        if "nohup" in command and "LIGHTSPEED_ENVD_GATEWAY_URL" in command:
            self.started = True
            return FakeExecResult(0)
        if ALIVE in command:
            self.polls += 1
            if self.envd_dies:
                return FakeExecResult(0, DEAD + "\n")
            if self.receipt is not None and self.polls >= self.receipt_after_polls:
                return FakeExecResult(0, json.dumps(self.receipt) + "\n")
            return FakeExecResult(0, ALIVE + "\n")
        if command.startswith("rm -f "):
            target = command.split()[2]
            self.files.pop(target, None)
            return FakeExecResult(0)
        if command.startswith("tail -n"):
            return FakeExecResult(0, self.log_tail)
        if "kill -TERM" in command:
            self.stopped = True
            return FakeExecResult(0)
        return FakeExecResult(0)

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        self.files[target_path] = Path(source_path).read_bytes()


def _run_view(run_id: str, status: str, usage: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "id": run_id,
        "status": status,
        "source": {"type": "input", "items": [{"type": "text", "text": "..."}]},
        "startedAtMs": 1_000,
        "completedAtMs": 2_000 if status in {"completed", "failed", "cancelled"} else None,
        "usage": usage,
        "entries": [{"id": "entry_1", "kind": "message", "contentRef": "blob_1"}],
        "toolBatches": [{"id": "batch_1"}],
        "pendingApprovals": [],
    }


class FakeLightspeed:
    """Scripted Lightspeed JSON-RPC server behind ``httpx.MockTransport``."""

    def __init__(
        self,
        *,
        run_statuses: tuple[str, ...] = ("running", "completed"),
        models: list[dict[str, Any]] | None = None,
        errors: dict[str, dict[str, Any]] | None = None,
        environment: dict[str, Any] | None = None,
        failure_message: str = "provider refused the request",
        server_git_sha: str = GIT_SHA,
    ) -> None:
        self.server_git_sha = server_git_sha
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.auth_headers: list[str | None] = []
        self.session_id = "session_1"
        self.run_id = "run_1"
        self._statuses = list(run_statuses)
        self._status_index = 0
        self.usage = {
            "inputTokens": 1200,
            "cachedInputTokens": 300,
            "outputTokens": 80,
            "reasoningTokens": 20,
            "totalTokens": 1280,
        }
        self.models = models or [
            {
                "providerId": "openai",
                "model": "model-snapshot",
                "apiKind": "openai:responses",
                "displayName": "model-snapshot",
                "capabilities": {},
                "source": "provider",
                "fetchedAtMs": 1,
            }
        ]
        self.errors = errors or {}
        self.environment = environment or {
            "environmentId": RECEIPT["environmentId"],
            "requestId": "req_1",
            "status": "ready",
            "desiredPower": "running",
            "publicIngressEnabled": False,
            "createdAtMs": 1,
            "updatedAtMs": 1,
            "incarnation": {"incarnationId": RECEIPT["incarnationId"]},
            "source": {
                "type": "registered",
                "registrationKeyId": "key_1",
                "daemonId": RECEIPT["daemonId"],
                "identityMode": "ephemeral",
            },
            "metadata": {"source": "harbor"},
        }
        self.failure_message = failure_message
        # Provider errors to report on successive models/list calls (then healthy).
        self.provider_errors: list[str] = []

    def events(self) -> list[dict[str, Any]]:
        """One scripted run: two model calls around one tool batch, then the terminal
        event matching the run's final status (12 events, 4.4 s of run time)."""
        run = self.run_id
        terminal = self._statuses[-1]
        kinds: list[tuple[int, dict[str, Any]]] = [
            (900, {"type": "runAccepted", "runId": run, "source": {"type": "input"}}),
            (1000, {"type": "runStarted", "runId": run}),
            (1050, {"type": "turnStarted", "runId": run, "turnId": "turn_1"}),
            (1100, {"type": "turnGenerationRequested", "runId": run, "turnId": "turn_1"}),
            (
                3100,
                {
                    "type": "turnGenerationCompleted",
                    "runId": run,
                    "turnId": "turn_1",
                    "status": "completed",
                    "usage": self.usage,
                },
            ),
            (
                3200,
                {
                    "type": "toolBatchStarted",
                    "runId": run,
                    "turnId": "turn_1",
                    "batchId": "batch_1",
                    "calls": [],
                },
            ),
            (
                3200,
                {
                    "type": "toolCallStarted",
                    "runId": run,
                    "turnId": "turn_1",
                    "batchId": "batch_1",
                    "callId": "call_1",
                },
            ),
            (
                4200,
                {
                    "type": "toolCallCompleted",
                    "runId": run,
                    "turnId": "turn_1",
                    "batchId": "batch_1",
                    "callId": "call_1",
                    "status": "succeeded",
                    "effects": [],
                },
            ),
            (
                4200,
                {
                    "type": "toolBatchCompleted",
                    "runId": run,
                    "turnId": "turn_1",
                    "batchId": "batch_1",
                },
            ),
            (4300, {"type": "turnGenerationRequested", "runId": run, "turnId": "turn_1"}),
            (
                5300,
                {
                    "type": "turnGenerationCompleted",
                    "runId": run,
                    "turnId": "turn_1",
                    "status": "completed",
                    "usage": self.usage,
                },
            ),
        ]
        if terminal == "failed":
            kinds.append(
                (5400, {"type": "runFailed", "runId": run, "message": self.failure_message})
            )
        elif terminal == "cancelled":
            kinds.append((5400, {"type": "runCancelled", "runId": run}))
        else:
            kinds.append((5400, {"type": "runCompleted", "runId": run, "outputRef": "blob_out"}))
        return [
            {
                "cursor": {"seq": i + 1},
                "sessionId": self.session_id,
                "observedAtMs": at,
                "joins": {},
                "kind": kind,
            }
            for i, (at, kind) in enumerate(kinds)
        ]

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def params(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def _current_status(self, advance: bool) -> str:
        status = self._statuses[min(self._status_index, len(self._statuses) - 1)]
        if advance and self._status_index < len(self._statuses) - 1:
            self._status_index += 1
        return status

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        method, params = body["method"], body.get("params") or {}
        self.calls.append((method, params))
        self.auth_headers.append(request.headers.get("authorization"))
        if method in self.errors:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": self.errors[method]}
            )
        result = self._dispatch(method, params)
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": body["id"],
                "result": {"result": result, "notifications": []},
            },
        )

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": "lightspeed.agent.api.v1",
                "serverInfo": {
                    "name": "lightspeed",
                    "version": f"test+{GIT_SHA[:12]}",
                    "gitSha": self.server_git_sha,
                    "envd": {
                        "gitSha": self.server_git_sha,
                        "protocolVersion": 2,
                        "targets": ["x86_64-unknown-linux-musl"],
                        "version": "0.1.0",
                    },
                },
                "capabilities": {},
            }
        if method == "models/list":
            error = self.provider_errors.pop(0) if self.provider_errors else None
            return {
                "providers": [
                    {
                        "providerId": "openai",
                        "apiKinds": ["openai:responses"],
                        "credential": "configured",
                        "credentialSource": "deployment",
                        "error": error,
                    }
                ],
                "models": [] if error else self.models,
            }
        if method == "environments/read":
            return {"environment": self.environment}
        if method == "environments/close":
            return {"environment": {**self.environment, "status": "closing"}}
        if method == "environments/list":
            return {"environments": [self.environment]}
        if method == "session/start":
            return {
                "session": {
                    "id": self.session_id,
                    "status": "idle",
                    "configRevision": 1,
                    "contextRevision": 0,
                }
            }
        if method == "session/environments/activate":
            return {
                "session": {"id": self.session_id, "activeEnvironmentId": params["environmentId"]}
            }
        if method == "session/runs/start":
            return {"run": _run_view(self.run_id, "queued", None)}
        if method == "session/read":
            status = self._current_status(advance=True)
            summary = {
                "id": self.run_id,
                "status": status,
                "acceptedAtMs": 900,
                "source": {"type": "input"},
                "usage": self.usage,
                "pendingApprovals": [],
            }
            active = summary if status in {"queued", "running"} else None
            return {
                "session": {
                    "id": self.session_id,
                    "status": "active" if active else "idle",
                    "activeRun": active,
                    "runs": [summary],
                },
                "hasOlderRuns": False,
            }
        if method == "session/runs/read":
            return {"run": _run_view(self.run_id, self._current_status(advance=False), self.usage)}
        if method == "session/runs/cancel":
            self._statuses = ["cancelled"]
            self._status_index = 0
            return {"run": _run_view(self.run_id, "cancelled", self.usage)}
        if method == "session/close":
            return {"session": {"id": self.session_id, "status": "closed"}}
        if method == "session/events/read":
            events = self.events()
            after = params.get("after")
            after_seq = after.get("seq") if isinstance(after, dict) else None
            remaining = [e for e in events if after_seq is None or e["cursor"]["seq"] > after_seq]
            page = remaining[: params.get("limit") or 500]
            return {
                "events": page,
                "complete": len(page) == len(remaining),
                "nextCursor": {"seq": page[-1]["cursor"]["seq"]} if page else None,
                "headCursor": {"seq": events[-1]["cursor"]["seq"]} if events else None,
            }
        raise AssertionError(f"unexpected method {method}")
