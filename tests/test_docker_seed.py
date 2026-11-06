from unittest.mock import Mock

import pytest

from jailbee import docker_seed, golden
from jailbee.incus import IncusError


@pytest.fixture
def rig(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    incus = Mock()
    incus.exists.return_value = False
    monkeypatch.setattr(docker_seed, "record_outcome", Mock())
    monkeypatch.setattr(docker_seed, "stop_container", Mock())
    monkeypatch.setattr(golden, "promote_instance", Mock())
    monkeypatch.setattr(golden, "promote_image", Mock())
    monkeypatch.setattr(golden, "_strip_local_nics", Mock())
    monkeypatch.setattr(golden, "resolve_golden_placement", lambda *args: golden.GoldenPlacement("pool", "btrfs"))
    def execute(name, cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "ls"] and "--format" in cmd:
            return "app:latest\n"
        if cmd[:3] == ["docker", "system", "df"]:
            return '{"Type":"Images","Size":"1.2GB"}\n'
        return ""
    incus.exec.side_effect = execute
    return cfg, incus


def test_instance_seeds_and_promotes(rig):
    cfg, incus = rig
    req = docker_seed.SeedRequest("repo-source", False, True)
    result = docker_seed.seed_golden(cfg, incus, req, deadline=docker_seed.time.monotonic() + 60)
    assert (result.outcome, result.image_count, result.size_bytes) == ("seeded", 1, 1200000000)
    staging = golden.golden_seeding_name(cfg.golden.alias)
    incus.copy.assert_called_once_with(golden.golden_instance_name(cfg.golden.alias), staging, instance_only=True, config={"security.nesting": "true"})
    incus.exec_pipe.assert_called_once()
    golden.promote_instance.assert_called_once_with(incus, staging, cfg.golden.alias)
    docker_seed.record_outcome.assert_called_once_with(cfg, result)


def test_image_mode_and_cleanup(rig, monkeypatch):
    cfg, incus = rig
    monkeypatch.setattr(golden, "resolve_golden_placement", lambda *args: golden.GoldenPlacement("pool", "dir"))
    result = docker_seed.seed_golden(cfg, incus, docker_seed.SeedRequest(None, False, False), deadline=docker_seed.time.monotonic() + 60, source_name="ephemeral")
    assert result.outcome == "seeded" and result.source == "seed-container"
    incus.init.assert_called_once()
    golden.promote_image.assert_called_once()
    incus.delete.assert_called_once_with(golden.golden_seeding_name(cfg.golden.alias), force=True)


def test_filters_tagged_images(rig):
    _, incus = rig
    incus.exec.side_effect = None
    incus.exec.return_value = "<none>:x\napp:<none>\napp:v1\nother:v1\napp:v1\n"
    assert docker_seed.list_tagged_images(incus, "source", ["app:*"]) == ["app:v1"]


def test_no_images_skips(rig):
    cfg, incus = rig
    incus.exec.side_effect = None
    incus.exec.return_value = ""
    result = docker_seed.seed_golden(cfg, incus, docker_seed.SeedRequest("source", False, True), deadline=docker_seed.time.monotonic() + 60)
    assert result.outcome == "skipped"
    incus.copy.assert_not_called()
    docker_seed.record_outcome.assert_called_once_with(cfg, result)


def test_failure_cleans_staging_and_records(rig):
    cfg, incus = rig
    incus.exec_pipe.side_effect = IncusError("pipe failed")
    result = docker_seed.seed_golden(cfg, incus, docker_seed.SeedRequest("source", False, True), deadline=docker_seed.time.monotonic() + 60)
    assert result.outcome == "failed" and "pipe failed" in result.detail
    incus.delete.assert_called_once_with(golden.golden_seeding_name(cfg.golden.alias), force=True)
    golden.promote_instance.assert_not_called()
    docker_seed.record_outcome.assert_called_once_with(cfg, result)


def test_preflight(rig, monkeypatch):
    cfg, incus = rig
    monkeypatch.setattr(golden, "repo_uses_docker", lambda cfg: True)
    monkeypatch.setattr(docker_seed, "shared_golden_reason_for", lambda cfg: "shared with other")
    with pytest.raises(docker_seed.SeedPreflightError, match="shared"):
        docker_seed.preflight(cfg, incus, docker_seed.SeedRequest(None, False, False))
    monkeypatch.setattr(docker_seed, "shared_golden_reason_for", lambda cfg: None)
    incus.list_containers.return_value = [{"name": "source", "status": "Stopped"}]
    with pytest.raises(docker_seed.SeedPreflightError, match="not running"):
        docker_seed.preflight(cfg, incus, docker_seed.SeedRequest("source", False, True))
    incus.exists.return_value = True
    with pytest.raises(docker_seed.SeedPreflightError, match="already exists"):
        docker_seed.preflight(cfg, incus, docker_seed.SeedRequest(None, False, False))
    monkeypatch.setattr(golden, "repo_uses_docker", lambda cfg: False)
    with pytest.raises(docker_seed.SeedPreflightError, match="Docker"):
        docker_seed.preflight(cfg, incus, docker_seed.SeedRequest(None, False, False))
