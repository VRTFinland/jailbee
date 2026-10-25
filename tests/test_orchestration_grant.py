"""Tests for the orchestration grant models and label keys."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jailbee.orchestration import labels
from jailbee.orchestration.grant import ChildRights, Grant, Grantable, GrantError


def test_label_keys_match_spec() -> None:
    assert labels.GRANT == "user.jailbee.orchestration.grant"
    assert labels.RIGHTS == "user.jailbee.orchestration.rights"
    assert labels.SPAWNED_BY == "user.jailbee.spawned_by"
    assert labels.STATE == "user.jailbee.orchestration.state"
    assert labels.DONE_SHA == "user.jailbee.orchestration.done_sha"
    assert labels.LAST_IMPORT == "user.jailbee.orchestration.last_import"


def test_default_grant_label_is_the_spec_shape() -> None:
    assert json.loads(Grant().to_label()) == {
        "version": 1,
        "max_running_children": 2,
        "operations": ["import", "message", "restart", "spawn", "stop"],
        "grantable": {"mounts": [], "loose": False, "egress": []},
    }


def test_operations_are_sorted_and_deduped() -> None:
    g = Grant(operations=("stop", "spawn", "message", "spawn", "destroy"))
    assert g.operations == ("destroy", "message", "spawn", "stop")


@pytest.mark.parametrize("missing", ["spawn", "message"])
def test_implied_operations_cannot_be_left_out(missing: str) -> None:
    ops = tuple(o for o in ("spawn", "message", "stop") if o != missing)
    with pytest.raises(ValidationError, match=missing):
        Grant(operations=ops)  # type: ignore[arg-type]  # str vs Literal in a parametrized test


def test_limit_bounds() -> None:
    with pytest.raises(ValidationError):
        Grant(max_running_children=0)
    with pytest.raises(ValidationError):
        Grant(max_running_children=65)


def test_invalid_egress_entry_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Grantable(egress=("not a host",))


def test_label_round_trip() -> None:
    g = Grant(
        max_running_children=4,
        grantable=Grantable(mounts=("docs", "data"), loose=True, egress=("pypi.org",)),
    )
    assert Grant.from_label(g.to_label()) == g
    assert g.grantable.mounts == ("data", "docs")


@pytest.mark.parametrize("raw", ["{", "[]", '{"version": 1, "bogus": 1}', '{"version": 2}'])
def test_bad_label_is_a_grant_error(raw: str) -> None:
    with pytest.raises(GrantError, match="invalid grant label"):
        Grant.from_label(raw)


def test_child_rights_round_trip() -> None:
    r = ChildRights(mounts=("data",), egress=("pypi.org",))
    assert json.loads(r.to_label()) == {
        "version": 1,
        "mounts": ["data"],
        "loose": False,
        "egress": ["pypi.org"],
    }
    assert ChildRights.from_label(r.to_label()) == r


def test_ensure_running_is_a_safe_noop() -> None:
    from jailbee.orchestration import service

    assert service.ensure_running() is None
