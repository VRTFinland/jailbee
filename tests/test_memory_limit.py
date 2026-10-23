from unittest.mock import Mock

import pytest

from jailbee.memory_limit import (
    SizeOption,
    below_usage,
    migrate_pinned,
    same_size,
    set_limit,
    size_options,
    validate_size,
)

GiB = 1024**3


@pytest.mark.parametrize("failure", [RuntimeError("usage unavailable"), None])
def test_run_mem_does_not_write_when_usage_gather_fails(mocker, make_cfg, tmp_path, failure):
    from jailbee.memory_limit import run_mem

    cfg = make_cfg(tmp_path)
    incus = Mock()
    mocker.patch("jailbee.lifecycle.list_containers", side_effect=failure, return_value=[])
    with pytest.raises((RuntimeError, ValueError)):
        run_mem(cfg, incus, "missing", "8GiB", force=False, confirm=Mock(), interactive=False)
    incus.config_set.assert_not_called()
    incus.config_unset_checked.assert_not_called()


@pytest.mark.parametrize("size", ["24GiB", "default"])
def test_run_mem_propagates_write_errors(mocker, make_cfg, tmp_path, size):
    from types import SimpleNamespace

    from jailbee.memory_limit import run_mem

    cfg = make_cfg(tmp_path)
    incus = Mock()
    incus.config_set.side_effect = RuntimeError("set failed")
    incus.config_unset_checked.side_effect = RuntimeError("unset failed")
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[SimpleNamespace(name="box", memory_usage=None)])
    with pytest.raises(RuntimeError, match="failed"):
        run_mem(cfg, incus, "box", size, force=False, confirm=Mock(), interactive=False)



def test_validate_incus_sizes_and_percentages() -> None:
    assert validate_size("24GiB") is None
    assert validate_size("50%") is None
    assert validate_size("lots") is not None
    assert validate_size("0GiB") is not None
    assert validate_size("150%") is not None


def test_same_size_compares_bytes_or_exact_unparsed_values() -> None:
    assert same_size("16384MiB", "16GiB")
    assert not same_size("50%", "16GiB")
    assert same_size("50%", "50%")


def test_size_options_show_default_and_host_eligible_presets() -> None:
    options = size_options(current="16GiB", override=False, default="16GiB", host_total=20 * GiB)

    assert options == [
        SizeOption("Default (16GiB)", "default", current=True),
        SizeOption("4GiB", "4GiB"),
        SizeOption("8GiB", "8GiB"),
        SizeOption("16GiB", "16GiB"),
        SizeOption("Other…", "other"),
    ]


def test_size_options_insert_non_preset_override_in_byte_order() -> None:
    options = size_options(current="12GiB", override=True, default="16GiB", host_total=20 * GiB)

    assert [option.value for option in options] == [
        "default",
        "4GiB",
        "8GiB",
        "12GiB",
        "16GiB",
        "other",
    ]
    assert options[3].current


def test_size_options_show_all_presets_when_host_total_is_unknown() -> None:
    options = size_options(current=None, override=False, default="16GiB", host_total=None)

    assert [option.value for option in options] == [
        "default",
        "4GiB",
        "8GiB",
        "16GiB",
        "24GiB",
        "32GiB",
        "other",
    ]


def test_below_usage_requires_known_fixed_size_and_usage() -> None:
    assert below_usage("4GiB", 5 * GiB)
    assert not below_usage("50%", 5 * GiB)
    assert not below_usage("4GiB", None)


def test_set_limit_sets_or_unsets_instance_key() -> None:
    incus = Mock()

    set_limit(incus, "box", "24GiB")
    set_limit(incus, "box", None)

    incus.config_set.assert_called_once_with("box", "limits.memory", "24GiB")
    incus.config_unset_checked.assert_called_once_with("box", "limits.memory")


def test_migrate_pinned_unsets_only_default_sized_repo_containers() -> None:
    incus = Mock()
    raws = [
        {"name": "a", "profiles": ["repo-base"], "config": {"limits.memory": "16384MiB"}},
        {"name": "b", "profiles": ["repo-base"], "config": {"limits.memory": "24GiB"}},
        {"name": "c", "profiles": [], "config": {"limits.memory": "16GiB"}},
        {"name": "d", "profiles": ["repo-base"], "config": {}},
    ]

    assert migrate_pinned(incus, raws, default="16GiB", base_profile="repo-base") == ["a"]
    incus.config_unset_checked.assert_called_once_with("a", "limits.memory")
