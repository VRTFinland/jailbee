"""The two-layer Claude credential group model."""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee import claude_groups
from tests.conftest import make_cfg


def _cfg(tmp_path: Path, group_dir: Path | None = None):
    cfg = make_cfg(tmp_path / "myrepo", shared_dir=tmp_path / "shared")
    if group_dir is not None:
        cfg = cfg.model_copy(update={"claude_credentials_dir": group_dir})
    return cfg


def test_validate_accepts_a_normal_name():
    assert claude_groups.validate_group_name("work") == "work"
    assert claude_groups.validate_group_name("work-2") == "work-2"


@pytest.mark.parametrize("bad", ["Work", "-work", "_work", "work_2", "work/x", ""])
def test_validate_rejects_names_outside_the_grammar(bad: str):
    with pytest.raises(claude_groups.GroupError):
        claude_groups.validate_group_name(bad)


def test_validate_rejects_the_reserved_word_none():
    with pytest.raises(claude_groups.GroupError) as e:
        claude_groups.validate_group_name("none")
    assert "reserved" in str(e.value).lower()


def test_repo_group_is_the_resolved_directory_name(tmp_path: Path):
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.repo_group(cfg) == "work"


def test_repo_group_is_none_when_the_repo_shares_nothing(tmp_path: Path):
    assert claude_groups.repo_group(_cfg(tmp_path)) is None


def test_container_override_absent_label_means_inherit(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    assert claude_groups.container_override(incus, "myrepo-x") is None


def test_container_override_names_a_group(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = "personal"
    assert claude_groups.container_override(incus, "myrepo-x") == claude_groups.Override("personal")


def test_container_override_no_group_sentinel(mocker):
    incus = mocker.MagicMock()
    incus.config_get.return_value = claude_groups.NO_GROUP
    assert claude_groups.container_override(incus, "myrepo-x") == claude_groups.Override(None)


def test_container_override_ignores_a_garbage_label(mocker):
    """A hand-edited label must not resolve to a path outside the store."""
    incus = mocker.MagicMock()
    incus.config_get.return_value = "../../etc"
    assert claude_groups.container_override(incus, "myrepo-x") is None


def test_effective_group_prefers_the_container(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = "personal"
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.effective_group(cfg, incus, "myrepo-x") == "personal"


def test_effective_group_falls_back_to_the_repo(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = None
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.effective_group(cfg, incus, "myrepo-x") == "work"


def test_effective_group_container_can_opt_out_of_the_repos_group(mocker, tmp_path: Path):
    incus = mocker.MagicMock()
    incus.config_get.return_value = claude_groups.NO_GROUP
    cfg = _cfg(tmp_path, tmp_path / "creds" / "work")
    assert claude_groups.effective_group(cfg, incus, "myrepo-x") is None


def test_group_dir_is_a_sibling_of_the_parked_store(monkeypatch, tmp_path: Path):
    from jailbee import claude_pool

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert claude_groups.group_dir("work").parent == claude_pool.store_dir().parent
