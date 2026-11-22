"""PR numbers render as OSC 8 hyperlinks built from the repo's GitHub slug."""

from __future__ import annotations

import io
from datetime import UTC, datetime

import pytest
from rich.console import Console
from rich.style import Style
from rich.text import Text

from jailbee.dashboard import details as dd
from jailbee.dashboard import format as dfmt
from jailbee.lifecycle import ContainerInfo, ls_field_specs

NOW = datetime(2026, 11, 22, tzinfo=UTC)
URL = "https://github.com/acme/widgets/pull/12"


def _c(**kw: object) -> ContainerInfo:
    base: dict[str, object] = {
        "name": "p-x",
        "state": "Running",
        "network": "strict",
        "ip": None,
        "memory_limit": None,
        "repo": "p",
        "pr_number": 12,
        "pr_author": True,
        "pr_slug": "acme/widgets",
    }
    base.update(kw)
    return ContainerInfo(**base)  # type: ignore[arg-type]


def _spec():
    return next(s for s in ls_field_specs(now=NOW) if s.name == "pr")


@pytest.fixture(autouse=True)
def _links_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JAILBEE_NO_HYPERLINKS", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")


def _link_of(markup: str) -> str | None:
    links = [
        Style.parse(str(span.style)).link
        for span in Text.from_markup(markup).spans
        if "link " in str(span.style)
    ]
    return links[0] if links else None


def test_pr_cell_links_number_to_github() -> None:
    markup = dfmt.dashboard_cell(_spec(), _c(), NOW)
    assert Text.from_markup(markup).plain == "#12"
    assert _link_of(markup) == URL


def test_review_container_keeps_arrow_and_links() -> None:
    markup = dfmt.dashboard_cell(_spec(), _c(pr_author=False), NOW)
    assert Text.from_markup(markup).plain == "#12↓"
    assert _link_of(markup) == URL


def test_osc8_reaches_the_terminal() -> None:
    out = io.StringIO()
    Console(file=out, force_terminal=True, color_system="standard").print(
        Text.from_markup(dfmt.dashboard_cell(_spec(), _c(), NOW))
    )
    assert "\x1b]8;id=" in out.getvalue() and URL in out.getvalue()


@pytest.mark.parametrize("slug", [None, "not a slug", "a/b]c[", "../x/y"])
def test_no_valid_slug_means_plain_text(slug: str | None) -> None:
    markup = dfmt.dashboard_cell(_spec(), _c(pr_slug=slug), NOW)
    assert markup == "#12"


def test_env_switch_disables_links(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAILBEE_NO_HYPERLINKS", "1")
    assert dfmt.dashboard_cell(_spec(), _c(), NOW) == "#12"


def test_dumb_terminal_gets_plain_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TERM", "dumb")
    assert dfmt.dashboard_cell(_spec(), _c(), NOW) == "#12"


def test_no_pr_no_cell() -> None:
    assert dfmt.dashboard_cell(_spec(), _c(pr_number=None), NOW) == ""


def test_card_pr_line_is_linked() -> None:
    c = _c()
    panel = dd.container_panel(c, NOW)
    assert _link_of(" ".join(panel.summary)) == URL


def test_table_cell_keeps_link_and_width() -> None:
    from jailbee.dashboard.tui.fleet import _cell

    text = _cell(dfmt.dashboard_cell(_spec(), _c(), NOW), 6, "left")
    assert text.cell_len == 6
    assert text.plain.rstrip() == "#12"
    assert any("link " in str(span.style) for span in text.spans)
