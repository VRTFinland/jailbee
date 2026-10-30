import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import make_cfg


@pytest.mark.parametrize("work", [False, True])
def test_expired_egress_removed_full_acl_path(tmp_path, mocker, work):
    from jailbee.egress_revert import check_and_revert_egress
    from jailbee.egress_scope import EGRESS_UNTIL

    cfg = make_cfg(tmp_path)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    store = {"user.jailbee.egress_extra": '["pypi.org", "keep.org"]',
             EGRESS_UNTIL: json.dumps({"pypi.org": (now-timedelta(seconds=1)).isoformat(),
                                      "keep.org": (now+timedelta(hours=1)).isoformat()})}
    incus = mocker.Mock()
    incus.list_containers.return_value = [{"name": "c", "profiles": [f"{cfg.container_prefix}-base"]}]
    incus.config_get.side_effect = lambda name, key: store.get(key)
    incus.config_set.side_effect = lambda name, key, value: store.__setitem__(key, value)
    incus.config_unset.side_effect = lambda name, key: store.pop(key, None)
    mocker.patch("jailbee.network_generation.generation_of", return_value="work" if work else "legacy")
    mocker.patch("jailbee.lifecycle.current_network_mode", return_value="strict")
    legacy = mocker.patch("jailbee.egress_scope.apply_container_acl")
    acl = mocker.patch("jailbee.work_acl.apply_work_container_acl")
    mocker.patch("jailbee.work_acl.reconcile_work_acl")
    mocker.patch("jailbee.egress_scope._repin_hosts_quietly")
    mocker.patch("jailbee.egress_proxy.sync_container", return_value=False)
    check_and_revert_egress(cfg, incus, now=now)
    assert json.loads(store["user.jailbee.egress_extra"]) == ["keep.org"]
    assert list(json.loads(store[EGRESS_UNTIL])) == ["keep.org"]
    assert acl.called == work
    assert legacy.called != work


@pytest.mark.parametrize("held", [True, False])
def test_egress_orphan_expiry_and_autostart(tmp_path, mocker, held):
    from jailbee.egress_revert import check_and_revert_egress
    from jailbee.egress_scope import EGRESS_UNTIL

    cfg = make_cfg(tmp_path)
    now = datetime(2026, 10, 30, tzinfo=UTC)
    incus = mocker.Mock()
    incus.list_containers.return_value = [{"name": "c", "profiles": [f"{cfg.container_prefix}-base"]}]
    incus.config_get.side_effect = lambda name, key: json.dumps({"gone.org": now.isoformat()}) if key == EGRESS_UNTIL else None
    mocker.patch("jailbee.egress_revert.autostart_holds", return_value=held)
    check_and_revert_egress(cfg, incus, now=now)
    assert incus.config_unset.called != held


def test_expiry_failure_preserves_deadline_for_retry(tmp_path, mocker):
    from jailbee.egress_scope import EGRESS_UNTIL, remove_container_entry

    incus = mocker.Mock()
    incus.config_get.side_effect = lambda n, k: '["pypi.org"]' if k == "user.jailbee.egress_extra" else '{"pypi.org":"2026-10-30T00:00:00+00:00"}'
    mocker.patch("jailbee.egress_scope._sync_container_entry", side_effect=ValueError("ACL"))
    with pytest.raises(ValueError, match="ACL"):
        remove_container_entry(make_cfg(tmp_path), incus, "c", "pypi.org")
    assert not any(c.args[1] == EGRESS_UNTIL for c in incus.config_unset.call_args_list)
