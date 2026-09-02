from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from lightspeed_harbor.client import METHODS, LightspeedClient, LightspeedError

OPENRPC = (
    Path(__file__).resolve().parents[2]
    / "lightspeed"
    / "crates"
    / "api"
    / "contract"
    / "openrpc.json"
)


def _server(handler):
    return httpx.MockTransport(handler)


async def test_envelope_and_bearer_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"id": 1, "result": {"result": {"ok": True}, "notifications": []}}
        )

    async with LightspeedClient(
        "https://ls.example/rpc", "lsk_test", transport=_server(handler)
    ) as client:
        result = await client.call("initialize", {"clientInfo": {"name": "x"}})
    assert result == {"ok": True}
    assert seen["headers"]["authorization"] == "Bearer lsk_test"
    assert seen["body"]["jsonrpc"] == "2.0"
    assert seen["body"]["method"] == "initialize"
    assert seen["body"]["params"] == {"clientInfo": {"name": "x"}}
    assert isinstance(seen["body"]["id"], int)


async def test_no_key_means_no_header():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"id": 1, "result": {"result": {}}})

    async with LightspeedClient(
        "http://127.0.0.1:18080/rpc", None, transport=_server(handler)
    ) as client:
        await client.initialize()
    assert seen["auth"] is None


async def test_universe_header_for_trusted_header_gateways():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["universe"] = request.headers.get("x-lightspeed-universe")
        return httpx.Response(200, json={"id": 1, "result": {"result": {}}})

    async with LightspeedClient(
        "http://127.0.0.1:18080/rpc", "local", transport=_server(handler), universe="u-1"
    ) as client:
        await client.initialize()
    assert seen["universe"] == "u-1"


async def test_error_data_kind_wins_over_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": 1,
                "error": {
                    "code": -32010,
                    "message": "x",
                    "data": {"kind": "environment_not_ready", "message": "booting"},
                },
            },
        )

    async with LightspeedClient(
        "https://ls.example/rpc", "k", transport=_server(handler)
    ) as client:
        with pytest.raises(LightspeedError) as excinfo:
            await client.environments_read("environment_1")
    assert excinfo.value.kind == "environment_not_ready"
    assert excinfo.value.message == "booting"
    assert excinfo.value.method == "environments/read"


async def test_error_code_maps_when_no_data():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": 1, "error": {"code": -32004, "message": "no such session"}}
        )

    async with LightspeedClient(
        "https://ls.example/rpc", "k", transport=_server(handler)
    ) as client:
        with pytest.raises(LightspeedError) as excinfo:
            await client.session_close("session_1")
    assert excinfo.value.not_found
    assert "no such session" in excinfo.value.message


async def test_http_failure_is_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    async with LightspeedClient(
        "https://ls.example/rpc", "k", transport=_server(handler)
    ) as client:
        with pytest.raises(LightspeedError) as excinfo:
            await client.initialize()
    assert excinfo.value.kind == "transport"
    assert excinfo.value.code == 502


async def test_unknown_method_is_rejected_locally():
    async with LightspeedClient(
        "https://ls.example/rpc", "k", transport=_server(lambda r: httpx.Response(200))
    ) as client:
        with pytest.raises(ValueError, match="not part of the adapter contract"):
            await client.call("operator/universes/delete")


async def test_runs_start_keeps_instruction_bytes():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = json.loads(request.content)["params"]
        return httpx.Response(200, json={"id": 1, "result": {"result": {"run": {"id": "run_1"}}}})

    instruction = "Fix the build.\n\n  keep   spacing\t and unicode: é 🚀\n"
    async with LightspeedClient(
        "https://ls.example/rpc", "k", transport=_server(handler)
    ) as client:
        await client.session_runs_start("session_1", instruction, submission_id="sub_1")
    assert seen["params"]["source"] == {
        "type": "input",
        "items": [{"type": "text", "text": instruction}],
    }
    assert seen["params"]["submissionId"] == "sub_1"


def test_methods_exist_in_released_contract():
    if not OPENRPC.is_file():
        pytest.skip(f"sibling checkout not present: {OPENRPC}")
    names = {m["name"] for m in json.loads(OPENRPC.read_text())["methods"]}
    missing = sorted(METHODS - names)
    assert not missing, f"methods missing from openrpc.json: {missing}"
