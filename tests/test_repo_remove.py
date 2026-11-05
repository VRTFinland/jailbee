from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlmodel import Session

from jailbee.db.models import RegisteredRepo
from jailbee.repo_remove import list_repos


def test_registered_missing_directory(db_engine, tmp_path):
    with Session(db_engine) as session:
        session.add(RegisteredRepo(container_prefix="gone", repo_root=str(tmp_path / "gone"), registered_at=datetime.now(UTC)))
        session.commit()
    incus = MagicMock()
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.list_images.return_value = []
    rows = list_repos(incus, lambda: Session(db_engine))
    assert [(r.prefix, r.state, r.base) for r in rows] == [("gone", "dir missing", None)]


def test_leftovers_and_exact_container_ownership(db_engine):
    incus = MagicMock()
    incus.profile_list.return_value = ["foo-base", "foo-bar-base", "default"]
    incus.list_containers.return_value = [
        {"name": "one", "profiles": ["foo-bar-base", "foo-base"]},
        {"name": "two", "profiles": ["foo-base"]},
        {"name": "three", "profiles": None},
    ]
    incus.list_images.return_value = []
    rows = list_repos(incus, lambda: Session(db_engine))
    assert [(r.prefix, r.state, r.containers) for r in rows] == [
        ("foo", "leftover", 1), ("foo-bar", "leftover", 1)
    ]
    incus.list_containers.assert_called_once_with(fast=True)


def test_config_alias_sizes_scope_and_invalid_config(db_engine, tmp_path, mocker, make_cfg):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    for prefix in ("ok", "bad", "hidden"):
        root = tmp_path / prefix
        root.mkdir()
        (root / ".jailbee").mkdir()
        (root / ".jailbee" / "config.yaml").write_text("bad: [")
        with Session(db_engine) as session:
            session.add(RegisteredRepo(container_prefix=prefix, repo_root=str(root), registered_at=datetime.now(UTC)))
            session.commit()
    cfg = make_cfg(tmp_path / "ok", golden={"alias": "custom"})
    def load(root):
        if root.name == "ok":
            return cfg
        raise ValueError("invalid config")
    mocker.patch("jailbee.repo_remove.load_repo_config", side_effect=load)
    incus = MagicMock()
    incus.profile_list.return_value = ["hidden-leftover-base"]
    incus.list_containers.return_value = [{"name": "bad-base--golden", "profiles": []}]
    incus.list_images.return_value = [{"aliases": [{"name": "custom"}], "size": 2048}]
    rows = list_repos(incus, lambda: Session(db_engine), scope=RemoteRepoScope(frozenset({"hidden", "hidden-leftover"})))
    assert [(r.prefix, r.state, r.base) for r in rows] == [("bad", "no config", "instance"), ("ok", "ok", "2.0 KB")]


def test_missing_config_does_not_synthesize(db_engine, tmp_path, mocker):
    with Session(db_engine) as session:
        session.add(RegisteredRepo(container_prefix="plain", repo_root=str(tmp_path), registered_at=datetime.now(UTC)))
        session.commit()
    loader = mocker.patch("jailbee.repo_remove.load_repo_config")
    incus = MagicMock()
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.list_images.return_value = []
    assert list_repos(incus, lambda: Session(db_engine))[0].state == "no config"
    loader.assert_not_called()
