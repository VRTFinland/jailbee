"""The repo's agent picker, frontend-agnostic: its choice list and label text.

`jailbee agent use` asks profile, then tier, through `jailbee.prompting`, so the
same two questions are a terminal picker at a terminal and a modal in a
dashboard. This module builds the rows of both questions and the compact
``profile/model`` label the repo row shows. The state is read elsewhere; nothing
here touches config or a container, so a per-container picker can reuse it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.prompting import Option

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from jailbee.orchestration.runner_model import Runner

PLAIN = "claude"
DEFAULT_TIER = ""  # picker sentinel: a `None` value would read as a cancel
CURRENT = " (current)"
REPO_DEFAULT = "(repo default)"  # picker sentinel: a container with no choice of its own


def split_current(runner: Runner | None) -> tuple[str | None, str | None]:
    """(profile, tier) of ``runner``; plain Claude is the profile ``claude``."""
    if runner is None:
        return None, None
    return runner.profile or PLAIN, runner.model


def runner_label(runner: Runner) -> str:
    """``claude/opus``, ``codex/sonnet``, or just ``codex`` when no tier is set."""
    profile, model = split_current(runner)
    return f"{profile}/{model}" if model else str(profile)


def profile_options(
    offered: Mapping[str, Sequence[str]], current: str | None, *, repo_default: bool = False
) -> list[Option[str]]:
    """One row per offered profile (the config's served ones), the current marked.

    ``repo_default`` (the per-container picker) puts a "repo default" row first;
    it is the current one when ``current`` is None.
    """
    head = (
        [
            Option(
                REPO_DEFAULT,
                "repo default" + (CURRENT if current is None else ""),
                "repo default",
                "drop this container's own choice and follow the repo",
            )
        ]
        if repo_default
        else []
    )
    return head + [
        Option(
            name,
            name + (CURRENT if name == current else ""),
            name,
            "plain Claude" if name == PLAIN else "claude-jb through this LiteLLM profile",
        )
        for name in offered
    ]


def tier_options(tiers: Sequence[str], current: str | None) -> list[Option[str]]:
    """ "profile default" plus only the tiers the profile maps, the current marked."""
    chosen = current or DEFAULT_TIER

    def mark(value: str, title: str) -> str:
        return title + (CURRENT if value == chosen else "")

    return [
        Option(
            DEFAULT_TIER,
            mark(DEFAULT_TIER, "profile default"),
            "default",
            "the profile's own default tier",
        ),
        *(Option(t, mark(t, t), t) for t in tiers),
    ]
