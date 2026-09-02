"""Run provenance manifest.

Planned responsibilities (see ``docs/next-steps.md``, slice 1): record what is
needed to reproduce a run without querying live state. At least the adapter
commit and ``uv.lock`` digest, Harbor and Codex agent versions, the Lightspeed
``initialize`` response and server build, profile revision and digest,
provider id and resolved model settings, ``envd`` version, target, and
SHA-256, dataset ref and task/image digests, environment provider and
resource/network settings, attempts, concurrency, timeouts, exclusions, UTC
start/end times, and the redacted preflight result.
"""
