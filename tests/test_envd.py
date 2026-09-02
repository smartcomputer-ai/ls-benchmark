from __future__ import annotations

from uuid import UUID

import pytest

from lightspeed_harbor.envd import (
    METADATA_MAX_VALUE_BYTES,
    correlation_metadata,
)


def test_correlation_metadata_uses_context_id_and_omits_unknown_fields():
    context_id = UUID("594025f3-7d65-4655-8576-4bee95002eae")
    metadata = correlation_metadata(
        context_id=context_id, session_id="hello-world__bZZeEkw__agent", attempt=2
    )
    assert metadata["source"] == "harbor"
    assert metadata["agent"] == "lightspeed"
    assert metadata["harborContextId"] == str(context_id)
    assert metadata["harborSessionId"] == "hello-world__bZZeEkw__agent"
    assert metadata["harborAttempt"] == "2"
    assert "harborJobId" not in metadata
    assert "harborTrialId" not in metadata
    assert "harborTaskName" not in metadata


def test_correlation_metadata_bounds_values():
    metadata = correlation_metadata(context_id=None, session_id=None, task_name="x" * 1000)
    assert len(metadata["harborTaskName"].encode()) == METADATA_MAX_VALUE_BYTES


def test_correlation_metadata_never_contains_secrets():
    metadata = correlation_metadata(context_id=None, session_id="s")
    assert not any(key.lower().endswith("key") for key in metadata)


def test_reserved_prefix_is_rejected():
    with pytest.raises(ValueError, match="reserved"):
        correlation_metadata(context_id=None, session_id="s", extra={"lightspeed.internal": "x"})


def test_entry_limit_is_enforced():
    extra = {f"k{i}": "v" for i in range(40)}
    with pytest.raises(ValueError, match="entries"):
        correlation_metadata(context_id=None, session_id="s", extra=extra)
