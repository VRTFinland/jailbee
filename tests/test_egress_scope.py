"""Tests for jailbee.egress_scope."""

from __future__ import annotations

import json

import pytest

from jailbee import egress_scope
from jailbee.egress import EgressEntry


def test_repo_extras_starts_empty(db_session):
    assert egress_scope.repo_extras(db_session, "myrepo") == []


def test_add_repo_extra_is_idempotent(db_session, frozen_now):
    assert egress_scope.add_repo_extra(db_session, "myrepo", "nexus.corp:443", now=frozen_now)
    assert not egress_scope.add_repo_extra(db_session, "myrepo", "nexus.corp:443", now=frozen_now)
    assert egress_scope.repo_extras(db_session, "myrepo") == ["nexus.corp:443"]


def test_repo_extras_are_scoped_by_prefix(db_session, frozen_now):
    egress_scope.add_repo_extra(db_session, "myrepo", "nexus.corp:443", now=frozen_now)
    egress_scope.add_repo_extra(db_session, "other", "elsewhere.corp:443", now=frozen_now)
    assert egress_scope.repo_extras(db_session, "myrepo") == ["nexus.corp:443"]


def test_repo_extras_are_sorted(db_session, frozen_now):
    for entry in ("zulu.corp:443", "alpha.corp:443"):
        egress_scope.add_repo_extra(db_session, "myrepo", entry, now=frozen_now)
    assert egress_scope.repo_extras(db_session, "myrepo") == [
        "alpha.corp:443",
        "zulu.corp:443",
    ]


def test_remove_repo_extra_reports_whether_it_removed(db_session, frozen_now):
    egress_scope.add_repo_extra(db_session, "myrepo", "nexus.corp:443", now=frozen_now)
    assert egress_scope.remove_repo_extra(db_session, "myrepo", "nexus.corp:443")
    assert not egress_scope.remove_repo_extra(db_session, "myrepo", "nexus.corp:443")
    assert egress_scope.repo_extras(db_session, "myrepo") == []


def test_container_extras_reads_the_label(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = json.dumps(["nexus.corp:443", "10.0.5.7"])
    assert egress_scope.container_extras(incus, "myrepo-feat") == [
        "10.0.5.7",
        "nexus.corp:443",
    ]
    incus.config_get.assert_called_once_with("myrepo-feat", egress_scope.EGRESS_EXTRA_KEY)


def test_container_extras_is_empty_when_unset(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert egress_scope.container_extras(incus, "myrepo-feat") == []


@pytest.mark.parametrize("payload", ["not json", '{"a": 1}', '["ok", 7]'])
def test_container_extras_warns_and_yields_empty_on_garbage(mocker, payload):
    incus = mocker.MagicMock()
    incus.config_get.return_value = payload
    warn = mocker.patch("jailbee.tui.warn")

    assert egress_scope.container_extras(incus, "myrepo-feat") == []
    assert warn.called


def test_set_container_extras_sorts_dedupes_and_json_encodes(mocker):
    incus = mocker.MagicMock()
    egress_scope.set_container_extras(
        incus, "myrepo-feat", ["zulu.corp:443", "alpha.corp:443", "zulu.corp:443"]
    )
    incus.config_set.assert_called_once_with(
        "myrepo-feat",
        egress_scope.EGRESS_EXTRA_KEY,
        json.dumps(["alpha.corp:443", "zulu.corp:443"]),
    )


def test_set_container_extras_unsets_the_label_when_empty(mocker):
    incus = mocker.MagicMock()
    egress_scope.set_container_extras(incus, "myrepo-feat", [])
    incus.config_unset.assert_called_once_with("myrepo-feat", egress_scope.EGRESS_EXTRA_KEY)
    incus.config_set.assert_not_called()


def test_extra_acl_name_is_the_plain_suffix_when_short():
    assert egress_scope.extra_acl_name("myrepo-feat") == "myrepo-feat-extra"


def test_extra_acl_name_stays_within_the_limit_and_stays_unique():
    long_a = "r" + "a" * 61
    long_b = "r" + "a" * 60 + "b"
    name_a = egress_scope.extra_acl_name(long_a)
    name_b = egress_scope.extra_acl_name(long_b)

    assert len(name_a) <= egress_scope.ACL_NAME_MAX
    assert name_a.endswith("-extra")
    # Two long names sharing a 48-char head must not collide.
    assert name_a != name_b


def test_extra_acl_name_is_deterministic():
    long = "r" + "a" * 61
    assert egress_scope.extra_acl_name(long) == egress_scope.extra_acl_name(long)


def test_effective_repo_entries_appends_overrides_after_config(
    db_session, make_cfg, tmp_path, frozen_now
):
    cfg = make_cfg(tmp_path / "myrepo", egress_allow=["github.com"])
    egress_scope.add_repo_extra(db_session, cfg.container_prefix, "nexus.corp:443", now=frozen_now)

    entries = egress_scope.effective_repo_entries(cfg, db_session)

    assert entries[0] == "github.com"
    assert "nexus.corp:443" in entries


def test_effective_repo_entries_dedupes_an_override_already_in_config(
    db_session, make_cfg, tmp_path, frozen_now
):
    cfg = make_cfg(tmp_path / "myrepo", egress_allow=["github.com"])
    egress_scope.add_repo_extra(db_session, cfg.container_prefix, "github.com", now=frozen_now)

    assert egress_scope.effective_repo_entries(cfg, db_session).count("github.com") == 1


def test_repo_file_egress_allow_reads_only_that_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("egress_allow:\n  - github.com\n  - 10.0.0.0/8\n")
    assert egress_scope.repo_file_egress_allow(path) == ["github.com", "10.0.0.0/8"]


def test_repo_file_egress_allow_handles_an_absent_key(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("container_prefix: myrepo\n")
    assert egress_scope.repo_file_egress_allow(path) == []


def test_classify_sources_marks_a_config_duplicate_redundant(
    db_session, make_cfg, tmp_path, frozen_now, mocker
):
    cfg = make_cfg(tmp_path / "myrepo", egress_allow=["github.com"])
    egress_scope.add_repo_extra(db_session, cfg.container_prefix, "github.com", now=frozen_now)
    incus = mocker.MagicMock()

    rows = egress_scope.classify_sources(cfg, db_session, incus)

    override = next(r for r in rows if r.source == "repo-override")
    assert override.entry == "github.com"
    assert override.redundant


def test_classify_sources_includes_container_rows_only_when_asked(
    db_session, make_cfg, tmp_path, mocker
):
    cfg = make_cfg(tmp_path / "myrepo", egress_allow=["github.com"])
    incus = mocker.MagicMock()
    incus.config_get.return_value = '["nexus.corp:443"]'

    without = egress_scope.classify_sources(cfg, db_session, incus)
    assert all(r.source != "container" for r in without)

    with_ct = egress_scope.classify_sources(cfg, db_session, incus, container="myrepo-feat")
    assert [r.entry for r in with_ct if r.source == "container"] == ["nexus.corp:443"]


def test_render_config_block_emits_file_entries_then_overrides():
    block = egress_scope.render_config_block(
        ["github.com", "archive.ubuntu.com:443"],
        ["nexus.corp:443", "10.0.5.0/24"],
        prefix="myrepo",
    )
    assert (
        block.index("github.com")
        < block.index("archive.ubuntu.com:443")
        < block.index("nexus.corp:443")
        < block.index("10.0.5.0/24")
    )


def test_render_config_block_output_is_a_single_valid_egress_allow_key():
    import yaml

    block = egress_scope.render_config_block(["github.com"], ["nexus.corp:443"], prefix="myrepo")
    parsed = yaml.safe_load(block)
    assert parsed == {"egress_allow": ["github.com", "nexus.corp:443"]}


def test_render_config_block_dedupes_an_override_already_in_the_file():
    import yaml

    block = egress_scope.render_config_block(["github.com"], ["github.com"], prefix="myrepo")
    assert yaml.safe_load(block) == {"egress_allow": ["github.com"]}


def test_render_config_block_emits_no_key_when_there_is_nothing_to_promote():
    import yaml

    block = egress_scope.render_config_block(["github.com"], [], prefix="myrepo")
    assert yaml.safe_load(block) is None
    assert block.lstrip().startswith("#")


def _incus_with(mocker, *, extras, local_eth0=None):
    incus = mocker.MagicMock()
    incus.config_get.return_value = json.dumps(extras) if extras else None
    incus.list_containers.return_value = [
        {
            "name": "myrepo-feat",
            "status": "Running",
            "profiles": ["myrepo-base", "myrepo-net-strict"],
            "config": {},
            "devices": {"eth0": local_eth0} if local_eth0 else {},
        }
    ]
    return incus


def test_apply_container_acl_writes_acl_and_overrides_the_nic(make_cfg, tmp_path, mocker):
    import yaml

    from jailbee.network import ALLOWLIST_DESC_PREFIX

    mocker.patch(
        "jailbee.egress_scope.resolve_entries",
        return_value=[
            EgressEntry(destinations=["10.0.5.7"], port=443, description="nexus.corp:443")
        ],
    )
    cfg = make_cfg(tmp_path / "myrepo")
    incus = _incus_with(mocker, extras=["nexus.corp:443"])
    incus.network_acl_exists.return_value = True

    egress_scope.apply_container_acl(cfg, incus, "myrepo-feat", mode="strict")

    incus.network_acl_set_yaml.assert_called_once()
    acl_written, body_written = incus.network_acl_set_yaml.call_args[0]
    assert acl_written == "myrepo-feat-extra"
    # The YAML body is the enforcement payload: a wrong body silently allows
    # or blocks the wrong destinations, so parse it back and check the rule.
    parsed_body = yaml.safe_load(body_written)
    assert {
        "action": "allow",
        "destination": "10.0.5.7",
        "description": f"{ALLOWLIST_DESC_PREFIX}nexus.corp:443",
        "state": "enabled",
        "protocol": "tcp",
        "destination_port": "443",
    } in parsed_body["egress"]
    incus.config_device_override.assert_called_once_with(
        "myrepo-feat",
        "eth0",
        {
            "type": "nic",
            "network": "incusbr0",
            "security.acls": "myrepo-allowlist,myrepo-feat-extra",
        },
    )


def test_apply_container_acl_creates_the_acl_when_it_does_not_exist(make_cfg, tmp_path, mocker):
    """`network_acl_set_yaml` is `incus network acl edit`, which requires the
    ACL to already exist — the first materialisation on a real host must
    create it first."""
    mocker.patch(
        "jailbee.egress_scope.resolve_entries",
        return_value=[
            EgressEntry(destinations=["10.0.5.7"], port=443, description="nexus.corp:443")
        ],
    )
    cfg = make_cfg(tmp_path / "myrepo")
    incus = _incus_with(mocker, extras=["nexus.corp:443"])
    incus.network_acl_exists.return_value = False

    egress_scope.apply_container_acl(cfg, incus, "myrepo-feat", mode="strict")

    incus.network_acl_create.assert_called_once_with("myrepo-feat-extra")


def test_apply_container_acl_does_not_recreate_an_existing_acl(make_cfg, tmp_path, mocker):
    mocker.patch(
        "jailbee.egress_scope.resolve_entries",
        return_value=[
            EgressEntry(destinations=["10.0.5.7"], port=443, description="nexus.corp:443")
        ],
    )
    cfg = make_cfg(tmp_path / "myrepo")
    incus = _incus_with(mocker, extras=["nexus.corp:443"])
    incus.network_acl_exists.return_value = True

    egress_scope.apply_container_acl(cfg, incus, "myrepo-feat", mode="strict")

    incus.network_acl_create.assert_not_called()


def test_apply_container_acl_in_loose_mode_tears_the_override_down(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path / "myrepo")
    incus = _incus_with(mocker, extras=["nexus.corp:443"], local_eth0={"type": "nic"})
    incus.network_acl_exists.return_value = True

    egress_scope.apply_container_acl(cfg, incus, "myrepo-feat", mode="loose")

    incus.config_device_remove.assert_called_once_with("myrepo-feat", "eth0", missing_ok=True)
    incus.config_device_override.assert_not_called()
    # An orphan ACL left behind on every `jailbee net loose` is a real leak,
    # not just tidiness — assert the delete actually happens.
    incus.network_acl_delete.assert_called_once_with("myrepo-feat-extra")
    # And the ORDER matters: Incus refuses to delete an ACL still referenced
    # by an instance NIC, so the NIC device must come off first.
    methods = [call[0] for call in incus.mock_calls]
    assert methods.index("config_device_remove") < methods.index("network_acl_delete")


def test_apply_container_acl_with_no_extras_removes_acl_and_override(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path / "myrepo")
    incus = _incus_with(mocker, extras=[], local_eth0={"type": "nic"})
    incus.network_acl_exists.return_value = True

    egress_scope.apply_container_acl(cfg, incus, "myrepo-feat", mode="strict")

    incus.config_device_remove.assert_called_once_with("myrepo-feat", "eth0", missing_ok=True)
    incus.network_acl_delete.assert_called_once_with("myrepo-feat-extra")
    methods = [call[0] for call in incus.mock_calls]
    assert methods.index("config_device_remove") < methods.index("network_acl_delete")


def test_apply_container_acl_updates_an_existing_override_in_place(make_cfg, tmp_path, mocker):
    """Re-materialising must not detach the NIC of a running container."""
    mocker.patch(
        "jailbee.egress_scope.resolve_entries",
        return_value=[
            EgressEntry(destinations=["10.0.5.7"], port=443, description="nexus.corp:443")
        ],
    )
    cfg = make_cfg(tmp_path / "myrepo")
    incus = _incus_with(
        mocker,
        extras=["nexus.corp:443"],
        local_eth0={"type": "nic", "network": "incusbr0", "security.acls": "stale"},
    )

    egress_scope.apply_container_acl(cfg, incus, "myrepo-feat", mode="strict")

    incus.config_device_override.assert_not_called()
    incus.config_device_set.assert_called_once_with(
        "myrepo-feat",
        "eth0",
        {
            "type": "nic",
            "network": "incusbr0",
            "security.acls": "myrepo-allowlist,myrepo-feat-extra",
        },
    )


def test_drop_container_acl_deletes_only_an_existing_acl(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path / "myrepo")
    incus = mocker.MagicMock()
    incus.network_acl_exists.return_value = False

    egress_scope.drop_container_acl(cfg, incus, "myrepo-feat")

    incus.network_acl_delete.assert_not_called()
