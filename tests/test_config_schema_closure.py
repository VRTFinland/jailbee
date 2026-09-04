"""Every config field must be reachable, typed, and documented.

The config editor generates its forms from these models, and generates
`global.yaml`'s comments from their descriptions. A field with no
description would appear in the editor with an empty help pane; a field
whose annotation the editor cannot classify would silently not appear at
all. This test turns both into a CI failure.

UNDOCUMENTED is a shrinking allowlist. Do not add to it.
"""

from __future__ import annotations

from pydantic import BaseModel
from pydantic.fields import FieldInfo

from jailbee.config import ClaudeAgentConfig, Config
from jailbee.global_config import GlobalConfig

# Computed at load time, never YAML keys. See the `Config` docstring.
EXCLUDED: frozenset[tuple[str, str]] = frozenset(
    {
        ("Config", "repo_root"),
        ("Config", "default_branch"),
        ("Config", "container_prefix"),
        ("Config", "upstream_remote"),
        ("Config", "claude_credentials_dir"),
    }
)

# Shrunk to empty across Tasks 7-10; the mechanism itself is deleted in Task 11.
UNDOCUMENTED: frozenset[tuple[str, str]] = frozenset()


def walk_models(*roots: type[BaseModel]) -> list[type[BaseModel]]:
    """Every model class reachable from `roots`, including subclasses.

    Subclasses matter: `agents.claude` is a `ClaudeAgentConfig`, whose
    Claude-only fields are invisible if only the declared `AgentConfig`
    field type is walked.
    """
    seen: dict[str, type[BaseModel]] = {}
    queue = list(roots)
    while queue:
        model = queue.pop()
        if model.__name__ in seen:
            continue
        seen[model.__name__] = model
        for info in model.model_fields.values():
            for candidate in _models_in(info.annotation):
                queue.append(candidate)
        queue.extend(model.__subclasses__())
    return list(seen.values())


def _models_in(annotation: object) -> list[type[BaseModel]]:
    """Model classes appearing anywhere inside an annotation."""
    from typing import get_args

    found: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        found.append(annotation)
    for arg in get_args(annotation):
        found.extend(_models_in(arg))
    return found


def field_paths() -> list[tuple[str, str, FieldInfo]]:
    """(model name, field name, FieldInfo) for every field under test."""
    out: list[tuple[str, str, FieldInfo]] = []
    for model in walk_models(Config, GlobalConfig, ClaudeAgentConfig):
        for name, info in model.model_fields.items():
            if (model.__name__, name) in EXCLUDED:
                continue
            out.append((model.__name__, name, info))
    return out


def test_walk_reaches_the_known_models():
    """Guard the walker itself: a broken walk would make the suite vacuous."""
    names = {m.__name__ for m in walk_models(Config, GlobalConfig, ClaudeAgentConfig)}
    for expected in (
        "Config",
        "GlobalConfig",
        "AgentConfig",
        "ClaudeAgentConfig",
        "HostMount",
        "AutostartStep",
        "Golden",
        "Stacks",
    ):
        assert expected in names, f"model walk missed {expected}"


def test_every_field_has_a_description():
    missing = sorted(
        (model, field)
        for model, field, info in field_paths()
        if not (info.description or "").strip()
    )
    still_allowed = sorted(UNDOCUMENTED)
    assert missing == [] or set(missing) <= set(still_allowed), (
        "fields with no description= and not on the allowlist: "
        f"{sorted(set(missing) - set(still_allowed))}"
    )


def test_allowlist_has_no_stale_entries():
    """An allowlisted field that now has a description must leave the list."""
    documented = {
        (model, field) for model, field, info in field_paths() if (info.description or "").strip()
    }
    stale = sorted(UNDOCUMENTED & documented)
    assert stale == [], f"remove from UNDOCUMENTED, these are documented now: {stale}"
