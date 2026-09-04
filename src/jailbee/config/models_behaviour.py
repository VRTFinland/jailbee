"""CLI-behaviour policy models: confirmation, pull/push/new/destroy/boot
defaults, and the per-container resource defaults block.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from jailbee.config.models_net import _reject_offline


class ConfirmConfig(BaseModel):
    """Policy for confirming a bridge operation whose target jailbee chose itself.

    ``jailbee git push`` / ``pull`` / ``checkout`` settle on the single existing
    container without showing a picker. Nothing then states which host branch
    travels or which container branch it lands on — both can come from config
    (``push.default_source``, ``push.push_from``) or from the container's
    ``user.jailbee.base_branch`` label rather than from the command line.

    Off a TTY, ``pull``/``checkout`` still print the plan block and only skip
    the prompt; ``push`` requires an explicit container name off a TTY in the
    first place, so it never reaches this confirmation there.
    """

    model_config = ConfigDict(extra="forbid")
    auto_target: bool = Field(
        default=True,
        description=(
            "Print a plan block (branch names, tips, action) and ask before mutating "
            "anything when `jailbee git push`/`pull`/`checkout` auto-picked the target "
            "container. `false` skips the prompt. Overridable per-invocation with "
            "`--confirm`/`--no-confirm`."
        ),
    )


class PullConfig(BaseModel):
    """Policy for `jailbee git pull`'s post-merge cleanup prompts, run after
    merging a container's branch into its recorded base branch.
    """

    model_config = ConfigDict(extra="forbid")
    destroy_container: Literal["prompt", "always", "never"] = Field(
        default="prompt",
        description=(
            "Whether to destroy the container after a successful merge. `prompt` asks "
            "each time; `always`/`never` decide without asking. `--cleanup`/"
            "`--no-cleanup` on the CLI force both keys."
        ),
    )
    delete_branch: Literal["prompt", "always", "never"] = Field(
        default="prompt",
        description=(
            "Whether to delete the merged local host branch after a successful merge. "
            "`prompt` asks each time; `always`/`never` decide without asking."
        ),
    )


class PushConfig(BaseModel):
    """Policy for `jailbee git push`'s interactive default-picker.

    Layered: ``~/.config/jailbee/global.yaml`` is the user-wide default;
    ``<repo>/.jailbee/config.yaml`` may override per-repo via the standard
    deep-merge pipeline. The host's local ``refs/heads/<base>`` only
    advances on ``git pull``, so for any branch the user does not check out
    (typically the base branch) the remote-tracking ref is the fresher one
    — hence ``push_from``'s default. ``--pr`` and ``--current`` always
    resolve locally regardless of these keys; see `jailbee git push --help`.
    """

    model_config = ConfigDict(extra="forbid")
    default_action: Literal["merge", "rebase", "plain", "ask"] = Field(
        default="ask",
        description=(
            "What `jailbee git push` does after pushing the ref. `merge`/`rebase` "
            "integrate it into the base branch; `plain` is transport-only. `ask` "
            "(default) opens an interactive picker; CLI flags (`--merge`/`--rebase`/"
            "`--plain`) always win."
        ),
    )
    default_source: Literal["default-branch", "current", "base", "ask"] = Field(
        default="base",
        description=(
            "Which branch to push. `base` (default) resolves to each container's "
            "recorded base branch (`user.jailbee.base_branch`); `default-branch` always "
            "uses the repo's default branch; `current` uses the host's checked-out "
            "branch; `ask` opens an interactive picker every time."
        ),
    )
    push_from: Literal["local", "origin"] = Field(
        default="origin",
        description=(
            "Which copy of the source branch to push. `origin` (default) sends "
            "`refs/remotes/origin/<source>`, falling back to `refs/heads/<source>` when "
            "there's no upstream copy; `local` reverses that fallback order."
        ),
    )
    autofetch: bool = Field(
        default=True,
        description=(
            "Run `git fetch origin <source>` on the host before resolving the ref, so "
            "the remote-tracking copy is current. Only applies in `push_from: origin` "
            "mode; best-effort, so a failed fetch does not block the push."
        ),
    )


class NewConfig(BaseModel):
    """Policy for `jailbee new`'s starting-point selection.

    Applies to the default-branch fallback path (no --base, branch does
    not yet exist in the source repo). The `--base` path always uses
    local refs and skips autofetch; `--pr` performs its own up-front
    fetch via `gh` and is unaffected.
    """

    model_config = ConfigDict(extra="forbid")
    clone_from: Literal["local", "origin"] = Field(
        default="origin",
        description=(
            "Starting point when the requested branch doesn't yet exist in the source "
            "repo. `origin` (default) checks out `refs/remotes/origin/<default_branch>`, "
            "reflecting the upstream tip; `local` uses the host's local "
            "`refs/heads/<default_branch>`."
        ),
    )
    autofetch: bool = Field(
        default=True,
        description=(
            "With `clone_from: origin`, run `git fetch origin <default_branch>` on the "
            "host before resolving the ref, so a stale host doesn't propagate into the "
            "container. `false` relies on whatever the host already has."
        ),
    )
    background: bool = Field(
        default=False,
        description=(
            "Run `jailbee new` detached in the background by default. Overridable "
            "per-invocation with `--background`/`--no-background`."
        ),
    )
    submodules: bool = Field(
        default=True,
        description=(
            "Initialize the superproject's git submodules (recursively, offline from "
            "the host bind mount) in the new container. `false` skips this."
        ),
    )


class DestroyConfig(BaseModel):
    """Policy for `jailbee destroy`."""

    model_config = ConfigDict(extra="forbid")
    background: bool = Field(
        default=False,
        description=(
            "Run `jailbee destroy` detached in the background by default. Overridable "
            "per-invocation with `--background`/`--no-background`."
        ),
    )


class BootConfig(BaseModel):
    """Policy for `jailbee start` and `jailbee restart`.

    One key for both: what makes either slow is the autostart run that
    follows the boot, and it is the same run.
    """

    model_config = ConfigDict(extra="forbid")
    background: bool = Field(
        default=False,
        description=(
            "Run `jailbee start`/`jailbee restart` detached in the background by "
            "default. Overridable per-invocation with `--background`/`--no-background`."
        ),
    )


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory: str = Field(
        default="16GiB",
        description="Memory limit for new containers, as an Incus size string (e.g. `16GiB`).",
    )
    cpu: int = Field(
        default=8,
        description="CPU core limit for new containers.",
    )
    network: Literal["strict", "loose"] = Field(
        default="strict",
        description=(
            "Initial network mode for new containers: `strict` (default-deny egress "
            "allowlist) or `loose` (wider egress for debugging)."
        ),
    )
    storage_pool: str = Field(
        default="default",
        description="Incus storage pool new containers are created on.",
    )

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        return _reject_offline(v)
