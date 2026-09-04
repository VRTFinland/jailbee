"""Tests for the two YAML write policies.

Both renderers are `str -> str` (or `dict -> str`), so none of this needs
a terminal, a container or a real config file.
"""

from __future__ import annotations

import stat
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from jailbee.config_writer import DELETE, YamlChange, patch_file, patch_yaml, render_documented

ORIGINAL = """\
# Top-of-file banner that must survive.
claude_credentials:
  # Which repos share one Claude login.
  group: work
  repos:
    myrepo: personal

docker_registry_mirror:
  enabled: true
"""


def test_no_changes_is_byte_identical():
    assert patch_yaml(ORIGINAL, []) == ORIGINAL


def test_replacing_a_scalar_keeps_the_comments():
    out = patch_yaml(ORIGINAL, [YamlChange(("claude_credentials", "group"), "personal")])
    assert "Top-of-file banner that must survive." in out
    assert "Which repos share one Claude login." in out
    assert yaml.safe_load(out)["claude_credentials"]["group"] == "personal"
    # Untouched neighbours keep their values.
    assert yaml.safe_load(out)["docker_registry_mirror"]["enabled"] is True


def test_setting_a_nested_key_that_does_not_exist_yet():
    out = patch_yaml(ORIGINAL, [YamlChange(("claude_credentials", "repos", "other"), "work")])
    assert yaml.safe_load(out)["claude_credentials"]["repos"]["other"] == "work"
    assert yaml.safe_load(out)["claude_credentials"]["repos"]["myrepo"] == "personal"


def test_setting_none_writes_a_yaml_null():
    out = patch_yaml(ORIGINAL, [YamlChange(("claude_credentials", "repos", "myrepo"), None)])
    loaded = yaml.safe_load(out)
    assert "myrepo" in loaded["claude_credentials"]["repos"]
    assert loaded["claude_credentials"]["repos"]["myrepo"] is None


def test_delete_removes_the_key():
    out = patch_yaml(ORIGINAL, [YamlChange(("claude_credentials", "repos", "myrepo"), DELETE)])
    assert "myrepo" not in yaml.safe_load(out)["claude_credentials"]["repos"]


def test_delete_of_an_absent_key_is_a_no_op():
    out = patch_yaml(ORIGINAL, [YamlChange(("claude_credentials", "repos", "ghost"), DELETE)])
    assert yaml.safe_load(out) == yaml.safe_load(ORIGINAL)


def test_missing_intermediate_maps_are_created():
    out = patch_yaml("", [YamlChange(("claude_credentials", "repos", "myrepo"), "work")])
    assert yaml.safe_load(out)["claude_credentials"]["repos"]["myrepo"] == "work"


def test_patch_file_writes_the_change(tmp_path: Path):
    target = tmp_path / "global.yaml"
    target.write_text(ORIGINAL)
    changed = patch_file(target, [YamlChange(("claude_credentials", "group"), "personal")])
    assert changed is True
    assert yaml.safe_load(target.read_text())["claude_credentials"]["group"] == "personal"


def test_patch_file_preserves_mode(tmp_path: Path):
    target = tmp_path / "global.yaml"
    target.write_text(ORIGINAL)
    target.chmod(0o600)
    patch_file(target, [YamlChange(("claude_credentials", "group"), "personal")])
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_patch_file_no_op_leaves_the_bytes_alone(tmp_path: Path):
    target = tmp_path / "global.yaml"
    target.write_text(ORIGINAL)
    before = target.read_bytes()
    assert patch_file(target, []) is False
    assert target.read_bytes() == before


def test_patch_file_creates_a_missing_file(tmp_path: Path):
    target = tmp_path / "global.yaml"
    assert patch_file(target, [YamlChange(("claude_credentials", "group"), "work")]) is True
    assert yaml.safe_load(target.read_text())["claude_credentials"]["group"] == "work"
    # A file jailbee creates holds credentials-adjacent config; 0600 from birth.
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_patch_file_leaves_no_temp_file_behind(tmp_path: Path):
    target = tmp_path / "global.yaml"
    target.write_text(ORIGINAL)
    patch_file(target, [YamlChange(("claude_credentials", "group"), "personal")])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["global.yaml"]


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(default=False, description="Turn the widget on.")
    name: str = Field(default="", description="What the widget is called.")


def test_render_documented_emits_the_description_as_a_comment():
    text = render_documented({"enabled": True}, Sample, header="# generated\n")
    assert "# Turn the widget on." in text
    assert "enabled: true" in text


def test_render_documented_only_writes_the_given_keys():
    text = render_documented({"enabled": True}, Sample, header="# generated\n")
    assert "name:" not in text


def test_render_documented_starts_with_the_header():
    text = render_documented({"enabled": True}, Sample, header="# generated\n")
    assert text.startswith("# generated\n")


def test_render_documented_round_trips_to_the_same_values():
    values = {"enabled": True, "name": "widget"}
    text = render_documented(values, Sample, header="# generated\n")
    assert yaml.safe_load(text) == values


def test_render_documented_is_stable():
    """Rendering the same values twice gives byte-identical output.

    The generated file is rewritten on every save; unstable output would
    show up as spurious diffs in the user's dotfiles repo.
    """
    values = {"enabled": True, "name": "widget"}
    first = render_documented(values, Sample, header="# generated\n")
    second = render_documented(values, Sample, header="# generated\n")
    assert first == second


def test_render_documented_refuses_a_secretstr_value():
    """A SecretStr must never reach the renderer.

    `global.yaml` carries `github.api_tokens: dict[str, SecretStr]`. A
    caller that passed `model_dump()` output instead of the raw YAML
    mapping would hand this function SecretStr objects, and
    `model_dump(mode="json")` would hand it the literal string
    `**********`. Either way a regenerating save would overwrite the
    user's real tokens with a masked placeholder — silent, irreversible
    credential loss. Fail loudly instead.
    """
    import pytest
    from pydantic import SecretStr

    class WithSecret(BaseModel):
        model_config = ConfigDict(extra="forbid")
        token: SecretStr | None = Field(default=None, description="A token.")

    with pytest.raises(TypeError, match="SecretStr"):
        render_documented({"token": SecretStr("hunter2")}, WithSecret)


def test_render_documented_indents_nested_comments():
    """A nested key's comment must be indented to its key's column."""

    class Outer(BaseModel):
        model_config = ConfigDict(extra="forbid")
        inner: Sample = Field(default=Sample(), description="The inner block.")

    text = render_documented({"inner": {"enabled": True}}, Outer, header="")
    assert "  # Turn the widget on." in text
