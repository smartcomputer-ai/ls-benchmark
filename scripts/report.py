"""Report from a retained Harbor job directory. Never queries live state.

Reads every ``<trial>/result.json`` plus the adapter's
``agent/lightspeed-adapter/run.json`` where present, and writes a JSON summary
and a Markdown report next to the job. With one agent it reports success
rate, failure classes, token and timing aggregates, and a per-task table.
With two agents on the same tasks it adds the paired task-level difference and
a task-resampled bootstrap confidence interval (attempts for one task stay
together).

Usage: uv run python scripts/report.py --job-dir jobs/<job> [--job-dir jobs/<rerun> --supersede]
       [--drop-failure-class harness_setup] [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

BOOTSTRAP_ROUNDS = 2000


def load_trials(job_dir: Path) -> list[dict[str, Any]]:
    trials = []
    for result in sorted(job_dir.glob("*/result.json")):
        r = json.loads(result.read_text())
        trial_dir = result.parent
        task = trial_dir.name.split("__")[0]
        agent = (r.get("agent_info") or {}).get("name") or "unknown"
        reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        exc = r.get("exception_info") or {}
        ar = r.get("agent_result") or {}
        ls = (ar.get("metadata") or {}).get("lightspeed") or {}
        run_json = trial_dir / "agent" / "lightspeed-adapter" / "run.json"
        run = json.loads(run_json.read_text()) if run_json.is_file() else {}
        started = (r.get("started_at") or "")[:19]
        trials.append(
            {
                "trial": trial_dir.name,
                "task": task,
                "agent": agent,
                "reward": reward,
                "success": reward is not None and float(reward) >= 1.0,
                "exception": exc.get("exception_type"),
                "exception_message": (exc.get("exception_message") or "")[:200],
                "lightspeed_status": ls.get("status") or run.get("status"),
                "failure_class": ls.get("failure_class") or run.get("failure_class"),
                "n_input_tokens": ar.get("n_input_tokens"),
                "n_cache_tokens": ar.get("n_cache_tokens"),
                "n_output_tokens": ar.get("n_output_tokens"),
                "reasoning_tokens": ls.get("reasoning_tokens"),
                "timings": ls.get("timings") or run.get("timings") or {},
                "started_at": started,
            }
        )
    return trials


def _mean(values: list[float]) -> float | None:
    return round(statistics.fmean(values), 1) if values else None


def summarize_agent(trials: list[dict[str, Any]]) -> dict[str, Any]:
    eligible = [t for t in trials]
    successes = [t for t in eligible if t["success"]]
    classes: dict[str, int] = defaultdict(int)
    for t in eligible:
        if t["success"]:
            classes["success"] += 1
        elif t["exception"]:
            classes[t["failure_class"] or f"exception:{t['exception']}"] += 1
        elif t["lightspeed_status"] and t["lightspeed_status"] != "completed":
            classes[f"run_{t['lightspeed_status']}"] += 1
        else:
            classes["verification"] += 1
    tokens_in = [t["n_input_tokens"] for t in eligible if t["n_input_tokens"] is not None]
    tokens_out = [t["n_output_tokens"] for t in eligible if t["n_output_tokens"] is not None]
    run_sec = [
        t["timings"].get("run_terminal_sec")
        for t in eligible
        if t["timings"].get("run_terminal_sec")
    ]
    reg_sec = [
        t["timings"].get("registration_sec")
        for t in eligible
        if t["timings"].get("registration_sec")
    ]
    return {
        "trials": len(eligible),
        "successes": len(successes),
        "success_rate": round(len(successes) / len(eligible), 4) if eligible else None,
        "outcomes": dict(sorted(classes.items())),
        "mean_input_tokens": _mean(tokens_in),
        "mean_output_tokens": _mean(tokens_out),
        "total_input_tokens": sum(tokens_in) if tokens_in else None,
        "total_output_tokens": sum(tokens_out) if tokens_out else None,
        "mean_run_sec": _mean(run_sec),
        "max_run_sec": max(run_sec) if run_sec else None,
        "mean_registration_sec": _mean(reg_sec),
    }


def paired(trials: list[dict[str, Any]], a: str, b: str) -> dict[str, Any] | None:
    by_task: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for t in trials:
        by_task[t["task"]][t["agent"]].append(1.0 if t["success"] else 0.0)
    tasks = sorted(k for k, v in by_task.items() if a in v and b in v)
    if not tasks:
        return None
    diffs = [statistics.fmean(by_task[k][a]) - statistics.fmean(by_task[k][b]) for k in tasks]
    rng = random.Random(0)
    samples = []
    for _ in range(BOOTSTRAP_ROUNDS):
        picked = [diffs[rng.randrange(len(diffs))] for _ in diffs]
        samples.append(statistics.fmean(picked))
    samples.sort()
    return {
        "agents": [a, b],
        "paired_tasks": len(tasks),
        "mean_task_difference": round(statistics.fmean(diffs), 4),
        "ci95_task_bootstrap": [
            round(samples[int(0.025 * len(samples))], 4),
            round(samples[int(0.975 * len(samples))], 4),
        ],
        "tasks_only_a": sum(1 for d in diffs if d > 0),
        "tasks_only_b": sum(1 for d in diffs if d < 0),
    }


def markdown(job_dir: Path, summary: dict[str, Any], trials: list[dict[str, Any]]) -> str:
    lines = [f"# {job_dir.name}", ""]
    lines.append(
        "| agent | trials | successes | rate | mean in / out tokens | mean run s | outcomes |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for agent, s in summary["agents"].items():
        outcomes = ", ".join(f"{k} {v}" for k, v in s["outcomes"].items())
        lines.append(
            f"| {agent} | {s['trials']} | {s['successes']} | {s['success_rate']} | "
            f"{s['mean_input_tokens']} / {s['mean_output_tokens']} | "
            f"{s['mean_run_sec']} | {outcomes} |"
        )
    if summary.get("paired"):
        p = summary["paired"]
        lines += [
            "",
            f"Paired over {p['paired_tasks']} tasks ({p['agents'][0]} minus {p['agents'][1]}): "
            f"mean difference {p['mean_task_difference']}, 95% task-bootstrap CI "
            f"{p['ci95_task_bootstrap']}; {p['agents'][0]} alone solved {p['tasks_only_a']}, "
            f"{p['agents'][1]} alone {p['tasks_only_b']}.",
        ]
    lines += [
        "",
        "| task | agent | reward | status | class | in / out tokens | run s |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in sorted(trials, key=lambda t: (t["task"], t["agent"])):
        status = t["exception"] or t["lightspeed_status"] or ""
        lines.append(
            f"| {t['task']} | {t['agent']} | {t['reward']} | {status} | "
            f"{t['failure_class'] or ''} | "
            f"{t['n_input_tokens']} / {t['n_output_tokens']} | "
            f"{t['timings'].get('run_terminal_sec', '')} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--job-dir",
        type=Path,
        action="append",
        required=True,
        help="Harbor job output directory; repeat to merge several jobs (e.g. reruns)",
    )
    parser.add_argument(
        "--drop-failure-class",
        action="append",
        default=[],
        help="drop trials of this adapter failure class (e.g. harness_setup) after a rerun",
    )
    parser.add_argument(
        "--supersede",
        action="store_true",
        help="a later --job-dir replaces earlier trials of the same task (targeted reruns)",
    )
    parser.add_argument("--out", type=Path, default=None, help="where to write report files")
    args = parser.parse_args(argv)
    for job_dir in args.job_dir:
        if not job_dir.is_dir():
            print(f"job directory not found: {job_dir}", file=sys.stderr)
            return 2
    trials: list[dict[str, Any]] = []
    for job_dir in args.job_dir:
        batch = load_trials(job_dir)
        if args.supersede:
            replaced = {(t["task"], t["agent"]) for t in batch}
            trials = [t for t in trials if (t["task"], t["agent"]) not in replaced]
        trials.extend(batch)
    dropped = set(args.drop_failure_class)
    if dropped:
        trials = [t for t in trials if t["failure_class"] not in dropped]
    if not trials:
        print("no trials with result.json found", file=sys.stderr)
        return 1
    agents = sorted({t["agent"] for t in trials})
    summary: dict[str, Any] = {
        "job": ", ".join(j.name for j in args.job_dir),
        "agents": {a: summarize_agent([t for t in trials if t["agent"] == a]) for a in agents},
        "tasks": len({t["task"] for t in trials}),
        "dropped_failure_classes": sorted(dropped),
        "supersede": bool(args.supersede),
    }
    if len(agents) == 2:
        summary["paired"] = paired(trials, agents[0], agents[1])
    out = args.out or args.job_dir[0]
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(
        json.dumps({"summary": summary, "trials": trials}, indent=2) + "\n"
    )
    text = markdown(args.job_dir[0], summary, trials)
    (out / "report.md").write_text(text)
    print(text.split("\n\n| task")[0])
    print(f"written: {out / 'report.json'}, {out / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
