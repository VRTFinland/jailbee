import json
from unittest.mock import MagicMock

import pytest


def test_round_trip_and_drop():
    from jailbee.branch_pr import BRANCH_PRS_LABEL, BranchPr, BranchPrMap

    incus = MagicMock()
    labels = {}
    incus.config_get.side_effect = lambda full, key: labels.get(key)
    incus.config_set.side_effect = lambda full, key, value: labels.update({key: value})
    incus.config_unset_checked.side_effect = lambda full, key: labels.pop(key, None)
    state = BranchPrMap(incus, "c")
    a = BranchPr("a", 42, "main", "src/a", "a" * 40)
    b = BranchPr("b", 43, "main", "src/b", "b" * 40, False, True)
    state.put(a)
    state.put(b)
    assert state.read() == {"a": a, "b": b}
    assert state.get("a") == a
    assert state.numbers() == frozenset({42, 43})
    assert state.drop("a") is True
    assert state.read() == {"b": b}
    assert state.drop("missing") is False
    assert state.drop("b") is True
    incus.config_unset_checked.assert_called_once_with("c", BRANCH_PRS_LABEL)
    assert incus.config_set.call_count == 3
    assert state.read() == {}


@pytest.mark.parametrize("value", ["not json", "[]", "null", json.dumps({"a": {}, "b": 1})])
def test_malformed_label_is_ignored(value):
    from jailbee.branch_pr import BranchPrMap

    incus = MagicMock()
    incus.config_get.return_value = value
    assert BranchPrMap(incus, "c").read() == {}


def test_malformed_entries_are_skipped():
    from jailbee.branch_pr import BranchPrMap

    incus = MagicMock()
    incus.config_get.return_value = json.dumps({
        "ok": {"head": "ok", "pr": 42, "base": "main", "source": "s", "source_sha": "x"},
        "bad": {"head": "bad", "pr": True, "base": "main", "source": "s", "source_sha": "x"},
    })
    assert list(BranchPrMap(incus, "c").read()) == ["ok"]
