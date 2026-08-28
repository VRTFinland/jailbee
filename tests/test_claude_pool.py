"""jailbee's own Claude account pool."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from jailbee import claude_pool
from jailbee.claude_pool import Identity, Slot
from jailbee.global_config import GlobalConfig
from tests.conftest import make_cfg


def _cfg(tmp_path: Path, *, group: str | None = None):
    """A Config whose shared_dir is under tmp_path, never under $HOME.

    `claude_credentials_dir` is an ordinary Config field, so it is passed as
    an override — the same form `tests/test_doctor.py:1678` uses.
    """
    extra = (
        {"claude_credentials_dir": tmp_path / "creds" / group} if group is not None else {}
    )
    return make_cfg(tmp_path / "repo", shared_dir=tmp_path / "shared", **extra)


def test_store_dir_follows_xdg_data_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert claude_pool.store_dir() == (
        tmp_path / "data" / "jailbee" / "claude-credentials" / "_parked"
    )


def test_holder_is_the_config_home_without_a_group(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert claude_pool.config_home(cfg) == tmp_path / "shared" / "claude"
    assert claude_pool.holder_dir(cfg) == tmp_path / "shared" / "claude"
    assert (
        claude_pool.live_credential_path(cfg)
        == tmp_path / "shared" / "claude" / ".credentials.json"
    )


def test_holder_is_the_group_directory_with_a_group(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, group="work")
    # The config home does NOT move: only the credential is shared.
    assert claude_pool.config_home(cfg) == tmp_path / "shared" / "claude"
    assert claude_pool.holder_dir(cfg) == tmp_path / "creds" / "work"


def test_identity_file_prefers_the_legacy_config_json(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    assert claude_pool.identity_file(home) == home / ".claude.json"
    (home / ".config.json").write_text("{}", encoding="utf-8")
    assert claude_pool.identity_file(home) == home / ".config.json"


def _write_identity(home: Path, block: object) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": block, "projects": {"/x": {}}}), encoding="utf-8"
    )


def test_read_identity_returns_email_and_org(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    _write_identity(
        home,
        {
            "emailAddress": "Me@Corp.COM",
            "accountUuid": "aaaa",
            "organizationUuid": "A1B2C3D4-5678",
        },
    )
    assert claude_pool.read_identity(home) == Identity(
        email="Me@Corp.COM", org_uuid="A1B2C3D4-5678"
    )


def test_read_identity_tolerates_a_missing_organization(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    _write_identity(home, {"emailAddress": "me@example.com"})
    assert claude_pool.read_identity(home) == Identity(email="me@example.com")


@pytest.mark.parametrize(
    "block", [None, {}, {"emailAddress": ""}, {"emailAddress": 7}, "nonsense"]
)
def test_read_identity_is_none_when_unusable(tmp_path: Path, block: object) -> None:
    home = tmp_path / "claude"
    _write_identity(home, block)
    assert claude_pool.read_identity(home) is None


def test_read_identity_is_none_on_a_missing_or_torn_file(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    assert claude_pool.read_identity(home) is None
    home.mkdir()
    (home / ".claude.json").write_text('{"oauthAccount": {"emai', encoding="utf-8")
    assert claude_pool.read_identity(home) is None


def test_slug_lowercases_and_truncates_the_org() -> None:
    slug = claude_pool.slug_for(Identity("Me@Corp.COM", "A1B2C3D4-5678-90ab"))
    assert slug == "me@corp.com#a1b2c3d4"


def test_slug_omits_the_org_when_absent() -> None:
    assert claude_pool.slug_for(Identity("me@example.com")) == "me@example.com"


def test_slug_replaces_characters_a_filename_should_not_carry() -> None:
    assert claude_pool.slug_for(Identity("a b/c@x.com")) == "a-b-c@x.com"


def test_unknown_slot_name_is_sortable() -> None:
    name = claude_pool.unknown_slot_name(datetime(2026, 8, 28, 10, 42, 33))
    assert name == "unknown-20260828-104233"


def test_slot_email_and_org_hint() -> None:
    slot = Slot(name="me@corp.com#a1b2c3d4", path=Path("/x"), live=False)
    assert slot.email == "me@corp.com"
    assert slot.org_hint == "a1b2c3d4"

    plain = Slot(name="me@example.com", path=Path("/x"), live=False)
    assert plain.email == "me@example.com"
    assert plain.org_hint is None

    unknown = Slot(name="unknown-20260828-104233", path=Path("/x"), live=False)
    assert unknown.email is None
    assert unknown.org_hint is None

    live = Slot(name=claude_pool.LIVE_UNIDENTIFIED, path=Path("/x"), live=True)
    assert live.email is None


def _cred(**extra: object) -> str:
    return json.dumps({"claudeAiOauth": {"accessToken": "t", "refreshToken": "r"}, **extra})


def test_shared_fields_of_a_bare_login_is_empty() -> None:
    assert claude_pool.shared_fields(_cred()) == {}


def test_shared_fields_picks_only_the_allowlist() -> None:
    raw = _cred(
        mcpOAuth={"srv": 1},
        pluginSecrets={"p": 2},
        trustedDeviceToken="device",
        somethingNew="x",
    )
    assert claude_pool.shared_fields(raw) == {"mcpOAuth": {"srv": 1}, "pluginSecrets": {"p": 2}}


@pytest.mark.parametrize("raw", [None, "", "not json", "[1, 2]", '"a string"'])
def test_shared_fields_is_none_for_a_non_object(raw: str | None) -> None:
    assert claude_pool.shared_fields(raw) is None


def test_shared_fields_logs_an_unrecognized_sibling(caplog) -> None:
    with caplog.at_level("DEBUG", logger="jailbee.claude_pool"):
        claude_pool.shared_fields(_cred(brandNewKey={"a": 1}))
    assert "brandNewKey" in caplog.text


def test_compose_takes_shared_keys_from_the_live_credential() -> None:
    target = _cred(mcpOAuth={"stale": True})
    out = json.loads(claude_pool.compose_credential(target, {"mcpOAuth": {"fresh": True}}))
    assert out["mcpOAuth"] == {"fresh": True}


def test_compose_drops_a_shared_key_the_machine_no_longer_holds() -> None:
    target = _cred(mcpOAuth={"stale": True})
    out = json.loads(claude_pool.compose_credential(target, {}))
    assert "mcpOAuth" not in out


def test_compose_keeps_account_bound_and_unknown_keys_from_the_slot() -> None:
    target = _cred(trustedDeviceToken="slot-device", somethingNew="slot-value")
    out = json.loads(claude_pool.compose_credential(target, {"mcpOAuth": {"a": 1}}))
    assert out["trustedDeviceToken"] == "slot-device"
    assert out["somethingNew"] == "slot-value"


def test_compose_preserves_the_login_exactly() -> None:
    target = _cred()
    out = json.loads(claude_pool.compose_credential(target, {"mcpOAuth": {"a": 1}}))
    assert out["claudeAiOauth"] == {"accessToken": "t", "refreshToken": "r"}


@pytest.mark.parametrize(
    "target,live",
    [
        ("sk-ant-api-not-json", {"mcpOAuth": {}}),   # a managed API key
        ('{"other": 1}', {"mcpOAuth": {}}),           # no claudeAiOauth to compose around
        (_cred(mcpOAuth={"stale": True}), None),      # nothing live to take from
    ],
)
def test_compose_returns_the_target_verbatim_when_it_cannot_merge(
    target: str, live: dict[str, object] | None
) -> None:
    assert claude_pool.compose_credential(target, live) == target


def _park(tmp_path: Path, name: str, monkeypatch) -> Path:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = claude_pool.store_dir()
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.json"
    path.write_text(_cred(), encoding="utf-8")
    return path


def test_parked_slots_reads_the_store(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "b@x.com", monkeypatch)
    _park(tmp_path, "a@x.com#a1b2c3d4", monkeypatch)
    (claude_pool.store_dir() / "notes.txt").write_text("ignored", encoding="utf-8")

    slots = claude_pool.parked_slots()

    assert [s.name for s in slots] == ["a@x.com#a1b2c3d4", "b@x.com"]
    assert all(not s.live for s in slots)


def test_parked_slots_is_empty_when_the_store_does_not_exist(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert claude_pool.parked_slots() == []


def test_live_slot_is_named_after_the_identity(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    live = claude_pool.live_credential_path(cfg)
    live.parent.mkdir(parents=True)
    live.write_text(_cred(), encoding="utf-8")

    slot = claude_pool.live_slot(cfg, Identity("me@corp.com", "a1b2c3d4-99"))

    assert slot is not None
    assert slot.name == "me@corp.com#a1b2c3d4"
    assert slot.live is True
    assert slot.path == live


def test_live_slot_falls_back_to_a_display_name(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    live = claude_pool.live_credential_path(cfg)
    live.parent.mkdir(parents=True)
    live.write_text(_cred(), encoding="utf-8")

    slot = claude_pool.live_slot(cfg, None)

    assert slot is not None
    assert slot.name == claude_pool.LIVE_UNIDENTIFIED


def test_live_slot_is_none_when_nothing_is_logged_in(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    assert claude_pool.live_slot(cfg, None) is None


def _slot(name: str) -> Slot:
    return Slot(name=name, path=Path(f"/store/{name}.json"), live=False)


def test_resolve_ref_matches_a_full_slot_name() -> None:
    slots = [_slot("me@x.com#aaaa1111"), _slot("me@x.com#bbbb2222")]
    assert claude_pool.resolve_ref("me@x.com#bbbb2222", slots).name == "me@x.com#bbbb2222"


def test_resolve_ref_matches_a_bare_email() -> None:
    slots = [_slot("me@x.com#aaaa1111"), _slot("other@x.com")]
    assert claude_pool.resolve_ref("Me@X.com", slots).name == "me@x.com#aaaa1111"


def test_resolve_ref_reports_an_ambiguous_email() -> None:
    slots = [_slot("me@x.com#aaaa1111"), _slot("me@x.com#bbbb2222")]
    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.resolve_ref("me@x.com", slots)
    assert "aaaa1111" in str(excinfo.value)
    assert "bbbb2222" in str(excinfo.value)


def test_resolve_ref_lists_what_it_knows_when_nothing_matches() -> None:
    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.resolve_ref("nobody@x.com", [_slot("me@x.com")])
    assert "me@x.com" in str(excinfo.value)


def test_resolve_ref_reports_a_duplicated_name_as_corruption() -> None:
    """Two files with one name means one grant exists twice — the invariant
    the whole module is built to keep. Say so rather than picking one."""
    dupes = [
        Slot(name="me@x.com", path=Path("/store/me@x.com.json"), live=False),
        Slot(name="me@x.com", path=Path("/holder/.credentials.json"), live=True),
    ]
    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.resolve_ref("me@x.com", dupes)
    assert "/store/me@x.com.json" in str(excinfo.value)
    assert "/holder/.credentials.json" in str(excinfo.value)


def test_slug_sanitizes_the_organization_half_too() -> None:
    """The organization comes from a file containers write, so a separator in
    it must not survive into a slot name that later becomes a path."""
    slug = claude_pool.slug_for(Identity("me@x.com", "../../.."))
    assert "/" not in slug
    assert slug == "me@x.com#..-..-.."


def test_slug_never_starts_with_a_dot() -> None:
    assert claude_pool.slug_for(Identity(".hidden@x.com")) == "hidden@x.com"
    assert claude_pool.slug_for(Identity("..")) == "unnamed"


def test_read_identity_is_none_when_the_document_root_is_not_an_object(
    tmp_path: Path,
) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    (home / ".claude.json").write_text('["not", "an", "object"]', encoding="utf-8")
    assert claude_pool.read_identity(home) is None


def _register(prefix: str, repo_root: Path) -> None:
    """Insert a RegisteredRepo row into the autouse-isolated state DB.

    Rows go into the real (tmp) DB rather than a mocked engine, the same way
    tests/test_doctor.py:1684 does it — that is what makes the query honest.
    """
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    with Session(get_engine()) as session:
        session.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=str(repo_root),
                registered_at=datetime(2026, 8, 28, tzinfo=UTC),
            )
        )
        session.commit()


def _no_git(mocker) -> None:
    """Keep `load_config` off the `git` binary.

    `members()` loads other repos' configs, and `load_config` resolves
    `upstream_remote` and `default_branch` by shelling out. tmp_path is not a
    git repo, so the calls are slow and their results meaningless. Mocked the
    same way `tests/test_config_layered.py:26` does it.
    """
    mocker.patch("jailbee.config.detect_default_branch", return_value="main")
    mocker.patch("jailbee.config.detect_upstream_remote", return_value="origin")


def _write_repo(root: Path, *, shared: Path) -> None:
    """A minimal on-disk jailbee repo whose config load_config can read."""
    (root / ".jailbee").mkdir(parents=True)
    (root / ".jailbee" / "config.yaml").write_text(
        f"container_prefix: {root.name}\nshared_dir: {shared}\n", encoding="utf-8"
    )


def test_members_of_an_ungrouped_repo_is_the_repo_itself(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    found, unreachable = claude_pool.members(cfg, GlobalConfig())
    assert [m.container_prefix for m in found] == [cfg.container_prefix]
    assert found[0].config_home == tmp_path / "shared" / "claude"
    assert unreachable == []


def test_members_of_a_group_include_other_registered_repos(tmp_path: Path, mocker) -> None:
    _no_git(mocker)
    cfg = _cfg(tmp_path, group="work")
    other = tmp_path / "other"
    _write_repo(other, shared=tmp_path / "other-shared")
    _register("other", other)
    _register("outsider", tmp_path / "outsider")
    gcfg = GlobalConfig.model_validate(
        {"claude_credentials": {"group": "work", "repos": {"outsider": None}}}
    )

    found, unreachable = claude_pool.members(cfg, gcfg)

    assert [m.container_prefix for m in found] == sorted([cfg.container_prefix, "other"])
    assert any(m.config_home == tmp_path / "other-shared" / "claude" for m in found)
    assert unreachable == []


def test_members_names_a_repo_whose_config_will_not_load(tmp_path: Path, mocker) -> None:
    _no_git(mocker)
    cfg = _cfg(tmp_path, group="work")
    broken = tmp_path / "broken"
    (broken / ".jailbee").mkdir(parents=True)
    (broken / ".jailbee" / "config.yaml").write_text(": not yaml :", encoding="utf-8")
    _register("broken", broken)
    _register("gone", tmp_path / "gone")  # no config file at all
    gcfg = GlobalConfig.model_validate({"claude_credentials": {"group": "work"}})

    found, unreachable = claude_pool.members(cfg, gcfg)

    assert [m.container_prefix for m in found] == [cfg.container_prefix]
    assert unreachable == ["broken", "gone"]


def test_live_identity_prefers_the_calling_repo(tmp_path: Path) -> None:
    mine = tmp_path / "mine" / "claude"
    theirs = tmp_path / "theirs" / "claude"
    _write_identity(mine, {"emailAddress": "mine@x.com"})
    _write_identity(theirs, {"emailAddress": "theirs@x.com"})
    found = [
        claude_pool.Member("theirs", theirs),
        claude_pool.Member("mine", mine),
    ]

    assert claude_pool.live_identity(found, prefer="mine") == Identity("mine@x.com")


def test_live_identity_falls_back_to_another_member(tmp_path: Path) -> None:
    theirs = tmp_path / "theirs" / "claude"
    _write_identity(theirs, {"emailAddress": "theirs@x.com"})
    found = [
        claude_pool.Member("mine", tmp_path / "mine" / "claude"),
        claude_pool.Member("theirs", theirs),
    ]

    assert claude_pool.live_identity(found, prefer="mine") == Identity("theirs@x.com")


def test_live_identity_is_none_when_no_member_names_an_account(tmp_path: Path) -> None:
    found = [claude_pool.Member("mine", tmp_path / "mine" / "claude")]
    assert claude_pool.live_identity(found, prefer="mine") is None


def test_live_session_prefixes_reports_members_with_session_files(
    tmp_path: Path,
) -> None:
    busy = tmp_path / "busy" / "claude"
    (busy / "sessions").mkdir(parents=True)
    (busy / "sessions" / "4242.json").write_text("{}", encoding="utf-8")
    idle = tmp_path / "idle" / "claude"
    (idle / "sessions").mkdir(parents=True)

    found = [claude_pool.Member("busy", busy), claude_pool.Member("idle", idle)]

    assert claude_pool.live_session_prefixes(found) == ["busy"]


def _holder_with(cfg, raw: str) -> Path:
    path = claude_pool.live_credential_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return path


def test_park_names_the_slot_after_the_live_identity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    change = claude_pool.park(cfg, GlobalConfig())

    assert change.parked_as == "me@corp.com"
    assert (claude_pool.store_dir() / "me@corp.com.json").read_text() == _cred()
    assert not claude_pool.live_credential_path(cfg).exists()


def test_park_falls_back_to_a_timestamped_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())

    change = claude_pool.park(cfg, GlobalConfig(), now=datetime(2026, 8, 28, 10, 42, 33))

    assert change.parked_as == "unknown-20260828-104233"


def test_park_of_an_empty_holder_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)

    change = claude_pool.park(cfg, GlobalConfig())

    assert change.parked_as is None
    assert claude_pool.parked_slots() == []


def test_park_refuses_to_create_a_second_copy(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "me@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.park(cfg, GlobalConfig())

    assert "me@corp.com" in str(excinfo.value)
    # The live credential is untouched: a refused park must not lose a login.
    assert claude_pool.live_credential_path(cfg).exists()


def test_park_clears_the_recorded_account(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    home = claude_pool.config_home(cfg)
    _write_identity(home, {"emailAddress": "me@corp.com"})

    change = claude_pool.park(cfg, GlobalConfig())

    assert change.cleared == [cfg.container_prefix]
    data = json.loads((home / ".claude.json").read_text())
    assert "oauthAccount" not in data
    # Everything else in the file survives.
    assert data["projects"] == {"/x": {}}


def test_switch_parks_the_live_login_and_activates_the_target(
    tmp_path: Path, monkeypatch
) -> None:
    target = _park(tmp_path, "new@corp.com", monkeypatch)
    target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "new"}}), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred(mcpOAuth={"live": True}))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "old@corp.com"})

    change = claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    assert change.activated == "new@corp.com"
    assert change.parked_as == "old@corp.com"
    live = json.loads(claude_pool.live_credential_path(cfg).read_text())
    assert live["claudeAiOauth"] == {"accessToken": "new"}
    # The machine's live MCP tokens came across; the target's stale ones did not.
    assert live["mcpOAuth"] == {"live": True}
    # Exactly one file per login: the target is gone from the store.
    assert not target.exists()
    assert [s.name for s in claude_pool.parked_slots()] == ["old@corp.com"]


def test_switch_into_an_empty_holder_parks_nothing(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    change = claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    assert change.parked_as is None
    assert change.activated == "new@corp.com"
    assert claude_pool.live_credential_path(cfg).exists()


def test_switch_refuses_the_account_already_live(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.switch(cfg, GlobalConfig(), "me@corp.com")

    assert "already" in str(excinfo.value).lower()


def test_switch_writes_the_credential_readable_only_by_its_owner(
    tmp_path: Path, monkeypatch
) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    mode = claude_pool.live_credential_path(cfg).stat().st_mode & 0o777
    assert mode == 0o600


def test_switch_restores_both_files_when_activation_fails(
    tmp_path: Path, monkeypatch, mocker
) -> None:
    target = _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    live = _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "old@corp.com"})
    mocker.patch("jailbee.claude_pool._atomic_write", side_effect=OSError("disk full"))

    with pytest.raises(OSError):
        claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    assert live.read_text() == _cred()
    assert target.exists()
    assert not (claude_pool.store_dir() / "old@corp.com.json").exists()


def test_switch_clears_the_account_in_every_member_including_this_repo(
    tmp_path: Path, monkeypatch, mocker
) -> None:
    _no_git(mocker)
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path, group="work")
    other = tmp_path / "other"
    _write_repo(other, shared=tmp_path / "other-shared")
    _register("other", other)
    _register(cfg.container_prefix, tmp_path / "repo")
    gcfg = GlobalConfig.model_validate({"claude_credentials": {"group": "work"}})
    claude_pool.holder_dir(cfg).mkdir(parents=True)
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "old@corp.com"})
    _write_identity(tmp_path / "other-shared" / "claude", {"emailAddress": "old@corp.com"})

    change = claude_pool.switch(cfg, gcfg, "new@corp.com")

    assert change.cleared == sorted([cfg.container_prefix, "other"])
    for home in (
        claude_pool.config_home(cfg),
        tmp_path / "other-shared" / "claude",
    ):
        assert "oauthAccount" not in json.loads((home / ".claude.json").read_text())


def test_switch_names_a_member_whose_config_file_is_torn(
    tmp_path: Path, monkeypatch
) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)
    home = claude_pool.config_home(cfg)
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text('{"oauthAccount": {"emai', encoding="utf-8")

    change = claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    assert change.not_cleared == [cfg.container_prefix]
    # A file we could not read is never overwritten.
    assert (home / ".claude.json").read_text() == '{"oauthAccount": {"emai'


def test_switch_reports_a_member_that_looks_busy(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)
    sessions = claude_pool.config_home(cfg) / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "77.json").write_text("{}", encoding="utf-8")

    change = claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    assert change.live_sessions == [cfg.container_prefix]


def test_park_use_park_round_trip_preserves_the_login(tmp_path: Path, monkeypatch) -> None:
    """The spec's core promise: a login that goes out and comes back is the
    same login, byte for byte in its `claudeAiOauth` block."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    original = json.dumps(
        {"claudeAiOauth": {"accessToken": "a", "refreshToken": "b", "expiresAt": 123}}
    )
    _holder_with(cfg, original)
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "first@x.com"})

    claude_pool.park(cfg, GlobalConfig())
    _holder_with(cfg, json.dumps({"claudeAiOauth": {"accessToken": "second"}}))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "second@x.com"})
    claude_pool.switch(cfg, GlobalConfig(), "first@x.com")

    live = json.loads(claude_pool.live_credential_path(cfg).read_text())
    assert live["claudeAiOauth"] == json.loads(original)["claudeAiOauth"]
    assert [s.name for s in claude_pool.parked_slots()] == ["second@x.com"]


def test_remove_slot_refuses_the_live_account(tmp_path: Path) -> None:
    live = Slot(name="me@corp.com", path=tmp_path / "c.json", live=True)
    with pytest.raises(claude_pool.PoolError):
        claude_pool.remove_slot(live)


def test_remove_slot_deletes_a_parked_file(tmp_path: Path, monkeypatch) -> None:
    path = _park(tmp_path, "old@corp.com", monkeypatch)
    claude_pool.remove_slot(Slot(name="old@corp.com", path=path, live=False))
    assert not path.exists()


def test_list_slots_puts_the_live_account_first(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "zzz@x.com", monkeypatch)
    _park(tmp_path, "aaa@x.com", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "live@x.com"})

    slots = claude_pool.list_slots(cfg, GlobalConfig())

    assert [s.name for s in slots] == ["live@x.com", "aaa@x.com", "zzz@x.com"]
    assert slots[0].live is True


def test_invalidate_identity_reports_a_config_it_cannot_read(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    (home / ".claude.json").write_text('["not", "an", "object"]', encoding="utf-8")

    assert claude_pool.invalidate_identity(home) is False
    assert (home / ".claude.json").read_text() == '["not", "an", "object"]'


def test_invalidate_identity_is_true_when_there_is_nothing_to_clear(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    assert claude_pool.invalidate_identity(home) is True
    (home / ".claude.json").write_text('{"projects": {}}', encoding="utf-8")
    assert claude_pool.invalidate_identity(home) is True


def test_switch_leaves_no_staging_file_behind(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    claude_pool.switch(cfg, GlobalConfig(), "new@corp.com")

    assert list(claude_pool.store_dir().glob("*.activating")) == []


def test_switch_adopts_a_stage_a_killed_switch_left_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """A kill between the staging rename and the write leaves a file
    `parked_slots()` cannot see. The next switch must find it, not lose it."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = claude_pool.store_dir()
    store.mkdir(parents=True)
    (store / "orphan@x.com.json.activating").write_text(_cred(), encoding="utf-8")
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    change = claude_pool.switch(cfg, GlobalConfig(), "orphan@x.com")

    assert change.activated == "orphan@x.com"
    assert list(store.glob("*.activating")) == []


def test_switch_leaves_a_stage_alone_when_that_login_is_already_stored(
    tmp_path: Path, monkeypatch
) -> None:
    """Adopting would create a second copy of one grant. jailbee never deletes
    a credential on its own, so the stale stage stays for a human."""
    kept = _park(tmp_path, "dup@x.com", monkeypatch)
    store = claude_pool.store_dir()
    (store / "dup@x.com.json.activating").write_text(_cred(), encoding="utf-8")
    _park(tmp_path, "other@x.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    claude_pool.switch(cfg, GlobalConfig(), "other@x.com")

    assert (store / "dup@x.com.json.activating").exists()
    assert kept.exists()


def test_switch_leaves_a_stage_alone_when_its_grant_is_already_live(
    tmp_path: Path, monkeypatch
) -> None:
    """A kill after the credential was written leaves a stage whose grant is
    already live, while every config home still names the previous account.
    Adopting it on the name alone would make one login two files."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = claude_pool.store_dir()
    store.mkdir(parents=True)
    grant = json.dumps({"claudeAiOauth": {"accessToken": "t", "refreshToken": "shared"}})
    (store / "new@x.com.json.activating").write_text(grant, encoding="utf-8")
    _park(tmp_path, "other@x.com", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, grant)                      # the interrupted switch already landed it
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "old@x.com"})

    claude_pool.switch(cfg, GlobalConfig(), "other@x.com")

    assert (store / "new@x.com.json.activating").exists()
    assert not (store / "new@x.com.json").exists()


def test_switch_removes_what_it_wrote_when_the_holder_started_empty(
    tmp_path: Path, monkeypatch, mocker
) -> None:
    """Nothing was parked, so a failure after the write must leave the holder as
    empty as it found it — not holding a copy of the target's grant.

    The mock fails only the staging file's own unlink (by name, not every
    `Path.unlink` call) so the rollback's own cleanup call is left free to
    run and can be asserted on directly, rather than being defeated by the
    same blanket failure that triggers the rollback.
    """
    target = _park(tmp_path, "new@x.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)
    live_path = claude_pool.live_credential_path(cfg)
    real_unlink = Path.unlink

    def _flaky_unlink(self: Path, *args: object, **kwargs: object) -> None:
        if self.name.endswith(".activating"):
            raise OSError("busy")
        real_unlink(self, *args, **kwargs)  # type: ignore[arg-type]

    mocker.patch("pathlib.Path.unlink", _flaky_unlink)

    with pytest.raises(OSError):
        claude_pool.switch(cfg, GlobalConfig(), "new@x.com")

    # The target is back in the store and the holder is empty again — not
    # holding a second copy of the grant `_atomic_write` already wrote.
    assert target.exists()
    assert not live_path.exists()
