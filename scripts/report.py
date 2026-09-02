"""Paired report from a retained Harbor job directory.

Planned (docs/next-steps.md, slice 4): read Harbor results plus the Lightspeed
artifacts, compute successes / eligible trials, success rate by agent, paired
task-level difference, and a task-resampled confidence interval, then write a
machine-readable summary and a concise Markdown report. Never queries live
state.

Usage: uv run python scripts/report.py --job-dir jobs/<job-name>
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-dir", type=Path, required=True, help="Harbor job output directory")
    args = parser.parse_args(argv)
    if not args.job_dir.is_dir():
        print(f"job directory not found: {args.job_dir}", file=sys.stderr)
        return 2
    print("report is not implemented yet; see docs/next-steps.md, slice 4", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
