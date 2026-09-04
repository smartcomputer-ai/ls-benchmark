"""One Harbor trial through fakes of both boundaries: the sandbox and the Lightspeed API."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from harbor.models.agent.context import AgentContext

from lightspeed_harbor.agent import LightspeedAgent
from lightspeed_harbor.client import LightspeedClient
from lightspeed_harbor.config import HostSettings
from lightspeed_harbor.envd import SandboxPaths
from lightspeed_harbor.errors import HarnessSetupError
from tests.fakes import GIT_SHA as GIT_SHA_EXPECTED
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
    # setup() asks the server which build it is; run() records it again for provenance.
    assert methods[:7] == [
        "initialize",
        "initialize",
        "environments/read",
        "models/list",
        "session/start",
        "session/environments/activate",
        "session/runs/start",
    ]
    assert "session/runs/cancel" not in methods
    # The environment and its daemon outlive the agent phase so services the
    # agent left running are still there for the verifier.
    assert methods[-1] == "session/close"
    assert "environments/close" not in methods
    assert (
        fake.params("session/environments/activate")[0]["environmentId"] == RECEIPT["environmentId"]
    )

    start = fake.params("session/runs/start")[0]
    assert start["source"]["items"] == [{"type": "text", "text": INSTRUCTION}]
    assert start["submissionId"] == str(agent.context_id)

    # The harness prompt rides in an inline profile, so the config is the
    # profile document's config and no bare `config` is sent.
    session_start = fake.params("session/start")[0]
    assert "config" not in session_start
    profile = session_start["profile"]
    assert profile["kind"] == "inline"
    assert profile["profile"]["instructions"]["type"] == "text"
    # The bundled prompt goes out verbatim; its wording is the operator's.
    from importlib import resources

    bundled = resources.files("lightspeed_harbor").joinpath("prompts", "harbor-terminal.md")
    assert profile["profile"]["instructions"]["text"] == bundled.read_text()
    assert "# Working in this environment" in profile["profile"]["instructions"]["text"]
    config = profile["profile"]["config"]
    assert config["model"] == {
        "providerId": "openai",
        "model": "model-snapshot",
        "apiKind": "openai:responses",
    }
    assert config["features"] == {
        "environments": {"selectionTools": False, "jobs": True, "registrationKeys": ["key_1"]}
    }
    assert config["generation"] == {"reasoningEffort": "high"}
    assert session_start["displayName"] == agent.session_id
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
    assert lightspeed["cleanup"]["session/close"] == "ok"
    assert lightspeed["cleanup"]["envd/stop"].startswith("skipped")
    assert lightspeed["cleanup"]["environments/close"].startswith("skipped")

    # The key entered the sandbox only as a file, which is gone; no secret in any command.
    assert str(PATHS.key_file) not in env.files
    assert env.started and not env.stopped
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
    assert provenance["lightspeed"]["serverInfo"]["name"] == "lightspeed"
    assert provenance["lightspeed"]["serverInfo"]["gitSha"] == GIT_SHA_EXPECTED
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
        "tool_output_bytes": 0,
        "tool_output_truncations": 0,
        "sleep_commands": 0,
        "model_time_ms": 3000,
        "tool_time_ms": 1000,
        "time_to_first_model_request_ms": 100,
        "time_to_first_tool_call_ms": 2200,
        "run_duration_ms": 4400,
        "terminal_event": "runCompleted",
        "failure_kind": None,
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

    assert fake.methods() == ["initialize"], "setup only asks the server which build it is"
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
    assert fake.methods() == ["initialize"]


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
    assert "environments/close" not in fake.methods(), "kept alive for the verifier"
    assert not env.stopped


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
    assert "environments/close" not in methods
    assert not env.stopped
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


def test_measures_account_for_tool_output_sleep_polling_and_failure_kind():
    from lightspeed_harbor.agent import compute_measures

    events = [
        {"observedAtMs": 10, "kind": {"type": "runStarted", "runId": "run_1"}},
        {
            "observedAtMs": 20,
            "kind": {
                "type": "toolBatchStarted",
                "runId": "run_1",
                "batchId": "b1",
                "calls": [
                    {"callId": "c1", "arguments": '{"cmd": "sleep 30; tail build.log"}'},
                    {"callId": "c2", "arguments": '{"command": "make -j4"}'},
                    {"callId": "c3", "arguments": '{"argv": ["sleep", "5"]}'},
                    {"callId": "c4", "arguments": "not json"},
                ],
            },
        },
        {
            "observedAtMs": 30,
            "kind": {
                "type": "toolCallCompleted",
                "runId": "run_1",
                "status": "succeeded",
                "outputBytes": 70000,
                "truncated": True,
            },
        },
        {
            "observedAtMs": 31,
            "kind": {
                "type": "toolCallCompleted",
                "runId": "run_1",
                "status": "succeeded",
                "outputBytes": 12,
                "truncated": False,
            },
        },
        {
            "observedAtMs": 40,
            "kind": {"type": "runFailed", "runId": "run_1", "kind": "limit_exceeded"},
        },
    ]
    m = compute_measures(events, "run_1")
    assert m["sleep_commands"] == 2
    assert m["tool_output_bytes"] == 70012
    assert m["tool_output_truncations"] == 1
    assert m["terminal_event"] == "runFailed"
    assert m["failure_kind"] == "limit_exceeded"


def _discovery_transport(git_sha: str, binary: bytes = b"ELF-envd") -> httpx.MockTransport:
    """Serve a P152 discovery document and the archive it names."""
    import hashlib
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("lightspeed-envd")
        info.size = len(binary)
        tar.addfile(info, io.BytesIO(binary))
    archive = buffer.getvalue()
    digest = hashlib.sha256(archive).hexdigest()
    document = {
        "version": "0.1.0",
        "gitSha": git_sha,
        "channel": "main",
        "protocolVersion": 2,
        "artifacts": {
            "x86_64-unknown-linux-musl": {
                "file": "lightspeed-envd-0.1.0-x86_64-unknown-linux-musl.tar.gz",
                "sha256": digest,
                "url": "https://ls.example/.well-known/lightspeed-envd/x/envd.tar.gz",
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/lightspeed-envd":
            return httpx.Response(200, json=document)
        if request.url.path.endswith("envd.tar.gz"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _discovery_host(tmp_path: Path) -> HostSettings:
    return HostSettings(
        api_url="https://ls.example/rpc",
        api_key=API_KEY,
        registration_key=REGISTRATION_KEY,
        gateway_url="wss://ls.example/environment-gateway/connect",
        envd_discovery_url="https://ls.example/.well-known/lightspeed-envd",
    )


async def test_setup_resolves_envd_from_the_discovery_document(tmp_path: Path, monkeypatch):
    from tests.fakes import GIT_SHA

    monkeypatch.setattr("lightspeed_harbor.envd.default_cache_dir", lambda: tmp_path / "cache")
    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment()
    agent = make_agent(tmp_path, _discovery_host(tmp_path), fake)
    agent._artifact_transport = _discovery_transport(GIT_SHA)
    await agent.setup(env)
    assert agent._artifact.source == "discovery"
    assert agent._artifact.git_sha == GIT_SHA
    assert agent._artifact.target == "x86_64-unknown-linux-musl"
    assert env.files[str(PATHS.binary)] == b"ELF-envd"
    await agent.run(INSTRUCTION, env, AgentContext())
    provenance = _artifact(tmp_path, "provenance.json")
    assert provenance["envd"]["artifact"]["source"] == "discovery"
    assert provenance["lightspeed"]["serverInfo"]["gitSha"] == GIT_SHA


async def test_setup_refuses_an_envd_from_another_commit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("lightspeed_harbor.envd.default_cache_dir", lambda: tmp_path / "cache")
    fake = FakeLightspeed()
    env = FakeEnvironment()
    agent = make_agent(tmp_path, _discovery_host(tmp_path), fake)
    agent._artifact_transport = _discovery_transport("0" * 40)
    with pytest.raises(HarnessSetupError, match="mismatched daemon"):
        await agent.setup(env)
    assert str(PATHS.binary) not in env.files, "nothing is uploaded before the check"


async def test_sandbox_envd_must_report_the_servers_build(tmp_path: Path, host: HostSettings):
    from dataclasses import replace

    fake = FakeLightspeed()
    env = FakeEnvironment(version_output="lightspeed-envd 0.1.0 (git 0000000000, x86_64)")
    agent = make_agent(tmp_path, host, fake)
    with pytest.raises(HarnessSetupError, match="not the server's build"):
        await agent.setup(env)
    tolerant = make_agent(tmp_path, replace(host, envd_allow_mismatch=True), FakeLightspeed())
    await tolerant.setup(FakeEnvironment(version_output="lightspeed-envd 0.1.0 (git 0000000000)"))


async def test_session_carries_campaign_and_retention(tmp_path: Path, host: HostSettings):
    from dataclasses import replace

    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment()
    agent = make_agent(
        tmp_path, replace(host, campaign="tb2-lightspeed-2026-09-03", session_ttl_sec=3600), fake
    )
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    start = fake.params("session/start")[0]
    assert start["metadata"]["campaign"] == "tb2-lightspeed-2026-09-03"
    assert start["metadata"]["harborTaskName"] == "toy-file-write"
    assert start["metadata"]["source"] == "harbor"
    assert start["deleteAfterCloseMs"] == 3_600_000
    no_ttl = make_agent(
        tmp_path / "b",
        replace(host, session_ttl_sec=None),
        FakeLightspeed(run_statuses=("completed",)),
    )
    await no_ttl.setup(FakeEnvironment())
    await no_ttl.run(INSTRUCTION, FakeEnvironment(), AgentContext())


async def test_opting_out_tears_the_environment_down_after_the_run(
    tmp_path: Path, host: HostSettings
):
    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake, keep_environment_for_verifier=False)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    assert env.stopped
    assert fake.methods()[-2:] == ["session/close", "environments/close"]


async def test_no_instructions_sends_a_bare_config_without_jobs(tmp_path: Path, host: HostSettings):
    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake, instructions="none", jobs=False)
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    start = fake.params("session/start")[0]
    assert "profile" not in start
    assert start["config"]["features"]["environments"] == {
        "selectionTools": False,
        "jobs": False,
        "registrationKeys": ["key_1"],
    }
    assert _artifact(tmp_path, "provenance.json")["instructions"] is None


async def test_instructions_come_from_a_file_and_are_recorded(tmp_path: Path, host: HostSettings):
    import hashlib

    prompt = tmp_path / "custom.md"
    prompt.write_text("Use the tools well.\n")
    fake = FakeLightspeed(run_statuses=("completed",))
    env = FakeEnvironment()
    agent = make_agent(tmp_path, host, fake, instructions=str(prompt))
    await agent.setup(env)
    await agent.run(INSTRUCTION, env, AgentContext())
    document = fake.params("session/start")[0]["profile"]["profile"]
    assert document["instructions"] == {"type": "text", "text": "Use the tools well.\n"}
    record = _artifact(tmp_path, "provenance.json")["instructions"]
    assert record["source"] == str(prompt)
    assert record["sha256"] == hashlib.sha256(b"Use the tools well.\n").hexdigest()
    assert record["bytes"] == len(b"Use the tools well.\n")
    # The prompt text itself is not the task, so the run record must not
    # confuse the two: the instruction bytes are the task's alone.
    assert _artifact(tmp_path, "run.json")["instruction_bytes"] == len(INSTRUCTION.encode())


def test_unknown_instructions_fail_at_construction(tmp_path: Path, host: HostSettings):
    with pytest.raises(HarnessSetupError, match="neither a bundled prompt nor a file"):
        make_agent(tmp_path, host, FakeLightspeed(), instructions="no-such-prompt")
