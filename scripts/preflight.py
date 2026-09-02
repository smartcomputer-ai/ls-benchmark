"""Parity and oracle preflight for a paired Harbor job.

Planned (docs/next-steps.md, slice 4): resolve both agent configurations and
fail unless model snapshot, provider route, reasoning effort, processing tier,
output limit, instruction bytes, task/image digests, compute, network,
attempts, and concurrency match; run Harbor's oracle on the selected tasks;
make a real TLS/WebSocket reachability check from a sandbox; emit a redacted
preflight result for the provenance manifest.

Usage: uv run python scripts/preflight.py --config configs/smoke.local.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="Harbor job config")
    args = parser.parse_args(argv)
    if not args.config.is_file():
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2
    print("preflight is not implemented yet; see docs/next-steps.md, slice 4", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
