from unittest.mock import MagicMock

import pytest

from jailbee import aliases
from tests.conftest import make_cfg


def _raw(name, alias=None, prefix="myrepo"):
    config = {"user.jailbee.alias": alias} if alias else {}
    return {"name": name, "profiles": ["default", f"{prefix}-base"], "config": config}


@pytest.fixture
def cfg(tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    return make_cfg(repo)


def test_set_alias_writes_label(cfg):
    incus = MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("myrepo-b")]
    aliases.set_alias(cfg, incus, "myrepo-a", "login")
    incus.config_set.assert_called_once_with("myrepo-a", "user.jailbee.alias", "login")


@pytest.mark.parametrize("bad", ["", "Login", "-x", "x-", "a/b", "a_b"])
def test_set_alias_rejects_bad_charset(cfg, bad):
    incus = MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a")]
    with pytest.raises(aliases.AliasError):
        aliases.set_alias(cfg, incus, "myrepo-a", bad)
    incus.config_set.assert_not_called()


def test_set_alias_rejects_other_containers_short_name(cfg):
    incus = MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("myrepo-b")]
    with pytest.raises(aliases.AliasError, match="'b' is a container"):
        aliases.set_alias(cfg, incus, "myrepo-a", "b")


def test_set_alias_rejects_other_containers_alias(cfg):
    incus = MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("myrepo-b", alias="login")]
    with pytest.raises(aliases.AliasError, match="alias of 'b'"):
        aliases.set_alias(cfg, incus, "myrepo-a", "login")


def test_set_alias_to_own_short_name_clears(cfg):
    incus = MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a", alias="login")]
    aliases.set_alias(cfg, incus, "myrepo-a", "a")
    incus.config_unset.assert_called_once_with("myrepo-a", "user.jailbee.alias")
    incus.config_set.assert_not_called()


def test_set_alias_ignores_foreign_repo_names(cfg):
    incus = MagicMock()
    incus.list_containers.return_value = [_raw("myrepo-a"), _raw("other-login", prefix="other")]
    aliases.set_alias(cfg, incus, "myrepo-a", "login")
    incus.config_set.assert_called_once()


def test_clear_alias_unsets_label():
    incus = MagicMock()
    aliases.clear_alias(incus, "myrepo-a")
    incus.config_unset.assert_called_once_with("myrepo-a", "user.jailbee.alias")
