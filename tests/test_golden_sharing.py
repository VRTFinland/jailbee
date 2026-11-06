from datetime import UTC, datetime
from unittest.mock import MagicMock

from jailbee.config import SCRATCH_BASE_ALIAS
from jailbee.db.models import RegisteredRepo
from jailbee.golden_sharing import shared_golden_reason


def _row(prefix: str) -> RegisteredRepo:
    return RegisteredRepo(
        container_prefix=prefix,
        repo_root=f"/{prefix}",
        registered_at=datetime.now(UTC),
    )


def test_shared_golden_reason_for_scratch_alias():
    assert shared_golden_reason("foo", SCRATCH_BASE_ALIAS, []) == "shared scratch base"


def test_shared_golden_reason_for_another_repo_with_same_alias():
    other = _row("other")
    cfg = MagicMock()
    cfg.container_prefix = "other"
    cfg.golden.alias = "shared-base"

    reason = shared_golden_reason("foo", "shared-base", [other], load=lambda row: cfg)

    assert reason == "shared with other"


def test_shared_golden_reason_for_unloadable_other_repo():
    other = _row("other")

    reason = shared_golden_reason("foo", "foo-base", [other], load=lambda row: None)

    assert reason == "config unavailable for other"


def test_shared_golden_reason_for_sole_owner():
    assert shared_golden_reason("foo", "foo-base", []) is None
