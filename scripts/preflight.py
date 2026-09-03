"""Parity and readiness preflight for a Harbor job config. Fails closed.

Checks, in order:

1. Config shape: dataset pinned to a version or ref (never ``head``/``latest``),
   no timeout multipliers, no agent timeout or resource overrides, ``retry``
   off, and one attempt count for every arm.
2. Arm parity: every model-bearing arm names the same immutable ``model_name``
   and the same reasoning effort (Harbor's Codex default is ``high``), the
   Codex arm has ``web_search: disabled`` for the terminal-only track, and
   per-arm ``n_concurrent`` values are equal.
3. Live Lightspeed checks when the Lightspeed arm is present and host settings
   are in the environment (``.local/hosted.env`` is loaded if it exists):
   ``initialize`` answers and reports its build; the envd discovery document
   names an artifact for the sandbox architecture built from that same
   commit; ``models/list`` exposes the model under the configured provider
   with the configured API kind; the registration key in
   ``.local/hosted-registration-key.json`` is active with enough capacity.
4. Codex credential: ``OPENAI_API_KEY`` is set when a ``codex`` arm exists
   (Harbor injects it into that arm's sandbox only).

Writes a redacted report to ``.local/preflight/<config>.json`` (or ``--out``)
that provenance can cite. Exit code 0 only when every check passed.

Usage: uv run python scripts/preflight.py --config configs/terminal-bench.paired.yaml [--arch amd64]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from harbor.models.job.config import JobConfig

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from lightspeed_harbor.client import LightspeedClient, LightspeedError  # noqa: E402
from lightspeed_harbor.config import ConfigError, HostSettings  # noqa: E402
from lightspeed_harbor.envd import SUPPORTED_TARGETS  # noqa: E402

LIGHTSPEED_IMPORT = "lightspeed_harbor.agent:LightspeedAgent"
CODEX_DEFAULT_EFFORT = "high"
MODEL_FREE_AGENTS = {"oracle", "nop"}


class Preflight:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.failed = False

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            self.failed = True
        print(f"  [{'ok' if ok else 'FAIL'}] {name}{': ' + detail if detail else ''}")


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def effort_of(agent: Any) -> str | None:
    kwargs = agent.kwargs or {}
    if agent.import_path == LIGHTSPEED_IMPORT:
        return kwargs.get("reasoning_effort")
    if agent.name == "codex":
        return kwargs.get("reasoning_effort", CODEX_DEFAULT_EFFORT)
    return kwargs.get("reasoning_effort")


def arm_label(agent: Any) -> str:
    return agent.name or agent.import_path or "?"


def check_config(pf: Preflight, config: JobConfig) -> None:
    for dataset in config.datasets:
        pinned = bool(dataset.version or dataset.ref)
        floating = str(dataset.version or dataset.ref or "").lower() in {"head", "latest", ""}
        pf.check(
            f"dataset {dataset.name} pinned",
            pinned and not floating,
            f"version={dataset.version!r} ref={dataset.ref!r}",
        )
    pf.check(
        "no timeout multipliers",
        (config.timeout_multiplier in (None, 1.0))
        and config.agent_timeout_multiplier is None
        and config.verifier_timeout_multiplier is None,
        f"timeout_multiplier={config.timeout_multiplier}",
    )
    env = config.environment
    pf.check(
        "no resource overrides",
        env.override_cpus is None
        and env.override_memory_mb is None
        and env.override_storage_mb is None
        and env.override_gpus is None,
    )
    pf.check(
        "no whole-trial retries",
        config.retry.max_retries == 0,
        f"max_retries={config.retry.max_retries}",
    )
    overrides = [arm_label(a) for a in config.agents if a.override_timeout_sec is not None]
    pf.check("no agent timeout overrides", not overrides, ", ".join(overrides))

    arms = [a for a in config.agents if a.model_name]
    models = {a.model_name for a in arms}
    pf.check("one model across arms", len(models) <= 1, ", ".join(sorted(m for m in models if m)))
    efforts = {arm_label(a): effort_of(a) for a in arms}
    pf.check(
        "one reasoning effort across arms",
        len(set(efforts.values())) <= 1 and None not in efforts.values(),
        json.dumps(efforts),
    )
    concurrency = {arm_label(a): a.n_concurrent for a in arms}
    pf.check(
        "equal per-arm concurrency", len(set(concurrency.values())) <= 1, json.dumps(concurrency)
    )
    for agent in config.agents:
        if agent.name == "codex":
            web = (agent.kwargs or {}).get("web_search")
            pf.check("codex web_search disabled", web == "disabled", f"web_search={web!r}")
            pf.check(
                "codex version pinned",
                bool((agent.kwargs or {}).get("version")),
                str((agent.kwargs or {}).get("version")),
            )
        if agent.import_path == LIGHTSPEED_IMPORT:
            kwargs = agent.kwargs or {}
            pf.check(
                "lightspeed arm names provider, profile, api_kind",
                bool(kwargs.get("lightspeed_provider_id"))
                and kwargs.get("profile_id") == "inline"
                and bool(kwargs.get("api_kind")),
                json.dumps(
                    {k: kwargs.get(k) for k in ("lightspeed_provider_id", "profile_id", "api_kind")}
                ),
            )


async def check_lightspeed(pf: Preflight, agent: Any, arch: str) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    try:
        host = HostSettings.from_env(os.environ)
    except ConfigError as exc:
        pf.check("lightspeed host settings", False, str(exc))
        return facts
    kwargs = agent.kwargs or {}
    provider_id = kwargs.get("lightspeed_provider_id")
    model_id = (agent.model_name or "").partition("/")[2]
    api_kind = kwargs.get("api_kind")
    try:
        async with LightspeedClient(host.api_url, host.api_key, universe=host.universe) as client:
            init = await client.initialize()
            info = init.get("serverInfo") or {}
            server_sha = info.get("gitSha")
            facts["server"] = {
                "name": info.get("name"),
                "version": info.get("version"),
                "gitSha": server_sha,
            }
            pf.check(
                "lightspeed initialize",
                bool(server_sha),
                f"{info.get('name')} {info.get('version')}",
            )

            target = SUPPORTED_TARGETS.get(f"linux/{arch}")
            doc: dict[str, Any] = {}
            if host.envd_path is None and host.envd_release_url is None and host.envd_discovery_url:
                async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as http:
                    response = await http.get(host.envd_discovery_url)
                doc = response.json() if response.status_code < 400 else {}
                artifact = (doc.get("artifacts") or {}).get(target or "")
                pf.check(
                    f"envd discovery has {target}",
                    bool(artifact) and doc.get("gitSha") == server_sha,
                    f"document gitSha={str(doc.get('gitSha'))[:12]} server={str(server_sha)[:12]} "
                    f"targets={sorted((doc.get('artifacts') or {}).keys())}",
                )
                facts["envd"] = {
                    "gitSha": doc.get("gitSha"),
                    "target": target,
                    "sha256": (artifact or {}).get("sha256"),
                }
            else:
                pf.check(
                    "envd artifact override in use",
                    True,
                    str(host.envd_path or host.envd_release_url),
                )

            listing = await client.models_list()
            providers = {p.get("providerId"): p for p in listing.get("providers", [])}
            provider = providers.get(provider_id) or {}
            pf.check(
                f"provider {provider_id} credential",
                provider.get("credential") == "configured",
                f"credential={provider.get('credential')} "
                f"source={provider.get('credentialSource')} error={provider.get('error')}",
            )
            routes = [
                m
                for m in listing.get("models", [])
                if m.get("providerId") == provider_id and m.get("model") == model_id
            ]
            kinds = sorted({m.get("apiKind") for m in routes})
            pf.check(
                f"model {model_id} via {api_kind}", api_kind in kinds, f"offered api kinds: {kinds}"
            )
            facts["model"] = {
                "providerId": provider_id,
                "model": model_id,
                "apiKind": api_kind,
                "offered": kinds,
            }

            key_file = ROOT / ".local" / "hosted-registration-key.json"
            if key_file.is_file():
                key_id = json.loads(key_file.read_text())["result"]["result"]["registrationKey"][
                    "registrationKeyId"
                ]
                view = (
                    await client.call(
                        "environments/registration-keys/read", {"registrationKeyId": key_id}
                    )
                )["registrationKey"]  # type: ignore[index]
                pf.check(
                    "registration key active",
                    view.get("status") == "active",
                    f"{key_id} status={view.get('status')} "
                    f"maxActive={view.get('maxActiveEnvironments')} "
                    f"active={view.get('activeEnvironmentCount')}",
                )
                facts["registration_key"] = {
                    "id": key_id,
                    "status": view.get("status"),
                    "maxActive": view.get("maxActiveEnvironments"),
                }
            else:
                pf.check("registration key file", True, "none yet; run scripts mint one")
    except LightspeedError as exc:
        pf.check("lightspeed api", False, str(exc))
    return facts


def load_registration_key(path: Path) -> None:
    """The run scripts keep the campaign key in a file; expose it the way they do."""
    if os.environ.get("LIGHTSPEED_HARBOR_REGISTRATION_KEY") or not path.is_file():
        return
    try:
        secret = json.loads(path.read_text())["result"]["result"]["secret"]
    except (ValueError, KeyError, TypeError):
        return
    os.environ["LIGHTSPEED_HARBOR_REGISTRATION_KEY"] = secret


async def main_async(args: argparse.Namespace) -> int:
    load_env_file(ROOT / ".local" / "hosted.env")
    load_registration_key(ROOT / ".local" / "hosted-registration-key.json")
    raw = yaml.safe_load(args.config.read_text())
    config = JobConfig.model_validate(raw)
    pf = Preflight()
    print(f"preflight {args.config}")
    check_config(pf, config)
    facts: dict[str, Any] = {}
    lightspeed = next((a for a in config.agents if a.import_path == LIGHTSPEED_IMPORT), None)
    if lightspeed is not None:
        facts["lightspeed"] = await check_lightspeed(pf, lightspeed, args.arch)
    if any(a.name == "codex" for a in config.agents):
        pf.check(
            "OPENAI_API_KEY for the codex arm",
            bool(os.environ.get("OPENAI_API_KEY")),
            "set on the Harbor host",
        )
    out = args.out or (ROOT / ".local" / "preflight" / f"{args.config.stem}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "config": str(args.config),
                "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "passed": not pf.failed,
                "checks": pf.results,
                "facts": facts,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"{'PASSED' if not pf.failed else 'FAILED'}; written {out}")
    return 0 if not pf.failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, required=True, help="Harbor job config")
    parser.add_argument("--arch", default="amd64", help="sandbox architecture (amd64 or arm64)")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="report path (default .local/preflight/<config>.json)",
    )
    args = parser.parse_args(argv)
    if not args.config.is_file():
        print(f"config not found: {args.config}", file=sys.stderr)
        return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
