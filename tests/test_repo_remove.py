from datetime import UTC, datetime
from unittest.mock import MagicMock

from sqlmodel import Session

from jailbee.db.models import RegisteredRepo
from jailbee.repo_remove import list_repos


def test_registered_missing_directory(db_engine, tmp_path):
    with Session(db_engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="gone",
                repo_root=str(tmp_path / "gone"),
                registered_at=datetime.now(UTC),
            )
        )
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
        ("foo", "leftover", 1),
        ("foo-bar", "leftover", 1),
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
            session.add(
                RegisteredRepo(
                    container_prefix=prefix, repo_root=str(root), registered_at=datetime.now(UTC)
                )
            )
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
    rows = list_repos(
        incus,
        lambda: Session(db_engine),
        scope=RemoteRepoScope(frozenset({"hidden", "hidden-leftover"})),
    )
    assert [(r.prefix, r.state, r.base) for r in rows] == [
        ("bad", "no config", "instance"),
        ("ok", "ok", "2.0 KB"),
    ]


def test_missing_config_does_not_synthesize(db_engine, tmp_path, mocker):
    with Session(db_engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="plain", repo_root=str(tmp_path), registered_at=datetime.now(UTC)
            )
        )
        session.commit()
    loader = mocker.patch("jailbee.repo_remove.load_repo_config")
    incus = MagicMock()
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.list_images.return_value = []
    assert list_repos(incus, lambda: Session(db_engine))[0].state == "no config"
    loader.assert_not_called()


def test_plan_only_exact_resources_and_stale_pools(db_engine, tmp_path, mocker):
    from jailbee.db.models import PoolIP
    from jailbee.global_config import GlobalConfig
    from jailbee.repo_remove import plan_removal

    now = datetime.now(UTC)
    with Session(db_engine) as session:
        for prefix in ("foo", "foo-bar"):
            session.add(
                RegisteredRepo(
                    container_prefix=prefix, repo_root=str(tmp_path / prefix), registered_at=now
                )
            )
        for owner in ("foo", "ct:foo-gone", "ct:foo-bar-gone"):
            session.add(
                PoolIP(
                    container_prefix=owner,
                    hostname="example.com",
                    ip="1.2.3.4",
                    first_seen=now,
                    last_seen=now,
                )
            )
        session.commit()
    mocker.patch("jailbee.repo_remove.load_global_config", return_value=(GlobalConfig(), []))
    incus = MagicMock()
    incus.profile_list.return_value = ["foo-base", "foo-bar-base", "foo-binds"]
    incus.network_acl_list.return_value = ["foo-allowlist", "foo-bar-allowlist", "foo-one-extra"]
    incus.list_containers.return_value = [
        {"name": "foo-one", "profiles": ["foo-base"]},
        {"name": "foo-bar-one", "profiles": ["foo-bar-base"]},
    ]
    incus.list_images.return_value = [
        {
            "aliases": [
                {"name": "foo-base"},
                {"name": "foo-base-2026-01-01"},
                {"name": "foo-bar-base"},
            ]
        }
    ]
    plan = plan_removal("foo", incus, lambda: Session(db_engine))
    assert plan.containers == ["foo-one"]
    assert plan.profiles == ["foo-base", "foo-binds"]
    assert set(plan.acls) == {"foo-allowlist", "foo-one-extra"}
    assert set(plan.golden_images) == {"foo-base", "foo-base-2026-01-01"}
    assert plan.state_counts["pool_ip"] == 2


def test_synthetic_registration_loads_alias(db_engine, tmp_path, mocker, make_cfg):
    with Session(db_engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="scratch",
                repo_root=str(tmp_path),
                registered_at=datetime.now(UTC),
                synthetic_config=True,
            )
        )
        session.commit()
    cfg = make_cfg(tmp_path, container_prefix="scratch", golden={"alias": "scratch-image"})
    mocker.patch("jailbee.repo_remove.load_repo_config", return_value=cfg)
    incus = MagicMock()
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.list_images.return_value = [{"aliases": [{"name": "scratch-image"}], "size": 2048}]
    row = list_repos(incus, lambda: Session(db_engine))[0]
    assert (row.state, row.base) == ("ok", "2.0 KB")


def test_plan_shared_alias_and_overridden_data(db_engine, tmp_path, mocker, make_cfg):
    from jailbee.global_config import GlobalConfig
    from jailbee.repo_remove import plan_removal

    for name in ("foo", "other"):
        root = tmp_path / name
        root.mkdir()
        (root / ".jailbee").mkdir()
        (root / ".jailbee" / "config.yaml").touch()
        with Session(db_engine) as session:
            session.add(
                RegisteredRepo(
                    container_prefix=name, repo_root=str(root), registered_at=datetime.now(UTC)
                )
            )
            session.commit()
    shared = tmp_path / "custom"
    shared.mkdir()
    configs = {
        name: make_cfg(
            tmp_path / name, container_prefix=name, golden={"alias": "same"}, shared_dir=shared
        )
        for name in ("foo", "other")
    }
    mocker.patch(
        "jailbee.repo_remove.load_repo_config", side_effect=lambda root: configs[root.name]
    )
    mocker.patch(
        "jailbee.repo_remove.load_global_config",
        return_value=(GlobalConfig(credentials={"repos": {"foo": None}}), []),
    )
    incus = MagicMock()
    incus.profile_list.return_value = []
    incus.list_containers.return_value = []
    incus.network_acl_list.return_value = []
    plan = plan_removal("foo", incus, lambda: Session(db_engine), purge=True)
    assert plan.golden_kept_reason == "shared with other"
    assert plan.golden_images == []
    assert shared in plan.kept_files
    assert shared not in plan.purge_files
    assert plan.global_refs == ["credentials.repos"]


def test_scratch_golden_is_kept_and_not_counted_as_container(db_engine, tmp_path, mocker, make_cfg):
    from jailbee.config import SCRATCH_BASE_ALIAS
    from jailbee.global_config import GlobalConfig
    from jailbee.repo_remove import plan_removal

    with Session(db_engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix="foo",
                repo_root=str(tmp_path),
                registered_at=datetime.now(UTC),
                synthetic_config=True,
            )
        )
        session.commit()
    cfg = make_cfg(tmp_path, container_prefix="foo", golden={"alias": SCRATCH_BASE_ALIAS})
    mocker.patch("jailbee.repo_remove.load_repo_config", return_value=cfg)
    mocker.patch("jailbee.repo_remove.load_global_config", return_value=(GlobalConfig(), []))
    incus = MagicMock()
    incus.profile_list.return_value = ["foo-base"]
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = [
        {"name": SCRATCH_BASE_ALIAS + "--golden", "profiles": ["foo-base"]}
    ]
    plan = plan_removal("foo", incus, lambda: Session(db_engine))
    assert plan.containers == []
    assert plan.golden_instances == []
    assert plan.golden_kept_reason == "shared scratch base"
