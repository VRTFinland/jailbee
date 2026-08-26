"""Tests for Incus profile YAML generation."""

from pathlib import Path

import pytest
import yaml

from jailbee.config import load_config
from jailbee.profiles import (
    ProfileNames,
    base_profile_yaml,
    binds_profile_yaml,
    net_profile_yaml,
    profile_names,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg():
    return load_config(FIXTURES / "full_config.yaml")


def test_base_profile_has_security_nesting():
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert parsed["config"]["security.nesting"] == "true"


def test_base_profile_has_no_apparmor_unconfined_workaround():
    """Ubuntu 26.04 ships Incus 6.0.5-8 with the nested-Docker AppArmor fix,
    so the lxc.apparmor.profile=unconfined workaround is gone and AppArmor
    mediation is restored inside gie containers.
    """
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    assert "raw.lxc" not in out
    assert "apparmor.profile=unconfined" not in out


def test_base_profile_has_idmap():
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    raw_idmap = parsed["config"]["raw.idmap"]
    assert "uid 53023 53023" in raw_idmap
    assert "gid 53023 53023" in raw_idmap


def test_base_profile_omits_runtime_socket_devices():
    """Regression: the four /run/user/<uid>/* socket bind
    mounts must NOT live in the gisgro-base profile, because profile-
    level disk devices race with systemd-logind's tmpfs creation. They
    are attached per-container post-boot via runtime_mounts.
    """
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    for runtime_dev in (
        "wayland-socket",
        "pulse-socket",
        "dbus-socket",
        "gpg-socket",
    ):
        assert runtime_dev not in devices, (
            f"{runtime_dev} must be attached at runtime, not via profile (see runtime_mounts.py)"
        )


def test_base_profile_passes_render_nodes_as_unix_char(mocker):
    """Each host /dev/dri/renderD* is exposed via its own unix-char device.

    Replaces the legacy ``type: gpu, gputype: physical`` device, which
    auto-passed both render nodes AND the KMS ``card*`` nodes plus
    ``/dev/nvidia*`` chardevs. Render-only passthrough is the minimum
    surface for GPU-accelerated Chrome/Mesa on a Wayland host.
    """
    mocker.patch(
        "jailbee.profiles._host_render_nodes",
        return_value=[Path("/dev/dri/renderD128"), Path("/dev/dri/renderD129")],
    )
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]
    for node_name in ("renderD128", "renderD129"):
        device = devices[f"dri-{node_name}"]
        assert device["type"] == "unix-char"
        assert device["source"] == f"/dev/dri/{node_name}"
        assert device["path"] == f"/dev/dri/{node_name}"


def test_base_profile_render_nodes_use_mode_0666(mocker):
    """Render node must be mode 0666 for dev user to open it.
    The host's `render` group GID isn't translated through raw.idmap, so
    without the mode override Chrome falls back to software rendering.
    """
    mocker.patch(
        "jailbee.profiles._host_render_nodes",
        return_value=[Path("/dev/dri/renderD128")],
    )
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]
    assert devices["dri-renderD128"]["mode"] == "0666"


def test_base_profile_omits_legacy_gpu_device(mocker):
    """Regression: the ``type: gpu`` umbrella device must not be emitted,
    because it implicitly drags in KMS ``card*`` nodes and (on NVIDIA
    hosts) all ``/dev/nvidia*`` chardevs — surface that render-node-only
    passthrough is meant to drop.
    """
    mocker.patch(
        "jailbee.profiles._host_render_nodes",
        return_value=[Path("/dev/dri/renderD128")],
    )
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]
    assert "gpu" not in devices
    for device in devices.values():
        assert device.get("type") != "gpu"


def test_base_profile_emits_no_render_devices_when_host_has_none(mocker):
    """Headless hosts (e.g. CI) have no /dev/dri/renderD* nodes. The
    profile must still validate and just omit the GPU devices — Chrome
    falls back to software rendering, which is fine in those contexts.
    """
    mocker.patch("jailbee.profiles._host_render_nodes", return_value=[])
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]
    assert not any(name.startswith("dri-") for name in devices)
    # Fonts device must still be there — it's unrelated to the GPU path.
    assert "fonts" in devices


def test_base_profile_has_env_vars():
    cfg = _cfg()
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert parsed["config"]["environment.WAYLAND_DISPLAY"] == "wayland-0"
    assert parsed["config"]["environment.XDG_RUNTIME_DIR"] == "/run/user/53023"


def test_base_profile_includes_container_env():
    cfg = _cfg()
    cfg = cfg.model_copy(
        update={
            "container": cfg.container.model_copy(
                update={"env": {"FOO": "bar", "NODE_OPTIONS": "--max-old-space-size=4096"}}
            )
        }
    )
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert parsed["config"]["environment.FOO"] == "bar"
    assert parsed["config"]["environment.NODE_OPTIONS"] == "--max-old-space-size=4096"


def test_base_profile_container_env_overrides_gie_defaults():
    """User-set container.env wins over gie's built-in environment.* keys."""
    cfg = _cfg()
    cfg = cfg.model_copy(
        update={"container": cfg.container.model_copy(update={"env": {"DISPLAY": ":1"}})}
    )
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert parsed["config"]["environment.DISPLAY"] == ":1"


def test_base_profile_sets_claude_config_dir_when_enabled(make_cfg, tmp_path):
    """`CLAUDE_CONFIG_DIR` must reach every `incus exec`, login shell or not
    — belt-and-suspenders for the `/etc/profile.d` export, which only
    login shells source (e.g. a JetBrains IDE's integrated terminal)."""
    cfg = make_cfg(tmp_path, claude={"enabled": True})
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert parsed["config"]["environment.CLAUDE_CONFIG_DIR"] == "/home/dev/.claude"


def test_base_profile_omits_claude_config_dir_when_disabled(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, claude={"enabled": False})
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert "environment.CLAUDE_CONFIG_DIR" not in parsed["config"]


def test_base_profile_container_env_overrides_claude_config_dir(make_cfg, tmp_path):
    """A user's `container.env` override must win over the built-in
    CLAUDE_CONFIG_DIR default — same pattern as SSH_AUTH_SOCK/DISPLAY."""
    cfg = make_cfg(tmp_path, claude={"enabled": True})
    cfg = cfg.model_copy(
        update={
            "container": cfg.container.model_copy(
                update={"env": {"CLAUDE_CONFIG_DIR": "/custom/path"}}
            )
        }
    )
    out = base_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert parsed["config"]["environment.CLAUDE_CONFIG_DIR"] == "/custom/path"


def test_binds_profile_includes_host_mounts():
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    # full_config.yaml has ~/.gnupg and ~/.gitconfig
    assert "host-gnupg" in devices
    assert devices["host-gnupg"]["readonly"] == "true"
    assert "host-gitconfig" in devices


def test_binds_profile_does_not_include_source_repo_bind():
    """The repo-source bind (`host-source`) lives on each container, not
    in the shared `<prefix>-binds` profile.

    See `lifecycle.new_container` for the per-container attach.
    """
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert "host-source" not in parsed["devices"]


def test_binds_profile_binds_host_localtime_readonly(mocker):
    """/etc/localtime is bind-mounted RO so container TZ matches host."""
    mocker.patch("jailbee.profiles._host_has_timezone_file", return_value=True)
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    assert "host-localtime" in devices
    assert devices["host-localtime"]["source"] == "/etc/localtime"
    assert devices["host-localtime"]["path"] == "/etc/localtime"
    assert devices["host-localtime"]["readonly"] == "true"


def test_binds_profile_binds_host_timezone_when_present(mocker):
    """Debian/Ubuntu hosts: /etc/timezone is also bind-mounted RO."""
    mocker.patch("jailbee.profiles._host_has_timezone_file", return_value=True)
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    assert "host-timezone" in devices
    assert devices["host-timezone"]["readonly"] == "true"


def test_binds_profile_skips_host_timezone_when_missing(mocker):
    """Hosts without /etc/timezone (e.g. some non-Debian distros) must not error."""
    mocker.patch("jailbee.profiles._host_has_timezone_file", return_value=False)
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    assert "host-localtime" in devices
    assert "host-timezone" not in devices


def test_binds_profile_binds_host_tmux_paths_readonly(mocker):
    """Existing host tmux config/plugin paths are RO-bound into
    the dev user's home so the container's autostart tmux session inherits
    the user's prefix, keybinds and plugins."""
    mocker.patch(
        "jailbee.profiles._host_tmux_paths",
        return_value=[
            ("host-tmux-conf", Path("/home/u/.tmux.conf"), ".tmux.conf"),
            (
                "host-tmux-plugins",
                Path("/home/u/.tmux/plugins"),
                ".tmux/plugins",
            ),
            ("host-tmux-xdg", Path("/home/u/.config/tmux"), ".config/tmux"),
        ],
    )
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]
    assert devices["host-tmux-conf"]["source"] == "/home/u/.tmux.conf"
    assert devices["host-tmux-conf"]["path"] == "/home/dev/.tmux.conf"
    assert devices["host-tmux-conf"]["readonly"] == "true"
    assert devices["host-tmux-plugins"]["path"] == "/home/dev/.tmux/plugins"
    assert devices["host-tmux-plugins"]["readonly"] == "true"
    assert devices["host-tmux-xdg"]["path"] == "/home/dev/.config/tmux"
    assert devices["host-tmux-xdg"]["readonly"] == "true"


def test_binds_profile_skips_host_tmux_paths_when_missing(mocker):
    """Hosts without tmux config must not error and must not add device
    entries for non-existent paths."""
    mocker.patch(
        "jailbee.profiles._host_tmux_paths",
        return_value=[],
    )
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]
    assert "host-tmux-conf" not in devices
    assert "host-tmux-plugins" not in devices
    assert "host-tmux-xdg" not in devices


def test_host_tmux_paths_filters_to_existing(tmp_path, mocker):
    """_host_tmux_paths returns only paths that exist under ~."""
    from jailbee.profiles import _host_tmux_paths

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".tmux.conf").write_text("# test conf\n")
    (fake_home / ".tmux" / "plugins").mkdir(parents=True)
    # XDG location intentionally absent
    mocker.patch("jailbee.profiles.Path.home", return_value=fake_home)

    triples = _host_tmux_paths()
    names = {device for device, _, _ in triples}
    assert names == {"host-tmux-conf", "host-tmux-plugins"}


def test_binds_profile_includes_shared_caches_rw():
    """Language caches are opt-in (not in the stack-neutral default), but
    when configured explicitly via `shared_caches:` they must render as
    RW disk devices on the binds profile."""
    from jailbee.config import SharedCache

    cfg = _cfg()
    cfg = cfg.model_copy(
        update={
            "shared_caches": [
                SharedCache(
                    name="pnpm-store",
                    host_subpath="caches/pnpm-store",
                    container_path="~/.local/share/pnpm/store",
                ),
                SharedCache(
                    name="gradle", host_subpath="caches/gradle", container_path="~/.gradle"
                ),
                SharedCache(name="npm", host_subpath="caches/npm", container_path="~/.npm"),
                SharedCache(name="m2", host_subpath="caches/m2", container_path="~/.m2"),
            ],
        }
    )
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    for name in ("shared-pnpm-store", "shared-gradle", "shared-npm", "shared-m2"):
        assert name in devices, f"Missing {name}"
        assert "readonly" not in devices[name] or devices[name].get("readonly") != "true"


def test_binds_profile_omits_shared_chrome_profile():
    """The chrome profile mount is now per-container via chrome_pool;
    must not be in the gisgro-binds profile."""
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    assert "shared-chrome-profile" not in parsed["devices"]


def test_binds_profile_includes_shared_jetbrains_when_enabled():
    """`_cfg()` loads full_config.yaml with jetbrains.enabled=true, so
    both shared-jetbrains-* devices appear on the binds profile."""
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    assert "shared-jetbrains-config" in devices
    assert "shared-jetbrains-data" in devices


def test_binds_profile_omits_shared_jetbrains_when_disabled():
    """With jetbrains.enabled=false, neither shared-jetbrains-* device
    appears on the binds profile."""
    cfg = _cfg()
    cfg = cfg.model_copy(update={"jetbrains": cfg.jetbrains.model_copy(update={"enabled": False})})
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]
    assert "shared-jetbrains-config" not in devices
    assert "shared-jetbrains-data" not in devices


def test_binds_profile_does_not_include_optional_mounts():
    cfg = _cfg()
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    # 'aws' is optional, never in the binds profile
    assert "optional-aws" not in parsed["devices"]


def test_net_strict_profile_attaches_acl():
    cfg = _cfg()
    out = net_profile_yaml(cfg, "strict")
    parsed = yaml.safe_load(out)
    eth0 = parsed["devices"]["eth0"]
    assert eth0["type"] == "nic"
    assert eth0["network"] == "incusbr0"
    # _cfg() loads fixtures from tests/fixtures/, so repo_root.name == "tests"
    assert eth0["security.acls"] == f"{cfg.container_prefix}-allowlist"


# ---------- ProfileNames factory + per-repo names


def test_profile_names_uses_container_prefix(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(repo)
    names = profile_names(cfg)
    assert isinstance(names, ProfileNames)
    assert names.base == "myrepo-base"
    assert names.binds == "myrepo-binds"
    assert names.net_strict == "myrepo-net-strict"
    assert names.net_loose == "myrepo-net-loose"
    assert names.net_by_mode == {
        "strict": "myrepo-net-strict",
        "loose": "myrepo-net-loose",
    }


def test_base_profile_yaml_has_per_repo_name(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    yaml_text = base_profile_yaml(make_cfg(repo))
    assert "name: myrepo-base" in yaml_text


def test_binds_profile_yaml_has_per_repo_name(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    yaml_text = binds_profile_yaml(make_cfg(repo))
    assert "name: myrepo-binds" in yaml_text


def test_net_strict_profile_references_per_repo_acl(make_cfg, tmp_path):
    repo = tmp_path / "myrepo"
    repo.mkdir()
    yaml_text = net_profile_yaml(make_cfg(repo), "strict")
    assert "name: myrepo-net-strict" in yaml_text
    assert "myrepo-allowlist" in yaml_text


def test_net_loose_profile_no_acl():
    cfg = _cfg()
    out = net_profile_yaml(cfg, "loose")
    parsed = yaml.safe_load(out)
    eth0 = parsed["devices"]["eth0"]
    assert eth0["type"] == "nic"
    assert "security.acls" not in eth0


def test_net_loose_profile_uses_gie_loose_bridge():
    cfg = _cfg()
    out = net_profile_yaml(cfg, "loose")
    parsed = yaml.safe_load(out)
    eth0 = parsed["devices"]["eth0"]
    assert eth0["network"] == "jailbee-loose", (
        "loose profile must point at the dedicated jailbee-loose bridge "
        "— incusbr0 carries the allowlist ACL and would "
        "filter loose traffic too"
    )


def test_loose_net_profile_yaml_matches_the_config_driven_one():
    """The prefix-only helper must stay byte-identical to the config-driven one.

    `loose_net_profile_yaml` exists so a loose profile can be written without
    loading a repo's whole config. If `net_profile_yaml(cfg, "loose")` ever
    grows a Config-derived key the prefix-only path cannot see, that path
    would quietly write a *different* profile than `jailbee apply` produces —
    a divergence nothing else in the suite would notice.
    """
    from jailbee.profiles import loose_net_profile_yaml

    cfg = _cfg()
    assert loose_net_profile_yaml(cfg.container_prefix) == net_profile_yaml(cfg, "loose")


def test_loose_net_profile_yaml_names_and_points_the_profile_from_the_prefix():
    from jailbee.profiles import loose_net_profile_yaml

    parsed = yaml.safe_load(loose_net_profile_yaml("otherapp"))
    assert parsed["name"] == "otherapp-net-loose"
    assert parsed["devices"]["eth0"] == {"type": "nic", "network": "jailbee-loose"}


# ---------- shared_caches integration


def test_binds_profile_default_caches_are_stack_neutral():
    """The stack-neutral default (`shared_caches` = [ssh]) means the
    language caches (pnpm/gradle/npm/m2) are absent unless configured
    explicitly. jetbrains/claude caches are unaffected — they're added
    by `effective_shared_caches` from their own `*.enabled` flags, not
    from the `shared_caches` default."""
    cfg = _cfg()
    yaml_text = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(yaml_text)
    devices = parsed["devices"]
    for name in ("shared-pnpm-store", "shared-gradle", "shared-npm", "shared-m2"):
        assert name not in devices
    for name in (
        "shared-ssh",
        "shared-jetbrains-config",
        "shared-jetbrains-data",
        "shared-claude",
    ):
        assert name in devices


def test_binds_profile_empty_shared_caches(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(update={"shared_caches": []})
    yaml_text = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(yaml_text)
    devices = parsed["devices"]
    for name in ("shared-pnpm-store", "shared-gradle", "shared-claude"):
        assert name not in devices


def test_binds_profile_omits_claude_when_disabled(make_cfg, tmp_path):
    """When claude.enabled is false the binds profile must not bind
    shared-claude / shared-claude-json devices, even if the default
    cache list once contained them."""
    cfg = make_cfg(tmp_path, claude={"enabled": False})
    yaml_text = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(yaml_text)
    devices = parsed["devices"]
    assert "shared-claude" not in devices
    assert "shared-claude-json" not in devices


def test_binds_profile_includes_claude_when_enabled(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, claude={"enabled": True})
    yaml_text = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(yaml_text)
    devices = parsed["devices"]
    assert "shared-claude" in devices
    # `.claude.json` is no longer its own file-level bind; it lives inside the
    # `claude` directory mount (CLAUDE_CONFIG_DIR).
    assert "shared-claude-json" not in devices


def test_binds_profile_custom_cache(make_cfg, tmp_path):
    from jailbee.config import SharedCache

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "shared_caches": [
                SharedCache(name="cargo", host_subpath="caches/cargo", container_path="~/.cargo"),
            ],
        }
    )
    yaml_text = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(yaml_text)
    cache = parsed["devices"]["shared-cargo"]
    assert cache["source"] == f"{cfg.shared_dir}/caches/cargo"
    assert cache["path"] == "/home/dev/.cargo"


def test_binds_profile_absolute_container_path(make_cfg, tmp_path):
    from jailbee.config import SharedCache

    cfg = make_cfg(tmp_path)
    cfg = cfg.model_copy(
        update={
            "shared_caches": [
                SharedCache(
                    name="opt-cache", host_subpath="opt-cache", container_path="/opt/cache"
                ),
            ],
        }
    )
    yaml_text = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(yaml_text)
    assert parsed["devices"]["shared-opt-cache"]["path"] == "/opt/cache"


def test_binds_profile_includes_host_jetbrains_userprefs_when_enabled():
    """With the flag true (default), the binds profile must
    contain a disk device for ~/.java/.userPrefs/jetbrains (RW). After
    the host-tooling refactor the device is contributed by
    Config.effective_host_mounts(), and the device name is derived
    from the last path segment (`jetbrains`)."""
    cfg = _cfg()
    # full_config.yaml does not set the flag, so default (True) applies
    out = binds_profile_yaml(cfg)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]

    assert "host-jetbrains" in devices
    device = devices["host-jetbrains"]
    assert device["type"] == "disk"
    # Container-side path = host-side path (auto-mount keeps them equal)
    assert device["path"].endswith("/.java/.userPrefs/jetbrains")
    # Source must point at the host home's userPrefs dir
    assert device["source"].endswith("/.java/.userPrefs/jetbrains")
    # RW — no readonly: true key
    assert "readonly" not in device or device["readonly"] != "true"


def test_binds_profile_omits_host_jetbrains_userprefs_when_disabled():
    """When the flag is false, no bind device is emitted."""
    cfg = _cfg()
    cfg_disabled = cfg.model_copy(
        update={"jetbrains": cfg.jetbrains.model_copy(update={"userprefs_from_host": False})}
    )

    out = binds_profile_yaml(cfg_disabled)
    parsed = yaml.safe_load(out)
    devices = parsed["devices"]

    assert "host-jetbrains" not in devices


def test_ssh_auth_sock_present_when_gpg_enabled(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, gpg={"enabled": True})
    yaml_text = base_profile_yaml(cfg)
    assert "SSH_AUTH_SOCK" in yaml_text
    assert "S.gpg-agent.ssh" in yaml_text


def test_ssh_auth_sock_omitted_when_gpg_disabled(tmp_path):
    from tests.conftest import make_cfg

    cfg = make_cfg(tmp_path, gpg={"enabled": False})
    yaml_text = base_profile_yaml(cfg)
    assert "SSH_AUTH_SOCK" not in yaml_text


def test_binds_profile_skips_host_mounts_under_repo_dir(tmp_path):
    """A host_mounts entry whose container path lives under
    /home/<user>/<repo>/ must NOT appear in the binds profile.

    Profile-level disk devices get mounted at `incus start`, which
    pre-creates the mount target — and with it the parent
    /home/<user>/<repo>/. `git clone /mnt/host-source /home/<user>/<repo>`
    then fails with "destination path already exists and is not an
    empty directory". lifecycle.new_container attaches these as
    per-container devices *after* clone instead.
    """
    from tests.conftest import make_cfg

    repo = tmp_path / "myrepo"
    repo.mkdir()
    src = tmp_path / "src-local"
    src.mkdir()
    cfg = make_cfg(
        repo,
        host_mounts=[
            {"host": str(src), "container": "/home/dev/myrepo/local", "readonly": True},
        ],
        gpg={"enabled": False},
        ssh={"enabled": False},
        jetbrains={"enabled": False},
        chrome={"enabled": False},
    )

    out = binds_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]

    # No device targets the under-repo path
    assert not any(d.get("path") == "/home/dev/myrepo/local" for d in devices.values())
    # And no device sources from our test path either
    assert not any(d.get("source") == str(src) for d in devices.values())


def test_binds_profile_keeps_host_mounts_outside_repo_dir(tmp_path):
    """Non-under-repo host_mounts are still emitted in the binds profile."""
    from tests.conftest import make_cfg

    repo = tmp_path / "myrepo"
    repo.mkdir()
    src = tmp_path / "src-elsewhere"
    src.mkdir()
    cfg = make_cfg(
        repo,
        host_mounts=[
            {"host": str(src), "container": "/home/dev/.elsewhere", "readonly": True},
        ],
        gpg={"enabled": False},
        ssh={"enabled": False},
        jetbrains={"enabled": False},
        chrome={"enabled": False},
    )

    out = binds_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]

    assert any(d.get("path") == "/home/dev/.elsewhere" for d in devices.values())


def test_binds_profile_skips_under_repo_shared_caches(tmp_path):
    """A shared_cache whose container_path lives under
    /home/<user>/<container_prefix>/ must NOT appear in the binds profile.
    Same reason as host_mounts (see test_binds_profile_skips_host_mounts_under_repo_dir):
    profile-level disks get mounted at `incus start`, which pre-creates the
    target and breaks `git clone`.

    Triggered today by the auto-added `jetbrains-idea` cache when
    `jetbrains.enabled AND share_idea`.
    """
    from tests.conftest import make_cfg

    repo = tmp_path / "myrepo"
    repo.mkdir()
    cfg = make_cfg(
        repo,
        jetbrains={"enabled": True, "share_idea": True},
        gpg={"enabled": False},
        ssh={"enabled": False},
        chrome={"enabled": False},
    )

    out = binds_profile_yaml(cfg)
    devices = yaml.safe_load(out)["devices"]

    # The under-repo cache is filtered out of the profile.
    assert "shared-jetbrains-idea" not in devices
    # Sibling jetbrains caches (NOT under repo) still appear.
    assert "shared-jetbrains-config" in devices
    assert "shared-jetbrains-data" in devices


# ---------- container_repo_dir_for follows container_prefix (#prefix-in-container)


def test_container_repo_dir_for_uses_container_prefix(make_cfg, tmp_path):
    """Host-side path derives from container_prefix, not repo_root.name."""
    from jailbee.profiles import container_repo_dir_for

    repo = tmp_path / "SampleApp"
    repo.mkdir()
    cfg = make_cfg(repo, container_prefix="gisgro")
    assert container_repo_dir_for(cfg) == "/home/dev/gisgro"


def test_is_under_repo_uses_container_prefix(make_cfg, tmp_path):
    """Mount-routing decision follows container_prefix."""
    from jailbee.profiles import is_under_repo

    repo = tmp_path / "SampleApp"
    repo.mkdir()
    cfg = make_cfg(repo, container_prefix="gisgro")
    assert is_under_repo("/home/dev/gisgro/.aws/credentials", cfg) is True
    assert is_under_repo("/home/dev/SampleApp/.aws/credentials", cfg) is False
    assert is_under_repo("/home/dev/elsewhere", cfg) is False


def _cfg_with_host_devices(devices):
    """Build a Config (via load_config) then graft on host_devices.

    Built before any ``Path.exists`` patch so load_config's own
    global.yaml/repo-config existence probes run against the real FS.
    """
    cfg = _cfg()
    return cfg.model_copy(update={"host_devices": devices})


def test_base_profile_passes_host_device_as_unix_char(mocker):
    from jailbee.config import HostDevice

    cfg = _cfg_with_host_devices([HostDevice(path="/dev/kvm", mode="0666")])
    mocker.patch("jailbee.profiles.Path.exists", return_value=True)
    devices = yaml.safe_load(base_profile_yaml(cfg))["devices"]
    dev = devices["hostdev-dev-kvm"]
    assert dev["type"] == "unix-char"
    assert dev["source"] == "/dev/kvm"
    assert dev["path"] == "/dev/kvm"
    assert dev["mode"] == "0666"


def test_base_profile_skips_absent_host_device(mocker):
    from jailbee.config import HostDevice

    cfg = _cfg_with_host_devices([HostDevice(path="/dev/kvm")])
    mocker.patch("jailbee.profiles.Path.exists", return_value=False)
    devices = yaml.safe_load(base_profile_yaml(cfg))["devices"]
    assert "hostdev-dev-kvm" not in devices


def test_base_profile_host_device_names_do_not_collide(mocker):
    from jailbee.config import HostDevice

    cfg = _cfg_with_host_devices(
        [
            HostDevice(path="/dev/bus/usb/001/004"),
            HostDevice(path="/dev/bus/usb/002/004"),
        ]
    )
    mocker.patch("jailbee.profiles.Path.exists", return_value=True)
    devices = yaml.safe_load(base_profile_yaml(cfg))["devices"]
    assert "hostdev-dev-bus-usb-001-004" in devices
    assert "hostdev-dev-bus-usb-002-004" in devices


def test_base_profile_host_device_source_and_gid_uid(mocker):
    from jailbee.config import HostDevice

    cfg = _cfg_with_host_devices(
        [HostDevice(path="/dev/kvm", source="/dev/kvm-host", gid=1000, uid=1000, mode=None)]
    )
    mocker.patch("jailbee.profiles.Path.exists", return_value=True)
    dev = yaml.safe_load(base_profile_yaml(cfg))["devices"]["hostdev-dev-kvm"]
    assert dev["source"] == "/dev/kvm-host"
    assert dev["gid"] == "1000"
    assert dev["uid"] == "1000"
    assert "mode" not in dev


def test_net_by_mode_has_no_offline():
    assert sorted(profile_names(_cfg()).net_by_mode) == ["loose", "strict"]


def test_net_profile_yaml_rejects_offline():
    with pytest.raises(ValueError, match="Unknown network mode"):
        net_profile_yaml(_cfg(), "offline")
