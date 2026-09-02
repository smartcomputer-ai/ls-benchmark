"""Committed Harbor job configs must parse under the pinned Harbor schema."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from harbor.models.job.config import JobConfig

CONFIGS = sorted((Path(__file__).resolve().parents[1] / "configs").glob("*.yaml"))


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
        assert agent.kwargs.get("profile_id")
    paired = {a.model_name for a in config.agents}
    assert len(paired) == 1, "both arms must use the same model_name"
