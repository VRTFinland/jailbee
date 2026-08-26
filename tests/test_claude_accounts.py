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


# --- use: the two-phase claim -----------------------------------------


class FakeCswap:
    """A `Cswap` stand-in. Never runs a subprocess."""

    def __init__(
        self,
        *,
        live,
        switch_error: Exception | None = None,
        landed=None,
        status_error: Exception | None = None,
    ) -> None:
        self._live = live
        self._switch_error = switch_error
        self._landed = landed
        self._status_error = status_error
        self.switch_calls: list[str] = []
        self.status_calls = 0

    def status(self):
        self.status_calls += 1
        if self._status_error is not None:
            raise self._status_error
        return self._live

    def switch(self, target: str):
        from jailbee.cswap import SwitchResult

        self.switch_calls.append(target)
        if self._switch_error is not None:
            raise self._switch_error
        if self._landed is not None:
            return self._landed
        # By default cswap lands on the account it was asked for: the slot is
        # looked up against the same listing the caller resolved from.
        account = next(a for a in ACCOUNTS if str(a.number) == target)
        return SwitchResult(
            message=f"Switched to Account-{target}", email=account.email, number=account.number
        )


def _live(email: str | None, *, managed: bool, number=None, org_uuid=""):
    from jailbee.cswap import LiveAccount

    return LiveAccount(email=email, managed=managed, number=number, org_uuid=org_uuid)


def test_use_switches_and_records_the_holding(db_session):
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    message = ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    assert cswap.switch_calls == ["2"], "always an explicit slot, never a bare rotate"
    assert "Account-2" in message
    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None
    assert row.state == ca.HELD
    assert row.container_prefix == "gisgro"
    assert row.slot == "2"


def test_use_drops_this_repos_previous_holding(db_session):
    _hold(db_session, WORK, "gisgro")
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    assert db_session.get(ClaudeAccountHolding, WORK.identity) is None, "the old row is gone"
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is not None


def test_use_refuses_an_account_held_by_another_repo_and_names_it(db_session):
    _hold(db_session, PERSONAL, "otherrepo")
    db_session.add(
        RegisteredRepo(
            container_prefix="otherrepo", repo_root="/home/x/src/otherrepo", registered_at=NOW
        )
    )
    db_session.commit()
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    message = str(exc.value)
    assert "otherrepo" in message
    assert "cd /home/x/src/otherrepo && jailbee claude release" in message, (
        "the fix must be directly runnable"
    )
    assert cswap.switch_calls == [], "nothing was switched"


def test_the_held_elsewhere_refusal_works_without_a_registered_root(db_session):
    _hold(db_session, PERSONAL, "goneaway")
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    message = str(exc.value)
    assert "goneaway" in message
    assert "jailbee claude release 2" in message, "the escape hatch for a lost checkout"


def test_use_refuses_an_account_that_is_not_allowed(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity})
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    assert "not allowed" in str(exc.value)
    assert "jailbee claude allow" in str(exc.value)
    assert cswap.switch_calls == []


def test_use_refuses_when_this_repos_live_login_is_not_in_the_pool(db_session):
    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    message = str(exc.value)
    assert "stray@example.com" in message
    assert "jailbee claude add" in message
    assert cswap.switch_calls == []
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None


def test_force_overrides_the_unsaved_login_refusal(db_session):
    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW, force=True)

    assert cswap.switch_calls == ["2"]


def test_force_does_not_override_held_elsewhere(db_session):
    """--force is about the caller's own login, not about another repo's lease."""
    _hold(db_session, PERSONAL, "otherrepo")
    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    with pytest.raises(ca.PoolError):
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW, force=True)

    assert cswap.switch_calls == []


def test_no_live_login_at_all_is_not_a_refusal(db_session):
    """A fresh shared dir has nothing to lose."""
    cswap = FakeCswap(live=_live(None, managed=False))

    ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    assert cswap.switch_calls == ["2"]


def test_a_failed_switch_deletes_the_claiming_row(db_session):
    from jailbee.cswap import CswapError

    cswap = FakeCswap(
        live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"),
        switch_error=CswapError("credential store is locked"),
    )

    with pytest.raises(CswapError):
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    db_session.expire_all()
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None, (
        "the claiming row is GONE, not merely 'cleanup was called'"
    )


def test_a_failed_switch_leaves_the_previous_holding_intact(db_session):
    from jailbee.cswap import CswapError

    _hold(db_session, WORK, "gisgro")
    cswap = FakeCswap(
        live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"),
        switch_error=CswapError("nope"),
    )

    with pytest.raises(CswapError):
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    db_session.expire_all()
    row = db_session.get(ClaudeAccountHolding, WORK.identity)
    assert row is not None and row.state == ca.HELD


def test_re_using_the_account_this_repo_already_holds_is_a_no_op(db_session):
    _hold(db_session, WORK, "gisgro")
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    message = ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="1", now=NOW)

    assert cswap.switch_calls == [], "no switch, no lock, no keychain touch"
    assert "already" in message.lower()


def test_use_refreshes_a_stale_slot_number_on_the_row(db_session):
    """`cswap move` renumbers slots; the ledger's slot is display-only and is
    refreshed on write."""
    db_session.add(
        ClaudeAccountHolding(
            email=PERSONAL.email,
            org_uuid=PERSONAL.org_uuid,
            container_prefix="gisgro",
            slot="9",
            state=ca.HELD,
            since=NOW,
        )
    )
    db_session.commit()
    cswap = FakeCswap(live=_live("me@example.com", managed=True, number=2, org_uuid=""))

    ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None and row.slot == "2"


def test_a_stale_claiming_row_of_this_repo_can_be_reclaimed(db_session):
    """A crashed `use` left a claiming row for this repo; retrying must work."""
    _hold(db_session, PERSONAL, "gisgro", state=ca.CLAIMING)
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None and row.state == ca.HELD


# --- use: the held-elsewhere message ----------------------------------


def test_the_held_elsewhere_refusal_says_when_the_holding_is_still_claiming(db_session):
    """A `claiming` row means either a switch is running right now or one
    crashed — the two need different answers, so the message must not present
    it as a settled holding."""
    _hold(db_session, PERSONAL, "otherrepo", state=ca.CLAIMING)
    db_session.add(
        RegisteredRepo(
            container_prefix="otherrepo", repo_root="/home/x/src/otherrepo", registered_at=NOW
        )
    )
    db_session.commit()
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    message = str(exc.value)
    assert "still `claiming`" in message
    assert "crashed" in message
    assert cswap.switch_calls == []


def test_the_held_elsewhere_refusal_offers_a_switch_before_a_release(db_session):
    """`release` is bookkeeping only: the credential stays behind and that repo
    keeps rotating it, so a switch — which checks the rotated credential back
    in — is the clean handover and has to be named first."""
    _hold(db_session, PERSONAL, "otherrepo")
    db_session.add(
        RegisteredRepo(
            container_prefix="otherrepo", repo_root="/home/x/src/otherrepo", registered_at=NOW
        )
    )
    db_session.commit()
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    message = str(exc.value)
    assert "jailbee claude use <other>" in message
    assert message.index("jailbee claude use <other>") < message.index("jailbee claude release"), (
        "the switch is the recommended handover, so it comes first"
    )
    assert "cd /home/x/src/otherrepo && jailbee claude release" in message, (
        "the release is still directly runnable"
    )


# --- use: a failed or mis-landed switch --------------------------------


def test_a_failed_switch_restores_a_previously_valid_holding_of_this_repo(db_session):
    """This repo already held the target as `held`; only the live login had
    drifted (an out-of-band `/login`). Deleting the row on failure would mark
    the account free while this repo may still be live on it."""
    from jailbee.cswap import CswapError

    earlier = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    db_session.add(
        ClaudeAccountHolding(
            email=PERSONAL.email,
            org_uuid=PERSONAL.org_uuid,
            container_prefix="gisgro",
            slot="9",
            state=ca.HELD,
            since=earlier,
        )
    )
    db_session.commit()
    cswap = FakeCswap(
        live=_live("stray@example.com", managed=False),
        switch_error=CswapError("credential store is locked"),
    )

    with pytest.raises(CswapError):
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW, force=True)

    db_session.expire_all()
    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None, "a holding that was valid before the command survives it"
    assert row.state == ca.HELD
    assert row.container_prefix == "gisgro"
    assert (row.slot, row.since) == ("9", earlier), "restored, not merely left as `held`"


def test_a_failed_switch_deletes_a_stale_claiming_row_it_had_reclaimed(db_session):
    """The other half: a `claiming` row is a crash artifact with no standing,
    so a failed retry must leave the account free, not stuck."""
    from jailbee.cswap import CswapError

    _hold(db_session, PERSONAL, "gisgro", state=ca.CLAIMING)
    cswap = FakeCswap(
        live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"),
        switch_error=CswapError("nope"),
    )

    with pytest.raises(CswapError):
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    db_session.expire_all()
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None


def test_a_switch_that_lands_on_another_account_fails_and_records_nothing(db_session):
    """`cswap switch 2` targets whatever is in slot 2 *now*; a `cswap move`
    between the listing and the switch would hand this repo a different
    account while the ledger claimed the one it asked for."""
    from jailbee.cswap import CswapError, SwitchResult

    cswap = FakeCswap(
        live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"),
        landed=SwitchResult(message="Switched to Account-2", email=CI.email, number=2),
    )

    with pytest.raises(CswapError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    message = str(exc.value)
    assert CI.email in message, "the message names what actually landed"
    assert "ledger was NOT updated" in message
    db_session.expire_all()
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None, (
        "no holding is written for an account cswap did not land on"
    )


def test_a_switch_reply_with_no_identity_is_taken_at_its_word(db_session):
    """cswap need not report a `to` block. An unverifiable reply is not a
    failure — refusing here would break the switch for a payload shape that is
    merely less informative."""
    from jailbee.cswap import SwitchResult

    cswap = FakeCswap(
        live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"),
        landed=SwitchResult(message="Switched", email=None, number=None),
    )

    ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None and row.state == ca.HELD


# --- use: a concurrent claim ------------------------------------------


def _integrity_error():
    """What the loser of two simultaneous INSERTs on the composite key gets."""
    from sqlalchemy.exc import IntegrityError

    return IntegrityError(
        "INSERT INTO claude_account_holding ...", {}, Exception("UNIQUE constraint failed")
    )


def test_a_concurrent_claim_is_a_refusal_not_a_traceback(db_session, mocker):
    """Two processes pass the holders() check and both INSERT; the primary key
    stops the double-hold — the loser must be told, not shown a stack trace."""
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))
    mocker.patch.object(db_session, "commit", side_effect=_integrity_error())

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    assert "claimed by another repo first" in str(exc.value)
    assert cswap.switch_calls == [], "the loser of the race switches nothing"


def test_a_concurrent_claim_names_the_winner_when_the_ledger_shows_it(db_session, mocker):
    """The row was absent on the first read and present after the conflict —
    exactly the concurrent case. Re-reading lets the refusal name the winner."""
    cswap = FakeCswap(live=_live("work@gisgro.com", managed=True, number=1, org_uuid="org-abc"))
    winner = ca.Holder(
        container_prefix="otherrepo", repo_root="/home/x/src/otherrepo", state=ca.HELD
    )
    mocker.patch.object(ca, "holders", side_effect=[{}, {PERSONAL.identity: winner}])
    mocker.patch.object(db_session, "commit", side_effect=_integrity_error())

    with pytest.raises(ca.PoolError) as exc:
        ca.use(db_session, cswap, ACCOUNTS, prefix="gisgro", ref="2", now=NOW)

    assert "otherrepo" in str(exc.value)
    assert cswap.switch_calls == []


# --- use: the --force confirmation comes after the refusals -----------


def test_the_force_prompt_is_not_reached_when_the_account_is_held_elsewhere(db_session):
    """Being asked to accept a risk on a command that then declines is worse
    than either outcome, so every ledger refusal precedes the prompt."""
    _hold(db_session, PERSONAL, "otherrepo")
    asked: list[str] = []

    def confirm(live) -> bool:
        asked.append(live.email or "")
        return True

    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    with pytest.raises(ca.PoolError):
        ca.use(
            db_session,
            cswap,
            ACCOUNTS,
            prefix="gisgro",
            ref="2",
            now=NOW,
            force=True,
            confirm=confirm,
        )

    assert asked == []
    assert cswap.status_calls == 0, "and the live login is not even probed"
    assert cswap.switch_calls == []


def test_the_force_prompt_is_not_reached_when_the_account_is_not_allowed(db_session):
    ca.set_allowed(db_session, "gisgro", {WORK.identity})
    asked: list[str] = []
    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    with pytest.raises(ca.PoolError):
        ca.use(
            db_session,
            cswap,
            ACCOUNTS,
            prefix="gisgro",
            ref="2",
            now=NOW,
            force=True,
            confirm=lambda live: bool(asked.append(live.email or "")) or True,
        )

    assert asked == []


def test_declining_the_force_prompt_cancels_and_switches_nothing(db_session):
    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    with pytest.raises(ca.PoolCancelledError) as exc:
        ca.use(
            db_session,
            cswap,
            ACCOUNTS,
            prefix="gisgro",
            ref="2",
            now=NOW,
            force=True,
            confirm=lambda live: False,
        )

    assert isinstance(exc.value, ca.PoolError), "a cancellation is still a PoolError"
    assert cswap.switch_calls == []
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None


def test_a_forced_use_probes_the_live_login_exactly_once(db_session):
    """The prompt used to run in `cli.py` off its own `cswap.status()`, so a
    `--force` switch paid for two probes."""
    cswap = FakeCswap(live=_live("stray@example.com", managed=False))

    ca.use(
        db_session,
        cswap,
        ACCOUNTS,
        prefix="gisgro",
        ref="2",
        now=NOW,
        force=True,
        confirm=lambda live: True,
    )

    assert cswap.status_calls == 1
    assert cswap.switch_calls == ["2"]


# --- capture: `jailbee claude add` ------------------------------------


def test_ensure_capture_allowed_passes_when_nobody_holds_the_identity(db_session):
    ca.ensure_capture_allowed(db_session, PERSONAL.identity, prefix="gisgro")


def test_ensure_capture_allowed_passes_when_this_repo_already_holds_it(db_session):
    """Re-capturing an account this repo holds only refreshes its own copy."""
    _hold(db_session, PERSONAL, "gisgro")

    ca.ensure_capture_allowed(db_session, PERSONAL.identity, prefix="gisgro")


def test_ensure_capture_allowed_refuses_when_another_repo_holds_the_identity(db_session):
    """Re-capturing overwrites the stored blob with THIS repo's lineage, so the
    holding repo's row would point at a credential it never got."""
    _hold(db_session, PERSONAL, "otherrepo")

    with pytest.raises(ca.PoolError) as exc:
        ca.ensure_capture_allowed(db_session, PERSONAL.identity, prefix="gisgro")

    message = str(exc.value)
    assert "otherrepo" in message
    assert PERSONAL.email in message
    assert "overwrite" in message
    assert f"jailbee claude release {PERSONAL.email}" in message, (
        "no listing was read, so the ref has to be the email, not a slot number"
    )


def test_claim_captured_records_a_held_row_for_this_repo(db_session):
    """Without this row the invariant is unenforced until this repo's first
    `use`: holders() is empty, every refusal passes, and the next repo to `use`
    the account lands a second live copy of one stored grant."""
    cswap = FakeCswap(live=_live(PERSONAL.email, managed=True, number=2, org_uuid=""))

    record = ca.claim_captured(
        db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=None, now=NOW
    )

    assert record.slot == "2", "the slot is read back from cswap, not guessed"
    assert record.taken_from is None
    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None
    assert (row.container_prefix, row.state, row.slot, row.since) == ("gisgro", ca.HELD, "2", NOW)


def test_claim_captured_falls_back_to_the_explicit_slot_when_cswap_cannot_be_read(db_session):
    """The capture already happened: a failed slot read-back must not cost the
    ledger row, which is the part that matters."""
    from jailbee.cswap import CswapError

    cswap = FakeCswap(live=None, status_error=CswapError("keychain locked"))

    record = ca.claim_captured(
        db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=7, now=NOW
    )

    assert record.slot == "7"
    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None and row.state == ca.HELD


def test_claim_captured_takes_over_a_row_that_named_another_repo(db_session):
    """The TOCTOU window between `ensure_capture_allowed` and here. The stored
    blob is now this repo's lineage, so recording this repo is the truth —
    leaving the row elsewhere would hand that repo a grant this one is live
    on."""
    _hold(db_session, PERSONAL, "otherrepo")
    cswap = FakeCswap(live=_live(PERSONAL.email, managed=True, number=2, org_uuid=""))

    record = ca.claim_captured(
        db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=None, now=NOW
    )

    row = db_session.get(ClaudeAccountHolding, PERSONAL.identity)
    assert row is not None and row.container_prefix == "gisgro"
    assert record.taken_from == "otherrepo", "the displaced repo is reported, not swallowed"


def test_claim_captured_keys_the_row_on_the_identity_cswap_reports_after_the_capture(db_session):
    """Before the capture, `cswap status` may report an unmatched login with no
    `organizationUuid`, so the caller's identity can be ("email", "") even for
    an organization account. Writing the row under that guess would key it on
    an account that does not exist and `holders()` would never match the real
    one — the same hole this function exists to close."""
    guessed = (WORK.email, "")
    cswap = FakeCswap(live=_live(WORK.email, managed=True, number=1, org_uuid="org-abc"))

    record = ca.claim_captured(db_session, cswap, guessed, prefix="gisgro", slot=None, now=NOW)

    assert record.identity == WORK.identity
    assert db_session.get(ClaudeAccountHolding, guessed) is None, "not under the guess"
    row = db_session.get(ClaudeAccountHolding, WORK.identity)
    assert row is not None and row.container_prefix == "gisgro"


def test_claim_captured_keeps_the_callers_identity_when_the_email_disagrees(db_session):
    """A different email means cswap is talking about something else; the row
    still has to exist for the login that was actually captured."""
    cswap = FakeCswap(live=_live(CI.email, managed=True, number=3, org_uuid=""))

    record = ca.claim_captured(
        db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=5, now=NOW
    )

    assert record.identity == PERSONAL.identity
    assert record.slot == "5", "and the slot read-back is not trusted either"
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is not None


def test_claim_captured_losing_the_insert_race_refuses_without_a_traceback(db_session, mocker):
    """Two repos capture the SAME identity at once: both pre-checks pass, both
    `cswap add` runs land, and the loser's INSERT hits the composite primary
    key. It must not be a raw IntegrityError — and the message must not imply
    the capture failed, because it did not."""
    cswap = FakeCswap(live=_live(PERSONAL.email, managed=True, number=2, org_uuid=""))
    winner = ca.Holder(
        container_prefix="otherrepo", repo_root="/home/x/src/otherrepo", state=ca.HELD
    )
    mocker.patch.object(ca, "holders", return_value={PERSONAL.identity: winner})
    mocker.patch.object(db_session, "commit", side_effect=_integrity_error())

    with pytest.raises(ca.PoolError) as exc:
        ca.claim_captured(db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=None, now=NOW)

    message = str(exc.value)
    assert "otherrepo" in message, "the winner is named"
    assert "capture itself succeeded" in message, "the capture did happen; say so"
    assert "jailbee claude add" in message, "and how to record the holding after"


def test_claim_captured_losing_to_its_own_repo_is_not_a_failure(db_session, mocker):
    """A concurrent `add` from this same repo produces exactly the end state
    this call wanted, so refusing would be a lie about an achieved outcome."""
    # A distinct slot on the stored row, so "read back off the winning row" is
    # actually discriminating: the status probe would have said "2".
    db_session.add(
        ClaudeAccountHolding(
            email=PERSONAL.email,
            org_uuid=PERSONAL.org_uuid,
            container_prefix="gisgro",
            slot="9",
            state=ca.HELD,
            since=NOW,
        )
    )
    db_session.commit()
    cswap = FakeCswap(live=_live(PERSONAL.email, managed=True, number=2, org_uuid=""))
    mine = ca.Holder(container_prefix="gisgro", repo_root=None, state=ca.HELD)
    mocker.patch.object(ca, "holders", return_value={PERSONAL.identity: mine})
    mocker.patch.object(db_session, "commit", side_effect=_integrity_error())

    record = ca.claim_captured(
        db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=None, now=NOW
    )

    assert record.identity == PERSONAL.identity
    assert record.taken_from is None
    assert record.slot == "9", "read back off the winning row, not from the probe"


def test_claim_captured_race_that_resolves_itself_says_to_re_run(db_session, mocker):
    """The conflicting row is gone again by the time the ledger is re-read, so
    nothing holds the account and a plain re-run records it."""
    cswap = FakeCswap(live=_live(PERSONAL.email, managed=True, number=2, org_uuid=""))
    mocker.patch.object(ca, "holders", return_value={})
    mocker.patch.object(db_session, "commit", side_effect=_integrity_error())

    with pytest.raises(ca.PoolError) as exc:
        ca.claim_captured(db_session, cswap, PERSONAL.identity, prefix="gisgro", slot=None, now=NOW)

    assert "Nothing holds the account now" in str(exc.value)


# --- release ----------------------------------------------------------


def test_release_repo_frees_this_repos_holding(db_session):
    _hold(db_session, WORK, "gisgro")
    _hold(db_session, PERSONAL, "otherrepo")

    freed = ca.release_repo(db_session, "gisgro")

    assert [r.email for r in freed] == [WORK.email]
    assert db_session.get(ClaudeAccountHolding, WORK.identity) is None
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is not None, (
        "another repo's holding is untouched"
    )


def test_release_repo_returns_nothing_when_this_repo_holds_nothing(db_session):
    assert ca.release_repo(db_session, "gisgro") == []


def test_release_repo_frees_every_row_left_by_a_crash_mid_claim(db_session):
    """A crash between phase 1 and phase 2 of `use` leaves this repo holding
    two rows: its old `held` one and the new `claiming` one. `.first()` would
    have deleted an arbitrary one and reported success, stranding the other."""
    _hold(db_session, WORK, "gisgro", state=ca.HELD)
    _hold(db_session, PERSONAL, "gisgro", state=ca.CLAIMING)
    _hold(db_session, CI, "otherrepo")

    freed = ca.release_repo(db_session, "gisgro")

    assert {r.email for r in freed} == {WORK.email, PERSONAL.email}, "both are reported"
    assert db_session.get(ClaudeAccountHolding, WORK.identity) is None
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None
    assert db_session.get(ClaudeAccountHolding, CI.identity) is not None


def test_release_repo_also_clears_a_stale_claiming_row(db_session):
    _hold(db_session, WORK, "gisgro", state=ca.CLAIMING)

    assert ca.release_repo(db_session, "gisgro") is not None
    assert db_session.get(ClaudeAccountHolding, WORK.identity) is None


def test_release_identity_frees_a_holding_wherever_it_is(db_session):
    """The escape hatch for a repo whose checkout is gone."""
    _hold(db_session, PERSONAL, "goneaway")

    freed = ca.release_identity(db_session, PERSONAL.identity)

    assert freed is not None and freed.container_prefix == "goneaway"
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None


def test_release_identity_returns_none_when_nobody_holds_it(db_session):
    assert ca.release_identity(db_session, PERSONAL.identity) is None


# --- remove -----------------------------------------------------------


def test_remove_drops_the_pool_entry_and_every_ledger_row(db_session):
    _hold(db_session, PERSONAL, "gisgro")
    ca.set_allowed(db_session, "gisgro", {PERSONAL.identity, WORK.identity})
    calls: list[str] = []

    class Remover:
        def remove(self, ref: str) -> None:
            calls.append(ref)

    ca.remove(db_session, Remover(), PERSONAL)

    assert calls == ["2"], "cswap is told the slot, jailbee keeps the identity"
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is None
    assert ca.allowed_identities(db_session, "gisgro") == {WORK.identity}


def test_remove_keeps_the_ledger_when_cswap_refuses(db_session):
    from jailbee.cswap import CswapError

    _hold(db_session, PERSONAL, "gisgro")

    class Refuser:
        def remove(self, ref: str) -> None:
            raise CswapError("cancelled")

    with pytest.raises(CswapError):
        ca.remove(db_session, Refuser(), PERSONAL)

    db_session.expire_all()
    assert db_session.get(ClaudeAccountHolding, PERSONAL.identity) is not None, (
        "an account still in the pool must still have its holding"
    )


# --- stale claims -----------------------------------------------------


def test_stale_claims_lists_only_claiming_rows(db_session):
    _hold(db_session, WORK, "gisgro", state=ca.HELD)
    _hold(db_session, PERSONAL, "otherrepo", state=ca.CLAIMING)

    stale = ca.stale_claims(db_session)

    assert [r.email for r in stale] == [PERSONAL.email]


def test_stale_claims_is_empty_when_the_ledger_is_clean(db_session):
    _hold(db_session, WORK, "gisgro")

    assert ca.stale_claims(db_session) == []
