"""Tests for the macOS delegation bridge. Fully mocked — no real VM."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

import pytest

from jailbee import entry, macos


def _cp(returncode: int, stdout: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_load_bridge_config_defaults_when_file_missing(tmp_path: Path) -> None:
    cfg = macos.load_bridge_config(tmp_path / "nope.yaml")
    assert cfg.transport == ["colima", "ssh"]
    assert cfg.tty_flag == ["-t"]
    assert cfg.workdir_flag == "--workdir"
    assert cfg.shared_root == Path.home()


def test_load_bridge_config_reads_overrides(tmp_path: Path) -> None:
    p = tmp_path / "macos.yaml"
    p.write_text("transport: [lima]\ntty_flag: ['-t']\nworkdir_flag: --dir\nshared_root: ~/code\n")
    cfg = macos.load_bridge_config(p)
    assert cfg.transport == ["lima"]
    assert cfg.tty_flag == ["-t"]
    assert cfg.workdir_flag == "--dir"
    assert cfg.shared_root == Path.home() / "code"


def test_load_bridge_config_rejects_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / "macos.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(macos.BridgeError):
        macos.load_bridge_config(p)


def test_load_bridge_config_rejects_empty_transport(tmp_path: Path) -> None:
    p = tmp_path / "macos.yaml"
    p.write_text("transport: []\n")
    with pytest.raises(macos.BridgeError, match="transport must be a non-empty list"):
        macos.load_bridge_config(p)


def test_config_path_lives_under_jailbee(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_config_path() returns a path under ~/.config/jailbee/, not the old ~/.config/gie/."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert macos._config_path() == tmp_path / "jailbee" / "macos.yaml"


def test_preflight_transport_binary_missing(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value=None)
    cfg = macos.BridgeConfig()
    with pytest.raises(macos.BridgeError, match="colima not found"):
        macos.preflight(cfg, Path.home())


def test_preflight_vm_not_running(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/usr/bin/colima")
    mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(1))
    cfg = macos.BridgeConfig()
    with pytest.raises(macos.BridgeError, match="VM not running"):
        macos.preflight(cfg, Path.home())


def test_preflight_jailbee_not_installed(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/usr/bin/colima")
    # 1st call (`true`) succeeds; 2nd call (`jailbee version`) fails.
    mocker.patch(
        "jailbee.macos.subprocess.run",
        side_effect=[_cp(0), _cp(127)],
    )
    cfg = macos.BridgeConfig()
    with pytest.raises(macos.BridgeError, match="jailbee mac bootstrap"):
        macos.preflight(cfg, Path.home())


def test_preflight_cwd_outside_shared_root(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/usr/bin/colima")
    mocker.patch(
        "jailbee.macos.subprocess.run",
        side_effect=[_cp(0), _cp(0, stdout="9.9.9")],
    )
    cfg = macos.BridgeConfig(shared_root=Path("/Users/me"))
    with pytest.raises(macos.BridgeError, match="must live under"):
        macos.preflight(cfg, Path("/tmp/elsewhere"))


def test_preflight_ok_and_version_mismatch_warns(mocker, capsys) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/usr/bin/colima")
    mocker.patch(
        "jailbee.macos.subprocess.run",
        side_effect=[_cp(0), _cp(0, stdout="0.0.0-different")],
    )
    cfg = macos.BridgeConfig(shared_root=Path("/Users/me"))
    macos.preflight(cfg, Path("/Users/me/repo"))  # no raise
    assert "differs" in capsys.readouterr().err


def test_delegate_builds_command_and_propagates_exit(mocker) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(7))
    cfg = macos.BridgeConfig()
    with pytest.raises(SystemExit) as exc:
        macos._delegate(cfg, ["new", "feat/x"], Path("/Users/me/repo"), isatty=False)
    assert exc.value.code == 7
    run.assert_called_once_with(
        ["colima", "ssh", "--workdir", "/Users/me/repo", "--", "jailbee", "new", "feat/x"],
        check=False,
    )


def test_delegate_inserts_tty_flag_when_interactive(mocker) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    cfg = macos.BridgeConfig(tty_flag=["-t"])
    with pytest.raises(SystemExit):
        macos._delegate(cfg, ["shell", "c"], Path("/Users/me/repo"), isatty=True)
    assert run.call_args.args[0] == [
        "colima",
        "ssh",
        "-t",
        "--workdir",
        "/Users/me/repo",
        "--",
        "jailbee",
        "shell",
        "c",
    ]


def test_maybe_delegate_noop_on_linux(mocker) -> None:
    spy = mocker.patch("jailbee.macos._delegate")
    assert macos.maybe_delegate(["new", "x"], platform="linux") is None
    spy.assert_not_called()


def test_maybe_delegate_routes_mac_command(mocker) -> None:
    run_mac = mocker.patch("jailbee.macos._run_mac_command", return_value=0)
    with pytest.raises(SystemExit) as exc:
        macos.maybe_delegate(["mac", "doctor"], platform="darwin")
    assert exc.value.code == 0
    run_mac.assert_called_once_with(["doctor"])


def test_maybe_delegate_preflight_then_delegate(mocker) -> None:
    load_cfg = mocker.patch("jailbee.macos.load_bridge_config", return_value=macos.BridgeConfig())
    cfg = load_cfg.return_value
    pf = mocker.patch("jailbee.macos.preflight")
    dg = mocker.patch("jailbee.macos._delegate", side_effect=SystemExit(0))
    with pytest.raises(SystemExit):
        macos.maybe_delegate(["new", "x"], platform="darwin")
    pf.assert_called_once_with(cfg, Path.cwd())
    dg.assert_called_once_with(cfg, ["new", "x"], Path.cwd())


def test_maybe_delegate_preflight_error_exits_1(mocker, capsys) -> None:
    mocker.patch("jailbee.macos.load_bridge_config", return_value=macos.BridgeConfig())
    mocker.patch(
        "jailbee.macos.preflight",
        side_effect=macos.BridgeError("Colima VM not running. Start once: ..."),
    )
    with pytest.raises(SystemExit) as exc:
        macos.maybe_delegate(["new", "x"], platform="darwin")
    assert exc.value.code == 1
    assert "VM not running" in capsys.readouterr().err


def test_run_mac_command_unknown_returns_2(capsys) -> None:
    assert macos._run_mac_command(["frobnicate"]) == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_mac_doctor_ok(mocker, capsys) -> None:
    mocker.patch("jailbee.macos.load_bridge_config", return_value=macos.BridgeConfig())
    mocker.patch("jailbee.macos.preflight")
    assert macos._run_mac_command(["doctor"]) == 0
    assert "OK" in capsys.readouterr().out


def test_mac_doctor_reports_failure(mocker, capsys) -> None:
    mocker.patch("jailbee.macos.load_bridge_config", return_value=macos.BridgeConfig())
    msg = "jailbee is not installed in the VM. Run once: jailbee mac bootstrap"
    mocker.patch(
        "jailbee.macos.preflight",
        side_effect=macos.BridgeError(msg),
    )
    assert macos._run_mac_command(["doctor"]) == 1
    assert "jailbee mac bootstrap" in capsys.readouterr().err


def test_mac_bootstrap_installs_from_vrtfinland(mocker) -> None:
    mocker.patch("jailbee.macos.load_bridge_config", return_value=macos.BridgeConfig())
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    assert macos._run_mac_command(["bootstrap"]) == 0
    # The install step must reference the published repo.
    joined = " ".join(str(c) for call in run.call_args_list for c in call.args[0])
    assert "git+https://github.com/VRTFinland/jailbee" in joined


def test_main_runs_app_when_not_delegating(mocker) -> None:
    # maybe_delegate returns (Linux path); app() must then run.
    mocker.patch("jailbee.macos.maybe_delegate", return_value=None)
    app = mocker.patch("jailbee.cli.app")
    entry.main()
    app.assert_called_once()


def test_main_does_not_run_app_when_delegating(mocker) -> None:
    # On the delegate path maybe_delegate raises SystemExit; app() must NOT run.
    mocker.patch("jailbee.macos.maybe_delegate", side_effect=SystemExit(0))
    app = mocker.patch("jailbee.cli.app")
    with pytest.raises(SystemExit):
        entry.main()
    app.assert_not_called()


def test_main_catches_bridge_error_from_maybe_delegate(mocker, capsys) -> None:
    mocker.patch(
        "jailbee.macos.maybe_delegate",
        side_effect=macos.BridgeError("bad config"),
    )
    app = mocker.patch("jailbee.cli.app")
    with pytest.raises(SystemExit) as exc:
        entry.main()
    assert exc.value.code == 1
    assert "bad config" in capsys.readouterr().err
    app.assert_not_called()


# --- Apple `container machine` backend -------------------------------------------------


def _apple_cfg(**over) -> macos.BridgeConfig:  # type: ignore[no-untyped-def]
    base = dict(
        backend="apple",
        machine="jb",
        transport=["container", "machine", "run", "-n", "jb", "--root"],
        user="me",
        tty_flag=["-i", "-t"],
        workdir_flag="",
        shared_root=Path("/Users/me"),
    )
    base.update(over)
    return macos.BridgeConfig(**base)


def test_colima_defaults_unchanged() -> None:
    cfg = macos.BridgeConfig()
    assert (cfg.backend, cfg.mode, cfg.gui) == ("colima", "mac-repos", False)


def test_apple_backend_defaults_from_yaml(tmp_path: Path, mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/opt/homebrew/bin/container")
    p = tmp_path / "macos.yaml"
    p.write_text("backend: apple\nmachine: jb\nmode: isolated\ngui: true\n")
    cfg = macos.load_bridge_config(p)
    assert cfg.transport == ["/opt/homebrew/bin/container", "machine", "run", "-n", "jb", "--root"]
    assert cfg.tty_flag == ["-i", "-t"]
    assert cfg.workdir_flag == ""
    assert (cfg.machine, cfg.mode, cfg.gui) == ("jb", "isolated", True)


def test_apple_backend_delegate_passes_one_quoted_command(mocker) -> None:
    """`container machine run` re-splits argv through bash -c: the command must be ONE string."""
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(7))
    with pytest.raises(SystemExit) as exc:
        macos._delegate(_apple_cfg(), ["new", "feat x"], Path("/Users/me/repo"), isatty=False)
    assert exc.value.code == 7
    cmd = run.call_args.args[0]
    assert cmd[:8] == ["container", "machine", "run", "-n", "jb", "--root", "-i", "--"]
    assert len(cmd) == 9
    # root enters the machine, then drops to the login user WITH its supplementary groups
    # (a plain machine-run session has none, so incus-admin would be missing).
    assert cmd[8].startswith("runuser -u me -- bash -c ")
    assert "feat x" in cmd[8]
    assert ".local/bin" in cmd[8]
    assert "--workdir" not in cmd


def test_apple_backend_delegate_uses_tty_flags_when_interactive(mocker) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    with pytest.raises(SystemExit):
        macos._delegate(_apple_cfg(), ["shell", "c"], Path("/Users/me/repo"), isatty=True)
    assert run.call_args.args[0][:8] == [
        "container",
        "machine",
        "run",
        "-n",
        "jb",
        "--root",
        "-i",
        "-t",
    ]


def test_isolated_mode_skips_shared_root_check(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/x/container")
    mocker.patch("jailbee.macos.subprocess.run", side_effect=[_cp(0), _cp(0, stdout="9.9.9")])
    macos.preflight(_apple_cfg(mode="isolated"), Path("/tmp/elsewhere"))  # no raise


def test_mac_repos_mode_still_checks_shared_root(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value="/x/container")
    mocker.patch("jailbee.macos.subprocess.run", side_effect=[_cp(0), _cp(0, stdout="9.9.9")])
    with pytest.raises(macos.BridgeError, match="must live under"):
        macos.preflight(_apple_cfg(), Path("/tmp/elsewhere"))


def test_apple_preflight_messages_name_jailbee_mac_setup(mocker) -> None:
    mocker.patch("jailbee.macos.shutil.which", return_value=None)
    with pytest.raises(macos.BridgeError, match="jailbee mac setup"):
        macos.preflight(_apple_cfg(), Path("/Users/me"))
    mocker.patch("jailbee.macos.shutil.which", return_value="/x/container")
    mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(1))
    with pytest.raises(macos.BridgeError, match="container machine run -n jb"):
        macos.preflight(_apple_cfg(), Path("/Users/me"))
    mocker.patch("jailbee.macos.subprocess.run", side_effect=[_cp(0), _cp(127)])
    with pytest.raises(macos.BridgeError, match="jailbee mac setup"):
        macos.preflight(_apple_cfg(), Path("/Users/me"))


# --- GUI commands over waypipe --------------------------------------------------------


def test_gui_command_uses_waypipe_when_enabled(mocker, tmp_path: Path) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    mocker.patch(
        "jailbee.macos._ensure_cocoa_way",
        return_value={"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/t"},
    )
    mocker.patch("jailbee.macos._repo_prefix", return_value="pfx")
    with pytest.raises(SystemExit):
        macos._delegate(
            _apple_cfg(gui=True), ["firefox", "c1"], Path("/Users/me/repo"), isatty=False
        )
    cmd = run.call_args.args[0]
    assert cmd[:3] == ["waypipe", "--compress=zstd", "ssh"]
    assert cmd[-5:] == ["jailbee-ssh", "firefox", "c1", "--repo", "pfx"]
    assert run.call_args.kwargs["env"]["WAYLAND_DISPLAY"] == "wayland-1"
    assert "-t" not in cmd


def test_gui_dashboard_gets_a_tty_when_interactive(mocker) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    mocker.patch("jailbee.macos._ensure_cocoa_way", return_value={})
    mocker.patch("jailbee.macos._repo_prefix", return_value=None)
    with pytest.raises(SystemExit):
        macos._delegate(_apple_cfg(gui=True), ["dashboard"], Path("/Users/me/repo"), isatty=True)
    cmd = run.call_args.args[0]
    assert cmd[-2:] == ["jailbee-ssh", "dashboard"]
    assert "-t" in cmd


def test_explicit_repo_is_not_resolved_again(mocker) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    mocker.patch("jailbee.macos._ensure_cocoa_way", return_value={})
    spy = mocker.patch("jailbee.macos._repo_prefix")
    with pytest.raises(SystemExit):
        macos._delegate(
            _apple_cfg(gui=True), ["chrome", "c", "--repo", "x"], Path("/Users/me/r"), isatty=False
        )
    spy.assert_not_called()
    assert run.call_args.args[0][-5:] == ["jailbee-ssh", "chrome", "c", "--repo", "x"]


def test_non_gui_command_uses_machine_run(mocker) -> None:
    run = mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    spy = mocker.patch("jailbee.macos._ensure_cocoa_way")
    with pytest.raises(SystemExit):
        macos._delegate(_apple_cfg(gui=True), ["ls"], Path("/Users/me/repo"), isatty=False)
    spy.assert_not_called()
    assert run.call_args.args[0][:3] == ["container", "machine", "run"]


def test_gui_disabled_never_touches_cocoa_way(mocker) -> None:
    mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(0))
    spy = mocker.patch("jailbee.macos._ensure_cocoa_way")
    with pytest.raises(SystemExit):
        macos._delegate(
            _apple_cfg(gui=False), ["firefox", "c"], Path("/Users/me/repo"), isatty=False
        )
    spy.assert_not_called()


def test_repo_prefix_reads_container_prefix_from_config_show(mocker) -> None:
    mocker.patch(
        "jailbee.macos.subprocess.run",
        return_value=_cp(0, stdout="container_prefix: demo\nfoo: 1\n"),
    )
    assert macos._repo_prefix(_apple_cfg(), Path("/Users/me/repo")) == "demo"
    mocker.patch("jailbee.macos.subprocess.run", return_value=_cp(1))
    assert macos._repo_prefix(_apple_cfg(), Path("/Users/me/repo")) is None


def test_ensure_cocoa_way_reuses_a_running_compositor(mocker, tmp_path: Path, monkeypatch) -> None:
    import socket as _socket

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    d = tmp_path / "cocoa-way"
    d.mkdir()
    s = _socket.socket(_socket.AF_UNIX)
    s.bind(str(d / "wayland-1"))
    popen = mocker.patch("jailbee.macos.subprocess.Popen")
    try:
        env = macos._ensure_cocoa_way()
    finally:
        s.close()
    popen.assert_not_called()
    assert env == {"XDG_RUNTIME_DIR": str(d), "WAYLAND_DISPLAY": "wayland-1"}


def test_ensure_cocoa_way_starts_rootless_and_waits_for_the_socket(
    mocker, tmp_path: Path, monkeypatch
) -> None:
    import socket as _socket

    monkeypatch.setenv("TMPDIR", str(tmp_path))
    d = tmp_path / "cocoa-way"
    d.mkdir()
    holder: list[_socket.socket] = []

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        assert argv == ["cocoa-way"]
        assert kwargs["env"]["COCOA_WAY_PRESENTATION"] == "rootless"
        assert kwargs["start_new_session"] is True
        s = _socket.socket(_socket.AF_UNIX)
        s.bind(str(d / "wayland-0"))
        holder.append(s)

    mocker.patch("jailbee.macos.subprocess.Popen", side_effect=fake_popen)
    mocker.patch("jailbee.macos.time.sleep")
    try:
        env = macos._ensure_cocoa_way()
    finally:
        for s in holder:
            s.close()
    assert env["WAYLAND_DISPLAY"] == "wayland-0"


def test_ensure_cocoa_way_gives_up_with_install_hint(mocker, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    mocker.patch("jailbee.macos.subprocess.Popen")
    mocker.patch("jailbee.macos.time.sleep")
    with pytest.raises(macos.BridgeError, match="brew install cocoa-way"):
        macos._ensure_cocoa_way()


def test_mac_setup_hands_over_to_the_setup_command(mocker) -> None:
    main = mocker.patch("jailbee.macsetup.cli.main", return_value=0)
    assert macos._run_mac_command(["setup", "--isolated"]) == 0
    main.assert_called_once_with(["setup", "--isolated"])


@pytest.mark.parametrize("sub", ["upgrade", "uninstall"])
def test_mac_upgrade_and_uninstall_hand_over(mocker, sub: str) -> None:
    main = mocker.patch("jailbee.macsetup.cli.main", return_value=0)
    assert macos._run_mac_command([sub]) == 0
    main.assert_called_once_with([sub])


def test_mac_doctor_uses_the_layer_report_for_apple_backend(mocker) -> None:
    mocker.patch("jailbee.macos.load_bridge_config", return_value=_apple_cfg())
    main = mocker.patch("jailbee.macsetup.cli.main", return_value=1)
    assert macos._run_mac_command(["doctor"]) == 1
    main.assert_called_once_with(["doctor"])


def test_usage_lists_the_new_subcommands(capsys) -> None:
    assert macos._run_mac_command([]) == 2
    assert "setup" in capsys.readouterr().err
