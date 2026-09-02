"""Round-trip YAML editing for jailbee's config files."""

from __future__ import annotations

import yaml

from jailbee.config_writer import DELETE, YamlChange, patch_yaml

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
