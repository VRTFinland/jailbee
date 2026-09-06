"""Shared constants and helpers used across the config package.

Path expansion, YAML parsing, deep-merge, and the host-vs-config key split
used when loading `~/.config/jailbee/global.yaml`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BeforeValidator

from jailbee.config.errors import ConfigError
from jailbee.paths import expand_path

# Container's unix username is hardcoded — must match the user baked into the
# golden image by provision/install.sh. It used to be configurable; made fixed
# because nothing enforced consistency between this value and the golden-image
# user, and the symptom was a confusing Permission-denied on `jailbee new`.
CONTAINER_USERNAME = "dev"

# Keys in ~/.config/jailbee/global.yaml that belong to GlobalConfig (host-level)
# and must NOT be merged into the Config layer. `docker_registry_mirror`
# exists in both schemas with different shapes (host-level: {port, enabled,
# image, data_dir}; Config-level: {extra_registries}) — we resolve the
# ambiguity by treating it as host-level at the global file, and only
# accepting the Config-level shape at the repo file.
#
# `ls` and `dashboard` are here for a different reason: they exist in both
# schemas with the *same* shape and are merged field-by-field by
# `Config._effective_columns` (repo block over global block), exactly like
# `loose_auto_revert`. Letting them through to `deep_merge` as well would
# apply the list rule to `fields`/`hide` — which *appends* a non-empty
# overlay — so a global `fields: [name, state]` plus a repo
# `fields: [name, ip]` produced `[name, state, name, ip]` and rendered NAME
# twice. Splitting them out keeps `Config._effective_columns` the single
# merge mechanism for this shape. `dashboard` stays in this set even though
# the `dashboard:` block itself is deprecated (see
# `Config.validate_runtime`/`global_config.global_config_issues`): the key
# is still validated on both layers, and a repo-level block would hit the
# same list-append bug if it were ever let through to `deep_merge`.
#
# Note `loose_auto_revert` is *not* in this set even though it has exactly
# the same "merged field-by-field, not through deep_merge" shape — see
# `Config.effective_loose_auto_revert`. That routing is deliberate and
# belongs to an earlier spec; don't "fix" the apparent 3-vs-4-fields
# asymmetry by adding it here. It works today only because every
# `LooseAutoRevert` field is a scalar (`enabled: bool`, `after: str | int`),
# so `deep_merge`'s append-a-list behaviour never triggers. The day a list
# field is added to `LooseAutoRevert`, it reintroduces the exact append bug
# `ls`/`dashboard` were split out to avoid, and would need the same
# treatment (its own merge method, kept out of `deep_merge`).
#
# `agents` deliberately stays OUT of this set. It is a mapping keyed by agent
# name, so `deep_merge` recurses per agent instead of hitting the list rule —
# a repo layer adjusting one field of a globally-defined agent merges cleanly.
# Its one list-valued field, `egress_allow`, *wants* the append behaviour: a
# repo adding a single host to a global agent is the intended use. Don't
# "fix" the apparent asymmetry with `ls`/`dashboard` by adding it here.
#
# `apps` also stays OUT of this set, and for the same reason as `agents` — it
# is a mapping keyed by app name, so a repo layer adjusting one field of a
# globally-defined app is the intended layering. But unlike `AgentConfig`,
# whose `command` is a `str`, `AppEntry.command` is a `list[str]` and so hits
# the append rule: a repo overriding a global app's binary had its path
# *appended*, becoming an argument of the very binary it meant to replace.
# `merge_apps_raw` below is the per-app merge that fixes it; the one caller
# is `loader.load_config_from_layers`. Do not "simplify" that call site back
# into the plain `deep_merge`.
#
# `claude_credentials` is host-level because it must never reach the Config
# layer: a group name in a committed `.jailbee/config.yaml` would apply to
# every teammate. It is resolved to `Config.claude_credentials_dir` on the
# load path (Task 2) instead of being merged.
#
# `scratch` is here for a load-breaking reason, not a tidiness one: this set
# routes a key to `GlobalConfig` *instead of* the `deep_merge` layer. Left
# out, `scratch:` would be merged into every repo's config as an unknown
# top-level key, and `Config` is extra="forbid" — every command in every
# configured repo on the host would start failing validation.
_HOST_LEVEL_KEYS: frozenset[str] = frozenset(
    {"docker_registry_mirror", "ls", "dashboard", "claude_credentials", "scratch", "config_edit"}
)


SCRATCH_ORIGIN_SUFFIX = " (scratch.config)"
"""Appended to `global.yaml`'s path to name the synthesized repo layer's source.

A synthesized repo layer has no file of its own: it comes from the `scratch:`
block of the global config, so error messages and `jailbee config show` label
it `<global.yaml> (scratch.config)`. One constant rather than the literal
repeated at each site, so the label the user sees and the label they can grep
for stay the same string.
"""


def _split_host_keys(
    raw: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    """Return (host_level, config_level) sub-dicts of a global.yaml raw load."""
    host = {k: v for k, v in raw.items() if k in _HOST_LEVEL_KEYS}
    config = {k: v for k, v in raw.items() if k not in _HOST_LEVEL_KEYS}
    return host, config


def _read_yaml_or_empty(path: Path) -> dict[str, object]:
    """Read and parse a YAML file. Missing file -> {}. Invalid YAML -> ConfigError."""
    if not path.exists():
        return {}
    return _parse_yaml_text(path.read_text(), str(path))


def _parse_yaml_text(text: str, origin: str) -> dict[str, object]:
    """Parse YAML text into a mapping, mirroring `_read_yaml_or_empty`.

    `origin` is a human-readable label for error messages (a path, or a
    "<ref>:<path>" locator for a config read out of git).
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {origin}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Top level of {origin} must be a mapping; got {type(raw).__name__}.")
    return raw


def deep_merge(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    """Deep-merge two raw config dicts.

    Rules:
      * scalars: overlay wins (None clears)
      * lists:   overlay appended; `[]` overlay = reset to empty list
      * dicts:   recursive deep_merge per key
      * type mismatch (different shape): overlay wins

    Inputs are not mutated.
    """
    result: dict[str, object] = {k: _copy(v) for k, v in base.items()}
    for key, overlay_value in overlay.items():
        if key not in result:
            result[key] = _copy(overlay_value)
            continue
        base_value = result[key]
        if isinstance(base_value, dict) and isinstance(overlay_value, dict):
            result[key] = deep_merge(base_value, overlay_value)
        elif isinstance(base_value, list) and isinstance(overlay_value, list):
            # Empty overlay list = explicit reset; non-empty = append.
            result[key] = [] if not overlay_value else base_value + list(overlay_value)
        else:
            # Scalar override, type mismatch, or None-clear: overlay wins.
            result[key] = _copy(overlay_value)
    return result


_APP_OVERRIDE_KEYS: frozenset[str] = frozenset({"command"})
"""`apps.<name>` keys a repo layer *replaces* rather than appends to.

`command` names which binary the app is; a second element is an argument to
the first, never a replacement of it. Every other key keeps `deep_merge`'s
rules, so `args` still appends — a repo adding one flag to a globally
defined app is the intended use, exactly as `agents.<name>.egress_allow`
appends — and `env` still merges per variable.
"""


def merge_apps_raw(base: dict[str, object], overlay: dict[str, object]) -> dict[str, object]:
    """Merge two config layers' `apps:` blocks, per app name.

    `deep_merge` alone is wrong here. It appends lists, and
    `AppEntry.command` is a `list[str]`, so

        global: {figma: {command: [/opt/global/figma]}}
        repo:   {figma: {command: [/opt/repo/figma]}}

    merged to `command: [/opt/global/figma, /opt/repo/figma]` — the repo's
    override became an *argument* to the global binary. The two legal
    spellings disagreed as well: `command: /opt/repo/figma` (a string) hits
    `deep_merge`'s overlay-wins branch and overrode correctly, while the
    list form appended.

    So: `deep_merge` does the work, then every key in `_APP_OVERRIDE_KEYS`
    that the overlay sets is forced to the overlay's own value. A string
    `command` already behaved this way, which is the behaviour the two
    spellings now share.
    """
    merged = deep_merge(base, overlay)
    for name, entry in overlay.items():
        if not isinstance(entry, dict):
            # Not a mapping: `deep_merge` already let the overlay win whole,
            # and Pydantic will reject it with its own message.
            continue
        target = merged.get(name)
        if not isinstance(target, dict):
            continue
        for key in _APP_OVERRIDE_KEYS & entry.keys():
            target[key] = _copy(entry[key])
    return merged


def _copy(value: object) -> object:
    """Shallow recursive copy for dict/list, identity for scalars."""
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _expand(value: str | Path) -> Path:
    return expand_path(value)


PathExpanded = Annotated[Path, BeforeValidator(_expand)]
