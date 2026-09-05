"""Pydantic config models -> a flat tree of editable field specs.

The editor generates its forms from here rather than from a hand-written
list, so a new config field appears in it automatically. Two closure
tests hold that property up: every field must classify to a known
`FieldKind` (`test_config_edit_schema.py`) and must carry a
`description=` (`test_config_schema_closure.py`). Without them the
property decays silently — a field the editor cannot classify simply
never renders.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeGuard, Union, get_args, get_origin

from pydantic import BaseModel, SecretStr
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from jailbee.config import Config
from jailbee.config.common import _HOST_LEVEL_KEYS  # router impl; must stay in sync
from jailbee.global_config import GlobalConfig


class FieldKind(StrEnum):
    """How the editor renders and edits one field.

    Twelve kinds cover every leaf `build_specs` produces — 77 of them under
    `repo_specs()`, 85 under `global_specs()`. `OPAQUE` is the honest
    thirteenth: a field whose schema cannot generate a form.
    """

    BOOL = "bool"
    STR = "str"
    INT = "int"
    PATH = "path"
    CHOICE = "choice"
    STR_LIST = "str_list"
    STR_MAP = "str_map"
    BOOL_MAP = "bool_map"
    MODEL_LIST = "model_list"
    MODEL_MAP = "model_map"
    SCALAR_UNION = "scalar_union"
    SUBMODEL = "submodel"
    OPAQUE = "opaque"


COMPUTED_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Config", "repo_root"),
        ("Config", "default_branch"),
        ("Config", "upstream_remote"),
        ("Config", "claude_credentials_dir"),
    }
)
"""(model, field) pairs that are never YAML keys, so never editable.

Set by `_build_config_from_dict` from the repo checkout and from
`global.yaml`'s host-level block. Rendering one as editable would offer
the user a key the loader overwrites on the next load.

`container_prefix` is deliberately **not** here: its *fallback* is
computed (`repo_root.name` when unset), but the key itself is a
documented, hand-edited YAML key. `tests/test_config_schema_closure.py`
imports this set, so the exclusion list exists once.
"""


@dataclass(frozen=True)
class Classified:
    """What `classify` learned from one annotation.

    `choices` is authoritative for `CHOICE` (the value must be one of
    them) and only a hint for `SCALAR_UNION`, where a free-text arm
    coexists with the literals — see `classify`.
    """

    kind: FieldKind
    choices: tuple[object, ...] = ()
    item_model: type[BaseModel] | None = None
    optional: bool = False
    secret: bool = False


def _strip_annotated(annotation: object) -> object:
    """Unwrap `Annotated[T, ...]` down to `T`.

    `PathExpanded` is `Annotated[Path, BeforeValidator(_expand)]`, so
    without this every expanded-path field would fall through to the
    final `TypeError`.
    """
    while hasattr(annotation, "__metadata__"):
        annotation = get_args(annotation)[0]
    return annotation


def _is_union(annotation: object) -> bool:
    """True for both spellings: `Union[a, b]` and `a | b`."""
    return get_origin(annotation) in (Union, types.UnionType)


def _is_model(annotation: object) -> TypeGuard[type[BaseModel]]:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def classify(annotation: object) -> Classified:
    """Map a pydantic field annotation onto a `FieldKind`.

    Raises `TypeError` for a shape it does not know — deliberately loud,
    because the alternative is a config field that silently never appears
    in the editor. `test_every_config_field_classifies` turns that into a
    CI failure rather than a user-visible gap.
    """
    ann = _strip_annotated(annotation)

    if _is_union(ann):
        args = [_strip_annotated(a) for a in get_args(ann)]
        rest = [a for a in args if a is not type(None)]
        optional = len(rest) != len(args)
        if len(rest) == 1:
            inner = classify(rest[0])
            return Classified(inner.kind, inner.choices, inner.item_model, True, inner.secret)
        # A union of two or more non-None arms: a scalar with several
        # accepted spellings (`bool | Literal["auto"]`, `bool | str`,
        # `str | int`). Its literal arms become suggestions; the open arm
        # means free text must stay legal, so this is not a CHOICE.
        choices: list[object] = []
        for arm in rest:
            if get_origin(arm) is Literal:
                choices.extend(get_args(arm))
            elif arm is bool:
                choices.extend((True, False))
        return Classified(FieldKind.SCALAR_UNION, tuple(choices), None, optional)

    if get_origin(ann) is Literal:
        literal_args = get_args(ann)
        # `Literal["auto", True, False]` is a tri-state spelled as one
        # Literal rather than as a union; a plain string Literal is a
        # closed choice list.
        kind = (
            FieldKind.SCALAR_UNION
            if any(isinstance(a, bool) for a in literal_args)
            else FieldKind.CHOICE
        )
        return Classified(kind, literal_args)

    origin = get_origin(ann)
    if origin is list:
        item = _strip_annotated(get_args(ann)[0])
        if _is_model(item):
            return Classified(FieldKind.MODEL_LIST, item_model=item)
        return Classified(FieldKind.STR_LIST)

    if origin is dict:
        value = _strip_annotated(get_args(ann)[1])
        if _is_model(value):
            return Classified(FieldKind.MODEL_MAP, item_model=value)
        if value is bool:
            return Classified(FieldKind.BOOL_MAP)
        if value is SecretStr:
            return Classified(FieldKind.STR_MAP, secret=True)
        if _is_union(value):
            arms = [a for a in get_args(value) if a is not type(None)]
            if arms == [str]:
                return Classified(FieldKind.STR_MAP)
        if value is str:
            return Classified(FieldKind.STR_MAP)
        # `dict[str, object]` — `scratch.config`, a free-form overlay with
        # no schema of its own. Nothing can generate a form for it.
        return Classified(FieldKind.OPAQUE)

    if _is_model(ann):
        return Classified(FieldKind.SUBMODEL, item_model=ann)
    if ann is bool:
        return Classified(FieldKind.BOOL)
    if ann is int:
        return Classified(FieldKind.INT)
    if isinstance(ann, type) and issubclass(ann, Path):
        return Classified(FieldKind.PATH)
    if ann is str:
        return Classified(FieldKind.STR)
    raise TypeError(f"config_edit.schema cannot classify annotation: {annotation!r}")


@dataclass(frozen=True)
class FieldSpec:
    """One editable leaf of the config tree.

    `path` is the YAML key path, which is also the identity used by
    staged changes (`config_writer.YamlChange.path`) and by
    `BASIC_FIELDS`. `default` is the schema default, shown in the help
    pane so the user can see what resetting the field gives back.
    """

    path: tuple[str, ...]
    label: str
    kind: FieldKind
    description: str
    default: object
    choices: tuple[object, ...] = ()
    item_model: type[BaseModel] | None = None
    optional: bool = False
    secret: bool = False
    advanced: bool = True


def _default_of(info: FieldInfo) -> object:
    """The field's default, with `default_factory` called.

    A `default_factory` field reports `PydanticUndefined` as its
    `default`, which would render as the string "PydanticUndefined" in
    the help pane. Calling the factory gives the real empty value.
    """
    if info.default_factory is not None:
        # `default_factory` may take the already-validated data as its one
        # argument; none of jailbee's do, so the no-arg call is correct here.
        return info.default_factory()  # type: ignore[call-arg]  # no validated-data factories in jailbee
    if info.default is PydanticUndefined:
        return None
    return info.default


def build_specs(model: type[BaseModel]) -> tuple[FieldSpec, ...]:
    """Every editable leaf reachable from `model`, in declaration order.

    A `SUBMODEL` field is recursed into rather than emitted: `gpg` is a
    section header the UI derives, `gpg.enabled` is the editable thing. A
    *collection* of models (`host_mounts`, `agents`) stays a leaf — there
    is no single `host_mounts.host` to edit — and carries `item_model` for
    the drill-down screen to render.
    """
    return tuple(_walk(model, prefix=(), stack=(model,)))


def _walk(
    model: type[BaseModel],
    *,
    prefix: tuple[str, ...],
    stack: tuple[type[BaseModel], ...],
) -> list[FieldSpec]:
    out: list[FieldSpec] = []
    for name, info in model.model_fields.items():
        if (model.__name__, name) in COMPUTED_FIELDS:
            continue
        found = classify(info.annotation)
        path = (*prefix, name)
        # The `item_model is not None` half is load-bearing, not defensive:
        # `_is_model` is a `TypeGuard`, so `Classified.item_model` is typed
        # `type[BaseModel] | None` and mypy --strict needs the narrowing
        # before it can be passed to `_walk`.
        if found.kind is FieldKind.SUBMODEL and found.item_model is not None:
            # `stack` guards a self-referential model from recursing
            # forever. None exists today; the guard costs one comparison
            # and turns a future hang into a missing section.
            if found.item_model in stack:
                continue
            out.extend(_walk(found.item_model, prefix=path, stack=(*stack, found.item_model)))
            continue
        out.append(
            FieldSpec(
                path=path,
                label=name,
                kind=found.kind,
                description=(info.description or "").strip(),
                default=_default_of(info),
                choices=found.choices,
                item_model=found.item_model,
                optional=found.optional,
                secret=found.secret,
                advanced=path not in BASIC_FIELDS,
            )
        )
    return out


BASIC_FIELDS: frozenset[tuple[str, ...]] = frozenset(
    {
        # Identity and sizing — the first things a new repo sets.
        ("container_prefix",),
        ("defaults", "memory"),
        ("defaults", "cpu"),
        ("defaults", "network"),
        ("defaults", "storage_pool"),
        # Egress: the setting users reach for most after `net loose`.
        ("egress_allow",),
        # What goes in the image.
        ("golden", "ubuntu_version"),
        ("golden", "stacks", "java"),
        ("golden", "stacks", "node"),
        ("golden", "stacks", "python"),
        ("golden", "stacks", "docker"),
        ("golden", "extra_apt_packages"),
        # Host plumbing.
        ("host_mounts",),
        ("host_ports",),
        ("share_local",),
        ("shared_caches",),
        # Host-tooling master switches, plus JetBrains' companion IDE
        # choice. Their other sub-fields stay advanced.
        ("gpg", "enabled"),
        ("ssh", "enabled"),
        ("jetbrains", "enabled"),
        ("jetbrains", "ide"),
        ("chrome", "enabled"),
        # Agents and startup.
        ("agents",),
        ("autostart", "on_create"),
        ("autostart", "on_start"),
        ("after_new",),
        # Workflow defaults people actually change.
        ("new", "background"),
        ("push", "default_action"),
        ("pull", "destroy_container"),
    }
)
"""The 28 paths the default view shows; everything else is behind "show all".

Curation lives here rather than as metadata on the models: a config model
should not also carry a presentational concern, and the curated set is only
reviewable when it is readable in one place.

A path absent from this set is advanced. That is the safe default — the
alternative silently rots as fields are added, and search (which ignores
this filter entirely) is the real answer at this schema size.
"""


GLOBAL_ONLY_KEYS: frozenset[str] = frozenset(
    {"github", "claude_credentials", "claude_credentials_dir"}
)
"""Top-level keys `load_config_from_layers` refuses in a repo config.

Tokens and credential-group names are host-local: a repo config is
typically committed, so a value here would leak or would name a group
that exists on one machine only. The editor keeps these in the repo tree
and renders them disabled with the reason (spec 3.3) rather than hiding
them, so the setting does not appear to have vanished.

Kept in step with the ban list in `config/loader.py` by
`test_global_only_keys_is_the_documented_ban_list`.
"""


def repo_specs() -> tuple[FieldSpec, ...]:
    """Editable leaves of a repo's `.jailbee/config.yaml`."""
    return build_specs(Config)


def global_specs() -> tuple[FieldSpec, ...]:
    """Editable leaves of `~/.config/jailbee/global.yaml`.

    Two trees concatenated on the `_HOST_LEVEL_KEYS` boundary, the same
    split `_split_host_keys` applies at load time and
    `config_init.render_global_template` applies when generating the
    file. Host-level keys are modelled on `GlobalConfig`; everything else
    overlays `Config`. Three keys are declared on both models with
    different shapes, so taking either tree whole would show the user the
    wrong fields for half the file.
    """
    overlay = [s for s in build_specs(Config) if s.path[0] not in _HOST_LEVEL_KEYS]
    host = [s for s in build_specs(GlobalConfig) if s.path[0] in _HOST_LEVEL_KEYS]
    return tuple(overlay + host)
