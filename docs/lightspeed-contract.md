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
| `LIGHTSPEED_ENVD_CA_FILE` | Optional extra TLS trust anchors for a gateway behind a private CA. |

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

## envd artifact

The binary is built from `crates/environment-daemon` as `lightspeed-envd`.
The release pipeline (`scripts/release/build-dist.sh`) stages it under the
name `envd` for target `x86_64-unknown-linux-gnu` alongside a checksum file.
No standalone `envd` release URL is published yet; until one exists, use the
`LIGHTSPEED_HARBOR_ENVD_PATH` override with a locally built binary:

```bash
cd ../lightspeed
cargo build -p environment-daemon --release
# target/release/lightspeed-envd on the host architecture; cross-compile for
# the sandbox architecture (linux/amd64 first) before uploading.
```

## Local stack for integration tests

`../lightspeed/scripts/dev/README.md` documents `./dev.sh` profiles. The
registration live suite
`crates/temporal-server/tests/environment_registration_live.rs` shows the full
outbound registration lifecycle against a real gateway and is the reference
for what the adapter must observe.
