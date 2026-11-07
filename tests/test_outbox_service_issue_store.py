"""Issue proposal deletion through the host store: row versions guard it."""

from __future__ import annotations

import json

import pytest

from jailbee.db import state_dir
from jailbee.outbox import service, store_db
from jailbee.outbox.delete import DeleteSelection, plan_delete
from jailbee.outbox.models import OutboxChanged, ProposalId
from tests.outbox_support import IDENTITY, issue_files, store
from tests.test_outbox_service import _stage_in_host_store

UUID = "uuid-1"


def _env(mocker, make_cfg, tmp_path):
    incus = mocker.Mock()
    incus.list_containers.return_value = [
        {
            "name": IDENTITY.full_name,
            "created_at": IDENTITY.created_at,
            "config": {"volatile.uuid": UUID},
        }
    ]
    mocker.patch.object(service, "read_store", return_value=store("pr", {}))
    return make_cfg(tmp_path), incus, None


def _plan(cfg, incus, journals, name, selection=None):
    view = service.load_container(cfg, incus, IDENTITY.full_name)
    return plan_delete(view, ProposalId("issue", name), selection or DeleteSelection())


def _rows():
    return store_db.read_proposals(state_dir() / "orchestration", UUID, "issue")


def test_whole_deletion_removes_the_row_and_its_bodies(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    name = _stage_in_host_store(UUID)
    plan = _plan(cfg, incus, journals, name)
    service.execute_delete(cfg, incus, IDENTITY.full_name, plan)
    assert _rows() == []


def test_a_row_changed_after_planning_is_refused(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    name = _stage_in_host_store(UUID)
    plan = _plan(cfg, incus, journals, name)
    # Same text, new row version: only the version distinguishes the two.
    (row,) = _rows()
    store_db.host_store(state_dir() / "orchestration").edit_proposal(
        UUID, "issue", name, row.revision, row.manifest
    )
    with pytest.raises(OutboxChanged):
        service.execute_delete(cfg, incus, IDENTITY.full_name, plan)
    assert len(_rows()) == 1


def test_action_deletion_edits_the_row_and_bumps_its_version(mocker, make_cfg, tmp_path):
    cfg, incus, journals = _env(mocker, make_cfg, tmp_path)
    name = _stage_in_host_store(UUID)
    plan = _plan(cfg, incus, journals, name, DeleteSelection(action=2))
    service.execute_delete(cfg, incus, IDENTITY.full_name, plan)
    (row,) = _rows()
    assert row.revision == 2
    original = len(json.loads(issue_files()["001.json"])["actions"])
    assert len(json.loads(row.manifest)["actions"]) == original - 1
