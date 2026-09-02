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
    workdir: str = "/app"
    uname: str = "x86_64"
    version_output: str = "lightspeed-envd 0.1.0 (deadbeef)"
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
        if command.endswith("--version"):
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
    ) -> None:
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
                "serverInfo": {"name": "lightspeed", "version": "test"},
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
            return {
                "events": [
                    {
                        "cursor": {"seq": 1},
                        "sessionId": self.session_id,
                        "observedAtMs": 1,
                        "joins": {},
                        "kind": {
                            "type": "runFailed",
                            "runId": self.run_id,
                            "message": self.failure_message,
                        },
                    }
                ],
                "complete": True,
                "nextCursor": {"seq": 1},
            }
        raise AssertionError(f"unexpected method {method}")
