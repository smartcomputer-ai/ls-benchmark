"""Lightspeed JSON-RPC client used from the Harbor host.

Hand-written over ``httpx`` (already pinned through Harbor) against the
released contract in the sibling checkout, ``crates/api/contract``. It never
depends on Lightspeed source internals.

Transport: one ``POST`` per call to ``LIGHTSPEED_API_URL`` with
``{"jsonrpc": "2.0", "id", "method", "params"}``. Every method answers with
``AgentApiOutcome<T>``, ``{"result": T, "notifications": [...]}``, or a
JSON-RPC ``error`` whose ``data`` is an ``AgentApiError`` ``{kind, message}``.
Auth is ``Authorization: Bearer <LIGHTSPEED_API_KEY>``; a ``single``-mode
gateway ignores the header, ``api-key`` mode requires it. A ``trusted-header``
gateway (behind an authenticating proxy, or the local ``./dev.sh`` full
profile) instead resolves the tenant from ``x-lightspeed-universe``.
"""

from __future__ import annotations

import itertools
from collections.abc import Mapping
from typing import Any

import httpx

from lightspeed_harbor import __version__

JsonObject = dict[str, Any]

# Every method the adapter may call. ``tests/test_client.py`` checks these
# names against ``openrpc.json`` when the sibling checkout is present.
METHODS: frozenset[str] = frozenset(
    {
        "initialize",
        "models/list",
        "session/start",
        "session/read",
        "session/close",
        "session/events/read",
        "session/environments/activate",
        "session/runs/start",
        "session/runs/read",
        "session/runs/cancel",
        "environments/read",
        "environments/list",
        "environments/close",
    }
)

_CODE_KINDS = {
    -32602: "invalid_request",
    -32004: "not_found",
    -32009: "conflict",
    -32010: "rejected",
}

RUN_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})


class LightspeedError(Exception):
    """A JSON-RPC error (``kind`` from ``AgentApiErrorKind``) or a transport failure
    (``kind == "transport"``)."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        method: str | None = None,
        code: int | None = None,
    ) -> None:
        super().__init__(f"{method or 'lightspeed'}: {kind}: {message}")
        self.kind = kind
        self.message = message
        self.method = method
        self.code = code

    @property
    def not_found(self) -> bool:
        return self.kind == "not_found"


class LightspeedClient:
    """Async JSON-RPC client. Use as ``async with LightspeedClient(...) as client``."""

    def __init__(
        self,
        api_url: str,
        api_key: str | None,
        *,
        timeout_sec: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        client_name: str = "ls-benchmark",
        universe: str | None = None,
    ) -> None:
        self.api_url = api_url
        self._api_key = api_key
        self._universe = universe
        self._timeout_sec = timeout_sec
        self._transport = transport
        self._client_name = client_name
        self._ids = itertools.count(1)
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LightspeedClient:
        headers = {"content-type": "application/json", "user-agent": f"ls-benchmark/{__version__}"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"
        if self._universe:
            headers["x-lightspeed-universe"] = self._universe
        self._http = httpx.AsyncClient(
            headers=headers, timeout=self._timeout_sec, transport=self._transport
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def call(self, method: str, params: JsonObject | None = None) -> JsonObject:
        """Call one method and return the outcome's ``result`` object."""
        outcome = await self.call_outcome(method, params)
        result = outcome.get("result")
        if not isinstance(result, dict):
            raise LightspeedError("internal", "outcome has no result object", method=method)
        return result

    async def call_outcome(self, method: str, params: JsonObject | None = None) -> JsonObject:
        """Call one method and return the full ``AgentApiOutcome`` (result plus notifications)."""
        if method not in METHODS:
            raise ValueError(f"method {method!r} is not part of the adapter contract")
        if self._http is None:
            raise RuntimeError("LightspeedClient must be used as an async context manager")
        request = {
            "jsonrpc": "2.0",
            "id": next(self._ids),
            "method": method,
            "params": params if params is not None else {},
        }
        try:
            response = await self._http.post(self.api_url, json=request)
        except httpx.HTTPError as exc:
            raise LightspeedError("transport", str(exc), method=method) from exc
        if response.status_code >= 400:
            raise LightspeedError(
                "transport",
                f"HTTP {response.status_code}: {response.text[:200]}",
                method=method,
                code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise LightspeedError("transport", "response is not JSON", method=method) from exc
        if not isinstance(payload, dict):
            raise LightspeedError("transport", "response is not a JSON object", method=method)
        error = payload.get("error")
        if error is not None:
            data = error.get("data") if isinstance(error, dict) else None
            data = data if isinstance(data, dict) else {}
            code = error.get("code") if isinstance(error, dict) else None
            kind = data.get("kind") or _CODE_KINDS.get(code, "internal")
            message = data.get("message") or (
                error.get("message") if isinstance(error, dict) else ""
            )
            raise LightspeedError(str(kind), str(message), method=method, code=code)
        outcome = payload.get("result")
        if not isinstance(outcome, dict):
            raise LightspeedError("internal", "response has no result", method=method)
        return outcome

    # --- typed helpers -------------------------------------------------

    async def initialize(self) -> JsonObject:
        return await self.call(
            "initialize",
            {"clientInfo": {"name": self._client_name, "version": __version__}},
        )

    async def models_list(self, *, selectable_only: bool = False) -> JsonObject:
        return await self.call("models/list", {"selectableOnly": selectable_only})

    async def session_start(
        self,
        *,
        session_id: str | None = None,
        display_name: str | None = None,
        metadata: Mapping[str, str] | None = None,
        config: JsonObject | None = None,
        profile: JsonObject | None = None,
    ) -> JsonObject:
        params: JsonObject = {}
        if session_id is not None:
            params["sessionId"] = session_id
        if display_name is not None:
            params["displayName"] = display_name
        if metadata:
            params["metadata"] = dict(metadata)
        if config is not None:
            params["config"] = config
        if profile is not None:
            params["profile"] = profile
        return await self.call("session/start", params)

    async def session_read(self, session_id: str) -> JsonObject:
        return await self.call("session/read", {"sessionId": session_id})

    async def session_close(self, session_id: str, *, force: bool = False) -> JsonObject:
        return await self.call("session/close", {"sessionId": session_id, "force": force})

    async def session_events_read(
        self,
        session_id: str,
        *,
        after_seq: int | None = None,
        limit: int | None = None,
        wait_ms: int | None = None,
    ) -> JsonObject:
        params: JsonObject = {"sessionId": session_id}
        if after_seq is not None:
            params["after"] = {"seq": after_seq}
        if limit is not None:
            params["limit"] = limit
        if wait_ms is not None:
            params["waitMs"] = wait_ms
        return await self.call("session/events/read", params)

    async def session_environments_activate(
        self, session_id: str, environment_id: str
    ) -> JsonObject:
        return await self.call(
            "session/environments/activate",
            {"sessionId": session_id, "environmentId": environment_id},
        )

    async def session_runs_start(
        self,
        session_id: str,
        text: str,
        *,
        submission_id: str | None = None,
        config: JsonObject | None = None,
    ) -> JsonObject:
        params: JsonObject = {
            "sessionId": session_id,
            "source": {"type": "input", "items": [{"type": "text", "text": text}]},
        }
        if submission_id is not None:
            params["submissionId"] = submission_id
        if config is not None:
            params["config"] = config
        return await self.call("session/runs/start", params)

    async def session_runs_read(self, session_id: str, run_id: str) -> JsonObject:
        return await self.call("session/runs/read", {"sessionId": session_id, "runId": run_id})

    async def session_runs_cancel(self, session_id: str, run_id: str) -> JsonObject:
        return await self.call("session/runs/cancel", {"sessionId": session_id, "runId": run_id})

    async def environments_read(self, environment_id: str) -> JsonObject:
        return await self.call("environments/read", {"environmentId": environment_id})

    async def environments_close(self, environment_id: str) -> JsonObject:
        return await self.call("environments/close", {"environmentId": environment_id})

    async def environments_list(
        self, *, registration_key_id: str | None = None, status: str | None = None
    ) -> JsonObject:
        params: JsonObject = {}
        if registration_key_id is not None:
            params["registrationKeyId"] = registration_key_id
        if status is not None:
            params["status"] = status
        return await self.call("environments/list", params)
