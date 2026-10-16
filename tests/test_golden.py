"""Tests for golden image building."""

import base64
import re
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

from jailbee.config import Stacks, load_config
from jailbee.golden import (
    ArchivedImage,
    GoldenImageUsage,
    build_golden_image,
    find_all_archived_images,
    find_archived_images,
    gather_golden_usage,
    repo_uses_docker,
    resolved_snippet_names,
    resolved_snippet_paths,
)
from jailbee.incus import IncusError
from jailbee.stopping import CLEAN_STOP_BUDGET

FIXTURES = Path(__file__).parent / "fixtures"


def _incus(pools_driver="dir", existing=(), images=(), archives=()):
    """A stateful fake: `exists`/`image_exists` follow renames, deletes, publish.

    The default profile's root pool is ``default``; ``cow`` exists too. Both
    carry ``pools_driver`` (``btrfs`` selects instance mode).
    """
    incus = MagicMock()
    instances = set(existing)
    image_aliases = set(images) | {a for a, _size in archives}
    sizes = dict.fromkeys(images, 100) | dict(archives)

    incus.exists.side_effect = lambda name: name in instances

    def _rename(old, new):
        instances.discard(old)
        instances.add(new)

    def _delete(name, force=False):
        instances.discard(name)

    def _launch(image, name, **kwargs):
        instances.add(name)

    def _publish(container, alias):
        image_aliases.add(alias)

    def _alias_rename(old, new):
        image_aliases.discard(old)
        image_aliases.add(new)

    def _image_delete(ref):
        image_aliases.discard(ref)

    incus.rename.side_effect = _rename
    incus.delete.side_effect = _delete
    incus.launch.side_effect = _launch
    incus.publish.side_effect = _publish
    incus.image_alias_rename.side_effect = _alias_rename
    incus.image_delete.side_effect = _image_delete
    incus.image_exists.side_effect = lambda alias: alias in image_aliases
    incus.list_storage_pools.return_value = [
        {"name": "default", "driver": pools_driver},
        {"name": "cow", "driver": pools_driver},
    ]
    incus.profile_show.return_value = _PROFILE_YAML
    incus.list_images.side_effect = lambda: [
        {"aliases": [{"name": a}], "size": sizes.get(a, 100)} for a in sorted(image_aliases)
    ]
    return incus


def _build_name(cfg):
    return f"{cfg.golden.alias}--build"


def _golden_name(cfg):
    return f"{cfg.golden.alias}--golden"


def _call_index(incus, name, *args, **kwargs):
    """Index of the first `incus.<name>(*args, **kwargs)` in `mock_calls`."""
    for i, c in enumerate(incus.mock_calls):
        if c[0] == name and tuple(c.args) == args and (not kwargs or c.kwargs == kwargs):
            return i
    raise AssertionError(f"{name}{args} not called; calls: {incus.mock_calls}")


def test_build_calls_launch_exec_publish_delete():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()

    build_golden_image(cfg, incus)

    expected_build_name = _build_name(cfg)

    assert incus.launch.called
    launch_args = incus.launch.call_args.args
    launch_kwargs = incus.launch.call_args.kwargs
    # incus.launch(image, name, config=..., network=..., storage_pool=...)
    assert "ubuntu" in launch_args[0].lower()
    assert launch_args[1] == expected_build_name
    # Nesting required so systemd-networkd starts inside the build container
    assert launch_kwargs.get("config", {}).get("security.nesting") == "true"
    # Build must use jailbee-loose to bypass the strict ACL on incusbr0.
    assert launch_kwargs.get("network") == "jailbee-loose"

    # exec is called to copy provision script and run it
    assert incus.exec.called
    incus.publish.assert_called_once()
    incus.delete.assert_called_once_with(expected_build_name, force=True)


def test_build_container_name_uses_alias(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = _incus()
    build_golden_image(cfg, incus)
    launched_name = incus.launch.call_args.args[1]
    assert launched_name == f"{cfg.golden.alias}--build"


def test_build_announces_the_image_it_publishes(make_cfg, tmp_path, capsys):
    # A synthesized (scratch) config names its build container per directory but
    # publishes one shared alias; the opening line must say which image is built.
    cfg = make_cfg(tmp_path)
    incus = _incus()
    build_golden_image(cfg, incus)
    out = capsys.readouterr()
    text = out.out + out.err
    assert f"Building {cfg.golden.alias} from" in text
    assert _build_name(cfg) in text
    assert f"Golden image '{cfg.golden.alias}' built" in text


def test_build_uses_config_versions():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()
    build_golden_image(cfg, incus)

    # exec should pass the configured Java/Node versions in env
    env_calls = [c.kwargs.get("env", {}) for c in incus.exec.call_args_list]
    merged: dict[str, str] = {}
    for envd in env_calls:
        if envd:
            merged.update(envd)
    # Java package should mention corretto when configured
    assert any("corretto" in str(v).lower() for v in merged.values())
    # Node major version
    assert merged.get("NODE_MAJOR") == "24"
    # Python is no longer a configurable version — it comes from the base
    # image, so no PYTHON_VERSION env is passed to the provisioning script.
    assert "PYTHON_VERSION" not in merged


def test_build_warns_on_deprecated_pinned_python(mocker):
    """full_config.yaml still pins `golden.python`; the build must ignore it
    (no PYTHON_VERSION env) and emit a soft deprecation warning."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    assert cfg.golden.python  # fixture pins a version → triggers the warning
    warn = mocker.patch("jailbee.golden.warn")
    incus = _incus()

    build_golden_image(cfg, incus)

    assert warn.called
    assert any("golden.python" in str(c.args[0]) for c in warn.call_args_list)


# ---------- Placement, one version, instance mode


def test_build_launches_on_the_configured_pool(make_cfg, tmp_path):
    cfg = _with_pool(make_cfg(tmp_path), "cow")
    incus = _incus()
    build_golden_image(cfg, incus)
    assert incus.launch.call_args.kwargs["storage_pool"] == "cow"


def test_build_launches_on_the_profile_pool_when_none_configured(make_cfg, tmp_path):
    cfg = _with_pool(make_cfg(tmp_path), None)
    incus = _incus()
    build_golden_image(cfg, incus)
    assert incus.launch.call_args.kwargs["storage_pool"] == "default"


def test_build_unknown_pool_fails_before_launch(make_cfg, tmp_path):
    cfg = _with_pool(make_cfg(tmp_path), "nope")
    incus = _incus()
    with pytest.raises(ValueError, match="nope"):
        build_golden_image(cfg, incus)
    incus.launch.assert_not_called()
    incus.delete.assert_not_called()


def test_image_mode_publishes_under_temp_alias_then_swaps(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = _incus(images={alias})

    build_golden_image(cfg, incus)

    build = _build_name(cfg)
    i_publish = _call_index(incus, "publish", build, f"{alias}--new")
    i_aside = _call_index(incus, "image_alias_rename", alias, f"{alias}--old")
    i_promote = _call_index(incus, "image_alias_rename", f"{alias}--new", alias)
    i_drop = _call_index(incus, "image_delete", f"{alias}--old")
    assert i_publish < i_aside < i_promote < i_drop
    # No dated archive is ever created.
    dated = re.compile(rf"^{re.escape(alias)}-\d{{4}}-\d{{2}}-\d{{2}}$")
    for c in incus.image_alias_rename.call_args_list:
        assert not dated.match(c.args[1])
    assert incus.image_exists(alias)
    assert not incus.image_exists(f"{alias}--new")
    assert not incus.image_exists(f"{alias}--old")


def test_image_mode_first_build_has_no_swap(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = _incus()

    build_golden_image(cfg, incus)

    incus.publish.assert_called_once_with(_build_name(cfg), f"{alias}--new")
    assert incus.image_alias_rename.call_args_list == [call(f"{alias}--new", alias)]
    assert call(f"{alias}--old") not in incus.image_delete.call_args_list


def test_image_mode_publish_failure_leaves_old_image_aliased(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = _incus(images={alias})
    incus.publish.side_effect = IncusError("publish boom")

    with pytest.raises(IncusError, match="publish boom"):
        build_golden_image(cfg, incus)

    assert all(c.args[0] != alias for c in incus.image_alias_rename.call_args_list)
    incus.delete.assert_called_once_with(_build_name(cfg), force=True)
    assert incus.image_exists(alias)


def test_instance_mode_does_not_publish(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = _incus(pools_driver="btrfs")

    build_golden_image(cfg, incus)

    incus.publish.assert_not_called()
    incus.rename.assert_any_call(_build_name(cfg), _golden_name(cfg))
    i_promote = _call_index(incus, "rename", _build_name(cfg), _golden_name(cfg))
    later = incus.mock_calls[i_promote:]
    assert not any(c[0] == "delete" and c.args[0] == _build_name(cfg) for c in later)
    assert incus.exists(_golden_name(cfg))


def test_instance_mode_announces_the_golden_instance(make_cfg, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    build_golden_image(cfg, _incus(pools_driver="btrfs"))
    out = capsys.readouterr()
    assert f"Golden instance '{_golden_name(cfg)}' built" in out.out + out.err


def test_instance_mode_strips_build_nic_and_nesting_before_swap(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = _incus(pools_driver="btrfs")
    build = _build_name(cfg)

    build_golden_image(cfg, incus)

    i_nic = _call_index(incus, "config_device_remove", build, "eth0", missing_ok=True)
    i_nest = _call_index(incus, "config_unset", build, "security.nesting")
    i_promote = _call_index(incus, "rename", build, _golden_name(cfg))
    assert i_nic < i_promote
    assert i_nest < i_promote
    for c in incus.config_set.call_args_list:
        assert "boot.autostart" not in c.args


def test_instance_mode_swaps_old_golden_aside_then_deletes_it(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    golden = _golden_name(cfg)
    incus = _incus(pools_driver="btrfs", existing={golden})

    build_golden_image(cfg, incus)

    i_aside = _call_index(incus, "rename", golden, f"{alias}--old")
    i_promote = _call_index(incus, "rename", _build_name(cfg), golden)
    i_drop = _call_index(incus, "delete", f"{alias}--old", force=True)
    assert i_aside < i_promote < i_drop
    assert incus.exists(golden)
    assert not incus.exists(f"{alias}--old")


def test_instance_mode_failure_before_swap_keeps_old_golden(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    golden = _golden_name(cfg)
    incus = _incus(pools_driver="btrfs", existing={golden})
    incus.exec.side_effect = RuntimeError("provisioning boom")

    with pytest.raises(RuntimeError, match="provisioning boom"):
        build_golden_image(cfg, incus)

    incus.rename.assert_not_called()
    incus.delete.assert_called_once_with(_build_name(cfg), force=True)
    assert incus.exists(golden)


def test_build_restores_aside_golden_left_by_a_crash(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    golden = _golden_name(cfg)
    incus = _incus(pools_driver="btrfs", existing={f"{alias}--old"})

    build_golden_image(cfg, incus)

    i_restore = _call_index(incus, "rename", f"{alias}--old", golden)
    assert i_restore < _call_index(incus, "launch", *incus.launch.call_args.args)


def test_build_drops_aside_golden_when_golden_also_exists(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    golden = _golden_name(cfg)
    incus = _incus(pools_driver="btrfs", existing={f"{alias}--old", golden})

    build_golden_image(cfg, incus)

    i_drop = _call_index(incus, "delete", f"{alias}--old", force=True)
    assert i_drop < _call_index(incus, "launch", *incus.launch.call_args.args)
    assert call(f"{alias}--old", golden) not in incus.rename.call_args_list


def test_build_drops_leftover_temp_image_aliases(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = _incus(images={f"{alias}--new", f"{alias}--old"})

    build_golden_image(cfg, incus)

    launch_idx = _call_index(incus, "launch", *incus.launch.call_args.args)
    assert _call_index(incus, "image_delete", f"{alias}--new") < launch_idx
    assert _call_index(incus, "image_delete", f"{alias}--old") < launch_idx


def test_instance_mode_deletes_leftover_image_and_archives(make_cfg, tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = _incus(
        pools_driver="btrfs",
        images={alias},
        archives=[(f"{alias}-2026-01-01", 1024), (f"{alias}-2026-02-01", 1024)],
    )

    build_golden_image(cfg, incus)

    deleted = {c.args[0] for c in incus.image_delete.call_args_list}
    assert {alias, f"{alias}-2026-01-01", f"{alias}-2026-02-01"} <= deleted
    out = capsys.readouterr()
    assert "Removed 2 old golden archive(s), 2.0 KB" in out.out + out.err


def test_image_mode_deletes_leftover_golden_instance(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    golden = _golden_name(cfg)
    incus = _incus(existing={golden})

    build_golden_image(cfg, incus)

    incus.delete.assert_any_call(golden, force=True)
    assert not incus.exists(golden)
    incus.publish.assert_called_once()


def test_image_mode_deletes_archives(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = _incus(images={alias}, archives=[(f"{alias}-2026-01-01", 10)])

    build_golden_image(cfg, incus)

    incus.image_delete.assert_any_call(f"{alias}-2026-01-01")
    assert incus.image_exists(alias)


def test_cleanup_failure_is_a_warning_not_a_failure(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    archive = f"{alias}-2026-01-01"
    incus = _incus(pools_driver="btrfs", archives=[(archive, 10)])
    plain_delete = incus.image_delete.side_effect

    def image_delete(ref):
        if ref == archive:
            raise IncusError("Image is in use")
        plain_delete(ref)

    incus.image_delete.side_effect = image_delete
    warn = mocker.patch("jailbee.golden.warn")
    success = mocker.patch("jailbee.golden.success")

    build_golden_image(cfg, incus)

    assert any(archive in str(c.args[0]) for c in warn.call_args_list)
    success.assert_called_once()


def test_build_removes_legacy_build_container_name(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    legacy = f"{cfg.container_prefix}-base-build"
    incus = _incus(existing={legacy})

    build_golden_image(cfg, incus)

    i_drop = _call_index(incus, "delete", legacy, force=True)
    assert i_drop < _call_index(incus, "launch", *incus.launch.call_args.args)


# ---------- provision_script + provision_env


def _find_install_run_call(incus):
    return next(
        c for c in incus.exec.call_args_list if c.args[1] == ["bash", "/provision/install.sh"]
    )


def _find_install_copy_call(incus):
    """Locate the base64 copy of install.sh into the build container."""
    for c in incus.exec.call_args_list:
        cmd = c.args[1]
        if cmd[:2] == ["bash", "-c"] and "base64 -d > /provision/install.sh" in cmd[2]:
            return c
    raise AssertionError("install.sh copy exec not found")


def test_build_uses_custom_provision_script(make_cfg, tmp_path, mocker):
    script = tmp_path / "custom.sh"
    script.write_text("#!/bin/bash\necho custom-marker-xyz\n")
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"provision_script": script},
            ),
        }
    )
    incus = _incus()
    build_golden_image(cfg, incus)

    copy_call = _find_install_copy_call(incus)
    match = re.search(r"echo '([^']+)'", copy_call.args[1][2])
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode()
    assert "custom-marker-xyz" in decoded


def test_build_uses_relative_provision_script_under_repo_root(
    make_cfg,
    tmp_path,
    mocker,
):
    (tmp_path / ".gie").mkdir()
    rel_script = tmp_path / ".gie" / "install.sh"
    rel_script.write_text("#!/bin/bash\necho relative-marker\n")
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"provision_script": Path("./.gie/install.sh")},
            ),
        }
    )
    incus = _incus()
    build_golden_image(cfg, incus)

    copy_call = _find_install_copy_call(incus)
    match = re.search(r"echo '([^']+)'", copy_call.args[1][2])
    assert match is not None
    decoded = base64.b64decode(match.group(1)).decode()
    assert "relative-marker" in decoded


def test_build_passes_empty_extra_apt_packages_env(make_cfg, tmp_path, mocker):
    """Default config: EXTRA_APT_PACKAGES env var is present but empty so
    install.sh's `: "${EXTRA_APT_PACKAGES:=}"` and conditional install
    block work consistently across builds."""
    cfg = make_cfg(tmp_path)
    incus = _incus()
    build_golden_image(cfg, incus)
    install_call = _find_install_run_call(incus)
    env = install_call.kwargs["env"]
    assert env["EXTRA_APT_PACKAGES"] == ""


def test_build_passes_extra_apt_packages_env(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"extra_apt_packages": ["mariadb-client", "postgresql-client"]},
            ),
        }
    )
    incus = _incus()
    build_golden_image(cfg, incus)
    install_call = _find_install_run_call(incus)
    env = install_call.kwargs["env"]
    assert env["EXTRA_APT_PACKAGES"] == "mariadb-client postgresql-client"


def test_build_provision_env_merged_into_exec(make_cfg, tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"provision_env": {"EXTRA_PKG": "redis"}},
            ),
        }
    )
    incus = _incus()
    build_golden_image(cfg, incus)
    install_call = _find_install_run_call(incus)
    env = install_call.kwargs["env"]
    assert env["EXTRA_PKG"] == "redis"
    assert "CONTAINER_USER" not in env
    assert env["CONTAINER_UID"] == str(cfg.container_user.uid)


# ---------- Build container cleanup on failure / orphan recovery


def test_build_deletes_container_when_provisioning_fails():
    """If a provisioning step raises, the build container must still be
    cleaned up so the next `gie base build` doesn't hit a UNIQUE
    constraint failure on the same name.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()
    incus.exec.side_effect = RuntimeError("provisioning boom")

    with pytest.raises(RuntimeError, match="provisioning boom"):
        build_golden_image(cfg, incus)

    expected = _build_name(cfg)
    incus.delete.assert_called_once_with(expected, force=True)


def test_build_survives_a_build_container_that_will_not_shut_down():
    """A stuck shutdown must not discard a finished provisioning run.

    incusd gives an unqualified `incus stop` 600s and then fails with
    `Failed shutting down instance … context deadline exceeded`. The build
    container is about to be published and deleted, so its rootfs is all
    that matters: pull the plug and carry on rather than throw the build
    away at the last step.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()
    expected = _build_name(cfg)

    def stop(name, **kwargs):
        if kwargs.get("force"):
            return None
        raise IncusError(
            f"`incus stop {name}` failed (exit 1): Error: Failed shutting down "
            'instance, status is "Running": context deadline exceeded'
        )

    incus.stop.side_effect = stop

    build_golden_image(cfg, incus)

    incus.publish.assert_called_once()
    assert incus.stop.call_args_list[-1] == call(expected, force=True)
    # The clean attempt must be time-bounded, not left on incusd's 600s.
    assert incus.stop.call_args_list[0].kwargs.get("timeout")


def test_build_stop_asks_for_a_clean_shutdown_first():
    """A healthy container is shut down properly — no gratuitous power cut."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()

    build_golden_image(cfg, incus)

    expected = _build_name(cfg)
    incus.stop.assert_called_once_with(expected, timeout=CLEAN_STOP_BUDGET)


def test_build_removes_orphan_container_before_launch():
    """Recover from a previous failed build that left the container
    behind: detect on entry and delete before launching the new one.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    expected = _build_name(cfg)
    incus = _incus(existing={expected})

    build_golden_image(cfg, incus)

    # Two deletes: preflight (orphan) and post-publish cleanup.
    assert incus.delete.call_count == 2
    assert all(c == ((expected,), {"force": True}) for c in incus.delete.call_args_list)

    # Preflight delete must precede launch; otherwise the launch still
    # fails on the leftover container name.
    timeline = [c[0] for c in incus.mock_calls]
    first_delete = timeline.index("delete")
    launch_idx = timeline.index("launch")
    assert first_delete < launch_idx


# ---------- Phase B Task 3: install.d staging --------------------------------


@pytest.fixture
def cfg_for_golden(tmp_path, make_cfg):
    """Minimal Config wired for build_golden_image.

    Writes a real (legacy) `.gie/config.yaml` file so `repo_config_dir_name`
    resolves this fixture's repo to `.gie` — an empty directory alone no
    longer signals which config dir a repo uses.
    """
    repo_root = tmp_path / "myrepo"
    (repo_root / ".gie").mkdir(parents=True)
    (repo_root / ".gie" / "config.yaml").write_text("")
    cfg = make_cfg(repo_root)
    return cfg


def test_build_passes_new_env_vars(cfg_for_golden, mocker):
    incus = _incus()
    build_golden_image(cfg_for_golden, incus)

    install_sh_exec = next(
        c for c in incus.exec.call_args_list if c.args[1] == ["bash", "/provision/install.sh"]
    )
    env = install_sh_exec.kwargs["env"]
    assert env["JAILBEE_USER_HOME"] == "/home/dev"
    assert env["JAILBEE_PROVISION_DIR"] == "/provision"


def test_build_stages_install_d_directory(cfg_for_golden, mocker, tmp_path):
    """When a repo has .gie/install.d/foo.sh, it must be pushed to
    /provision/install.d/foo.sh in the build container.
    """
    repo_snippet = cfg_for_golden.repo_root / ".gie" / "install.d" / "55-custom.sh"
    repo_snippet.parent.mkdir(parents=True)
    repo_snippet.write_text("#!/bin/bash\necho hi\n")

    incus = _incus()
    build_golden_image(cfg_for_golden, incus)

    # Find the exec call that writes the snippet
    snippet_write = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and any("/provision/install.d/55-custom.sh" in str(arg) for arg in c.args[1])
    ]
    assert snippet_write, "expected an exec call pushing the snippet to /provision/install.d/"


def test_build_skips_install_d_when_provision_script_override(cfg_for_golden, mocker, tmp_path):
    """If golden.provision_script is set, install.d/ is NOT staged."""
    custom_script = tmp_path / "custom-install.sh"
    custom_script.write_text("#!/bin/bash\nexit 0\n")
    object.__setattr__(cfg_for_golden.golden, "provision_script", custom_script)

    # Even if a repo snippet exists, it shouldn't be staged when an override is set
    repo_snippet = cfg_for_golden.repo_root / ".gie" / "install.d" / "55-custom.sh"
    repo_snippet.parent.mkdir(parents=True)
    repo_snippet.write_text("#!/bin/bash\n")

    incus = _incus()
    build_golden_image(cfg_for_golden, incus)

    snippet_writes = [
        c
        for c in incus.exec.call_args_list
        if len(c.args) > 1
        and isinstance(c.args[1], list)
        and any("/provision/install.d/" in str(arg) for arg in c.args[1])
    ]
    assert not snippet_writes, (
        f"no install.d/ staging when provision_script is set; got: {snippet_writes}"
    )


def test_build_stages_user_install_d_from_xdg_config_home(
    cfg_for_golden, mocker, tmp_path, monkeypatch
):
    """The user's install.d must be read where the rest of jailbee writes it.

    `global_config.default_global_config_path()` honours `XDG_CONFIG_HOME`, so
    a hardcoded `~/.config/jailbee/install.d` here would silently ignore every
    snippet belonging to a user who sets that variable — with no error, just
    provisioning that quietly skips their customisations.
    """
    xdg_config = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    user_snippet = xdg_config / "jailbee" / "install.d" / "60-user.sh"
    user_snippet.parent.mkdir(parents=True)
    user_snippet.write_text("#!/bin/bash\necho user\n")

    incus = _incus()
    build_golden_image(cfg_for_golden, incus)

    assert "60-user.sh" in _staged_snippet_names(incus)


def _merged_exec_env(incus: MagicMock) -> dict[str, str]:
    merged: dict[str, str] = {}
    for c in incus.exec.call_args_list:
        merged.update(c.kwargs.get("env", {}) or {})
    return merged


def _staged_snippet_names(incus: MagicMock) -> list[str]:
    names: list[str] = []
    for c in incus.exec.call_args_list:
        if len(c.args) > 1 and isinstance(c.args[1], list):
            m = re.search(r"/provision/install\.d/(\S+\.sh)", " ".join(c.args[1]))
            if m:
                names.append(m.group(1))
    return names


def _fresh_incus() -> MagicMock:
    incus = _incus()
    return incus


def test_build_env_uses_stack_java_and_node():
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(update={"stacks": Stacks(java="openjdk-21", node=22)})
        }
    )
    incus = _fresh_incus()
    build_golden_image(cfg, incus)
    env = _merged_exec_env(incus)
    assert env["JAVA_PACKAGE"] == "openjdk-21-jdk"
    assert env["NODE_MAJOR"] == "22"


def test_build_stages_stack_snippets():
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={"stacks": Stacks(java="corretto-17", docker=True)}
            )
        }
    )
    incus = _fresh_incus()
    build_golden_image(cfg, incus)
    staged = _staged_snippet_names(incus)
    assert "20-corretto.sh" in staged
    assert "50-docker.sh" in staged
    assert "90-registry-mirror-ca.sh" in staged


def test_build_stages_stack_snippets_respects_disable_snippets_opt_out():
    """D5: the auto-added `90-registry-mirror-ca` snippet (fired when both
    java and docker stacks are on) can be opted out of via
    `golden.disable_snippets: ["90-registry-mirror-ca"]`, same as any other
    bundled snippet — the stack-derived name isn't special-cased past the
    disable list.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(
        update={
            "golden": cfg.golden.model_copy(
                update={
                    "stacks": Stacks(java="corretto-17", docker=True),
                    "disable_snippets": ["90-registry-mirror-ca"],
                }
            )
        }
    )
    incus = _fresh_incus()
    build_golden_image(cfg, incus)
    staged = _staged_snippet_names(incus)
    assert "20-corretto.sh" in staged
    assert "50-docker.sh" in staged
    assert "90-registry-mirror-ca.sh" not in staged


# ---------- Task 2: find_archived_images


def _img(aliases, size=100):
    return {"aliases": [{"name": n} for n in aliases], "size": size}


def test_find_archived_images_matches_dated_aliases(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = MagicMock()
    incus.list_images.return_value = [
        _img([alias]),  # live alias — ignored
        _img([f"{alias}-2026-07-20"], 500),
        _img([f"{alias}-2026-07-18"], 300),
        _img(["some-other-image"]),  # unrelated — ignored
        _img([]),  # aliasless — ignored
    ]
    result = find_archived_images(cfg, incus)
    assert result == [
        ArchivedImage(alias=f"{alias}-2026-07-20", date=date(2026, 7, 20), size_bytes=500),
        ArchivedImage(alias=f"{alias}-2026-07-18", date=date(2026, 7, 18), size_bytes=300),
    ]


def test_find_archived_images_ignores_malformed_suffix(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    alias = cfg.golden.alias
    incus = MagicMock()
    incus.list_images.return_value = [
        _img([f"{alias}-not-a-date"]),
        _img([f"{alias}-2026-7-1"]),  # not zero-padded → no match
    ]
    assert find_archived_images(cfg, incus) == []


def test_find_archived_images_empty(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.list_images.return_value = []
    assert find_archived_images(cfg, incus) == []


def test_find_all_archived_images_across_prefixes(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [
        _img(["foo-base"]),  # live — ignored
        _img(["foo-base-2026-07-20"], 500),
        _img(["bar-base-2026-07-18"], 300),
        _img(["bar-base"]),  # live — ignored
        _img(["unrelated-image"]),  # ignored
    ]
    result = find_all_archived_images(incus, ["foo-base", "bar-base"])
    assert [(a.alias, a.size_bytes) for a in result] == [
        ("foo-base-2026-07-20", 500),
        ("bar-base-2026-07-18", 300),
    ]
    # sorted newest-first across prefixes
    assert result[0].date >= result[1].date


def test_find_all_archived_images_dedups_base_aliases(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [_img(["foo-base-2026-07-20"], 500)]
    result = find_all_archived_images(incus, ["foo-base", "foo-base"])
    assert len(result) == 1  # one archive, not doubled by the repeated base alias


# ---------- Task 2: GoldenImageUsage + gather_golden_usage


def test_gather_golden_usage_classifies_live_and_archives(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [
        _img(["foo-base"], 2000),  # live
        _img(["foo-base-2026-07-20"], 500),
        _img(["foo-base-2026-07-18"], 300),
        _img(["bar-base-2026-07-01"], 100),  # bar: archive only, no live
        _img(["unrelated"], 999),  # ignored
    ]
    result = gather_golden_usage(incus, ["foo-base", "bar-base"])
    assert result[0] == GoldenImageUsage(
        base_alias="foo-base",
        live_size_bytes=2000,
        archives=[
            ArchivedImage("foo-base-2026-07-20", date(2026, 7, 20), 500),
            ArchivedImage("foo-base-2026-07-18", date(2026, 7, 18), 300),
        ],
    )
    assert result[1] == GoldenImageUsage(
        base_alias="bar-base",
        live_size_bytes=None,  # no live image for bar
        archives=[ArchivedImage("bar-base-2026-07-01", date(2026, 7, 1), 100)],
    )


def test_gather_golden_usage_live_only(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [_img(["foo-base"], 2000)]
    result = gather_golden_usage(incus, ["foo-base"])
    assert result == [GoldenImageUsage("foo-base", 2000, [])]


def test_gather_golden_usage_dedups_base_aliases(make_cfg, tmp_path):
    incus = MagicMock()
    incus.list_images.return_value = [_img(["foo-base"], 2000)]
    result = gather_golden_usage(incus, ["foo-base", "foo-base"])
    assert len(result) == 1


def test_provisioning_runs_under_a_live_status_line(mocker):
    """install.sh's apt output is captured, so this step prints nothing for
    minutes. It is the one place in the build that needs a spinner."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()
    calls: list[str] = []
    # MagicMock already implements the context-manager protocol.
    status = mocker.MagicMock()
    mocker.patch(
        "jailbee.golden.status_with_elapsed",
        side_effect=lambda message: calls.append(message) or status,
    )

    build_golden_image(cfg, incus)

    assert len(calls) == 1
    assert "provisioning script" in calls[0]


def test_provisioning_failure_still_deletes_the_build_container():
    """The status line must not swallow the failure or leak the container."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()

    def fail_on_install(name, cmd, **kwargs):
        # Keyed on the command rather than on a call index: the build runs a
        # varying number of setup execs before this one.
        if cmd == ["bash", "/provision/install.sh"]:
            raise IncusError("apt-get: no such package")
        return ""

    incus.exec.side_effect = fail_on_install

    with pytest.raises(IncusError, match="no such package"):
        build_golden_image(cfg, incus)

    assert incus.delete.called
    assert incus.delete.call_args.kwargs.get("force") is True


def _repo_cfg(tmp_path, make_cfg, **overrides):
    """A Config whose repo_root has a real `.jailbee/config.yaml`.

    `repo_config_dir_name` picks the directory by the *file's* presence, so an
    empty directory is not enough — without the file, a repo snippet written
    under `.jailbee/install.d/` would be looked for under `.gie/install.d/`.
    """
    repo_root = tmp_path / "repo"
    (repo_root / ".jailbee").mkdir(parents=True)
    (repo_root / ".jailbee" / "config.yaml").write_text("")
    return make_cfg(repo_root, **overrides)


def test_resolved_snippet_names_sees_stack_docker(tmp_path, make_cfg):
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"stacks": {"docker": True}})
    assert "docker" in resolved_snippet_names(cfg)


def test_resolved_snippet_names_sees_enable_snippets_escape_hatch(tmp_path, make_cfg):
    """`golden.stacks` is sugar over `enable_snippets`; both must be visible."""
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"enable_snippets": ["50-docker"]})
    assert "docker" in resolved_snippet_names(cfg)


def test_resolved_snippet_names_sees_a_repo_owned_snippet(tmp_path, make_cfg):
    cfg = _repo_cfg(tmp_path, make_cfg)
    snippet = cfg.repo_root / ".jailbee" / "install.d" / "50-docker.sh"
    snippet.parent.mkdir(parents=True)
    snippet.write_text("#!/bin/bash\n")
    assert "docker" in resolved_snippet_names(cfg)


def test_a_scratch_directorys_install_d_never_reaches_the_shared_image(tmp_path, make_cfg):
    """M3: a synthesized config builds the *host-shared* `jailbee-scratch-base`
    image, so no per-directory input may reach it (spec §6). A directory with
    a `.jailbee/install.d/` but no `config.yaml` would otherwise inject its
    snippets into an image every scratch container on the host runs."""
    repo_root = tmp_path / "tutkimus"
    snippet = repo_root / ".jailbee" / "install.d" / "50-docker.sh"
    snippet.parent.mkdir(parents=True)
    snippet.write_text("#!/bin/bash\n")
    cfg = make_cfg(repo_root)

    # Sanity: without the guard this snippet IS picked up — so the assertion
    # below tests the guard rather than a directory that was never read.
    assert snippet in resolved_snippet_paths(cfg)

    cfg._synthetic = True
    assert snippet not in resolved_snippet_paths(cfg)
    assert "docker" not in resolved_snippet_names(cfg)


def test_resolved_snippet_names_honours_disable_snippets(tmp_path, make_cfg):
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"stacks": {"docker": True}, "disable_snippets": ["docker"]},
    )
    assert "docker" not in resolved_snippet_names(cfg)


def test_resolved_snippet_paths_empty_when_provision_script_overrides(tmp_path, make_cfg):
    """A custom provision script stages no install.d snippets at all."""
    script = tmp_path / "custom-install.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"provision_script": str(script), "stacks": {"docker": True}},
    )
    assert resolved_snippet_paths(cfg) == []


def test_repo_uses_docker_falls_back_to_the_stack_bool_with_provision_script(tmp_path, make_cfg):
    """install.d is bypassed, so the sugar bool is the only signal left."""
    script = tmp_path / "custom-install.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"provision_script": str(script), "stacks": {"docker": True}},
    )
    assert repo_uses_docker(cfg) is True


def test_repo_uses_docker_false_for_a_plain_repo(tmp_path, make_cfg):
    assert repo_uses_docker(_repo_cfg(tmp_path, make_cfg)) is False


def test_repo_uses_docker_sees_extra_apt_packages(tmp_path, make_cfg):
    """`docker.io` from the archive lands in the image via 05-extra-apt.sh
    without the `docker` snippet ever resolving."""
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"extra_apt_packages": ["curl", "docker.io"]})
    assert repo_uses_docker(cfg) is True


def test_repo_uses_docker_sees_extra_apt_packages_with_a_provision_script(tmp_path, make_cfg):
    """extra_apt_packages is independent of install.d, so the signal survives
    the branch where a custom provision script replaces install.sh."""
    script = tmp_path / "custom-install.sh"
    script.write_text("#!/bin/bash\nexit 0\n")
    cfg = _repo_cfg(
        tmp_path,
        make_cfg,
        golden={"provision_script": str(script), "extra_apt_packages": ["docker-ce"]},
    )
    assert repo_uses_docker(cfg) is True


def test_repo_uses_docker_ignores_unrelated_extra_apt_packages(tmp_path, make_cfg):
    """The prefix test must not fire on packages that merely mention docker
    late in the name."""
    cfg = _repo_cfg(tmp_path, make_cfg, golden={"extra_apt_packages": ["golang-docker-dev"]})
    assert repo_uses_docker(cfg) is False


def test_image_sourced_browsers_stage_their_snippets(tmp_path, make_cfg):
    from jailbee.golden import browser_snippet_names

    cfg = make_cfg(
        tmp_path,
        browsers={
            "chrome": {"enabled": True, "source": "image", "host_path": None},
            "firefox": {"enabled": True, "source": "image"},
        },
    )
    assert browser_snippet_names(cfg) == ["70-chrome.sh", "70-firefox.sh"]


def test_host_sourced_browsers_stage_nothing(tmp_path, make_cfg):
    from jailbee.golden import browser_snippet_names

    cfg = make_cfg(tmp_path, browsers={"chrome": {"enabled": True, "source": "host"}})
    assert browser_snippet_names(cfg) == []


def test_disabled_browsers_stage_nothing(tmp_path, make_cfg):
    from jailbee.golden import browser_snippet_names

    assert browser_snippet_names(make_cfg(tmp_path)) == []


def test_both_browser_snippets_exist_in_available_library():
    """Ordering ('70' runs after '60-gui-libs') is a claim about the actual
    staged snippet order, not about the literal file names — that is
    covered by
    ``test_image_sourced_browser_is_staged_after_gui_libs`` below, which
    resolves real snippet paths instead of asserting a string against
    itself.
    """
    from jailbee.golden import _resource_dir

    available = _resource_dir("install.d.available")
    for name in ("70-chrome.sh", "70-firefox.sh"):
        assert (available / name).is_file(), f"{name} missing from install.d.available"


def test_image_sourced_browser_is_staged_after_gui_libs(tmp_path, make_cfg):
    """Exercises the fold-in at golden.py's `enabled = list(dict.fromkeys(...))`
    line, not just `browser_snippet_names` in isolation. Deleting
    `*browser_snippet_names(cfg),` from that line leaves every one of the
    other browser tests passing (they call `browser_snippet_names`
    directly) while `70-firefox.sh` silently stops being staged — this is
    the test that would catch it, by asserting it is actually present in
    `resolved_snippet_paths` and ordered after `60-gui-libs.sh`.
    """
    cfg = make_cfg(tmp_path, browsers={"firefox": {"enabled": True, "source": "image"}})
    names = [p.name for p in resolved_snippet_paths(cfg)]
    assert "70-firefox.sh" in names
    assert names.index("60-gui-libs.sh") < names.index("70-firefox.sh")


# ---------- Loose bridge prerequisite -----------------------------------------


def test_build_creates_the_missing_loose_bridge_before_launch():
    """A host that never ran `jailbee init` (the scratch first run) has no
    `jailbee-loose`; launching the build container on it failed with
    "Network not found". The build must create it first."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()
    incus.network_exists.return_value = False

    build_golden_image(cfg, incus)

    incus.network_create.assert_called_once_with("jailbee-loose")
    timeline = [c[0] for c in incus.mock_calls]
    assert timeline.index("network_create") < timeline.index("launch")


def test_build_leaves_an_existing_loose_bridge_alone():
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = _incus()
    incus.network_exists.return_value = True

    build_golden_image(cfg, incus)

    incus.network_create.assert_not_called()


# ---- Golden placement -------------------------------------------------------

_PROFILE_YAML = "devices:\n  root:\n    path: /\n    pool: default\n    type: disk\n"
_POOLS = [{"name": "cow", "driver": "btrfs"}, {"name": "default", "driver": "dir"}]


def _placement_incus(pools=_POOLS, profile=_PROFILE_YAML):
    incus = MagicMock()
    incus.list_storage_pools.return_value = pools
    incus.profile_show.return_value = profile
    return incus


def _with_pool(cfg, pool):
    return cfg.model_copy(
        update={"defaults": cfg.defaults.model_copy(update={"storage_pool": pool})}
    )


def test_placement_uses_defaults_storage_pool_and_its_driver(make_cfg, tmp_path):
    from jailbee.golden import GoldenPlacement, resolve_golden_placement

    cfg = _with_pool(make_cfg(tmp_path), "cow")
    placement = resolve_golden_placement(cfg, _placement_incus())
    assert placement == GoldenPlacement("cow", "btrfs")
    assert placement.instance_mode is True


def test_placement_falls_back_to_default_profile_root_pool(make_cfg, tmp_path):
    from jailbee.golden import GoldenPlacement, resolve_golden_placement

    cfg = _with_pool(make_cfg(tmp_path), None)
    placement = resolve_golden_placement(cfg, _placement_incus())
    assert placement == GoldenPlacement("default", "dir")
    assert placement.instance_mode is False


def test_placement_zfs_is_image_mode(make_cfg, tmp_path):
    from jailbee.golden import resolve_golden_placement

    cfg = _with_pool(make_cfg(tmp_path), "z")
    placement = resolve_golden_placement(cfg, _placement_incus([{"name": "z", "driver": "zfs"}]))
    assert placement.instance_mode is False


def test_placement_unknown_pool_raises_before_anything(make_cfg, tmp_path):
    from jailbee.golden import resolve_golden_placement

    cfg = _with_pool(make_cfg(tmp_path), "nope")
    incus = _placement_incus()
    with pytest.raises(ValueError, match="nope"):
        resolve_golden_placement(cfg, incus)
    incus.profile_show.assert_not_called()


def test_placement_without_root_device_is_image_mode(make_cfg, tmp_path):
    from jailbee.golden import GoldenPlacement, resolve_golden_placement

    cfg = _with_pool(make_cfg(tmp_path), None)
    incus = _placement_incus(profile="devices:\n  eth0:\n    type: nic\n")
    assert resolve_golden_placement(cfg, incus) == GoldenPlacement(None, None)


def test_golden_names_cannot_be_branch_containers(make_cfg, tmp_path):
    from jailbee.golden import golden_build_name, golden_instance_name
    from jailbee.lifecycle import derive_container_name

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(update={"container_prefix": "proj"})
    alias = "proj-base"
    for branch in ("golden", "base", "base--golden", "base-golden", "base--build", "base-build"):
        name = derive_container_name(cfg, branch)
        assert name != golden_instance_name(alias)
        assert name != golden_build_name(alias)


def test_golden_instance_pool_reads_expanded_root_disk():
    from jailbee.golden import golden_instance_pool

    incus = MagicMock()
    incus.config_show.return_value = _PROFILE_YAML
    assert golden_instance_pool(incus, "x--golden") == "default"
    incus.config_show.assert_called_once_with("x--golden", expanded=True)
    incus.config_show.return_value = "devices: {}\n"
    assert golden_instance_pool(incus, "x--golden") is None
