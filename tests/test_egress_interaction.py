"""Terminal prompts for `jailbee net egress add` / `rm`, through the prompting seams."""

from __future__ import annotations

import pytest

from jailbee import egress_interaction as interaction


@pytest.fixture
def terminal(mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)


def test_add_returns_the_typed_destination(mocker, terminal):
    mocker.patch("jailbee.prompting._ask", return_value="example.com")

    assert interaction.prompt_add_entry() == "example.com"


def test_add_asks_with_the_existing_destination_question(mocker, terminal):
    ask = mocker.patch("jailbee.prompting._ask", return_value="example.com")

    interaction.prompt_add_entry()

    noun = ask.call_args.args[0]
    assert f"{noun[:1].upper()}{noun[1:]}:" == (
        "Destination (host, host:port, *.domain, IPv4, or CIDR):"
    )


def test_add_reasks_an_invalid_destination_until_it_is_valid(mocker, terminal):
    mocker.patch("jailbee.prompting._ask", side_effect=["bad entry!!", "example.com"])

    assert interaction.prompt_add_entry() == "example.com"


def test_add_cancel_returns_none(mocker, terminal):
    mocker.patch("jailbee.prompting._ask", return_value=None)

    assert interaction.prompt_add_entry() is None


def test_pick_remove_returns_the_selected_override(mocker, terminal):
    mocker.patch("jailbee.prompting._select", return_value="a.example")

    assert interaction.pick_remove_entry(["a.example", "b.example"], scope="repo") == "a.example"


def test_pick_remove_shows_the_existing_question_even_for_one_entry(mocker, terminal):
    """Removal is destructive: a lone override is still asked about, never auto-taken."""
    select = mocker.patch("jailbee.prompting._select", return_value="a.example")

    assert interaction.pick_remove_entry(["a.example"], scope="container") == "a.example"
    assert select.call_args.args[0] == "container override to remove"


def test_pick_remove_cancel_returns_none(mocker, terminal):
    mocker.patch("jailbee.prompting._select", return_value=None)

    assert interaction.pick_remove_entry(["a.example"], scope="repo") is None


def test_pick_remove_with_nothing_stored_asks_nothing(mocker, terminal):
    select = mocker.patch("jailbee.prompting._select")

    assert interaction.pick_remove_entry([], scope="repo") is None
    select.assert_not_called()
