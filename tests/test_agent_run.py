"""One Harbor trial through fakes of both boundaries: the sandbox and the Lightspeed API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest
from harbor.models.agent.context import AgentContext

from lightspeed_harbor.agent import LightspeedAgent
from lightspeed_harbor.client import LightspeedClient
from lightspeed_harbor.config import HostSettings
from lightspeed_harbor.envd import SandboxPaths
from lightspeed_harbor.errors import HarnessSetupError
from tests.fakes import RECEIPT, FakeEnvironment, FakeLightspeed

REGISTRATION_KEY = "lsrk_super_secret_value"
API_KEY = "lsk_api_secret_value"
INSTRUCTION = "Create /app/hello.txt with `hello from the agent`.\n\n  Keep   spacing, é 🚀\n"
PATHS = SandboxPaths()


@pytest.fixture
def host(tmp_path: Path) -> HostSettings:
    binary = tmp_path / "lightspeed-envd"
    binary.write_bytes(b"fake-envd")
    ca = tmp_path / "gateway-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    return HostSettings(
        api_url="https://ls.example/rpc",
        api_key=API_KEY,
        registration_key=REGISTRATION_KEY,
        gateway_url="wss://ls.example/environment-gateway/connect",
        envd_path=binary,
        envd_ca_file=ca,
    )


def make_agent(
    tmp_path: Path, host: HostSettings, fake: FakeLightspeed, **kwargs
) -> LightspeedAgent:
    kwargs.setdefault("poll_interval_sec", 0.0)
    agent = LightspeedAgent(
        logs_dir=tmp_path / "agent",
        model_name="openai/model-snapshot",
        lightspeed_provider_id="openai",
        profile_id="inline",
        reasoning_effort="high",
        host_settings=host,
        **kwargs,
    )
    # Harbor's Trial sets these after construction.
    agent.session_id = "toy-file-write__abc123__agent"
    agent.context_id = UUID("594025f3-7d65-4655-8576-4bee95002eae")
    agent._make_client = lambda: LightspeedClient(
        host.api_url, host.api_key, transport=fake.transport()
    )
    return agent


def _artifact(tmp_path: Path, name: str) -> dict:
    return json.loads((tmp_path / "agent" / "lightspeed-adapter" / name).read_text())


async def test_happy_path_end_to_end(tmp_path: Path, host: HostSettings):
    fake = FakeLightspeed(run_statuses=("running", "completed"))
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake)
    context = AgentContext()

    await agent.setup(env)
    assert env.files[str(PATHS.binary)] == b"fake-envd"
    assert str(PATHS.ca_file) in env.files
    assert "session/start" not in fake.methods(), "setup creates no remote state"

    await agent.run(INSTRUCTION, env, context)

    methods = fake.methods()
    assert methods[:6] == [
        "initialize",
        "environments/read",
        "models/list",
        "session/start",
        "session/environments/activate",
        "session/runs/start",
    ]
    assert "session/runs/cancel" not in methods
    assert methods[-2:] == ["session/close", "environments/close"]
    assert (
        fake.params("session/environments/activate")[0]["environmentId"] == RECEIPT["environmentId"]
    )
    assert fake.params("environments/close")[0]["environmentId"] == RECEIPT["environmentId"]

    start = fake.params("session/runs/start")[0]
    assert start["source"]["items"] == [{"type": "text", "text": INSTRUCTION}]
    assert start["submissionId"] == str(agent.context_id)

    config = fake.params("session/start")[0]["config"]
    assert config["model"] == {
        "providerId": "openai",
        "model": "model-snapshot",
        "apiKind": "openai:responses",
    }
    assert config["features"] == {
        "environments": {"selectionTools": False, "registrationKeys": ["key_1"]}
    }
    assert config["generation"] == {"reasoningEffort": "high"}
    assert fake.params("session/start")[0]["displayName"] == agent.session_id
    assert all(header == f"Bearer {API_KEY}" for header in fake.auth_headers)

    assert (context.n_input_tokens, context.n_cache_tokens, context.n_output_tokens) == (
        1200,
        300,
        80,
    )
    lightspeed = context.metadata["lightspeed"]
    assert lightspeed["status"] == "completed"
    assert lightspeed["session_id"] == "session_1"
    assert lightspeed["reasoning_tokens"] == 20
    assert list(lightspeed["cleanup"]) == ["session/close", "envd/stop", "environments/close"]
    assert set(lightspeed["cleanup"].values()) == {"ok"}

    # The key entered the sandbox only as a file, which is gone; no secret in any command.
    assert str(PATHS.key_file) not in env.files
    assert env.started and env.stopped
    joined = "\n".join(env.commands())
    assert REGISTRATION_KEY not in joined and API_KEY not in joined
    assert "LIGHTSPEED_ENVD_REGISTRATION_KEY_FILE" in joined

    for name in ("registration.json", "run.json", "provenance.json"):
        text = (tmp_path / "agent" / "lightspeed-adapter" / name).read_text()
        assert REGISTRATION_KEY not in text and API_KEY not in text
    run = _artifact(tmp_path, "run.json")
    assert run["status"] == "completed"
    assert run["instruction_bytes"] == len(INSTRUCTION.encode())
    assert run["usage"]["inputTokens"] == 1200
    registration = _artifact(tmp_path, "registration.json")
    assert registration["receipt"] == RECEIPT
    assert registration["registration_key_id"] == "key_1"
    assert registration["metadata"]["harborContextId"] == str(agent.context_id)
    provenance = _artifact(tmp_path, "provenance.json")
    assert provenance["lightspeed"]["serverInfo"] == {"name": "lightspeed", "version": "test"}
    assert provenance["envd"]["artifact"]["sha256"]

    events = _artifact(tmp_path, "events.json")
    assert events["count"] == 12 and events["complete"] and not events["truncated"]
    assert events["events"][-1]["kind"]["type"] == "runCompleted"
    assert run["measures"] == {
        "model_calls": 2,
        "turns": 1,
        "tool_batches": 1,
        "tool_calls": 1,
        "tool_errors": 0,
        "model_time_ms": 3000,
        "tool_time_ms": 1000,
        "time_to_first_model_request_ms": 100,
        "time_to_first_tool_call_ms": 2200,
        "run_duration_ms": 4400,
        "terminal_event": "runCompleted",
    }
    assert context.metadata["lightspeed"]["measures"]["model_calls"] == 2
    assert context.metadata["lightspeed"]["events_exported"] == 12


async def test_failed_run_is_recorded_and_the_verifier_still_runs(
    tmp_path: Path, host: HostSettings
):
    fake = FakeLightspeed(run_statuses=("failed",))
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake)
    context = AgentContext()
    await agent.setup(env)

    await agent.run(INSTRUCTION, env, context)  # no exception: Harbor verifies what is left

    lightspeed = context.metadata["lightspeed"]
    assert lightspeed["status"] == "failed"
    assert lightspeed["failure_class"] == "agent_execution"
    assert "provider refused" in lightspeed["error"]
    assert "session/events/read" in fake.methods()
    assert "session/runs/cancel" not in fake.methods()
    assert _artifact(tmp_path, "run.json")["failure_class"] == "agent_execution"


async def test_envd_exit_before_receipt_is_a_harness_setup_failure(
    tmp_path: Path, host: HostSettings
):
    fake = FakeLightspeed()
    env = FakeEnvironment(envd_dies=True)
    agent = make_agent(tmp_path, host, fake)
    await agent.setup(env)

    with pytest.raises(HarnessSetupError, match="exited before writing a registration receipt"):
        await agent.run(INSTRUCTION, env, AgentContext())

    assert fake.calls == [], "no session and no model call without a receipt"
    assert str(PATHS.key_file) not in env.files, "key file removed during cleanup"
    assert env.stopped
    run = _artifact(tmp_path, "run.json")
    assert run["failure_class"] == "harness_setup"
    assert "key revoked" in run["error"]
    assert run["cleanup"]["key/delete"] == "ok"


async def test_registration_timeout_reports_the_log(tmp_path: Path, host: HostSettings):
    fake = FakeLightspeed()
    env = FakeEnvironment(receipt=None, log_tail="registering with wss://ls.example ...")
    agent = make_agent(tmp_path, host, fake, registration_timeout_sec=0.05)
    await agent.setup(env)
    with pytest.raises(HarnessSetupError, match="no registration receipt"):
        await agent.run(INSTRUCTION, env, AgentContext())
    assert fake.calls == []


async def test_unknown_model_fails_closed_without_a_session(tmp_path: Path, host: HostSettings):
    other = {
        "providerId": "openai",
        "model": "some-other-model",
        "apiKind": "openai:responses",
        "displayName": "x",
        "capabilities": {},
        "source": "provider",
        "fetchedAtMs": 1,
    }
    fake = FakeLightspeed(models=[other])
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake)
    await agent.setup(env)

    with pytest.raises(HarnessSetupError, match="not exposed by provider"):
        await agent.run(INSTRUCTION, env, AgentContext())

    assert "session/start" not in fake.methods()
    assert "environments/close" in fake.methods(), "the registered environment is closed"
    assert env.stopped


async def test_receipt_and_environment_record_must_agree(tmp_path: Path, host: HostSettings):
    fake = FakeLightspeed()
    fake.environment["source"]["daemonId"] = "daemon_other"
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake)
    await agent.setup(env)
    with pytest.raises(HarnessSetupError, match="daemon id"):
        await agent.run(INSTRUCTION, env, AgentContext())
    assert "session/start" not in fake.methods()


async def test_harbor_cancellation_cancels_the_run_and_cleans_up(
    tmp_path: Path, host: HostSettings
):
    fake = FakeLightspeed(run_statuses=("running",))  # never terminal on its own
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake, poll_interval_sec=0.001)
    context = AgentContext()
    await agent.setup(env)

    task = asyncio.create_task(agent.run(INSTRUCTION, env, context))
    for _ in range(500):
        await asyncio.sleep(0.002)
        if fake.methods().count("session/read") >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    methods = fake.methods()
    assert "session/runs/cancel" in methods
    assert methods.index("session/runs/cancel") < methods.index("session/close")
    assert "environments/close" in methods
    assert env.stopped
    assert context.metadata["lightspeed"]["cancelled"] is True
    assert context.n_input_tokens == 1200, "usage projected progressively before the timeout"
    run = _artifact(tmp_path, "run.json")
    assert run["cancelled"] is True
    assert run["cleanup"]["runs/cancel"] == "ok"
    # The cancel response is the final run view, so the record is complete.
    assert run["status"] == "cancelled"
    assert run["entries"] == 1 and run["tool_batches"] == 1
    assert run["cleanup"]["events/export"] == "ok"
    assert run["measures"]["terminal_event"] == "runCancelled"
    assert _artifact(tmp_path, "events.json")["count"] == 12


async def test_setup_rejects_an_unexpected_envd_version(tmp_path: Path, host: HostSettings):
    from dataclasses import replace

    fake = FakeLightspeed()
    env = FakeEnvironment(version_output="lightspeed-envd 0.0.9 (old)")
    agent = make_agent(tmp_path, replace(host, envd_version="0.1.0"), fake)
    with pytest.raises(HarnessSetupError, match="expected version"):
        await agent.setup(env)


async def test_run_without_setup_is_refused(tmp_path: Path, host: HostSettings):
    agent = make_agent(tmp_path, host, FakeLightspeed())
    with pytest.raises(HarnessSetupError, match="setup"):
        await agent.run(INSTRUCTION, FakeEnvironment(), AgentContext())


def test_short_placeholder_keys_do_not_poison_redaction():
    from lightspeed_harbor.artifacts import RedactionError, assert_no_secrets

    assert_no_secrets('{"job": "toy-local", "host": "localhost"}', ["local"])
    with pytest.raises(RedactionError):
        assert_no_secrets('{"key": "lsrk_DQCi0aqxxxxxxxxxxxx"}', ["lsrk_DQCi0aqxxxxxxxxxxxx"])


async def test_transient_model_catalog_error_is_retried(
    tmp_path: Path, host: HostSettings, monkeypatch
):
    import lightspeed_harbor.agent as agent_module

    monkeypatch.setattr(agent_module, "_MODEL_LIST_BACKOFF_SEC", 0.0)
    fake = FakeLightspeed(run_statuses=("completed",))
    fake.provider_errors = ["provider returned HTTP 500", "provider returned HTTP 500"]
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake)
    context = AgentContext()
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, context)
    assert fake.methods().count("models/list") == 3
    assert context.metadata["lightspeed"]["status"] == "completed"


async def test_persistent_model_catalog_error_fails_closed(
    tmp_path: Path, host: HostSettings, monkeypatch
):
    import lightspeed_harbor.agent as agent_module

    monkeypatch.setattr(agent_module, "_MODEL_LIST_BACKOFF_SEC", 0.0)
    fake = FakeLightspeed()
    fake.provider_errors = ["provider returned HTTP 500"] * 10
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake)
    await agent.setup(env)
    with pytest.raises(HarnessSetupError, match="provider returned HTTP 500"):
        await agent.run(INSTRUCTION, env, AgentContext())
    assert fake.methods().count("models/list") == agent_module._MODEL_LIST_ATTEMPTS
    assert "session/start" not in fake.methods()


async def test_working_directory_comes_from_the_sandbox_when_undeclared(
    tmp_path: Path, host: HostSettings
):
    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment(workdir=None, image_workdir="/root/project")
    agent = make_agent(tmp_path, host, fake)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    start = next(c for c in env.commands() if "LIGHTSPEED_ENVD_CWD" in c)
    assert "LIGHTSPEED_ENVD_CWD=/root/project" in start
    assert "cd /root/project" in start
    assert "pwd" in env.commands()


async def test_declared_workdir_wins_over_the_sandbox_default(tmp_path: Path, host: HostSettings):
    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment(workdir="/app", image_workdir="/somewhere/else")
    agent = make_agent(tmp_path, host, fake)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    start = next(c for c in env.commands() if "LIGHTSPEED_ENVD_CWD" in c)
    assert "LIGHTSPEED_ENVD_CWD=/app" in start
    assert "pwd" not in env.commands()


def test_measures_ignore_other_runs_and_unknown_shapes():
    from lightspeed_harbor.agent import compute_measures

    assert compute_measures([], "run_1")["model_calls"] == 0
    events = [
        {"observedAtMs": 10, "kind": {"type": "runStarted", "runId": "run_1"}},
        {"observedAtMs": 20, "kind": {"type": "turnGenerationRequested", "runId": "run_2"}},
        {
            "observedAtMs": 30,
            "kind": {"type": "toolCallCompleted", "runId": "run_1", "status": "failed"},
        },
        {"observedAtMs": "bad", "kind": {"type": "runCompleted", "runId": "run_1"}},
        {"kind": "not-a-dict"},
    ]
    m = compute_measures(events, "run_1")
    assert m["time_to_first_model_request_ms"] is None
    assert m["tool_errors"] == 1
    assert m["terminal_event"] is None
