"""The schema introspection the editor generates its forms from."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, SecretStr

from jailbee.config import ClaudeAgentConfig, Config
from jailbee.config_edit.schema import Classified, FieldKind, classify
from jailbee.global_config import GlobalConfig
from tests.test_config_schema_closure import walk_models


class _Item(BaseModel):
    x: int = 0


@pytest.mark.parametrize(
    ("annotation", "expected"),
    [
        (bool, Classified(FieldKind.BOOL)),
        (str, Classified(FieldKind.STR)),
        (int, Classified(FieldKind.INT)),
        (Path, Classified(FieldKind.PATH)),
        (str | None, Classified(FieldKind.STR, optional=True)),
        (Path | None, Classified(FieldKind.PATH, optional=True)),
        (list[str], Classified(FieldKind.STR_LIST)),
        (list[str] | None, Classified(FieldKind.STR_LIST, optional=True)),
        (dict[str, str], Classified(FieldKind.STR_MAP)),
        (dict[str, str | None], Classified(FieldKind.STR_MAP)),
        (dict[str, bool], Classified(FieldKind.BOOL_MAP)),
        (dict[str, object], Classified(FieldKind.OPAQUE)),
        (list[_Item], Classified(FieldKind.MODEL_LIST, item_model=_Item)),
        (dict[str, _Item], Classified(FieldKind.MODEL_MAP, item_model=_Item)),
        (_Item, Classified(FieldKind.SUBMODEL, item_model=_Item)),
        (_Item | None, Classified(FieldKind.SUBMODEL, item_model=_Item, optional=True)),
        (
            Literal["strict", "loose"],
            Classified(FieldKind.CHOICE, choices=("strict", "loose")),
        ),
        (
            Literal["strict", "loose"] | None,
            Classified(FieldKind.CHOICE, choices=("strict", "loose"), optional=True),
        ),
    ],
)
def test_classify_maps_each_annotation_shape(annotation, expected):
    assert classify(annotation) == expected


def test_secret_map_is_flagged():
    """`github.api_tokens` must never be rendered like an ordinary string map."""
    result = classify(dict[str, SecretStr])
    assert result.kind is FieldKind.STR_MAP
    assert result.secret is True


@pytest.mark.parametrize(
    ("annotation", "choices"),
    [
        (bool | Literal["auto"], (True, False, "auto")),
        (Literal["auto", True, False], ("auto", True, False)),
        (bool | str, (True, False)),
        (str | int, ()),
    ],
)
def test_scalar_unions_carry_their_literal_choices_as_hints(annotation, choices):
    """A scalar union offers its literal arms, but free text must stay legal.

    `Stacks.java` is `bool | str`: `true` and a version string like "21"
    are both valid, so the two booleans are a suggestion list, not a
    closed set. `LooseAutoRevert.after` is `str | int` with no literal
    arm at all and offers nothing.
    """
    result = classify(annotation)
    assert result.kind is FieldKind.SCALAR_UNION
    assert result.choices == choices


def test_every_config_field_classifies():
    """The closure that makes "schema-driven" true rather than aspirational.

    A field the editor cannot classify would silently not appear in it.
    """
    unclassifiable = []
    for model in walk_models(Config, GlobalConfig, ClaudeAgentConfig):
        for name, info in model.model_fields.items():
            try:
                classify(info.annotation)
            except TypeError:
                unclassifiable.append(f"{model.__name__}.{name}")
    assert unclassifiable == []


def test_opaque_is_only_ever_the_scratch_overlay():
    """OPAQUE means "no form can be generated" — a new one needs a decision.

    `scratch.config` is a free-form config overlay, so it has no schema to
    render. If a second field lands here, either it needs a real
    `FieldKind` or the editor is about to hide it.
    """
    opaque = [
        f"{model.__name__}.{name}"
        for model in walk_models(Config, GlobalConfig, ClaudeAgentConfig)
        for name, info in model.model_fields.items()
        if classify(info.annotation).kind is FieldKind.OPAQUE
    ]
    assert opaque == ["ScratchConfig.config"]


def test_container_prefix_is_editable_and_documented():
    """A real YAML key, not a computed attribute.

    `_build_config_from_dict` tells users to "Set `container_prefix:` ...
    explicitly", so the editor must be able to. Only the *fallback* is
    computed.
    """
    from jailbee.config_edit.schema import COMPUTED_FIELDS

    assert ("Config", "container_prefix") not in COMPUTED_FIELDS
    info = Config.model_fields["container_prefix"]
    assert (info.description or "").strip()


def test_computed_fields_are_the_four_that_have_no_yaml_key():
    from jailbee.config_edit.schema import COMPUTED_FIELDS

    assert COMPUTED_FIELDS == frozenset(
        {
            ("Config", "repo_root"),
            ("Config", "default_branch"),
            ("Config", "upstream_remote"),
            ("Config", "claude_credentials_dir"),
        }
    )


def _by_path(specs, dotted):
    wanted = tuple(dotted.split("."))
    matches = [s for s in specs if s.path == wanted]
    assert matches, f"no spec for {dotted}"
    return matches[0]


def test_build_specs_returns_leaves_with_dotted_paths():
    from jailbee.config_edit.schema import build_specs

    specs = build_specs(Config)
    paths = {s.path for s in specs}
    assert ("gpg", "enabled") in paths
    assert ("gpg",) not in paths, "a SUBMODEL is recursed into, not emitted"
    assert ("terminal", "kitty", "enabled") in paths, "recursion is not depth-limited"


def test_build_specs_carries_label_description_and_default():
    from jailbee.config_edit.schema import build_specs

    spec = _by_path(build_specs(Config), "jetbrains.ide")
    assert spec.label == "ide"
    assert spec.kind is FieldKind.CHOICE
    assert "idea" in spec.choices
    assert spec.default == "idea"
    assert spec.description.strip()


def test_build_specs_omits_computed_fields():
    from jailbee.config_edit.schema import build_specs

    paths = {s.path for s in build_specs(Config)}
    for computed in (
        "repo_root",
        "default_branch",
        "upstream_remote",
        "claude_credentials_dir",
    ):
        assert (computed,) not in paths
    assert ("container_prefix",) in paths


def test_collections_of_models_stay_leaves():
    """`host_mounts` is one row list, not six flattened HostMount fields.

    Flattening would produce `host_mounts.host` — a path that means
    nothing, because there are N mounts. The drill-down screen renders
    `item_model` instead.
    """
    from jailbee.config.models_host import HostMount
    from jailbee.config_edit.schema import build_specs

    spec = _by_path(build_specs(Config), "host_mounts")
    assert spec.kind is FieldKind.MODEL_LIST
    assert spec.item_model is HostMount
    assert not any(
        s.path[:1] == ("host_mounts",) and len(s.path) > 1 for s in build_specs(Config)
    )


def test_build_specs_covers_every_config_leaf():
    """77 leaves under Config, 14 under GlobalConfig, as measured.

    A count, not a list: it fails loudly when a field is added or a
    recursion rule changes, and the reviewer then decides which.

    The plan's task-3 brief said 76 for `Config`; re-measuring against the
    actual schema on this branch gives 77 (see task-3-report.md for the
    full per-model breakdown and the two independent recounts that agree
    with the code). `GlobalConfig`'s 14 matches the brief exactly.
    """
    from jailbee.config_edit.schema import build_specs

    assert len(build_specs(Config)) == 77
    assert len(build_specs(GlobalConfig)) == 14


def test_a_default_factory_field_reports_its_real_default():
    """`shared_caches` and `pooled_caches` use default_factory.

    Read as `FieldInfo.default` those are `PydanticUndefined`, which the
    help pane would render as the literal string "PydanticUndefined". The
    factory has to be called.
    """
    from jailbee.config_edit.schema import build_specs

    specs = build_specs(Config)
    caches = _by_path(specs, "shared_caches").default
    assert isinstance(caches, list) and caches, "the default set of shared caches"
    assert _by_path(specs, "pooled_caches").default == {}
    assert _by_path(specs, "egress_allow").default == []
