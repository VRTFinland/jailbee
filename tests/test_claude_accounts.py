"""Tests for the Claude account pool's policy layer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jailbee import claude_accounts as ca
from jailbee.cswap import Account
from jailbee.db.models import ClaudeAccountHolding, RegisteredRepo

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

WORK = Account(
    number=1,
    email="work@gisgro.com",
    org_uuid="org-abc",
    org_name="Gisgro",
    alias="work",
    active=True,
    disabled=False,
    usage_status="ok",
    five_hour_pct=34.0,
    seven_day_pct=61.0,
)
PERSONAL = Account(
    number=2,
    email="me@example.com",
    org_uuid="",
    org_name="",
    alias="personal",
    active=False,
    disabled=False,
    usage_status="ok",
    five_hour_pct=0.0,
    seven_day_pct=12.0,
)
CI = Account(
    number=3,
    email="ci@example.com",
    org_uuid="",
    org_name="",
    alias="",
    active=False,
    disabled=True,
    usage_status="relogin_required",
    five_hour_pct=None,
    seven_day_pct=None,
)
ACCOUNTS = [WORK, PERSONAL, CI]


def _hold(session, account: Account, prefix: str, *, state: str = ca.HELD) -> None:
    session.add(
        ClaudeAccountHolding(
            email=account.email,
            org_uuid=account.org_uuid,
            container_prefix=prefix,
            slot=str(account.number),
            state=state,
            since=NOW,
        )
    )
    session.commit()


# --- allowlist ---------------------------------------------------------


def test_no_rows_means_every_account_is_allowed(db_session):
    assert ca.allowed_identities(db_session, "gisgro") == set()

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")

    assert all(r.allowed for r in rows), "an empty allowlist allows everything"


def test_set_allowed_replaces_rather_than_appends(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity, CI.identity})
    ca.set_allowed(db_session, "gisgro", {PERSONAL.identity})

    assert ca.allowed_identities(db_session, "gisgro") == {PERSONAL.identity}


def test_set_allowed_with_an_empty_set_clears_the_list(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity})
    ca.set_allowed(db_session, "gisgro", set())

    assert ca.allowed_identities(db_session, "gisgro") == set()
    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    assert all(r.allowed for r in rows), "cleared means all, not none"


def test_one_repos_allowlist_does_not_touch_another(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity})
    ca.set_allowed(db_session, "otherrepo", {PERSONAL.identity})

    assert ca.allowed_identities(db_session, "gisgro") == {WORK.identity}
    assert ca.allowed_identities(db_session, "otherrepo") == {PERSONAL.identity}


def test_an_allowlist_filters_the_rows(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity})

    rows = {
        r.account.number: r.allowed for r in ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    }

    assert rows == {1: True, 2: False, 3: False}


def test_set_allowed_dedups_a_list_with_a_repeated_identity(db_session):
    """The primary key is (prefix, email, org_uuid); a caller passing a list
    with a duplicate must not hit an integrity error."""
    ca.set_allowed(db_session, "gisgro", [WORK.identity, WORK.identity])

    assert ca.allowed_identities(db_session, "gisgro") == {WORK.identity}


# --- holders ----------------------------------------------------------


def test_holders_maps_identity_to_the_holding_repo(db_session):
    _hold(db_session, PERSONAL, "otherrepo")
    db_session.add(
        RegisteredRepo(
            container_prefix="otherrepo", repo_root="/home/x/src/otherrepo", registered_at=NOW
        )
    )
    db_session.commit()

    held = ca.holders(db_session)

    assert held[PERSONAL.identity].container_prefix == "otherrepo"
    assert held[PERSONAL.identity].repo_root == "/home/x/src/otherrepo"
    assert held[PERSONAL.identity].state == ca.HELD


def test_a_holder_with_no_registered_repo_has_no_root(db_session):
    """A repo whose checkout is gone still shows as the holder — that is the
    case `jailbee claude release <ref>` exists for."""
    _hold(db_session, PERSONAL, "goneaway")

    held = ca.holders(db_session)

    assert held[PERSONAL.identity].container_prefix == "goneaway"
    assert held[PERSONAL.identity].repo_root is None


def test_rows_mark_this_repos_own_holding_as_mine(db_session):
    _hold(db_session, WORK, "gisgro")
    _hold(db_session, PERSONAL, "otherrepo")

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    by_number = {r.account.number: r for r in rows}

    assert by_number[1].mine is True
    assert by_number[2].mine is False
    assert by_number[3].holder is None


def test_blocked_reason_names_the_other_repo(db_session):
    _hold(db_session, PERSONAL, "otherrepo")

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    row = next(r for r in rows if r.account.number == 2)

    assert row.blocked_reason is not None
    assert "otherrepo" in row.blocked_reason


def test_a_not_allowed_account_is_blocked_with_its_own_reason(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity})

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    row = next(r for r in rows if r.account.number == 2)

    assert row.blocked_reason is not None
    assert "not allowed" in row.blocked_reason


def test_blocked_reason_prefers_not_allowed_over_held_by_another(db_session):
    """Both reasons apply here; "not allowed" wins — it is this repo's own
    restriction, and the more actionable message."""
    _hold(db_session, PERSONAL, "otherrepo")
    ca.set_allowed(db_session, "gisgro", {WORK.identity})

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    row = next(r for r in rows if r.account.number == 2)

    assert row.blocked_reason == "not allowed for this repo"


def test_this_repos_own_holding_is_never_blocked(db_session):
    _hold(db_session, WORK, "gisgro")

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    row = next(r for r in rows if r.account.number == 1)

    assert row.blocked_reason is None


def test_a_claiming_row_blocks_and_says_so(db_session):
    _hold(db_session, PERSONAL, "otherrepo", state=ca.CLAIMING)

    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")
    row = next(r for r in rows if r.account.number == 2)

    assert row.blocked_reason is not None
    assert "claiming" in row.blocked_reason


def test_holder_cell_is_a_dash_when_nobody_holds_it(db_session):
    rows = ca.account_rows(db_session, ACCOUNTS, prefix="gisgro")

    assert all(r.holder_cell == "-" for r in rows)


# --- reference resolution -----------------------------------------------


def test_resolve_ref_accepts_a_slot_number():
    assert ca.resolve_ref(ACCOUNTS, "2") is PERSONAL


def test_resolve_ref_by_digit_still_resolves_the_slot_when_unambiguous():
    """No other account is aliased "1", so the digit means slot 1 cleanly."""
    assert ca.resolve_ref(ACCOUNTS, "1") is WORK


def test_resolve_ref_refuses_when_a_digit_also_names_another_accounts_alias():
    """Nothing forbids a numeric alias: "2" could mean slot 2 (PERSONAL) or
    the account aliased "2" — picking the slot silently would burn the wrong
    account's quota."""
    numeric_alias = Account(
        number=5,
        email="fifth@example.com",
        org_uuid="",
        org_name="",
        alias="2",
        active=False,
        disabled=False,
        usage_status="ok",
        five_hour_pct=None,
        seven_day_pct=None,
    )

    with pytest.raises(ca.PoolError) as exc:
        ca.resolve_ref([WORK, PERSONAL, numeric_alias], "2")

    assert "2" in str(exc.value) and "5" in str(exc.value), "names both candidates"


def test_resolve_ref_by_digit_is_not_ambiguous_with_its_own_alias():
    """The same account being both slot 1 and aliased "1" has only one
    answer — no false ambiguity."""
    self_aliased = Account(
        number=1,
        email="work@gisgro.com",
        org_uuid="org-abc",
        org_name="Gisgro",
        alias="1",
        active=True,
        disabled=False,
        usage_status="ok",
        five_hour_pct=34.0,
        seven_day_pct=61.0,
    )

    assert ca.resolve_ref([self_aliased, PERSONAL, CI], "1") is self_aliased


def test_resolve_ref_accepts_an_alias():
    assert ca.resolve_ref(ACCOUNTS, "work") is WORK


def test_resolve_ref_refuses_an_ambiguous_alias():
    """Two accounts sharing an alias (case-insensitively) is a collision the
    same way an ambiguous email is."""
    dup_alias = Account(
        number=5,
        email="other@example.com",
        org_uuid="",
        org_name="",
        alias="Work",
        active=False,
        disabled=False,
        usage_status="ok",
        five_hour_pct=None,
        seven_day_pct=None,
    )

    with pytest.raises(ca.PoolError) as exc:
        ca.resolve_ref([WORK, dup_alias], "work")

    assert "1" in str(exc.value) and "5" in str(exc.value), "names both slots"


def test_resolve_ref_accepts_an_email():
    assert ca.resolve_ref(ACCOUNTS, "ci@example.com") is CI


def test_resolve_ref_is_case_insensitive_for_email_and_alias():
    assert ca.resolve_ref(ACCOUNTS, "WORK") is WORK
    assert ca.resolve_ref(ACCOUNTS, "CI@Example.com") is CI


def test_resolve_ref_refuses_an_unknown_reference():
    with pytest.raises(ca.PoolError) as exc:
        ca.resolve_ref(ACCOUNTS, "nope")

    assert "nope" in str(exc.value)
    assert "work" in str(exc.value), "the refusal lists what IS available"


def test_resolve_ref_refuses_an_ambiguous_email():
    """One email can hold two slots (personal + organization membership)."""
    dup = Account(
        number=4,
        email="work@gisgro.com",
        org_uuid="org-xyz",
        org_name="Other",
        alias="",
        active=False,
        disabled=False,
        usage_status="ok",
        five_hour_pct=None,
        seven_day_pct=None,
    )

    with pytest.raises(ca.PoolError) as exc:
        ca.resolve_ref([WORK, dup], "work@gisgro.com")

    assert "1" in str(exc.value) and "4" in str(exc.value), "names both slots"


def test_resolve_ref_refuses_when_the_pool_is_empty():
    with pytest.raises(ca.PoolError) as exc:
        ca.resolve_ref([], "1")

    assert "jailbee claude add" in str(exc.value)


# --- current account -----------------------------------------------------


def test_current_account_is_the_one_cswap_marks_active():
    assert ca.current_account(ACCOUNTS) is WORK


def test_current_account_is_none_when_no_pooled_account_is_live():
    parked = [
        Account(
            number=a.number,
            email=a.email,
            org_uuid=a.org_uuid,
            org_name=a.org_name,
            alias=a.alias,
            active=False,
            disabled=a.disabled,
            usage_status=a.usage_status,
            five_hour_pct=a.five_hour_pct,
            seven_day_pct=a.seven_day_pct,
        )
        for a in ACCOUNTS
    ]

    assert ca.current_account(parked) is None
