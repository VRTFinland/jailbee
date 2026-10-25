import pytest
from pydantic import ValidationError

from jailbee.config.models_orchestration import (
    GrantableConfig,
    OrchestrationHostConfig,
    OrchestrationProfile,
    OrchestrationView,
    RepoOrchestration,
)
from jailbee.global_config import GlobalConfig


def test_profile_defaults_match_grant_defaults():
    p = OrchestrationProfile()
    assert (p.max_running_children, p.operations, p.grantable.mounts) == (
        2,
        ["import", "message", "restart", "spawn", "stop"],
        [],
    )


def test_unknown_operation_rejected():
    with pytest.raises(ValidationError, match="fly"):
        OrchestrationProfile(operations=["spawn", "message", "fly"])


def test_operations_must_include_spawn_and_message():
    with pytest.raises(ValidationError, match="message"):
        OrchestrationProfile(operations=["spawn"])


def test_default_profile_must_exist():
    with pytest.raises(ValidationError, match="nope"):
        OrchestrationHostConfig(profiles={}, default_profile="nope")


def test_partial_context_skips_default_profile_check():
    cfg = OrchestrationHostConfig.model_validate(
        {"default_profile": "nope"}, context={"partial": True}
    )
    assert cfg.default_profile == "nope"


def test_bad_profile_name():
    with pytest.raises(ValidationError):
        OrchestrationHostConfig(profiles={"Bad Name": {}})


def test_grantable_egress_validated():
    with pytest.raises(ValidationError, match="egress"):
        GrantableConfig(egress=["not a valid entry!!"])


def test_repo_egress_validated_and_extra_forbidden():
    with pytest.raises(ValidationError):
        RepoOrchestration(egress=["not a valid entry!!"])
    with pytest.raises(ValidationError):
        RepoOrchestration.model_validate({"bogus": 1})


def test_view_default():
    v = OrchestrationView()
    assert (dict(v.profiles), v.default_profile, v.repo) == ({}, None, RepoOrchestration())


def test_global_config_accepts_orchestration_block():
    g = GlobalConfig.model_validate(
        {
            "orchestration": {
                "profiles": {"bugfixer": {"max_running_children": 5}},
                "default_profile": "bugfixer",
            }
        }
    )
    assert g.orchestration.profiles["bugfixer"].max_running_children == 5


def test_orchestration_is_a_host_level_key():
    from jailbee.config.common import _HOST_LEVEL_KEYS

    assert "orchestration" in _HOST_LEVEL_KEYS


def test_local_layer_peels_rather_than_refuses_orchestration():
    from jailbee.config.local_layer import split_local_raw

    overlay, _ = split_local_raw({"orchestration": {"default_profile": None}}, "/tmp/x.yaml")
    assert "orchestration" not in overlay
