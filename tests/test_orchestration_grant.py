"""Tests for the orchestration grant models and label keys."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError
from pytest_mock import MockerFixture

from jailbee.orchestration import labels
from jailbee.orchestration.grant import (
    ChildRights,
    Grant,
    Grantable,
    GrantError,
    RepoCap,
    cap_excess,
    check_rights,
    effective_grant,
    egress_covered,
)


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


def test_ensure_running_is_safe_and_never_calls_real_systemctl(
    mocker: MockerFixture,
) -> None:
    from jailbee.orchestration import service

    mocker.patch("shutil.which", return_value=None)
    run = mocker.patch("subprocess.run")
    mocker.patch("jailbee.orchestration.service.warn")

    assert service.ensure_running() is None
    run.assert_not_called()


@pytest.mark.parametrize(
    ("req", "allowed", "ok"),
    [
        ("pypi.org", ["pypi.org"], True),
        ("PyPI.org", ["pypi.org"], True),
        ("pypi.org", ["PyPI.org"], True),
        ("files.pypi.org", ["*.pypi.org"], True),
        ("files.pypi.org", ["*.PyPI.org"], True),
        ("pypi.org", ["*.pypi.org"], True),
        ("*.x.pypi.org", ["*.pypi.org"], True),
        ("evil-pypi.org", ["*.pypi.org"], False),
        ("pypi.org:8443", ["pypi.org"], False),
        ("files.pypi.org:443", ["*.pypi.org:443"], True),
        ("files.pypi.org", ["*.pypi.org:443"], False),
        ("10.0.0.0/8", ["10.0.0.0/8"], True),
        ("10.1.0.0/16", ["10.0.0.0/8"], False),
        ("example.com", [], False),
    ],
)
def test_egress_covered(req: str, allowed: list[str], ok: bool) -> None:
    assert egress_covered(req, allowed) is ok


def test_cap_excess_names_each_problem() -> None:
    g = Grant(
        max_running_children=5,
        grantable=Grantable(mounts=("data", "secrets"), egress=("pypi.org", "evil.com")),
    )
    cap = RepoCap(max_running_children=3, mounts=("data",), egress=("*.pypi.org",))
    msgs = cap_excess(g, cap)
    assert len(msgs) == 3
    assert any("3" in m and "5" in m for m in msgs)
    assert any("secrets" in m for m in msgs)
    assert any("evil.com" in m for m in msgs)


def test_repo_cap_forbids_loose() -> None:
    g = Grant(grantable=Grantable(loose=True))
    cap = RepoCap(loose=False)
    assert any("loose" in m for m in cap_excess(g, cap))
    eff = effective_grant(g, cap)
    assert eff is not None
    assert eff.grantable.loose is False
    msgs = check_rights(g, cap, ChildRights(loose=True), optional_mounts=set())
    assert msgs and "loose" in msgs[0]
    assert cap_excess(g, RepoCap(loose=None)) == []


def test_cap_not_allowed() -> None:
    assert cap_excess(Grant(), RepoCap(allowed=False)) == [
        "orchestration is not allowed in this repo (.jailbee/config.yaml)"
    ]
    assert effective_grant(Grant(), RepoCap(allowed=False)) is None
    msgs = check_rights(Grant(), RepoCap(allowed=False), ChildRights(), set())
    assert msgs == ["orchestration is not allowed in this repo"]


def test_no_cap_means_no_excess_and_identity() -> None:
    g = Grant(grantable=Grantable(mounts=("data",)))
    assert cap_excess(g, RepoCap()) == []
    assert effective_grant(g, RepoCap()) == g


def test_effective_grant_intersects() -> None:
    g = Grant(
        max_running_children=5,
        grantable=Grantable(mounts=("a", "b"), egress=("x.org", "y.org")),
    )
    e = effective_grant(g, RepoCap(max_running_children=2, mounts=("b",), egress=("x.org",)))
    assert e is not None
    assert (e.max_running_children, e.grantable.mounts, e.grantable.egress) == (
        2,
        ("b",),
        ("x.org",),
    )


def test_check_rights_refuses_every_excess() -> None:
    g = Grant(grantable=Grantable(mounts=("data",), egress=("*.pypi.org",)))
    r = ChildRights(mounts=("data", "secrets"), loose=True, egress=("files.pypi.org", "evil.com"))
    msgs = check_rights(g, RepoCap(), r, optional_mounts={"data"})
    assert len(msgs) == 3
    assert all(any(w in m for m in msgs) for w in ("secrets", "loose", "evil.com"))


def test_check_rights_mount_must_exist_in_repo() -> None:
    g = Grant(grantable=Grantable(mounts=("data",)))
    msgs = check_rights(g, RepoCap(), ChildRights(mounts=("data",)), optional_mounts=set())
    assert msgs and "data" in msgs[0]


def test_check_rights_within_grant_is_clean() -> None:
    g = Grant(grantable=Grantable(mounts=("data",), loose=True, egress=("pypi.org",)))
    r = ChildRights(mounts=("data",), loose=True, egress=("pypi.org",))
    assert check_rights(g, RepoCap(), r, {"data"}) == []
