"""Tests for jailbee.egress_scope."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from jailbee import egress_scope


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
