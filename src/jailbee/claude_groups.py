"""jailbee's own Claude credential-group binding.

**This module is a binding, not an implementation.** The generic half —
agent-agnostic in name only, since the group directory and the running-probe
are the only two pieces that vary by agent so far — lives in
`accounts/groups.py`. Every name below is re-exported from there under the
name this module has always used, so `cli.py` and the existing tests keep the
module they know. `group_dir` and `claude_running` are thin wrappers rather
than straight re-exports, because their callers here still pass one
argument where `accounts/groups.py` now wants an agent name too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.accounts.groups import GROUP_LABEL as GROUP_LABEL
from jailbee.accounts.groups import INHERIT as INHERIT
from jailbee.accounts.groups import NO_GROUP as NO_GROUP
from jailbee.accounts.groups import RESERVED_GROUP_NAMES as RESERVED_GROUP_NAMES
from jailbee.accounts.groups import GroupError as GroupError
from jailbee.accounts.groups import Override as Override
from jailbee.accounts.groups import _config_home_path as _config_home_path
from jailbee.accounts.groups import _creds_env_key as _creds_env_key
from jailbee.accounts.groups import _creds_mount_path as _creds_mount_path
from jailbee.accounts.groups import _label_group as _label_group
from jailbee.accounts.groups import _local_creds_device as _local_creds_device
from jailbee.accounts.groups import _profile_has_creds_device as _profile_has_creds_device
from jailbee.accounts.groups import agent_running as agent_running
from jailbee.accounts.groups import authoritative_in as authoritative_in
from jailbee.accounts.groups import authoritative_prefixes as authoritative_prefixes
from jailbee.accounts.groups import authoritative_prefixes_from as authoritative_prefixes_from
from jailbee.accounts.groups import clear_container_group as clear_container_group
from jailbee.accounts.groups import container_groups as container_groups
from jailbee.accounts.groups import container_override as container_override
from jailbee.accounts.groups import effective_group as effective_group
from jailbee.accounts.groups import groups_by_prefix_from as groups_by_prefix_from
from jailbee.accounts.groups import override_is_redundant as override_is_redundant
from jailbee.accounts.groups import redundant_overrides as redundant_overrides
from jailbee.accounts.groups import repo_group as repo_group
from jailbee.accounts.groups import set_container_group as set_container_group
from jailbee.accounts.groups import validate_group_name as validate_group_name

if TYPE_CHECKING:
    from pathlib import Path

    from jailbee.config import Config
    from jailbee.incus import Incus


def group_dir(name: str) -> Path:
    """The credential directory for Claude's group `name`."""
    from jailbee.accounts.groups import group_dir as _group_dir

    return _group_dir("claude", name)


def ensure_group_dir(name: str) -> Path:
    """Create Claude's group `name` credential directory at 0700 and return it."""
    from jailbee.accounts.groups import ensure_group_dir as _ensure_group_dir

    return _ensure_group_dir("claude", name)


def claude_running(cfg: Config, incus: Incus, container: str) -> bool | None:
    """Whether Claude Code looks to be running in `container`.

    `agent_running` bound to Claude's own command, for `cli.py`'s callers.
    """
    return agent_running(cfg, incus, container, command=cfg.claude.command)
