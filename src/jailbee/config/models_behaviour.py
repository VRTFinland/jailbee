"""CLI-behaviour policy models: confirmation, pull/push/new/destroy/boot
defaults, and the per-container resource defaults block.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from jailbee.config.models_net import _reject_offline


class ConfirmConfig(BaseModel):
    """Policy for confirming a bridge operation whose target jailbee chose itself.

    ``jailbee git push`` / ``pull`` / ``checkout`` settle on the single existing
    container without showing a picker. Nothing then states which host branch
    travels or which container branch it lands on — both can come from config
    (``push.default_source``, ``push.push_from``) or from the container's
    ``user.jailbee.base_branch`` label rather than from the command line.

    With ``auto_target`` on (the default), those commands print a plan block and
    ask before mutating anything. Overridable per invocation with
    ``--confirm`` / ``--no-confirm``. Off a TTY, ``pull``/``checkout`` still
    print the block and only skip the prompt; ``push`` requires an explicit
    container name off a TTY in the first place, so it never reaches this
    confirmation there.
    """

    model_config = ConfigDict(extra="forbid")
    auto_target: bool = True


class PullConfig(BaseModel):
    """Policy for `jailbee git pull`'s post-merge cleanup prompts.

    Each step independently controls whether the cleanup runs always,
    never, or prompts the user interactively (default).
    """

    model_config = ConfigDict(extra="forbid")
    destroy_container: Literal["prompt", "always", "never"] = "prompt"
    delete_branch: Literal["prompt", "always", "never"] = "prompt"


class PushConfig(BaseModel):
    """Policy for `jailbee git push`'s interactive default-picker.

    Both keys may be ``"ask"`` to open an interactive prompt instead
    of using a baked-in default. Layered: ``~/.config/jailbee/global.yaml``
    is the user-wide default; ``<repo>/.jailbee/config.yaml`` may override
    per-repo via the standard deep-merge pipeline.

    ``default_source`` values:

    * ``"base"`` *(default)* — resolve to each container's recorded base
      branch (``user.jailbee.base_branch`` Incus metadata label), so the host
      pushes exactly what was branched from, per container.
    * ``"default-branch"`` — always use the repo's default branch (e.g.
      ``main``), regardless of which branch the container is on.
    * ``"current"`` — use the host's currently checked-out branch.
    * ``"ask"`` — open an interactive picker every time.

    ``default_source`` picks *which branch*; ``push_from`` picks *which
    copy of it*:

    * ``"origin"`` *(default)* — push ``refs/remotes/origin/<source>``,
      falling back to ``refs/heads/<source>`` when the branch has no
      upstream counterpart. The host's local ``refs/heads/<base>`` only
      advances on ``git pull``, so for any branch the user does not check
      out (typically the base branch) the remote-tracking ref is the
      fresher one. Mirrors ``new.clone_from='origin'``.
    * ``"local"`` — push ``refs/heads/<source>`` first, as ``jailbee`` did
      before, falling back to the remote-tracking ref.

    ``autofetch`` runs ``git fetch origin <source>`` on the host before
    resolving, so the remote-tracking ref is actually current — the
    counterpart of ``new.autofetch``. It only applies in ``"origin"``
    mode and is best-effort: a failure (offline, branch not on origin)
    is reported and the push proceeds with the refs already present.
    ``--pr`` and ``--current`` always resolve locally regardless of
    these keys; see `jailbee git push --help`.
    """

    model_config = ConfigDict(extra="forbid")
    default_action: Literal["merge", "rebase", "plain", "ask"] = "ask"
    default_source: Literal["default-branch", "current", "base", "ask"] = "base"
    push_from: Literal["local", "origin"] = "origin"
    autofetch: bool = True


class NewConfig(BaseModel):
    """Policy for `jailbee new`'s starting-point selection.

    Applies to the default-branch fallback path (no --base, branch does
    not yet exist in the source repo). The `--base` path always uses
    local refs and skips autofetch; `--pr` performs its own up-front
    fetch via `gh` and is unaffected.

    With ``clone_from='origin'``, the new container is checked out at
    ``refs/remotes/origin/<default_branch>`` on the host. If
    ``autofetch=True``, ``jailbee new`` runs ``git fetch origin <branch>``
    on the host before resolving that ref, so the clone reflects the
    upstream tip without the user having to fetch manually first.

    ``submodules`` controls whether git submodules are initialised
    (recursively) inside the new container.
    """

    model_config = ConfigDict(extra="forbid")
    clone_from: Literal["local", "origin"] = "origin"
    autofetch: bool = True
    background: bool = False
    """Run `jailbee new` detached in the background by default. Overridable
    per-invocation with `--background` / `--no-background`."""
    submodules: bool = True
    """Initialize the superproject's git submodules in the container
    (recursively, offline from the host bind mount). Set false to skip."""


class DestroyConfig(BaseModel):
    """Policy for `jailbee destroy`."""

    model_config = ConfigDict(extra="forbid")
    background: bool = False
    """Run `jailbee destroy` detached in the background by default. Overridable
    per-invocation with `--background` / `--no-background`."""


class BootConfig(BaseModel):
    """Policy for `jailbee start` and `jailbee restart`.

    One key for both: what makes either slow is the autostart run that
    follows the boot, and it is the same run.
    """

    model_config = ConfigDict(extra="forbid")
    background: bool = False
    """Run `jailbee start` / `jailbee restart` detached in the background by
    default. Overridable per-invocation with `--background` /
    `--no-background`."""


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory: str = "16GiB"
    cpu: int = 8
    network: Literal["strict", "loose"] = "strict"
    storage_pool: str = "default"

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        return _reject_offline(v)
