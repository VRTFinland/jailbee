"""Tests for GUI app launchers (open_ide / open_chrome).

These verify that launched apps redirect stdout/stderr to a per-app log file
inside the container, not to /dev/null. The user needs to be able to read
those logs to diagnose why a GUI failed to appear (Wayland sockets not
visible, missing libs, crash on startup, etc.).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jailbee.config import load_config
from jailbee.gui import open_chrome, open_ide
from jailbee.incus import Incus

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _stub_config_get(mocker):
    """Default Incus.config_get to None so the new label lookup in
    container_repo_dir falls back without shelling out to a real ``incus``
    binary. Individual tests can override per-instance if they need to.
    """
    mocker.patch.object(Incus, "config_get", return_value=None)


def _popen_bash_command(popen_mock) -> str:
    """Extract the `bash -c <SCRIPT>` argument passed to subprocess.Popen."""
    assert popen_mock.call_count == 1
    argv = popen_mock.call_args.args[0]
    # argv is: ["incus", "exec", container, ..., "--", "bash", "-c", "<script>"]
    return argv[-1]


def test_open_ide_redirects_to_log_file_not_dev_null(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(
        incus,
        "exec",
        return_value="/opt/jetbrains-toolbox/apps/intellij-idea-ultimate/bin/idea\n",
    )
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "idea")

    script = _popen_bash_command(popen)
    # stdout+stderr go to the log, not to /dev/null
    assert ">/tmp/jailbee-ide-idea.log" in script
    assert "2>&1" in script
    assert ">/dev/null" not in script
    # stdin is closed (</dev/null) and the inner process is setsid-detached
    # so it survives the launcher's bash exiting.
    assert "</dev/null" in script
    assert "setsid" in script
    # Backgrounded
    assert script.rstrip().endswith("&")


def test_open_ide_uses_app_specific_log_for_webstorm(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(
        incus,
        "exec",
        return_value="/opt/jetbrains-toolbox/apps/webstorm/bin/webstorm\n",
    )
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "webstorm")

    script = _popen_bash_command(popen)
    assert "/tmp/jailbee-ide-webstorm.log" in script
    assert "/tmp/jailbee-ide-idea.log" not in script


def test_open_ide_announces_log_path_in_info_message(mocker, capsys):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(
        incus,
        "exec",
        return_value="/opt/jetbrains-toolbox/apps/intellij-idea-ultimate/bin/idea\n",
    )
    mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "idea")

    out = capsys.readouterr().out
    assert "/tmp/jailbee-ide-idea.log" in out


def test_open_ide_skips_launch_when_no_launcher_found(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(incus, "exec", return_value="\n")
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "idea")

    popen.assert_not_called()


def test_open_chrome_redirects_to_log_file_not_dev_null(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    script = _popen_bash_command(popen)
    assert ">/tmp/jailbee-chrome.log" in script
    assert "2>&1" in script
    assert ">/dev/null" not in script
    assert "</dev/null" in script
    assert "setsid" in script
    assert script.rstrip().endswith("&")


def test_open_chrome_announces_log_path_in_info_message(mocker, capsys):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", "https://example.com")

    out = capsys.readouterr().out
    assert "/tmp/jailbee-chrome.log" in out


def test_open_chrome_passes_url_when_provided(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", "https://example.com")

    script = _popen_bash_command(popen)
    assert "https://example.com" in script


def _popen_env_args(popen_mock) -> dict[str, str]:
    """Extract env vars from `incus exec ... --env K=V ...` argv."""
    argv = popen_mock.call_args.args[0]
    env: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i] == "--env" and i + 1 < len(argv):
            k, _, v = argv[i + 1].partition("=")
            env[k] = v
            i += 2
        else:
            i += 1
    return env


def test_open_ide_passes_home_env_var(mocker):
    """Incus exec --user <uid> doesn't auto-set HOME, so apps
    that depend on it (~/.config, ~/.local) fail. _gui_env must include
    HOME=/home/<username>.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(
        incus,
        "exec",
        return_value="/opt/jetbrains-toolbox/apps/intellij-idea-ultimate/bin/idea\n",
    )
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "idea")

    env = _popen_env_args(popen)
    assert env.get("HOME") == "/home/dev"


def test_open_chrome_passes_home_env_var(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    env = _popen_env_args(popen)
    assert env.get("HOME") == "/home/dev"


def test_open_chrome_passes_ozone_wayland_on_wayland_host(mocker, monkeypatch):
    """Chrome defaults to X11 even with WAYLAND_DISPLAY set;
    pass --ozone-platform=wayland explicitly when the host is Wayland.
    """
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    script = _popen_bash_command(popen)
    assert "--ozone-platform=wayland" in script


def test_open_chrome_passes_dark_mode_flags_when_enabled(mocker):
    """Chrome.dark_mode=True (opt-in) forces Chrome into dark mode."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    cfg = cfg.model_copy(update={"chrome": cfg.chrome.model_copy(update={"dark_mode": True})})
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    script = _popen_bash_command(popen)
    assert "--force-dark-mode" in script
    assert "WebContentsForceDark" in script


def test_open_chrome_omits_dark_mode_flags_by_default(mocker):
    """Chrome.dark_mode defaults to False — no forced dark mode."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    script = _popen_bash_command(popen)
    assert "--force-dark-mode" not in script
    assert "WebContentsForceDark" not in script


def test_open_chrome_omits_ozone_wayland_on_x11_host(mocker, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    script = _popen_bash_command(popen)
    assert "--ozone-platform=wayland" not in script


def test_open_chrome_calls_allocate_before_popen(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    allocate = mocker.patch(
        "jailbee.chrome_pool.allocate",
        return_value=Path("/x/slot-0"),
    )
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    allocate.assert_called_once_with(cfg, incus, "feat-smoke")
    assert popen.called


def test_open_ide_popen_fully_detaches_from_parent(mocker):
    """Without start_new_session + DEVNULL stdio, the child `incus exec`
    shares gie's TTY. When gie exits the terminal is left in a broken
    state (`reset` needed) and SIGHUP propagation kills the GUI before
    it appears. Verify both detach knobs are set.
    """
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(
        incus,
        "exec",
        return_value="/opt/jetbrains-toolbox/apps/intellij-idea-ultimate/bin/idea\n",
    )
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "idea")

    import subprocess as sp

    kw = popen.call_args.kwargs
    assert kw.get("start_new_session") is True
    assert kw.get("stdin") == sp.DEVNULL
    assert kw.get("stdout") == sp.DEVNULL
    assert kw.get("stderr") == sp.DEVNULL


def test_open_chrome_popen_fully_detaches_from_parent(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch("jailbee.chrome_pool.allocate", return_value=Path("/x"))
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_chrome(cfg, incus, "feat-smoke", None)

    import subprocess as sp

    kw = popen.call_args.kwargs
    assert kw.get("start_new_session") is True
    assert kw.get("stdin") == sp.DEVNULL
    assert kw.get("stdout") == sp.DEVNULL
    assert kw.get("stderr") == sp.DEVNULL


# --- Extended JetBrains IDE support (pycharm, goland, clion, etc.) ---


@pytest.mark.parametrize(
    "ide_name",
    [
        "pycharm",
        "goland",
        "clion",
        "phpstorm",
        "rider",
        "rubymine",
        "datagrip",
        "rustrover",
        "aqua",
        "dataspell",
        "studio",
    ],
)
def test_open_ide_supports_additional_jetbrains_launchers(mocker, ide_name):
    """The Toolbox layout uses the IDE short name as the launcher binary
    name (e.g. apps/pycharm-professional/bin/pycharm,
    apps/android-studio/bin/studio). Verify the find command targets the
    correct launcher for each supported IDE."""
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    exec_mock = mocker.patch.object(
        incus,
        "exec",
        return_value=f"/opt/jetbrains-toolbox/apps/{ide_name}/bin/{ide_name}\n",
    )
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", ide_name)

    # find_cmd issued to incus.exec must search for the IDE-specific launcher.
    find_argv = exec_mock.call_args.args[1]
    find_cmd = " ".join(find_argv)
    assert f"-name '{ide_name}'" in find_cmd
    # And the launched process logs to a per-IDE log path.
    script = _popen_bash_command(popen)
    assert f"/tmp/jailbee-ide-{ide_name}.log" in script


def test_open_ide_rejects_unknown_app_name(mocker):
    cfg = load_config(FIXTURES / "full_config.yaml")
    incus = Incus()
    mocker.patch.object(incus, "exec")
    popen = mocker.patch("jailbee.gui.subprocess.Popen")

    open_ide(cfg, incus, "feat-smoke", "vim")

    popen.assert_not_called()
