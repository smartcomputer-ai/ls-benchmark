"""Committed Harbor job configs must parse under the pinned Harbor schema."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from harbor.models.job.config import JobConfig

ROOT = Path(__file__).resolve().parents[1]
CONFIGS = sorted((ROOT / "configs").glob("*.yaml"))


@pytest.mark.parametrize("path", CONFIGS, ids=[p.name for p in CONFIGS])
def test_config_matches_pinned_job_schema(path: Path):
    config = JobConfig.model_validate(yaml.safe_load(path.read_text()))
    lightspeed = [
        a for a in config.agents if a.import_path == "lightspeed_harbor.agent:LightspeedAgent"
    ]
    assert lightspeed, "every committed config must include the Lightspeed arm"
    for agent in lightspeed:
        assert agent.model_name, "the Lightspeed arm must name an explicit model"
        assert agent.kwargs.get("lightspeed_provider_id")
        assert agent.kwargs.get("profile_id") == "inline"
    # Model-bearing arms must agree; model-free helpers (oracle, nop) are allowed.
    paired = {a.model_name for a in config.agents if a.model_name}
    assert len(paired) == 1, "every model-bearing arm must use the same model_name"
    for task in config.tasks:
        if task.path is not None:
            assert (ROOT / task.path / "task.toml").is_file(), f"missing local task {task.path}"
