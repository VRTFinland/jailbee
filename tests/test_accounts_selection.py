"""Tests for cross-adapter account selection (`accounts/selection.py`).

Moved here from `test_cli_account.py` when the rules left `cli.py`: none of
this is argument parsing, and `choose`'s TTY rule is exercised by injecting the
two callables rather than by patching the terminal.
"""

from pathlib import Path

import pytest

from jailbee.accounts import selection
from jailbee.accounts.models import PoolError, Slot
from jailbee.global_config import GlobalConfig
from tests.conftest import NamedAdapter, patch_list_slots


@pytest.fixture
def repo(tmp_path, mocker, make_cfg):
    """A loaded repo config with a tmp shared_dir."""
    return make_cfg(tmp_path / "app", shared_dir=tmp_path / "shared", claude={"enabled": True})


def test_matching_choices_offers_every_parked_slot_of_every_adapter(repo, mocker):
    """No reference means every parked login of every selected adapter is on
    offer, and each choice carries the adapter that holds it."""

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [
                Slot("a@x.com", Path("/s/a.json"), live=False),
                Slot("live-a@x.com", Path("/h/a.json"), live=True),
            ],
            "fakeb": [Slot("b@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = selection.matching_choices(
        [fakea, fakeb], repo, GlobalConfig(), None, removable=False
    )

    # The live login is not a candidate: `switch` would refuse it.
    assert [(a.name, s.name) for a, s in choices] == [("fakea", "a@x.com"), ("fakeb", "b@x.com")]


def test_matching_choices_resolves_a_ref_in_one_adapter(repo, mocker):
    """A reference that matches one adapter selects it, by `resolve_ref`'s own
    exact-name-then-unique-email rule."""

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [Slot("a@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("b@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = selection.matching_choices(
        [fakea, fakeb], repo, GlobalConfig(), "a@x.com", removable=False
    )

    assert [(a.name, s.name) for a, s in choices] == [("fakea", "a@x.com")]


def test_matching_choices_keeps_a_ref_that_matches_two_adapters(repo, mocker):
    """One email parked under two agents is two successful matches, not a
    failure: the caller decides between them."""

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [Slot("me@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = selection.matching_choices(
        [fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False
    )

    assert [(a.name, s.name) for a, s in choices] == [
        ("fakea", "me@x.com"),
        ("fakeb", "me@x.com"),
    ]


def test_matching_choices_does_not_suppress_an_ambiguity_inside_one_adapter(repo, mocker):
    """Two grants of one email in one adapter are not solved by `-a`, so
    `resolve_ref`'s full-slot-name error is kept even when another adapter
    matched: silently picking the other agent could act on the wrong login."""

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [
                Slot("me@x.com#aaaa", Path("/s/a1.json"), live=False),
                Slot("me@x.com#bbbb", Path("/s/a2.json"), live=False),
            ],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )

    with pytest.raises(PoolError, match="matches several accounts"):
        selection.matching_choices(
            [fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False
        )


def test_matching_choices_raises_when_a_ref_matches_nothing(repo, mocker):
    """A typed reference with no match is an error, not an empty picker: only
    the caller knows whether the reference was typed or picked."""

    fakea = NamedAdapter("fakea")
    patch_list_slots(mocker, {"fakea": [Slot("a@x.com", Path("/s/a.json"), live=False)]})

    with pytest.raises(PoolError, match="no stored account matches"):
        selection.matching_choices([fakea], repo, GlobalConfig(), "nope@x.com", removable=False)


def test_account_adapters_without_an_agent_returns_every_pooled_one(repo):
    """No `-a` is normal: the command spans every enabled agent, `claude`
    first."""
    from jailbee.accounts.adapters import base
    from tests.conftest import with_agent

    cfg = with_agent(repo, "claude", enabled=True, command="claude")
    cfg = with_agent(cfg, "fakea", enabled=True, command="fakea")
    base.register(NamedAdapter("fakea"))
    try:
        assert [a.name for a in selection.adapters_for(cfg, None)] == ["claude", "fakea"]
    finally:
        base.ADAPTERS.pop("fakea", None)


def test_account_adapters_with_an_agent_returns_only_that_one(repo):
    """`-a` narrows before any store is read, so a reference is resolved
    against one adapter's slots."""
    from jailbee.accounts.adapters import base
    from tests.conftest import with_agent

    cfg = with_agent(repo, "claude", enabled=True, command="claude")
    cfg = with_agent(cfg, "fakea", enabled=True, command="fakea")
    base.register(NamedAdapter("fakea"))
    try:
        assert [a.name for a in selection.adapters_for(cfg, "fakea")] == ["fakea"]
    finally:
        base.ADAPTERS.pop("fakea", None)


def test_account_adapters_refuses_an_agent_with_no_adapter(repo):
    """A name jailbee has no adapter for is a typo or a build without that
    agent's module; the refusal says so instead of silently acting on every
    agent, and names what may be passed."""

    with pytest.raises(PoolError, match="agent `nope` has no account pool"):
        selection.adapters_for(repo, "nope")


def test_account_adapters_accepts_an_agent_this_repo_does_not_enable(repo):
    """The pool is host-wide: the parked store and the group holders live under
    `XDG_DATA_HOME`, not in the repo, so `-a claude` in a repo that happens to
    keep Claude off still names a real pool. Gating this on `enabled` broke
    every `jailbee claude ...` alias for exactly those repos — all four of them
    worked before the generic rewrite."""
    from jailbee.accounts.adapters import base
    from tests.conftest import with_agent

    disabled = with_agent(repo, "fakea", enabled=False, command="fakea")
    base.register(NamedAdapter("fakea"))
    try:
        assert [a.name for a in selection.adapters_for(disabled, "fakea")] == ["fakea"]
    finally:
        base.ADAPTERS.pop("fakea", None)


def test_account_adapters_without_an_agent_still_honours_enabled(repo):
    """The other half: omitting `-a` means "every *enabled* pooled agent", the
    repo-scoped reading, so a disabled agent must not be acted on implicitly."""
    from jailbee.accounts.adapters import base
    from tests.conftest import with_agent

    disabled = with_agent(repo, "fakea", enabled=False, command="fakea")
    base.register(NamedAdapter("fakea"))
    try:
        assert "fakea" not in [a.name for a in selection.adapters_for(disabled, None)]
    finally:
        base.ADAPTERS.pop("fakea", None)


def test_a_ref_across_two_adapters_opens_the_picker_on_a_tty(repo, mocker):

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [Slot("me@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )
    choices = selection.matching_choices(
        [fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False
    )
    pick = mocker.Mock(return_value=("fakeb", "me@x.com"))

    choice = selection.choose(
        choices,
        ref="me@x.com",
        nothing="none",
        message="Switch:",
        picker=pick,
        is_interactive=lambda: True,
    )

    assert choice is not None
    assert (choice[0].name, choice[1].name) == ("fakeb", "me@x.com")
    assert pick.call_args.args[0] == choices
    assert pick.call_args.args[1] == "Switch:"


def test_a_ref_across_two_adapters_off_a_tty_names_the_agent_options(repo, mocker):
    """A script cannot answer a picker, so the failure names the `-a` values
    it should have passed."""

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [Slot("me@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )
    choices = selection.matching_choices(
        [fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False
    )
    pick = mocker.Mock()

    with pytest.raises(PoolError) as e:
        selection.choose(
            choices,
            ref="me@x.com",
            nothing="none",
            message="Switch:",
            picker=pick,
            is_interactive=lambda: False,
        )

    assert "-a fakea" in str(e.value)
    assert "-a fakeb" in str(e.value)
    pick.assert_not_called()


def test_live_choices_selects_the_one_adapter_with_a_login(repo, mocker):
    """`park`'s candidates: an adapter with no live credential contributes
    nothing, and a lone login needs no prompt."""

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [Slot("live-a@x.com", Path("/h/a.json"), live=True)],
            "fakeb": [Slot("b@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = selection.live_choices([fakea, fakeb], repo, GlobalConfig())

    assert [(a.name, s.name) for a, s in choices] == [("fakea", "live-a@x.com")]
    pick = mocker.Mock()
    choice = selection.choose(
        choices,
        ref=None,
        nothing="nothing",
        message="Park:",
        picker=pick,
        is_interactive=lambda: False,
    )
    assert choice is not None
    assert choice[0].name == "fakea"
    pick.assert_not_called()


def test_live_choices_picks_when_several_adapters_have_one(repo, mocker):

    fakea = NamedAdapter("fakea")
    fakeb = NamedAdapter("fakeb")
    patch_list_slots(
        mocker,
        {
            "fakea": [Slot("a@x.com", Path("/h/a.json"), live=True)],
            "fakeb": [Slot("b@x.com", Path("/h/b.json"), live=True)],
        },
    )
    choices = selection.live_choices([fakea, fakeb], repo, GlobalConfig())
    pick = mocker.Mock(return_value=("fakeb", "b@x.com"))

    choice = selection.choose(
        choices,
        ref=None,
        nothing="nothing",
        message="Park:",
        picker=pick,
        is_interactive=lambda: True,
    )

    assert choice is not None
    assert (choice[0].name, choice[1].name) == ("fakeb", "b@x.com")
    assert [s.name for _, s in pick.call_args.args[0]] == ["a@x.com", "b@x.com"]
