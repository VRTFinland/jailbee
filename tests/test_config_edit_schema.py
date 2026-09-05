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
    assert not any(s.path[:1] == ("host_mounts",) and len(s.path) > 1 for s in build_specs(Config))


def test_build_specs_covers_every_config_leaf():
    """77 leaves under Config, 14 under GlobalConfig, as measured.

    A count, not a list: it fails loudly when a field is added or a
    recursion rule changes, and the reviewer then decides which.

    The plan's task-3 brief said 76 for `Config`. That count predates
    Task 2, which removed `container_prefix` from `COMPUTED_FIELDS` —
    turning it from an excluded computed attribute into an editable leaf
    and adding exactly one to the count: 76 + 1 = 77. `GlobalConfig`'s 14
    is unaffected, since `container_prefix` only ever lived on `Config`.
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


def test_repo_specs_is_the_config_tree():
    from jailbee.config_edit.schema import build_specs, repo_specs

    assert repo_specs() == build_specs(Config)


def test_global_specs_routes_host_level_keys_to_globalconfig():
    """`docker_registry_mirror` means a different model on each side.

    In `global.yaml` it is the mirror daemon's own settings (port,
    data_dir, image); in a repo config it is
    `DockerRegistryMirrorRepoConfig`, which only has `extra_registries`.
    """
    from jailbee.config_edit.schema import global_specs, repo_specs

    global_paths = {s.path for s in global_specs()}
    repo_paths = {s.path for s in repo_specs()}

    assert ("docker_registry_mirror", "port") in global_paths
    assert ("docker_registry_mirror", "port") not in repo_paths
    assert ("docker_registry_mirror", "extra_registries") in repo_paths
    assert ("docker_registry_mirror", "extra_registries") not in global_paths


def test_global_specs_keeps_the_config_overlay_keys():
    from jailbee.config_edit.schema import global_specs

    paths = {s.path for s in global_specs()}
    assert ("gpg", "enabled") in paths
    assert ("host_mounts",) in paths
    assert ("claude_credentials", "group") in paths, "host-level, from GlobalConfig"


def test_loose_auto_revert_appears_once_on_the_config_side():
    """Declared on both models, but not host-level, so YAML routes it to Config.

    `GlobalConfig.loose_auto_revert` is unreachable from YAML —
    `_load_unsanitized` validates only the host-level subset. Emitting
    both copies would show the user a duplicate section.
    """
    from jailbee.config_edit.schema import global_specs

    enabled = [s for s in global_specs() if s.path == ("loose_auto_revert", "enabled")]
    assert len(enabled) == 1


def test_global_only_keys_match_the_loader_ban_list():
    """The editor must disable exactly what `_load_config_from_repo_raw` rejects."""
    from jailbee.config_edit.schema import GLOBAL_ONLY_KEYS

    assert GLOBAL_ONLY_KEYS == frozenset({"github", "claude_credentials", "claude_credentials_dir"})


def test_github_stays_in_the_repo_tree_so_it_can_be_shown_disabled():
    """Spec 3.3: a global-only key renders disabled with a reason, not absent.

    Dropping it would leave the user wondering where the setting went.
    """
    from jailbee.config_edit.schema import repo_specs

    assert ("github", "enabled") in {s.path for s in repo_specs()}


def test_every_basic_path_exists_in_a_layer_tree():
    """A curated path that no longer exists would silently curate nothing."""
    from jailbee.config_edit.schema import BASIC_FIELDS, global_specs, repo_specs

    known = {s.path for s in repo_specs()} | {s.path for s in global_specs()}
    assert BASIC_FIELDS - known == frozenset()


def test_basic_set_is_a_readable_shortlist():
    """Spec 2.7 sizes it at ~25-30 of 90 leaves; a bloated set is no filter."""
    from jailbee.config_edit.schema import BASIC_FIELDS

    assert 20 <= len(BASIC_FIELDS) <= 35


def test_a_spec_defaults_to_advanced():
    """The safe direction: a field nobody curated must not sneak into the
    short list."""
    from jailbee.config_edit.schema import FieldSpec

    invented = FieldSpec(
        path=("something", "new"),
        label="new",
        kind=FieldKind.BOOL,
        description="x",
        default=False,
    )
    assert invented.advanced is True


def test_the_most_used_keys_are_not_advanced():
    from jailbee.config_edit.schema import repo_specs

    specs = {s.path: s for s in repo_specs()}
    for dotted in (
        "container_prefix",
        "defaults.network",
        "egress_allow",
        "golden.stacks.node",
        "jetbrains.enabled",
    ):
        assert specs[tuple(dotted.split("."))].advanced is False, dotted


def test_an_uncurated_key_is_advanced():
    from jailbee.config_edit.schema import repo_specs

    specs = {s.path: s for s in repo_specs()}
    assert specs[("ssh", "seed_from_host")].advanced is True
    assert specs[("jetbrains", "share_idea")].advanced is True
