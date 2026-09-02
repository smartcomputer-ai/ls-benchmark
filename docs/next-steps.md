# Next steps

Implementation backlog for this repository, ordered by the slices in the
Lightspeed design document
`../lightspeed/docs/roadmap/p149-harbor-end-to-end-agent-evaluation.md`.
Tick items as they land and record the Harbor and Lightspeed versions a slice
was verified against. Keep this file the single place where progress is
tracked; the Lightspeed roadmap document only links here.

## Verified so far (2026-09-02)

Scaffold established on Harbor `0.22.0`, Python `3.13`, uv `0.8.22`.

Harbor contract as found in the pinned package (`harbor.agents.base`,
`harbor.models`):

- `BaseAgent.__init__(logs_dir, model_name=None, logger=None, mcp_servers=None,
  skills_dir=None, *, extra_env=None, load_trajectory=None,
  environment_logs_dir=None, **kwargs)`. Job `agents[].kwargs` arrive as
  keyword arguments; `agents[].env` arrives as `extra_env`.
- After construction the trial sets `agent.session_id` (`<trial>__agent`) and
  `agent.context_id` (the trial UUID). `context_id` is the durable join key.
- Abstract members: `name()` (static), `version()`, `async setup(environment)`,
  `async run(instruction, environment, context)`. Optional
  `populate_context_post_run(context)` runs after logs sync to the host.
- `AgentContext` fields: `n_input_tokens` (includes cache), `n_cache_tokens`,
  `n_output_tokens`, `cost_usd`, `rollout_details`, `metadata`.
- Class flags default to false: `SUPPORTS_ATIF`, `SUPPORTS_RESUME`,
  `SUPPORTS_WINDOWS`, `SUPPORTS_HANDOFF`. ATIF models live in
  `harbor.models.trajectories`.
- `BaseEnvironment`: `default_user`, `exec(command, cwd, env, timeout_sec,
  user)`, `upload_file(source_path, target_path)`, `download_file`, `is_file`,
  `network_policy`. Sandbox paths: `/logs/agent`, `/logs/artifacts`,
  `/logs/verifier` (`harbor.models.trial.paths.EnvironmentPaths`).
- Job config (`harbor.models.job.config.JobConfig`): `n_attempts`,
  `n_concurrent_trials`, `retry` (`max_retries` defaults to 0 with a
  do-not-retry exception list), `environment.type` (`docker`, `daytona`, `e2b`,
  `modal`, `runloop`, `ec2`, ...), `environment.delete`, `datasets[]`
  (`name`, `version`, `ref`, `path`, `task_names`, `exclude_task_names`,
  `n_tasks`), `agents[]` (`name` or `import_path`,
  `model_name`, `n_concurrent`, `concurrency_group`, `kwargs`, `env`,
  `extra_allowed_hosts`, `override_timeout_sec`, `include_logs`).
- Built-in agents: `codex` (kwarg `reasoning_effort` maps to
  `-c model_reasoning_effort=`), `oracle`, `nop`.
- CLI: `harbor run -c <config>` (alias of `harbor job start`), `harbor task`,
  `harbor dataset`, `harbor trial`, `harbor analyze`.
- Dataset (corrected 2026-09-02): with no `registry_url`/`repo`, Harbor's
  default `HarborRegistryClient` resolves `name: terminal-bench`,
  `version: "2.0"`: 89 tasks from `laude-institute/terminal-bench-2` at
  `69671fbaac6d67a7ef0dfec016cc38a64ef7a77c` with prebuilt images
  `alexgshaw/<task>:20251031`. `terminal-bench-sample` `2.0` is a 10-task
  subset of the same repository. `terminal-bench/terminal-bench-2-1` is a Hub
  *leaderboard* slug, not a dataset name; the registry answers
  `Dataset terminal-bench/terminal-bench-2-1 not found`. Task defaults seen:
  `agent.timeout_sec = 900`, `verifier.timeout_sec = 900`, `cpus = 1`,
  `memory = 2G`–`4G`, `storage = 10G`, workdir `/app`.
- Image architecture: the inspected task images are single-manifest
  `linux/amd64`. Harbor's Docker backend never passes `--platform`, so on an
  arm64 daemon they run under emulation.

Lightspeed contract: see [lightspeed-contract.md](lightspeed-contract.md).

## Environment facts (2026-09-02)

- The Lightspeed release pipeline (`release/metadata.env`,
  `scripts/release/build-dist.sh`, `run-build-env.sh`) builds
  `lightspeed-envd` for `x86_64-unknown-linux-gnu` only, inside a
  digest-pinned `rust:1.97.1-bookworm` image run with `--platform
  linux/amd64`. There is no aarch64 Linux artifact.
- This laptop: Docker Desktop with a `linux/arm64` daemon, 8 CPUs, 7.6 GiB.
  It runs `--platform linux/amd64` containers under emulation (verified:
  `alpine uname -m` prints `x86_64`). The host Rust toolchain has only
  `aarch64-apple-darwin`; no `cross` or `cargo-zigbuild`.
- Codex arm setup needs egress to `raw.githubusercontent.com`, `nodejs.org`,
  and the npm registry (nvm plus `npm install -g @openai/codex`) before the
  agent phase. The `codex` agent takes a `version` kwarg to pin the CLI.
- Lightspeed API shapes the client will use (`api.schema.json`):
  - `session/start {sessionId?, displayName?, profile?, config?}`; `profile`
    is `{kind: "named", profileId}` or `{kind: "inline", profile}`.
  - `SessionConfig {model: {providerId, model, apiKind}, generation:
    {reasoningEffort, processingTier, maxOutputTokens, parallelToolUse},
    limits: {maxTurns, maxToolRounds}, features: {environments:
    {registrationKeys?, selectionTools?, jobs?}, mcp?, web?, subagents?,
    vfs?, timers?}, context: {compaction}}`.
  - `session/runs/start {sessionId, submissionId?, source: {type: "input",
    items: [{type: "text", text}]}, config?}`.
  - `session/runs/read {sessionId, runId}` returns `RunView {id, status,
    startedAtMs, completedAtMs, usage, entries, toolBatches,
    pendingApprovals}`; `status` is one of `queued | running | parked |
    cancelling | completed | failed | cancelled`; `usage` is `LlmUsageView
    {inputTokens, cachedInputTokens, cacheWriteInputTokens, outputTokens,
    reasoningTokens, totalTokens}`.
  - `session/events/read {sessionId, after?, limit?, waitMs?}` long-polls.
  - `environments/registration-keys/create {displayName, identityMode:
    "ephemeral", maxActiveEnvironments?, expiresAtMs?,
    ephemeralDisconnectGraceMs?}` returns the secret once; the Lightspeed
    CLI exposes it as the environments `registration-keys create`
    subcommand.
  - `environments/list {registrationKeyId?, status?}`; `models/list
    {selectableOnly?}` returns `{models, providers[{providerId, apiKinds,
    credential}]}`.

## Slice 1 — Adapter skeleton and reproducibility

Exit: Harbor can load the agent, expand a job, and run the adapter against
fakes without a source patch or unpinned dependency.

- [x] Repository, `pyproject.toml`, `uv.lock` pinning Harbor `0.22.0`.
- [x] `lightspeed_harbor.agent:LightspeedAgent` importable through
  `AgentFactory.create_agent_from_import_path`.
- [x] Host settings and agent kwargs validated fail-closed at construction.
- [x] CI running ruff and pytest under `uv sync --frozen`.
- [x] Every committed `configs/*.yaml` loads through the pinned `JobConfig`
  and names the Lightspeed arm with an explicit model (`tests/test_configs.py`).
- [x] Commit the smoke allowlist as `datasets[].task_names` (the pinned
  `DatasetConfig` also offers `exclude_task_names` and `n_tasks`, applied in
  that order). `configs/smoke.local.yaml` now names `terminal-bench` `2.0`
  and five tasks: `regex-log`, `log-summary-date-ranges`, `polyglot-c-py`,
  `sqlite-with-gcov`, `qemu-startup`. Oracle preflight decides whether they
  stay.
- [ ] Model mapping: resolve Harbor `model_name` (`<provider>/<id>`) to one
  explicit Lightspeed model-provider record via `lightspeed_provider_id`;
  reject aliases that are not immutable snapshots; no fallback.
- [ ] Provenance manifest (`provenance.py`): adapter commit, `uv.lock` digest,
  Harbor version, Codex agent version, Lightspeed `initialize` response,
  profile revision/digest, `envd` version and SHA-256, dataset ref, resolved
  model settings, UTC times.
- [ ] Fake `BaseEnvironment` and fake Lightspeed client for unit tests.
- [ ] A deterministic local toy task (`harbor init` template) whose verifier
  checks a filesystem mutation; verify `/logs` artifact collection with the
  `nop` and `oracle` agents.
- [ ] Decide whether the JSON-RPC client is hand-written over `httpx` or
  generated from `../lightspeed/crates/api/contract/openrpc.json`.

## Slice 2 — Real outbound environment lifecycle

Exit: a local Harbor toy task is completed by hosted Lightspeed through the
real `envd`, then verified by Harbor in the unchanged sandbox.

- [ ] `envd.py`: select the artifact for the sandbox platform (linux/amd64
  first, linux/arm64 next), verify SHA-256 on the host, upload through
  `environment.upload_file`, `chmod +x`, and check `envd --version` as
  `environment.default_user`. Unsupported platforms fail before a model call.
- [ ] Lightspeed side: publish a standalone `lightspeed-envd` release artifact
  with checksum, or document the release bundle path for the pinned Lightspeed
  version. Until then, `LIGHTSPEED_HARBOR_ENVD_PATH` is the only path.
- [ ] `run()`: write the registration key to a mode-`0600` file inside the
  sandbox, start `envd` with the variables in
  [lightspeed-contract.md](lightspeed-contract.md), wait for the receipt,
  validate identity mode and correlation fields, delete the key file.
- [ ] Never put the key in argv, process environment, logs, receipt, or
  artifacts. Test with a fake environment that records every `exec` call.
- [ ] `client.py`: `session/start` with the benchmark profile,
  `session/environments/activate` with the receipt's exact `environmentId`,
  `session/runs/start` with a `submissionId` and the instruction bytes
  unchanged, poll `session/runs/read` until terminal.
- [ ] Lightspeed side: define the committed benchmark profile
  (`harbor-terminal`): terminal-only toolset, no MCP, no skills, no bots, no
  sub-agents, no browser or web search.
- [ ] `finally`: cancel an active run, close the session, terminate the
  sandbox `envd` process group, `environments/close` the ephemeral
  environment. Cleanup errors never replace the original failure. The Harbor
  sandbox stays intact for the verifier.
- [ ] Harbor cancellation and timeout propagate to `session/runs/cancel`.
- [ ] Concurrency: two or more trials with one campaign key produce distinct
  receipts; an `envd` restart inside one live trial reconnects the same
  ephemeral identity during grace without creating a second environment.
- [ ] Leak audit helper: `environments/list` filtered by `registrationKeyId`.
- [ ] Network: `agents[].extra_allowed_hosts` for the gateway host on the
  Lightspeed arm only; verify the dataset network policy is otherwise
  unchanged.

## Slice 3 — Complete trial observability

Exit: every toy/smoke trial is diagnosable without querying the live service
or exposing credentials.

- [ ] Project usage, cost, terminal status, and timings into `AgentContext`
  (`n_input_tokens`, `n_cache_tokens`, `n_output_tokens`, `cost_usd`,
  `metadata`). Populate progressively so a timeout still leaves data.
- [ ] `artifacts.py`: write `/logs/agent/lightspeed/envd.log` and
  `/logs/artifacts/lightspeed/{registration,run,provenance,trajectory}.json`,
  bounded and redacted. Test that no artifact contains a registration key,
  API key, provider key, or authorization header.
- [ ] Export raw Lightspeed events from `session/events/read`; add ATIF
  conversion (`harbor.models.trajectories`) only where the mapping is
  faithful, otherwise mark trajectory support unavailable and keep
  `SUPPORTS_ATIF = False`.
- [ ] Failure taxonomy: dataset/preflight, compute infrastructure, harness
  setup, agent execution, verification, artifact-only. Map each adapter
  boundary to one class and record it in `run.json`.
- [ ] Infrastructure retry allowlist: only failures that occur before the
  agent can influence the sandbox. Align with Harbor's `RetryConfig`
  exclusion list; never retry a verifier failure, agent timeout, provider
  refusal, gateway error, or environment disconnect.
- [ ] Secondary measures: model calls, time to first model request and first
  environment operation, environment tool calls and errors, output
  truncations, setup/registration/cleanup durations.

## Slice 4 — Paired Terminal-Bench comparison

Exit: one command produces a reproducible paired local comparison.

- [ ] Commit `configs/terminal-bench.local.yaml` with the Codex and Lightspeed
  matrix: same immutable model id, same reasoning effort, equal
  `n_concurrent`, interleaved trials, pinned dataset ref.
- [ ] `scripts/preflight.py`: resolve both agent configurations and fail
  unless model, provider route, reasoning, processing tier, output limit,
  instruction bytes, task/image digests, compute, network, attempts, and
  concurrency match. Run Harbor's `oracle` on the selected tasks. Make a real
  TLS/WebSocket reachability check from a sandbox. Emit a redacted preflight
  result for the provenance manifest.
- [ ] Smoke allowlist covering file editing, long-running commands, process
  control, and output-heavy terminal interaction. Selection is for
  integration coverage, not score.
- [ ] `scripts/report.py`: read the Harbor job directory plus artifacts;
  compute successes / eligible trials, success rate by agent, paired
  task-level difference, and a task-resampled confidence interval (attempts
  for one task stay together). Never query live state.
- [ ] Audit one local Docker smoke run for equal instructions, users, working
  directories, resources, network, timeouts, and artifacts.

## Slice 5 — Remote compute and full campaign

Exit: Harbor runs from a developer machine while remote task compute connects
to hosted Lightspeed, with complete results and no leaked environments.

- [ ] Commit `configs/terminal-bench.remote.example.yaml` for one Harbor
  remote environment provider; only the `environment` section and gateway
  egress differ from the local config.
- [ ] Operator guide: gateway egress, registration-key policy (ephemeral,
  active limit, expiry, rotation), concurrency, provider quota, cleanup.
- [ ] Lightspeed side, if a second `environment-gateway` replica is ever
  needed: multi-replica owner routing (still open in the Lightspeed
  registration roadmap).
- [ ] Freeze the full-run manifest (tasks, attempts, exclusions with reasons),
  run oracle preflight, execute the paired campaign, retain the raw job
  directory, parity manifest, exclusions, and report.

## Path to the first run

Goal: one paired smoke job (Codex versus Lightspeed) on the committed
allowlist, verified by Harbor, with artifacts, before any full campaign. The
minimal cut across the slices, in order:

1. Lightspeed side (in flight): deploy the `harbor` branch with the
   `environment-gateway` role on a public `wss://` route; mint an
   api-key-mode `LIGHTSPEED_API_KEY` for the evaluation universe and one
   `ephemeral` registration key (`maxActiveEnvironments` = Harbor concurrency
   plus a small margin, campaign expiry); publish the
   `x86_64-unknown-linux-gnu` `envd` archive URL and SHA-256. Confirm through
   `models/list` that the chosen provider record exposes the immutable OpenAI
   snapshot the Codex arm will use.
2. `client.py`: hand-written JSON-RPC over `httpx` (already in `uv.lock`
   through Harbor), Bearer auth, `AgentApiOutcome` unwrapping, typed errors,
   the methods listed under environment facts. Unit-tested against a fake
   transport. A contract test checks every method name against
   `../lightspeed/crates/api/contract/openrpc.json` when the sibling checkout
   exists and is marked skipped, loudly, when it does not.
3. `envd.py` and `agent.setup`: probe `uname -m` inside the sandbox instead
   of trusting the host, map it to the artifact target, verify SHA-256,
   upload, `chmod`, run `--version`. Add `linux/arm64 ->
   aarch64-unknown-linux-gnu` to `SUPPORTED_TARGETS` so a locally built
   aarch64 binary serves development; release runs stay amd64.
4. `agent.run`: write the key file (`0600`), start `lightspeed-envd` with
   `setsid nohup ... &` as `default_user` and the `LIGHTSPEED_ENVD_*` set
   from `lightspeed-contract.md`, poll the receipt path with a bound, delete
   the key file, `session/start` with the inline profile below,
   `session/environments/activate`, `session/runs/start` with the
   instruction as one `text` item, poll `session/runs/read` (or long-poll
   `session/events/read`) until terminal, project usage into
   `AgentContext`, then in `finally`: `session/runs/cancel` if active,
   `session/close`, kill the `envd` process group, `environments/close`.
5. Inline benchmark profile for the first run: `features.environments`
   scoped to the campaign key through `registrationKeys`, `selectionTools:
   false`, no `mcp`, `web`, `subagents`, or `vfs`; `generation.reasoningEffort`
   from kwargs; `model` from `lightspeed_provider_id` plus `model_name`.
   Commit it on the Lightspeed side as `harbor-terminal` once stable.
6. Local toy task (`harbor task init`) whose verifier checks a file write.
   Run `nop`, `oracle`, then `LightspeedAgent` against the local `./dev.sh`
   stack (`ws://127.0.0.1` gateway, `LIGHTSPEED_HARBOR_ENVD_PATH` pointing at
   an aarch64 build made inside the release build image).
7. Smoke on the committed allowlist: `oracle` first, then the paired job, on
   native `linux/amd64` compute (see the decision below).

## Open decisions

- Package name `lightspeed_harbor` inside repository `ls-benchmark`: kept
  because the design document fixes the Harbor import path. Revisit only if
  the repository grows non-Harbor benchmarks.
- One registration key per campaign is the default. Per-job keys are an
  optional tighter policy, not a protocol requirement.
- Python `3.13` is pinned in `.python-version`; Harbor requires `>=3.12`.
- The Codex arm's provider credential uses Harbor's normal secret mechanism
  scoped to the Codex agent. The Lightspeed sandbox never receives a model
  credential.
- Where trials execute (decided 2026-09-02). Development of slices 2 and 3
  happens on this laptop against the local stack: native arm64 toy task, and
  the amd64 Terminal-Bench images only under emulation, which is acceptable
  for `oracle` checks and single-trial smoke but not for timed results. The
  paired smoke and every campaign run on native `linux/amd64` sandboxes,
  which is what the task images and the release `envd` are built for.
  First choice: Harbor plus Docker on one x86_64 Linux VM in our own
  infrastructure, same config as local with `environment.type: docker`, one
  CPU and 2–4 GiB per trial, so concurrency follows the box size. Scale-out
  option for a five-attempt campaign: a Harbor remote provider (`daytona`,
  `e2b`, `modal`, `runloop`; all amd64, all accept an `allowlist` network
  policy so `extra_allowed_hosts` can carry the gateway host), which changes
  only the `environment` block. Lightspeed itself is always the hosted
  deployment. Lightspeed's own Incus provider must not provision Harbor
  sandboxes; P149 keeps sandbox ownership with Harbor.
