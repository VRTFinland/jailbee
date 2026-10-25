"""Host profiles and the repo cap, turned into grant-model values."""

from __future__ import annotations

import pytest

from jailbee.config.models_orchestration import (
    GrantableConfig,
    OrchestrationProfile,
    OrchestrationView,
    RepoOrchestration,
)
from jailbee.orchestration.grant import Grant, Grantable, GrantError, RepoCap
from jailbee.orchestration.profiles import default_grant, profile_grant, repo_cap


@pytest.fixture
def cfg_with_view(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path / "repo")
    cfg._orchestration_view = OrchestrationView(
        profiles={
            "bugfixer": OrchestrationProfile(
                max_running_children=5,
                grantable=GrantableConfig(mounts=["data"]),
            )
        },
        default_profile="bugfixer",
        repo=RepoOrchestration(mounts=["data"]),
    )
    return cfg


def test_profile_grant_converts(cfg_with_view):
    g = profile_grant(cfg_with_view, "bugfixer")
    assert g == Grant(max_running_children=5, grantable=Grantable(mounts=("data",)))


def test_unknown_profile_lists_known(cfg_with_view):
    with pytest.raises(GrantError, match=r"nope.*bugfixer"):
        profile_grant(cfg_with_view, "nope")


def test_default_grant(cfg_with_view, make_cfg, tmp_path):
    assert default_grant(cfg_with_view).max_running_children == 5
    assert default_grant(make_cfg(tmp_path / "other")) is None


def test_repo_cap(cfg_with_view):
    assert repo_cap(cfg_with_view) == RepoCap(
        allowed=True, max_running_children=None, mounts=("data",), egress=None, loose=None
    )
