from unittest.mock import Mock

import pytest

from jailbee import docker_seed, golden
from jailbee.incus import IncusError


@pytest.fixture
def rig(make_cfg, tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    incus = Mock()
    incus.with_deadline.return_value = incus
    incus.exists.return_value = False
    monkeypatch.setattr(golden, "recover_seed_leftovers", Mock())
    monkeypatch.setattr(docker_seed, "record_outcome", Mock())
    monkeypatch.setattr(docker_seed, "stop_container", Mock())
    monkeypatch.setattr(golden, "promote_instance", Mock())
    monkeypatch.setattr(golden, "promote_image", Mock())
    monkeypatch.setattr(golden, "_strip_local_nics", Mock())
    monkeypatch.setattr(
        golden, "resolve_golden_placement", lambda *args: golden.GoldenPlacement("pool", "btrfs")
    )

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
    incus.copy.assert_called_once_with(
        golden.golden_instance_name(cfg.golden.alias),
        staging,
        instance_only=True,
        config={"security.nesting": "true"},
    )
    incus.exec_pipe.assert_called_once()
    golden.promote_instance.assert_called_once_with(
        incus, staging, cfg.golden.alias, cleanup_incus=incus
    )
    docker_seed.record_outcome.assert_called_once_with(cfg, result)


def test_image_mode_and_cleanup(rig, monkeypatch):
    cfg, incus = rig
    monkeypatch.setattr(
        golden, "resolve_golden_placement", lambda *args: golden.GoldenPlacement("pool", "dir")
    )
    result = docker_seed.seed_golden(
        cfg,
        incus,
        docker_seed.SeedRequest(None, False, False),
        deadline=docker_seed.time.monotonic() + 60,
        source_name="ephemeral",
    )
    assert result.outcome == "seeded" and result.source == "seed-container"
    incus.init.assert_called_once()
    golden.promote_image.assert_called_once()
    incus.delete.assert_called_once_with(golden.golden_seeding_name(cfg.golden.alias), force=True)


def test_seed_uses_deadline_client_but_cleans_up_with_original(rig):
    cfg, incus = rig
    bounded = Mock()
    bounded.exists.return_value = False
    bounded.exec.side_effect = incus.exec.side_effect
    bounded.exec_pipe.side_effect = IncusError("deadline expired")
    incus.with_deadline.return_value = bounded
    deadline = docker_seed.time.monotonic() + 60

    result = docker_seed.seed_golden(
        cfg, incus, docker_seed.SeedRequest("source", False, True), deadline=deadline
    )

    assert result.outcome == "failed"
    incus.with_deadline.assert_called_once_with(deadline)
    golden.recover_seed_leftovers.assert_called_once_with(cfg, bounded)
    bounded.copy.assert_called_once()
    bounded.delete.assert_not_called()
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
    result = docker_seed.seed_golden(
        cfg,
        incus,
        docker_seed.SeedRequest("source", False, True),
        deadline=docker_seed.time.monotonic() + 60,
    )
    assert result.outcome == "skipped"
    incus.copy.assert_not_called()
    docker_seed.record_outcome.assert_called_once_with(cfg, result)


def test_failure_cleans_staging_and_records(rig):
    cfg, incus = rig
    incus.exec_pipe.side_effect = IncusError("pipe failed")
    result = docker_seed.seed_golden(
        cfg,
        incus,
        docker_seed.SeedRequest("source", False, True),
        deadline=docker_seed.time.monotonic() + 60,
    )
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


def test_create_seed_options_and_fallback(rig, monkeypatch):
    from jailbee import create_flow, git, lifecycle

    cfg, incus = rig
    remote = Mock(return_value="upstream-sha")
    local = Mock(return_value="local-sha")
    monkeypatch.setattr(git, "rev_parse_remote", remote)
    monkeypatch.setattr(git, "rev_parse", local)
    extras = create_flow.HostExtras(("127.0.0.1", 123), None, {"x": 1}, ())
    monkeypatch.setattr(create_flow, "host_extras", Mock(return_value=extras))
    prepare = Mock()
    monkeypatch.setattr(create_flow, "prepare_host", prepare)
    create = Mock(return_value="seed")
    monkeypatch.setattr(lifecycle, "new_container", create)
    assert docker_seed.create_seed_container(cfg, incus, Mock()) == "seed"
    confirm = create.call_args.kwargs["confirm_fn"]
    assert callable(confirm) and confirm("Unexpected escalation?") is False
    opts = create.call_args.args[2]
    assert opts.clone_commit == "upstream-sha"
    assert opts.skip_agents and opts.autostart_services == []
    assert opts.autostart_override == "wait" and opts.cpu == cfg.defaults.cpu
    assert (
        opts.mirror_endpoint == extras.mirror_endpoint
        and opts.litellm_payload == extras.litellm_payload
    )
    assert prepare.call_args.kwargs["interaction"] == create_flow.Interaction.none()
    local.assert_not_called()
    remote.return_value = None
    docker_seed.create_seed_container(cfg, incus, Mock())
    assert create.call_args.args[2].clone_commit == "local-sha"


def test_ephemeral_source_clears_inherited_images_before_autostart(rig, monkeypatch):
    from jailbee import create_flow, git, lifecycle

    cfg, incus = rig
    name = f"{cfg.container_prefix}-docker-seed"
    images = {"stale:v1"}
    events = []
    monkeypatch.setattr(git, "rev_parse_remote", Mock(return_value="upstream-sha"))
    monkeypatch.setattr(create_flow, "host_extras", Mock(
        return_value=create_flow.HostExtras(None, None, None, ())
    ))
    monkeypatch.setattr(create_flow, "prepare_host", Mock())

    def execute(container, cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "ls"]:
            return "\n".join(images)
        if cmd[:4] == ["docker", "image", "rm", "-f"]:
            images.difference_update(cmd[4:])
            events.append("clear")
        return ""

    def create(_cfg, _incus, opts, **kwargs):
        kwargs.get("on_phase", lambda phase: None)("autostart")
        events.append("autostart")
        images.add("current:v2")
        return name

    incus.exec.side_effect = execute
    monkeypatch.setattr(lifecycle, "new_container", create)
    assert docker_seed.create_seed_container(cfg, incus, Mock()) == name
    assert images == {"current:v2"}
    assert events == ["clear", "autostart"]


@pytest.mark.parametrize("keep,fail", [(False, False), (False, True), (True, False)])
def test_run_seed_cleanup(rig, monkeypatch, keep, fail):
    from jailbee import lifecycle

    cfg, incus = rig
    name = f"{cfg.container_prefix}-docker-seed"
    incus.exists.side_effect = [False, True]
    def create(_cfg, _incus, _gcfg, *, on_created):
        on_created(name)
        if fail:
            raise RuntimeError("create failed")
        return name

    monkeypatch.setattr(docker_seed, "create_seed_container", create)
    monkeypatch.setattr(
        docker_seed,
        "seed_golden",
        Mock(return_value=docker_seed.SeedOutcome("seeded", "seed-container")),
    )
    destroy = Mock()
    monkeypatch.setattr(lifecycle, "destroy_container", destroy)
    result = docker_seed.run_seed(cfg, incus, Mock(), docker_seed.SeedRequest(None, keep, True))
    assert result.outcome == ("failed" if fail else "seeded")
    if keep:
        destroy.assert_not_called()
    else:
        destroy.assert_called_once_with(cfg, incus, name, force=True)


def test_seed_creation_race_does_not_destroy_other_container(rig, monkeypatch):
    from jailbee import lifecycle

    cfg, incus = rig
    incus.exists.side_effect = [False, True]
    monkeypatch.setattr(
        docker_seed, "create_seed_container", Mock(side_effect=RuntimeError("name taken"))
    )
    destroy = Mock()
    monkeypatch.setattr(lifecycle, "destroy_container", destroy)
    result = docker_seed.run_seed(cfg, incus, Mock(), docker_seed.SeedRequest(None, False, True))
    assert result.outcome == "failed"
    destroy.assert_not_called()


def test_seed_creation_failure_after_create_is_cleaned_up(rig, monkeypatch):
    from jailbee import lifecycle

    cfg, incus = rig
    name = f"{cfg.container_prefix}-docker-seed"
    incus.exists.side_effect = [False, True]

    def create(_cfg, _incus, _gcfg, *, on_created):
        on_created(name)
        raise RuntimeError("configuration failed")

    monkeypatch.setattr(docker_seed, "create_seed_container", create)
    destroy = Mock()
    monkeypatch.setattr(lifecycle, "destroy_container", destroy)
    result = docker_seed.run_seed(cfg, incus, Mock(), docker_seed.SeedRequest(None, False, True))
    assert result.outcome == "failed"
    destroy.assert_called_once_with(cfg, incus, name, force=True)


def test_existing_seed_is_never_destroyed(rig, monkeypatch):
    from jailbee import lifecycle

    cfg, incus = rig
    incus.exists.return_value = True
    destroy = Mock()
    monkeypatch.setattr(lifecycle, "destroy_container", destroy)
    result = docker_seed.run_seed(cfg, incus, Mock(), docker_seed.SeedRequest(None, False, True))
    assert result.outcome == "failed" and "already exists" in result.detail
    destroy.assert_not_called()


def test_timer_stops_and_joins_before_cleanup(rig, monkeypatch):
    from jailbee import lifecycle

    cfg, incus = rig
    name = f"{cfg.container_prefix}-docker-seed"
    incus.exists.side_effect = [False, True]
    watchdog = Mock()
    watchdog.exists.return_value = True
    incus.with_deadline.side_effect = [incus, watchdog]
    clock = iter([0, 0, 3601, 3601, 3601])
    monkeypatch.setattr(docker_seed.time, "monotonic", lambda: next(clock))
    events = []

    class Timer:
        def __init__(self, delay, callback):
            self.callback = callback

        def start(self):
            pass

        def cancel(self):
            events.append("cancel")

        def join(self):
            events.append("join")

    monkeypatch.setattr(docker_seed.threading, "Timer", Timer)
    def create(_cfg, _incus, _gcfg, *, on_created):
        on_created(name)
        timer.callback()
        return name

    timer = None
    def make_timer(delay, callback):
        nonlocal timer
        timer = Timer(delay, callback)
        return timer

    monkeypatch.setattr(docker_seed.threading, "Timer", make_timer)
    monkeypatch.setattr(docker_seed, "create_seed_container", create)
    monkeypatch.setattr(lifecycle, "destroy_container", lambda *a, **kw: events.append("destroy"))
    result = docker_seed.run_seed(cfg, incus, Mock(), docker_seed.SeedRequest(None, False, True))
    assert result.outcome == "failed" and "timed out" in result.detail
    watchdog.stop.assert_called_once_with(name, force=True)
    incus.stop.assert_not_called()
    assert incus.with_deadline.call_args_list[1].args == (3631,)
    assert events == ["cancel", "join", "destroy"]


def test_preflight_source_and_lookup_errors(rig, monkeypatch):
    cfg, incus = rig
    monkeypatch.setattr(golden, "repo_uses_docker", lambda cfg: True)
    monkeypatch.setattr(docker_seed, "shared_golden_reason_for", lambda cfg: None)
    req = docker_seed.SeedRequest("source", False, True)
    incus.list_containers.return_value = []
    with pytest.raises(docker_seed.SeedPreflightError, match="does not exist"):
        docker_seed.preflight(cfg, incus, req)
    incus.list_containers.return_value = [{"name": "source", "status": "Running"}]
    incus.exec.side_effect = IncusError("docker unavailable")
    with pytest.raises(docker_seed.SeedPreflightError, match="docker unavailable"):
        docker_seed.preflight(cfg, incus, req)
    incus.exec.side_effect = None
    docker_seed.preflight(cfg, incus, req)
    monkeypatch.setattr(
        docker_seed, "shared_golden_reason_for", Mock(side_effect=RuntimeError("db unavailable"))
    )
    with pytest.raises(docker_seed.SeedPreflightError, match="db unavailable"):
        docker_seed.preflight(cfg, incus, req)


def test_record_outcome_database_error_warns(make_cfg, tmp_path, monkeypatch):
    from jailbee import db

    monkeypatch.setattr(db, "get_engine", Mock(side_effect=RuntimeError("database unavailable")))
    warning = Mock()
    monkeypatch.setattr(docker_seed, "warn", warning)
    docker_seed.record_outcome(
        make_cfg(tmp_path), docker_seed.SeedOutcome("seeded", "seed-container")
    )
    assert "database unavailable" in warning.call_args.args[0]


def test_seed_operation_order(rig, monkeypatch):
    cfg, incus = rig
    events = []

    def execute(name, cmd, **kwargs):
        if cmd[:3] == ["docker", "image", "ls"]:
            if "--format" in cmd:
                return "app:v1"
            return "old-id"
        if cmd[:3] == ["docker", "image", "rm"]:
            events.append("remove")
        if cmd[:3] == ["docker", "image", "inspect"]:
            events.append("inspect")
        if cmd[:3] == ["docker", "system", "df"]:
            return '{"Type":"Images","Size":"1MB"}'
        if cmd[:2] == ["systemctl", "stop"]:
            events.append("stop-docker")
        return ""

    incus.exec.side_effect = execute
    incus.exec_pipe.side_effect = lambda *a, **kw: events.append("save-load")
    monkeypatch.setattr(
        docker_seed, "stop_container", lambda *a, **kw: events.append("stop-instance")
    )
    monkeypatch.setattr(golden, "promote_instance", lambda *a, **kw: events.append("promote"))
    result = docker_seed.seed_golden(
        cfg,
        incus,
        docker_seed.SeedRequest("source", False, True),
        deadline=docker_seed.time.monotonic() + 60,
    )
    assert result.outcome == "seeded"
    assert events == ["remove", "save-load", "inspect", "stop-docker", "stop-instance", "promote"]
