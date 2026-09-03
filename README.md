# ls-benchmark

End-to-end evaluation of the [Lightspeed](https://github.com/smartcomputer-ai/lightspeed)
agent system against other complete agent systems on the same terminal tasks.

The first comparison is Lightspeed versus the built-in Codex agent on
[Terminal-Bench](https://www.tbench.ai/), orchestrated by
[Harbor](https://harborframework.com/). Both arms use the same pinned model,
reasoning effort, task image, verifier, resources, and timeout. Each keeps its
native prompt, context management, tool loop, and terminal/file tools. The
result is a product-level comparison, not an attribution of a score difference
to one harness component.

## How it fits together

```text
Harbor job (laptop or CI runner)
├── built-in Codex agent            runs inside the task sandbox
└── LightspeedAgent (this repo)     runs in Harbor's orchestrator process
      ├── uploads and starts lightspeed-envd inside the Harbor sandbox
      ├── envd registers outbound to the hosted Lightspeed gateway
      ├── starts a Lightspeed session (process + job tools, the harness prompt
      │   from src/lightspeed_harbor/prompts/ as base instructions) and
      │   activates that exact environment
      ├── starts one run with the unmodified task instruction
      └── waits, exports artifacts, leaves envd and the environment alive
Harbor then runs the verifier in the same sandbox and destroys it; the
registration key's ephemeral grace closes the environment afterwards.
```

Lightspeed enters at Harbor's agent boundary as an external `BaseAgent`. It
never provisions the sandbox, reinterprets the verifier, or implements a Harbor
environment. Only `lightspeed-envd` runs inside the sandbox; all model turns
and environment operations run through hosted Lightspeed.


## The Lightspeed sibling checkout

The design, protocol, and API contracts this repository implements against
live in the Lightspeed repository. Keep it checked out as a sibling directory
so relative links in the docs resolve:

```text
dev/
├── lightspeed/      https://github.com/smartcomputer-ai/lightspeed
└── ls-benchmark/    this repository
```

If it is not checked out yet:

```bash
git clone https://github.com/smartcomputer-ai/lightspeed ../lightspeed
```

Start with these files in the sibling checkout:

- `docs/roadmap/p149-harbor-end-to-end-agent-evaluation.md` — the design this
  repository implements: ownership boundary, adapter contract, parity rules,
  artifacts, failure taxonomy, and implementation slices.
- `docs/roadmap/p148-key-based-outbound-environment-registration.md` —
  key-based outbound `envd` registration, the registration receipt, and
  ephemeral cleanup.
- `docs/variables.md` — the authoritative `LIGHTSPEED_ENVD_*` reference.
- `crates/api/contract/api-reference.md` and `api.schema.json` — the JSON-RPC
  methods and parameter shapes the adapter calls.
- `scripts/dev/README.md` — running a local Lightspeed stack for integration
  tests.

The sibling checkout is for reading, building a local `lightspeed-envd`, and
running a local stack. The adapter code must not import from it, read its
source tree, or depend on a monorepo-relative path. It talks to Lightspeed
only through released APIs and `envd` artifacts. Local overrides that point at
a locally built binary or a local endpoint are explicit configuration, not
defaults.

## Getting started

Requires [uv](https://docs.astral.sh/uv/). The lockfile pins Harbor and every
adapter dependency.

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run harbor --help
```

Host-side configuration is read from the environment; see
[.env.example](.env.example). No secret is ever written into a task sandbox
except the registration key file, which is deleted once the registration
receipt appears.

## License

Apache-2.0. See [LICENSE](LICENSE).
