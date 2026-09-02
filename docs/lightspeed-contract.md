# Lightspeed interfaces the adapter depends on

Facts verified against the sibling Lightspeed checkout on 2026-09-02. When
the two disagree, the Lightspeed repository wins; update this file.

Sources in `../lightspeed`:

- `docs/variables.md` (`LIGHTSPEED_ENVD_*` and client variables)
- `docs/roadmap/p148-key-based-outbound-environment-registration.md`
- `crates/api/contract/api-reference.md`, `api.schema.json`, `openrpc.json`
- `crates/environment-daemon` (binary `lightspeed-envd`)

## envd inside the sandbox

`lightspeed-envd` is configured entirely by environment variables. The adapter
starts it as Harbor's `environment.default_user` with:

| Variable | Adapter value |
|---|---|
| `LIGHTSPEED_ENVD_GATEWAY_URL` | Public connect route, `wss://<host>/environment-gateway/connect`. Plain `ws://` only toward loopback. |
| `LIGHTSPEED_ENVD_REGISTRATION_KEY_FILE` | Mode-`0600` temp file in the sandbox holding the registration key. Never use the direct `LIGHTSPEED_ENVD_REGISTRATION_KEY` form: on Linux the initial process environment stays readable through `/proc`. Delete the file after the receipt appears. |
| `LIGHTSPEED_ENVD_REGISTRATION_RECEIPT` | Path the daemon writes the receipt to once admitted. |
| `LIGHTSPEED_ENVD_REGISTRATION_NAME` | Optional display-name hint. |
| `LIGHTSPEED_ENVD_REGISTRATION_METADATA` | JSON object of string correlation metadata. At most 32 entries, keys up to 64 bytes, values up to 256 bytes, no control characters, no `lightspeed.` prefix. Descriptive only. |
| `LIGHTSPEED_ENVD_CWD` | Harbor's agent working directory. |
| `LIGHTSPEED_ENVD_FS_ROOT` | `/` for the terminal-only track, subject to the same uid, gid, mounts, and container policy as the Codex process. |
| `LIGHTSPEED_ENVD_STATE_DIR` | Private directory inside the trial sandbox. Holds the Ed25519 daemon key (`daemon-key`, mode `0600`). Deleting it registers a new environment. |
| `LIGHTSPEED_ENVD_CA_FILE` | Optional extra TLS trust anchors for a gateway behind a private CA. The adapter uploads `LIGHTSPEED_HARBOR_ENVD_CA_FILE` from the host and points this at the copy. |

Identity mode is registration-key policy, not a daemon setting. The daemon
has no mode flag.

### Registration receipt

Written atomically to the receipt path and emitted as one structured log
event. Contains no secret.

```json
{
  "environmentId": "environment_...",
  "incarnationId": "incarnation_...",
  "daemonId": "daemon_...",
  "connectionId": "connection_...",
  "identityMode": "ephemeral"
}
```

The adapter activates only `environmentId` from that trial's receipt. It never
proposes an environment id or uses a Harbor id as an authentication claim.

### Rejections

Retryable (gateway unavailable, handshake timeout, capacity, rate limit):
`envd` reconnects with backoff. Terminal (unknown, revoked, or expired key;
closed environment; invalid signature; unsupported protocol): `envd` writes no
receipt and exits non-zero. The adapter must treat a non-zero exit before a
receipt as a harness-setup failure of the Lightspeed arm.

## JSON-RPC API from the Harbor host

Endpoint: `LIGHTSPEED_API_URL`, normally `https://<host>/rpc`. Auth:
`Authorization: Bearer lsk_...` with `LIGHTSPEED_API_KEY`. Every method
returns `AgentApiOutcome<T>`. Full parameter shapes are in `api.schema.json`.

Methods the adapter calls, with the required parameters:

| Method | Required params | Use |
|---|---|---|
| `initialize` | none | Protocol version and server identity for provenance. |
| `session/start` | none (`sessionId`, `displayName`, `profile`, `config` optional) | Create the trial session with the benchmark profile or explicit config. |
| `session/environments/activate` | `sessionId`, `environmentId` | Attach the receipt's exact environment. Session must be idle. |
| `session/runs/start` | `sessionId`, `source` (`submissionId` optional, use it) | Start the run with the instruction bytes unchanged. Returns on acceptance, not completion. |
| `session/runs/read`, `session/events/read` | per schema | Poll for terminal status; export events. |
| `session/runs/cancel` | `sessionId`, `runId` | On Harbor cancellation or timeout. |
| `session/close` | `sessionId` (`force` optional) | Cleanup. |
| `environments/close` | `environmentId` | Explicit ephemeral close; idempotent, asynchronous. |
| `environments/list` | none (`registrationKeyId` filter) | Leak audit by campaign key. |
| `environments/registration-keys/create` | `displayName`, `identityMode` (`maxActiveEnvironments`, `expiresAtMs`, `ephemeralDisconnectGraceMs` optional) | Operator step; plaintext returned once. |

Registered environments are grouped by registration key. The key's display
name is the group shown in model and UI views.

### Adapter-side layout

Inside the sandbox the adapter keeps everything under
`/tmp/lightspeed-harbor/` (mode `0700`, owned by the task user): `bin/lightspeed-envd`,
`state/` (`LIGHTSPEED_ENVD_STATE_DIR`), `registration.key` (deleted after the
receipt), `receipt.json`, `envd.pid`, and `gateway-ca.pem`. The daemon is
started with `setsid nohup ... &` so its process group can be terminated in
`finally`; stdout and stderr go to `/logs/agent/lightspeed/envd.log`, which
Harbor syncs with the agent logs. The adapter's own JSON artifacts
(`registration.json`, `run.json`, `provenance.json`) are written on the Harbor
host under `<trial>/agent/lightspeed/`, so they exist even when the sandbox is
unreachable; that is the one deviation from the `/logs/artifacts/lightspeed/`
paths in P149.

## envd artifact

The binary is built from `crates/environment-daemon` as `lightspeed-envd`.
The release pipeline (`scripts/release/build-dist.sh`) packages it as
`lightspeed-envd-<version>-x86_64-unknown-linux-gnu.tar.gz` (one member,
`lightspeed-envd`) plus a checksum file. The adapter accepts either that
archive or a bare binary at `LIGHTSPEED_HARBOR_ENVD_RELEASE_URL`, verifies
`LIGHTSPEED_HARBOR_ENVD_SHA256` against the download, and caches the binary
under `~/.cache/ls-benchmark/envd/<sha256>/`.

Two facts to keep in mind when choosing the artifact:

- The release builds on `rust:1.97.1-bookworm`, so the binary links against
  glibc 2.36. Terminal-Bench images based on `debian:bullseye` (glibc 2.31)
  will fail `envd --version` in `setup`, before any model call. A musl
  (static) target or an older build base on the Lightspeed side removes the
  constraint; until then such tasks are preflight exclusions.
- Only `x86_64-unknown-linux-gnu` is published. The adapter probes the sandbox
  with `uname -m` and also accepts `aarch64-unknown-linux-gnu` for arm64
  daemons; build that one locally with `scripts/build-envd-linux.sh arm64`
  (Docker, same pinned toolchain image) and point
  `LIGHTSPEED_HARBOR_ENVD_PATH` at `.local/envd/aarch64-unknown-linux-gnu/lightspeed-envd`.

## Local stack for integration tests

`../lightspeed/scripts/dev/README.md` documents `./dev.sh` profiles. The
registration live suite
`crates/temporal-server/tests/environment_registration_live.rs` shows the full
outbound registration lifecycle against a real gateway and is the reference
for what the adapter must observe.
