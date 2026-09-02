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
    extra = {"claude_credentials_dir": tmp_path / "creds" / group} if group is not None else {}
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


@pytest.mark.parametrize("block", [None, {}, {"emailAddress": ""}, {"emailAddress": 7}, "nonsense"])
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


def test_read_identity_is_none_on_a_torn_multibyte_sequence(tmp_path: Path) -> None:
    """A write torn mid-character makes `read_text` raise `UnicodeDecodeError`,
    which is a `ValueError` — not covered by the `OSError`/`JSONDecodeError`
    pair. An unreadable config home is a fact to report, never a traceback."""
    home = tmp_path / "claude"
    home.mkdir()
    (home / ".claude.json").write_bytes(b'{"oauthAccount": {"emailAddress": "caf\xc3')

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
        ("sk-ant-api-not-json", {"mcpOAuth": {}}),  # a managed API key
        ('{"other": 1}', {"mcpOAuth": {}}),  # no claudeAiOauth to compose around
        (_cred(mcpOAuth={"stale": True}), None),  # nothing live to take from
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


def test_parked_slots_is_empty_when_the_store_does_not_exist(tmp_path: Path, monkeypatch) -> None:
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
    """Names are unique by construction, so two files under one name means
    something else wrote to the store. The message names both files and must
    **not** tell anyone to delete one: they may be two different logins, and
    following that advice would destroy a real one with no way back."""
    dupes = [
        Slot(name="me@x.com", path=Path("/store/me@x.com.json"), live=False),
        Slot(name="me@x.com", path=Path("/holder/.credentials.json"), live=True),
    ]
    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.resolve_ref("me@x.com", dupes)
    message = str(excinfo.value)
    assert "/store/me@x.com.json" in message
    assert "/holder/.credentials.json" in message
    assert "different logins" in message
    assert "remove or rename" not in message


def test_resolve_ref_quotes_the_stripped_reference_in_every_message() -> None:
    """All three failures name the same thing the lookup used. The duplicate
    branch already used the stripped form; the other two quoted the raw
    argument, so the same typo was echoed two different ways."""
    padded = "  Me@X.com  "

    with pytest.raises(claude_pool.PoolError) as unknown:
        claude_pool.resolve_ref(padded, [_slot("other@x.com")])
    assert "`Me@X.com`" in str(unknown.value)

    ambiguous = [_slot("me@x.com#aaaa1111"), _slot("me@x.com#bbbb2222")]
    with pytest.raises(claude_pool.PoolError) as several:
        claude_pool.resolve_ref(padded, ambiguous)
    assert "`Me@X.com`" in str(several.value)


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

    assert claude_pool.live_identity(
        found, prefer="mine", authoritative={"mine", "theirs"}
    ) == Identity("mine@x.com")


def test_live_identity_falls_back_to_another_member(tmp_path: Path) -> None:
    theirs = tmp_path / "theirs" / "claude"
    _write_identity(theirs, {"emailAddress": "theirs@x.com"})
    found = [
        claude_pool.Member("mine", tmp_path / "mine" / "claude"),
        claude_pool.Member("theirs", theirs),
    ]

    assert claude_pool.live_identity(
        found, prefer="mine", authoritative={"mine", "theirs"}
    ) == Identity("theirs@x.com")


def test_live_identity_is_none_when_no_member_names_an_account(tmp_path: Path) -> None:
    found = [claude_pool.Member("mine", tmp_path / "mine" / "claude")]
    assert claude_pool.live_identity(found, prefer="mine", authoritative={"mine"}) is None


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

    change = claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert change.parked_as == "me@corp.com"
    stored = claude_pool.store_dir() / "me@corp.com.json"
    assert _login_in(stored) == json.loads(_cred())["claudeAiOauth"]
    # The account record travels with the grant, so a later switch can restore it.
    assert json.loads(stored.read_text())[claude_pool.ACCOUNT_RECORD_KEY] == {
        "emailAddress": "me@corp.com"
    }
    assert not claude_pool.live_credential_path(cfg).exists()


def test_park_falls_back_to_a_timestamped_name(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())

    change = claude_pool.park(
        cfg,
        GlobalConfig(),
        authoritative={cfg.container_prefix},
        now=datetime(2026, 8, 28, 10, 42, 33),
    )

    assert change.parked_as == "unknown-20260828-104233"


def test_park_of_an_empty_holder_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)

    change = claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert change.parked_as is None
    assert claude_pool.parked_slots() == []


def test_park_refuses_to_store_the_same_login_twice(tmp_path: Path, monkeypatch) -> None:
    """The one case where a taken name really is a duplicate: the *same* grant.
    A second file would give one refresh-token lineage two refreshers."""
    _park(tmp_path, "me@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    message = str(excinfo.value)
    # The message says what is true — this login is already stored — and names
    # the escape, rather than telling the user to delete an unrelated file.
    assert "already stored as `me@corp.com`" in message
    assert "jailbee claude rm me@corp.com" in message
    # The live credential is untouched: a refused park must not lose a login.
    assert claude_pool.live_credential_path(cfg).exists()


def test_park_clears_the_recorded_account(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    home = claude_pool.config_home(cfg)
    _write_identity(home, {"emailAddress": "me@corp.com"})

    change = claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert change.updated == [cfg.container_prefix]
    data = json.loads((home / ".claude.json").read_text())
    assert "oauthAccount" not in data
    # Everything else in the file survives.
    assert data["projects"] == {"/x": {}}


def test_switch_parks_the_live_login_and_activates_the_target(tmp_path: Path, monkeypatch) -> None:
    target = _park(tmp_path, "new@corp.com", monkeypatch)
    target.write_text(json.dumps({"claudeAiOauth": {"accessToken": "new"}}), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred(mcpOAuth={"live": True}))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "old@corp.com"})

    change = claude_pool.switch(
        cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix}
    )

    assert change.activated == "new@corp.com"
    assert change.parked_as == "old@corp.com"
    live = json.loads(claude_pool.live_credential_path(cfg).read_text())
    assert live["claudeAiOauth"] == {"accessToken": "new"}
    # The machine's live MCP tokens came across; the target's stale ones did not.
    assert live["mcpOAuth"] == {"live": True}
    # Exactly one file per login: the target is gone from the store.
    assert not target.exists()
    assert [s.name for s in claude_pool.parked_slots()] == ["old@corp.com"]


ACCOUNT_BLOCK = {
    "emailAddress": "first@corp.com",
    "accountUuid": "1111-2222",
    "organizationUuid": "ccccdddd-1111",
    "organizationName": "First Org",
}
"""One `oauthAccount` as Claude Code writes it, for the record-carrying tests."""

SECOND_ACCOUNT_BLOCK = {
    "emailAddress": "second@corp.com",
    "organizationUuid": "aaaabbbb-9999",
}


def _park_carrying(tmp_path: Path, name: str, token: str, monkeypatch) -> Path:
    """A stored slot shaped like one `park` wrote: a grant plus its record."""
    path = _park(tmp_path, name, monkeypatch)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"refreshToken": token},
                claude_pool.ACCOUNT_RECORD_KEY: ACCOUNT_BLOCK,
            }
        ),
        encoding="utf-8",
    )
    return path


def _login_as(cfg, block: dict, token: str) -> None:
    """What a `/login` in a container leaves behind: a credential in the holder
    and an `oauthAccount` in the config home."""
    _holder_with(cfg, _grant(token))
    _write_identity(claude_pool.config_home(cfg), block)


def test_two_switches_in_a_row_keep_the_outgoing_account_name(tmp_path: Path, monkeypatch) -> None:
    """The reported bug. `switch` reads the live login's identity from the
    members' `oauthAccount`, and its own last step deletes it — so a second
    `switch` before any container had run Claude again found no identity and
    parked a perfectly identified login as `unknown-<timestamp>`, losing the
    only record of which account the file holds.

    Driven through `park` rather than by writing the store by hand: the fix
    rests on what `park` puts *inside* the parked file, so a hand-built store
    would let this test pass while the real flow stays broken.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)

    _login_as(cfg, SECOND_ACCOUNT_BLOCK, "second")
    assert (
        claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix}).parked_as
        == "second@corp.com#aaaabbbb"
    )
    _login_as(cfg, ACCOUNT_BLOCK, "first")

    first = claude_pool.switch(
        cfg, GlobalConfig(), "second@corp.com#aaaabbbb", authoritative={cfg.container_prefix}
    )
    assert first.parked_as == "first@corp.com#ccccdddd"

    # Nothing ran Claude in between, so nothing repopulated `oauthAccount`.
    back = claude_pool.switch(
        cfg, GlobalConfig(), "first@corp.com#ccccdddd", authoritative={cfg.container_prefix}
    )

    assert back.parked_as == "second@corp.com#aaaabbbb"
    assert [s.name for s in claude_pool.parked_slots()] == ["second@corp.com#aaaabbbb"]
    # And the holder can still say which account is live, which is what `ls`
    # rendered as `(unknown)` for every switch until a container ran Claude.
    live = [
        s
        for s in claude_pool.list_slots(cfg, GlobalConfig(), authoritative={cfg.container_prefix})
        if s.live
    ]
    assert [s.name for s in live] == ["first@corp.com#ccccdddd"]


def test_park_keeps_claude_codes_own_account_record_with_the_grant(
    tmp_path: Path, monkeypatch
) -> None:
    """Verbatim, not reconstructed: the slot name truncates the organization to
    eight characters, so the name can never rebuild the record. The parked file
    is jailbee's own — Claude Code never reads the store — so the record rides
    along inside the file whose grant it describes, which is what keeps it from
    becoming a ledger that can disagree with the directory.
    """
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("first"))
    _write_identity(claude_pool.config_home(cfg), ACCOUNT_BLOCK)

    change = claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert change.parked_as == "first@corp.com#ccccdddd"
    stored = json.loads((claude_pool.store_dir() / f"{change.parked_as}.json").read_text())
    assert stored[claude_pool.ACCOUNT_RECORD_KEY] == ACCOUNT_BLOCK


def test_switch_restores_the_activated_accounts_record(tmp_path: Path, monkeypatch) -> None:
    """Restoring beats clearing: `ls` names the live account immediately instead
    of `(unknown)` until some container happens to run Claude."""
    _park_carrying(tmp_path, "first@corp.com#ccccdddd", "first", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("other"))
    home = claude_pool.config_home(cfg)
    _write_identity(home, {"emailAddress": "other@corp.com"})

    change = claude_pool.switch(
        cfg, GlobalConfig(), "first@corp.com#ccccdddd", authoritative={cfg.container_prefix}
    )

    assert change.updated == [cfg.container_prefix]
    data = json.loads((home / ".claude.json").read_text())
    assert data["oauthAccount"] == ACCOUNT_BLOCK
    # Everything else in the config home survives, as it does for a clear.
    assert data["projects"] == {"/x": {}}


def test_the_activated_credential_never_carries_jailbees_record(
    tmp_path: Path, monkeypatch
) -> None:
    """The live file is Claude Code's, and it must be exactly the shape Claude
    Code wrote — jailbee's own key stays behind in the store."""
    _park_carrying(tmp_path, "first@corp.com#ccccdddd", "first", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("other", mcpOAuth={"srv": 1}))

    claude_pool.switch(
        cfg, GlobalConfig(), "first@corp.com#ccccdddd", authoritative={cfg.container_prefix}
    )

    live = json.loads(claude_pool.live_credential_path(cfg).read_text())
    assert claude_pool.ACCOUNT_RECORD_KEY not in live
    assert live["claudeAiOauth"] == {"refreshToken": "first"}
    assert live["mcpOAuth"] == {"srv": 1}


def test_the_activated_credential_drops_the_record_into_an_empty_holder(
    tmp_path: Path, monkeypatch
) -> None:
    """The empty-holder path returns the target unchanged when there are no
    shared fields to compose, and must still strip jailbee's key."""
    _park_carrying(tmp_path, "first@corp.com#ccccdddd", "first", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    claude_pool.switch(
        cfg, GlobalConfig(), "first@corp.com#ccccdddd", authoritative={cfg.container_prefix}
    )

    live = json.loads(claude_pool.live_credential_path(cfg).read_text())
    assert claude_pool.ACCOUNT_RECORD_KEY not in live


def test_switch_ignores_a_record_that_contradicts_the_slot_name(
    tmp_path: Path, monkeypatch
) -> None:
    """Storing the record beside the grant names the account twice for one file,
    and a hand-renamed slot changes only one of the two. Restoring the record
    then would write one account into the members' config while the user
    believed they activated the other. The filename wins."""
    renamed = _park(tmp_path, "renamed@corp.com", monkeypatch)
    renamed.write_text(
        json.dumps(
            {
                "claudeAiOauth": {"refreshToken": "first"},
                # The record still names the account the file was parked under.
                claude_pool.ACCOUNT_RECORD_KEY: ACCOUNT_BLOCK,
            }
        ),
        encoding="utf-8",
    )
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("other"))
    home = claude_pool.config_home(cfg)
    _write_identity(home, {"emailAddress": "other@corp.com"})

    claude_pool.switch(
        cfg, GlobalConfig(), "renamed@corp.com", authoritative={cfg.container_prefix}
    )

    # Invalidated, not repointed at the record's account: the pre-record
    # behaviour, which is correct and merely slower to display.
    data = json.loads((home / ".claude.json").read_text())
    assert "oauthAccount" not in data


def test_a_disambiguator_is_not_a_contradiction(tmp_path: Path, monkeypatch) -> None:
    """`~<disambiguator>` separates two grants of *one* account, so the record
    matches the derived name and must still be restored."""
    slot = _park_carrying(tmp_path, f"first@corp.com#ccccdddd~{PARK_STAMP}", "first", monkeypatch)
    assert slot.exists()
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("other"))
    home = claude_pool.config_home(cfg)
    _write_identity(home, {"emailAddress": "other@corp.com"})

    claude_pool.switch(
        cfg,
        GlobalConfig(),
        f"first@corp.com#ccccdddd~{PARK_STAMP}",
        authoritative={cfg.container_prefix},
    )

    assert json.loads((home / ".claude.json").read_text())["oauthAccount"] == ACCOUNT_BLOCK


def test_switch_still_clears_when_the_target_carries_no_record(tmp_path: Path, monkeypatch) -> None:
    """Back-compatibility, and the `/login` case: a login that jailbee never
    parked has no record to restore, so clearing stays the fallback and Claude
    Code repopulates the account on its next run."""
    target = _park(tmp_path, "first@corp.com", monkeypatch)
    target.write_text(_grant("first"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("other"))
    home = claude_pool.config_home(cfg)
    _write_identity(home, {"emailAddress": "other@corp.com"})

    claude_pool.switch(cfg, GlobalConfig(), "first@corp.com", authoritative={cfg.container_prefix})

    assert "oauthAccount" not in json.loads((home / ".claude.json").read_text())


def test_switch_into_an_empty_holder_parks_nothing(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    change = claude_pool.switch(
        cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix}
    )

    assert change.parked_as is None
    assert change.activated == "new@corp.com"
    assert claude_pool.live_credential_path(cfg).exists()


def test_switch_refuses_the_account_already_live(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _cred())
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.switch(cfg, GlobalConfig(), "me@corp.com", authoritative={cfg.container_prefix})

    assert "already" in str(excinfo.value).lower()


def test_switch_writes_the_credential_readable_only_by_its_owner(
    tmp_path: Path, monkeypatch
) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    claude_pool.switch(cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix})

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
        claude_pool.switch(
            cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix}
        )

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

    change = claude_pool.switch(
        cfg, gcfg, "new@corp.com", authoritative={cfg.container_prefix, "other"}
    )

    assert change.updated == sorted([cfg.container_prefix, "other"])
    for home in (
        claude_pool.config_home(cfg),
        tmp_path / "other-shared" / "claude",
    ):
        assert "oauthAccount" not in json.loads((home / ".claude.json").read_text())


def test_switch_names_a_member_whose_config_file_is_torn(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)
    home = claude_pool.config_home(cfg)
    home.mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text('{"oauthAccount": {"emai', encoding="utf-8")

    change = claude_pool.switch(
        cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix}
    )

    assert change.not_updated == [cfg.container_prefix]
    # A file we could not read is never overwritten.
    assert (home / ".claude.json").read_text() == '{"oauthAccount": {"emai'


def test_switch_reports_a_member_that_looks_busy(tmp_path: Path, monkeypatch) -> None:
    _park(tmp_path, "new@corp.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)
    sessions = claude_pool.config_home(cfg) / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "77.json").write_text("{}", encoding="utf-8")

    change = claude_pool.switch(
        cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix}
    )

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

    claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})
    _holder_with(cfg, json.dumps({"claudeAiOauth": {"accessToken": "second"}}))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "second@x.com"})
    claude_pool.switch(cfg, GlobalConfig(), "first@x.com", authoritative={cfg.container_prefix})

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

    slots = claude_pool.list_slots(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert [s.name for s in slots] == ["live@x.com", "aaa@x.com", "zzz@x.com"]
    assert slots[0].live is True


def test_invalidate_identity_reports_a_config_it_cannot_read(tmp_path: Path) -> None:
    home = tmp_path / "claude"
    home.mkdir()
    (home / ".claude.json").write_text('["not", "an", "object"]', encoding="utf-8")

    assert claude_pool.invalidate_identity(home) is False
    assert (home / ".claude.json").read_text() == '["not", "an", "object"]'


def test_invalidate_identity_reports_a_torn_multibyte_config(tmp_path: Path) -> None:
    """Same `UnicodeDecodeError` hole as `read_identity`, but reached from
    inside `switch` *after* the credential files have moved: a raise here would
    report a failed switch that had in fact already landed."""
    home = tmp_path / "claude"
    home.mkdir()
    torn = b'{"oauthAccount": {"emailAddress": "caf\xc3'
    (home / ".claude.json").write_bytes(torn)

    assert claude_pool.invalidate_identity(home) is False
    assert (home / ".claude.json").read_bytes() == torn


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

    claude_pool.switch(cfg, GlobalConfig(), "new@corp.com", authoritative={cfg.container_prefix})

    assert list(claude_pool.store_dir().glob("*.activating")) == []


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
        claude_pool.switch(cfg, GlobalConfig(), "new@x.com", authoritative={cfg.container_prefix})

    # The target is back in the store and the holder is empty again — not
    # holding a second copy of the grant `_atomic_write` already wrote.
    assert target.exists()
    assert not live_path.exists()


def test_switch_leaves_an_orphaned_staging_file_exactly_where_it_is(
    tmp_path: Path, monkeypatch
) -> None:
    """A stage a killed switch left behind is reported by `jailbee doctor`, not
    healed here. Renaming it home is the one move that can put one grant in two
    files: the store is host-wide, so the grant may be live in another holder
    this call cannot see. Leaving it costs a file on disk and nothing else."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    store = claude_pool.store_dir()
    store.mkdir(parents=True)
    stage = store / "orphan@x.com.json.activating"
    stage.write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "o", "refreshToken": "only"}}),
        encoding="utf-8",
    )
    _park(tmp_path, "other@x.com", monkeypatch)
    cfg = _cfg(tmp_path)
    claude_pool.holder_dir(cfg).mkdir(parents=True)

    change = claude_pool.switch(
        cfg, GlobalConfig(), "other@x.com", authoritative={cfg.container_prefix}
    )

    # The switch itself completes normally...
    assert change.activated == "other@x.com"
    # ...and the stage is neither adopted nor deleted.
    assert stage.read_text() == json.dumps(
        {"claudeAiOauth": {"accessToken": "o", "refreshToken": "only"}}
    )
    assert not (store / "orphan@x.com.json").exists()


# --- Two independent grants for one account -------------------------------
#
# `slug_for` derives a slot name from the account alone, so two *different*
# logins for one account collide. Both ways of reaching that state are
# ordinary: `/login` as the same account after a `park` (the documented way to
# add an account), and two holders on one host each logged into it. Refusing
# the second one used to freeze the whole holder — the check runs on every
# switch — so the pool could not be used for any account until someone renamed
# files by hand.


def _grant(token: str, **extra: object) -> str:
    """A credential whose refresh-token lineage is `token`."""
    return json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": token}, **extra})


def _login_in(path: Path) -> object:
    """The `claudeAiOauth` block a stored file holds.

    What the "this grant landed intact" assertions actually mean. Comparing the
    whole file would also pin `ACCOUNT_RECORD_KEY`, which `park` stamps in and
    which is not what those tests are about.
    """
    return json.loads(path.read_text())["claudeAiOauth"]


PARK_TIME = datetime(2026, 8, 28, 10, 42, 33)
PARK_STAMP = "20260828-104233"


def test_the_disambiguator_cannot_appear_in_a_derived_name() -> None:
    """The whole grammar rests on this: `~` separates a derived name from what
    makes it unique, which only works because `_SLUG_UNSAFE` strips `~` out of
    both halves. Widen that allowed set and slot names stop parsing."""
    slug = claude_pool.slug_for(Identity("a~b@x.com", "c~d~e"))
    assert claude_pool.DISAMBIGUATOR not in slug
    assert slug == "a-b@x.com#c-d-e"


def test_slot_email_and_org_hint_look_past_a_disambiguator() -> None:
    disambiguated = Slot(name=f"me@corp.com#a1b2c3d4~{PARK_STAMP}", path=Path("/x"), live=False)
    assert disambiguated.email == "me@corp.com"
    assert disambiguated.org_hint == "a1b2c3d4"

    no_org = Slot(name="me@corp.com~live", path=Path("/x"), live=True)
    assert no_org.email == "me@corp.com"
    assert no_org.org_hint is None

    unknown = Slot(name=f"unknown-{PARK_STAMP}~{PARK_STAMP}", path=Path("/x"), live=False)
    assert unknown.email is None
    assert unknown.org_hint is None

    unidentified = Slot(name=f"{claude_pool.LIVE_UNIDENTIFIED}~live", path=Path("/x"), live=True)
    assert unidentified.email is None
    assert unidentified.org_hint is None


def test_display_name_drops_only_the_organization() -> None:
    """`display_name` is the ACCOUNT column's cell, and the ORG column carries
    the `#<org8>` half. It must drop exactly that half and nothing else — the
    disambiguator especially, since it is what `resolve_ref` needs typed to
    tell two grants of one account apart."""
    both = Slot(name=f"me@corp.com#a1b2c3d4~{PARK_STAMP}", path=Path("/x"), live=False)
    assert both.display_name == f"me@corp.com~{PARK_STAMP}"
    assert both.disambiguator == PARK_STAMP

    org_only = Slot(name="me@corp.com#a1b2c3d4", path=Path("/x"), live=True)
    assert org_only.display_name == "me@corp.com"
    assert org_only.disambiguator is None

    plain = Slot(name="me@corp.com", path=Path("/x"), live=False)
    assert plain.display_name == "me@corp.com"


def test_display_name_keeps_an_emailless_name_whole() -> None:
    """The two emailless shapes have no `#<org8>` to drop, and their
    disambiguator is the only thing telling two of them apart — so the name
    passes through untouched rather than being rebuilt from `email`."""
    unidentified = Slot(name=f"{claude_pool.LIVE_UNIDENTIFIED}~live", path=Path("/x"), live=True)
    assert unidentified.display_name == f"{claude_pool.LIVE_UNIDENTIFIED}~live"
    assert unidentified.disambiguator == "live"

    unknown = Slot(name=f"unknown-{PARK_STAMP}", path=Path("/x"), live=False)
    assert unknown.display_name == f"unknown-{PARK_STAMP}"


def _slots() -> list[Slot]:
    return [
        Slot(name="live@corp.com", path=Path("/h/.credentials.json"), live=True),
        Slot(name="parked@corp.com", path=Path("/s/parked@corp.com.json"), live=False),
        Slot(name="other@x.com", path=Path("/s/other@x.com.json"), live=False),
    ]


def test_resolve_interactively_passes_a_typed_ref_straight_through() -> None:
    """A named account must not read the store or render a picker."""
    calls: list[object] = []

    def picker(slots):
        calls.append(slots)
        return "picked"

    result = claude_pool.resolve_interactively(
        _slots(),
        "typed@corp.com",
        purpose="switch to",
        picker=picker,
        is_interactive=lambda: True,
    )
    assert result == "typed@corp.com"
    assert calls == []


def test_resolve_interactively_never_offers_the_live_slot() -> None:
    """`switch` and `rm` both refuse the live login, so offering it in a picker
    would be offering a guaranteed error."""
    seen: list[list[str]] = []

    def picker(slots):
        seen.append([s.name for s in slots])
        return slots[0].name

    result = claude_pool.resolve_interactively(
        _slots(), None, purpose="switch to", picker=picker, is_interactive=lambda: True
    )
    assert seen == [["parked@corp.com", "other@x.com"]]
    assert result == "parked@corp.com"


def test_resolve_interactively_returns_none_when_the_picker_is_cancelled() -> None:
    """ESC is not an error: the caller aborts without printing a failure."""
    assert (
        claude_pool.resolve_interactively(
            _slots(), None, purpose="switch to", picker=lambda _: None, is_interactive=lambda: True
        )
        is None
    )


def test_resolve_interactively_rejects_a_holder_with_nothing_parked() -> None:
    """One live login and nothing parked is not an empty pool — but there is
    still nothing to switch *to*, and the message must say how to get one."""
    only_live = [Slot(name="live@corp.com", path=Path("/h/c.json"), live=True)]
    with pytest.raises(claude_pool.PoolError) as e:
        claude_pool.resolve_interactively(
            only_live,
            None,
            purpose="switch to",
            picker=lambda _: "never",
            is_interactive=lambda: True,
        )
    assert "no stored login to switch to" in str(e.value)
    assert "park" in str(e.value)


def test_resolve_interactively_names_the_candidates_without_a_tty() -> None:
    """A script cannot answer a picker, so the failure has to teach it the
    references it should have passed."""
    with pytest.raises(claude_pool.PoolError) as e:
        claude_pool.resolve_interactively(
            _slots(),
            None,
            purpose="delete",
            picker=lambda _: "never",
            is_interactive=lambda: False,
        )
    message = str(e.value)
    assert "run in a TTY" in message
    assert "other@x.com" in message and "parked@corp.com" in message
    assert "live@corp.com" not in message


def test_group_name_is_the_directory_name_or_none(tmp_path: Path) -> None:
    assert claude_pool.group_name(_cfg(tmp_path, group="gisgro")) == "gisgro"
    assert claude_pool.group_name(_cfg(tmp_path)) is None


def test_park_stores_a_second_independent_grant_for_one_account(
    tmp_path: Path, monkeypatch
) -> None:
    """`park`, `/login` as the same account, `park` again. Nothing about that
    is a duplicate: the two grants refresh independently."""
    first = _park(tmp_path, "me@corp.com", monkeypatch)
    first.write_text(_grant("lineage-1"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("lineage-2"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    change = claude_pool.park(
        cfg, GlobalConfig(), authoritative={cfg.container_prefix}, now=PARK_TIME
    )

    assert change.parked_as == f"me@corp.com~{PARK_STAMP}"
    assert first.read_text() == _grant("lineage-1")
    second = claude_pool.store_dir() / f"me@corp.com~{PARK_STAMP}.json"
    assert _login_in(second) == json.loads(_grant("lineage-2"))["claudeAiOauth"]
    assert not claude_pool.live_credential_path(cfg).exists()


def test_park_refuses_a_rotated_copy_of_a_stored_login(tmp_path: Path, monkeypatch) -> None:
    """An access token rotates while its refresh-token lineage does not, so two
    blocks can differ field by field and still be one login in two files."""
    stored = _park(tmp_path, "me@corp.com", monkeypatch)
    stored.write_text(_grant("same-lineage"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    rotated = {"claudeAiOauth": {"accessToken": "rotated", "refreshToken": "same-lineage"}}
    _holder_with(cfg, json.dumps(rotated))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix}, now=PARK_TIME)

    assert "already stored as `me@corp.com`" in str(excinfo.value)
    assert claude_pool.live_credential_path(cfg).exists()
    assert not (claude_pool.store_dir() / f"me@corp.com~{PARK_STAMP}.json").exists()


def test_park_fails_closed_when_the_two_grants_cannot_be_compared(
    tmp_path: Path, monkeypatch
) -> None:
    """Unreadable is not "different". The message must say jailbee could not
    tell — never that the files are copies, which would be a claim about
    something it failed to open."""
    stored = _park(tmp_path, "me@corp.com", monkeypatch)
    stored.write_text("not json at all", encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("lineage-2"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix}, now=PARK_TIME)

    message = str(excinfo.value)
    assert "could not read both files" in message
    assert "copies" not in message
    assert claude_pool.live_credential_path(cfg).exists()
    assert not (claude_pool.store_dir() / f"me@corp.com~{PARK_STAMP}.json").exists()


def test_park_never_overwrites_an_existing_disambiguated_slot(tmp_path: Path, monkeypatch) -> None:
    """A third grant parked in the same second as the second one still gets its
    own file, and every earlier grant is compared — not just the plain name, or
    one lineage could be parked twice under two different suffixes."""
    plain = _park(tmp_path, "me@corp.com", monkeypatch)
    plain.write_text(_grant("lineage-1"), encoding="utf-8")
    already = _park(tmp_path, f"me@corp.com~{PARK_STAMP}", monkeypatch)
    already.write_text(_grant("lineage-2"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("lineage-3"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    change = claude_pool.park(
        cfg, GlobalConfig(), authoritative={cfg.container_prefix}, now=PARK_TIME
    )

    assert change.parked_as == f"me@corp.com~{PARK_STAMP}-2"
    assert plain.read_text() == _grant("lineage-1")
    assert already.read_text() == _grant("lineage-2")
    third = claude_pool.store_dir() / f"me@corp.com~{PARK_STAMP}-2.json"
    assert _login_in(third) == json.loads(_grant("lineage-3"))["claudeAiOauth"]


def test_park_refuses_a_lineage_already_stored_under_a_disambiguated_name(
    tmp_path: Path, monkeypatch
) -> None:
    """The comparison covers every file sharing the derived name, so the escape
    hatch cannot be used to smuggle in a second copy of one lineage."""
    plain = _park(tmp_path, "me@corp.com", monkeypatch)
    plain.write_text(_grant("lineage-1"), encoding="utf-8")
    already = _park(tmp_path, f"me@corp.com~{PARK_STAMP}", monkeypatch)
    already.write_text(_grant("lineage-2"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("lineage-2"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@corp.com"})

    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix}, now=PARK_TIME)

    assert f"already stored as `me@corp.com~{PARK_STAMP}`" in str(excinfo.value)


def test_a_switch_for_another_account_survives_a_collided_one(tmp_path: Path, monkeypatch) -> None:
    """The wedge this fix exists for: the park-side name check runs on *every*
    switch, so one account with two grants used to make every other account
    unswitchable through that holder."""
    mine = _park(tmp_path, "me@x.com", monkeypatch)
    mine.write_text(_grant("mine-parked"), encoding="utf-8")
    colleague = _park(tmp_path, "colleague@x.com", monkeypatch)
    colleague.write_text(_grant("colleague"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("mine-live"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@x.com"})

    change = claude_pool.switch(
        cfg, GlobalConfig(), "colleague@x.com", authoritative={cfg.container_prefix}, now=PARK_TIME
    )

    assert change.activated == "colleague@x.com"
    assert change.parked_as == f"me@x.com~{PARK_STAMP}"
    assert json.loads(claude_pool.live_credential_path(cfg).read_text())["claudeAiOauth"] == {
        "accessToken": "a",
        "refreshToken": "colleague",
    }
    assert [s.name for s in claude_pool.parked_slots()] == [
        "me@x.com",
        f"me@x.com~{PARK_STAMP}",
    ]


def test_list_slots_disambiguates_a_live_name_a_parked_slot_holds(
    tmp_path: Path, monkeypatch
) -> None:
    """The state the documented add flow leaves behind. Two slots under one
    name make every `resolve_ref` for it an error, so the live one is renamed
    for display and reference."""
    _park(tmp_path, "me@x.com", monkeypatch)
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("live"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@x.com"})

    slots = claude_pool.list_slots(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert [(s.name, s.live) for s in slots] == [("me@x.com~live", True), ("me@x.com", False)]
    # Still one account as far as the columns are concerned.
    assert {s.email for s in slots} == {"me@x.com"}


def test_switch_to_a_parked_slot_whose_name_the_live_one_shares(
    tmp_path: Path, monkeypatch
) -> None:
    """Same state, exercised through `switch`. Before the live slot was
    disambiguated this raised `resolve_ref`'s "names 2 files" — the reference
    was unusable even though only one of the two was a stored file.

    The plain name comes back free here, and that is the point: `switch` stages
    the target out of the store *first*, so the grant it parks lands on
    `me@x.com.json` with no suffix. The two accounts have swapped places and
    there is still exactly one file per grant.
    """
    parked = _park(tmp_path, "me@x.com", monkeypatch)
    parked.write_text(_grant("parked"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    _holder_with(cfg, _grant("live"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@x.com"})

    change = claude_pool.switch(
        cfg, GlobalConfig(), "me@x.com", authoritative={cfg.container_prefix}, now=PARK_TIME
    )

    assert change.activated == "me@x.com"
    assert change.parked_as == "me@x.com"
    assert json.loads(claude_pool.live_credential_path(cfg).read_text())["claudeAiOauth"] == {
        "accessToken": "a",
        "refreshToken": "parked",
    }
    stored = claude_pool.parked_slots()
    assert [s.name for s in stored] == ["me@x.com"]
    assert json.loads(stored[0].path.read_text())["claudeAiOauth"]["refreshToken"] == "live"


def test_resolve_removable_picks_the_parked_file_of_a_duplicated_name() -> None:
    """`rm` never deletes a live login, so a name carried by the live slot and
    a parked file is unambiguous here even though `resolve_ref` refuses it.
    Without this the corruption would have no in-tool escape."""
    dupes = [
        Slot(name="me@x.com", path=Path("/store/me@x.com.json"), live=False),
        Slot(name="me@x.com", path=Path("/holder/.credentials.json"), live=True),
    ]
    assert claude_pool.resolve_removable("me@x.com", dupes).path == Path("/store/me@x.com.json")


def test_resolve_removable_still_reports_the_live_account() -> None:
    """The escape must not swallow the ordinary refusal: with no duplicate,
    `rm <live>` still resolves to the live slot so the caller can refuse it."""
    live = Slot(name="me@x.com", path=Path("/holder/.credentials.json"), live=True)
    assert claude_pool.resolve_removable("me@x.com", [live]).live is True


def test_resolve_removable_defers_to_resolve_ref_for_two_parked_files() -> None:
    """Two *parked* files under one name are still unanswerable — picking one
    to delete would be picking a login to destroy."""
    dupes = [
        Slot(name="me@x.com", path=Path("/store/a.json"), live=False),
        Slot(name="me@x.com", path=Path("/store/b.json"), live=False),
    ]
    with pytest.raises(claude_pool.PoolError):
        claude_pool.resolve_removable("me@x.com", dupes)


def test_compose_ignores_a_non_shared_key_offered_as_live(tmp_path: Path) -> None:
    """`compose_credential` is public, and a caller passing a whole credential
    through would write the live account's login into the target's file: one
    account's grant under another's identity, and two files for one lineage."""
    target = _grant("target-lineage")
    out = json.loads(
        claude_pool.compose_credential(
            target,
            {"claudeAiOauth": {"accessToken": "x", "refreshToken": "live-lineage"}, "mcpOAuth": {}},
        )
    )
    assert out["claudeAiOauth"] == {"accessToken": "a", "refreshToken": "target-lineage"}


def test_park_of_an_empty_holder_creates_nothing(tmp_path: Path, monkeypatch) -> None:
    """Discovering there is nothing to park is a read. Taking the lock would
    create the holder and both lock directories as a side effect of it."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    cfg = _cfg(tmp_path, group="work")

    change = claude_pool.park(cfg, GlobalConfig(), authoritative={cfg.container_prefix})

    assert change.parked_as is None
    assert not claude_pool.holder_dir(cfg).exists()


def test_the_live_account_refusal_has_one_source(tmp_path: Path) -> None:
    """`cli.claude_rm_cmd` pre-checks so the prompt is skipped, and
    `remove_slot` refuses again for non-CLI callers. One sentence, two sites."""
    live = Slot(name="me@corp.com", path=tmp_path / "c.json", live=True)
    with pytest.raises(claude_pool.PoolError) as excinfo:
        claude_pool.remove_slot(live)
    assert str(excinfo.value) == claude_pool.live_account_refusal("me@corp.com")


def test_switch_rolls_back_a_park_that_had_to_disambiguate(
    tmp_path: Path, monkeypatch, mocker
) -> None:
    """The rollback moves back the file that was written, not the one whose
    name was asked for. Rebuilding the path from the slot name works only while
    the two are the same string — after a disambiguation it names a file that
    does not exist, and the live credential is stranded in the store."""
    mine = _park(tmp_path, "me@x.com", monkeypatch)
    mine.write_text(_grant("mine-parked"), encoding="utf-8")
    colleague = _park(tmp_path, "colleague@x.com", monkeypatch)
    colleague.write_text(_grant("colleague"), encoding="utf-8")
    cfg = _cfg(tmp_path)
    live = _holder_with(cfg, _grant("mine-live"))
    _write_identity(claude_pool.config_home(cfg), {"emailAddress": "me@x.com"})
    mocker.patch("jailbee.claude_pool._atomic_write", side_effect=OSError("disk full"))

    with pytest.raises(OSError):
        claude_pool.switch(
            cfg,
            GlobalConfig(),
            "colleague@x.com",
            authoritative={cfg.container_prefix},
            now=PARK_TIME,
        )

    assert live.read_text() == _grant("mine-live")
    assert not (claude_pool.store_dir() / f"me@x.com~{PARK_STAMP}.json").exists()
    assert [s.name for s in claude_pool.parked_slots()] == ["colleague@x.com", "me@x.com"]


def test_live_account_ignores_a_non_authoritative_member(tmp_path):
    """A repo spanning two groups must not name the holder's login."""
    from jailbee import claude_pool

    home = tmp_path / "mixed-home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "wrong@example.com"}})
    )
    found = [claude_pool.Member("mixed", home)]

    assert claude_pool.live_account(found, prefer="mixed", authoritative=set()) is None
    assert (
        claude_pool.live_account(found, prefer="mixed", authoritative={"mixed"}).identity.email
        == "wrong@example.com"
    )


def test_park_names_the_file_unknown_when_nothing_is_authoritative(tmp_path, mocker):
    """Never wrong, sometimes nameless."""
    from datetime import datetime

    from jailbee import claude_pool

    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared")
    home = claude_pool.config_home(cfg)
    home.mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "wrong@example.com"}})
    )
    live = claude_pool.live_credential_path(cfg)
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_text(json.dumps({"claudeAiOauth": {"refreshToken": "rt-1"}}))
    mocker.patch("jailbee.claude_pool.store_dir", return_value=tmp_path / "store")

    change = claude_pool.park(
        cfg, GlobalConfig(), authoritative=set(), now=datetime(2026, 9, 2, 10, 0, 0)
    )

    assert change.parked_as is not None
    assert claude_pool.is_unidentified(change.parked_as)
    assert "wrong@example.com" not in change.parked_as
