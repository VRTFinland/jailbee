"""Behaviour checks for the /etc/profile.d/jailbee-env.sh snippet baked into
the golden image by provision/install.sh.

The snippet is the login-shell fallback for SSH_AUTH_SOCK. It must not
point the variable at the host gpg-agent socket when that socket isn't
there (`gpg.enabled: false` hosts may run no agent at all), and it must
not overwrite a value the base profile or `container.env` already set.

The heredoc body is extracted from install.sh and executed with a real
bash so the guards are tested, not just their spelling. Nothing here
touches Incus or the network.
"""

from __future__ import annotations

import importlib.resources
import socket
import subprocess
from pathlib import Path

SNIPPET_HEREDOC_START = "cat > /etc/profile.d/jailbee-env.sh <<'EOF'\n"
UNSET_MARKER = "<unset>"


def _install_sh() -> str:
    return importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()


def _jailbee_env_snippet() -> str:
    """Body of the /etc/profile.d/jailbee-env.sh heredoc inside install.sh."""
    script = _install_sh()
    start = script.index(SNIPPET_HEREDOC_START) + len(SNIPPET_HEREDOC_START)
    end = script.index("\nEOF\n", start)
    return script[start:end] + "\n"


def _run_snippet(
    env: dict[str, str],
    var: str = "SSH_AUTH_SOCK",
) -> subprocess.CompletedProcess[str]:
    """Source the snippet in a fresh bash and print the resulting value.

    ``env`` fully replaces the environment, so the host's own values can't
    leak into the assertion.
    """
    probe = f'{_jailbee_env_snippet()}\nprintf "%s" "${{{var}-{UNSET_MARKER}}}"\n'
    return subprocess.run(
        ["bash", "-c", probe],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        check=False,
    )


def _make_agent_socket(runtime_dir: Path) -> Path:
    """Create a real AF_UNIX socket at <runtime>/gnupg/S.gpg-agent.ssh."""
    gnupg = runtime_dir / "gnupg"
    gnupg.mkdir(parents=True)
    path = gnupg / "S.gpg-agent.ssh"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    sock.close()  # the socket file survives; only the listener goes away
    return path


def test_install_sh_masks_ubuntus_automatic_apt_machinery():
    """apt-daily fires minutes after every boot: it takes the dpkg lock out
    from under whoever is installing something, and a run still in flight at
    shutdown blocks systemd — which is how a stop can burn incusd's whole
    600s budget and fail. Masked, not disabled, so an apt upgrade inside the
    container cannot quietly re-enable it.
    """
    script = _install_sh()
    assert "systemctl mask" in script
    for unit in (
        "apt-daily.timer",
        "apt-daily-upgrade.timer",
        "unattended-upgrades.service",
    ):
        assert unit in script, f"{unit} is not masked by install.sh"
    # Before our own apt run, or the lock race it prevents is already lost.
    assert script.index("systemctl mask") < script.index("apt-get update")


def test_install_sh_writes_the_profile_d_snippet():
    assert SNIPPET_HEREDOC_START in _install_sh()
    assert "SSH_AUTH_SOCK" in _jailbee_env_snippet()


def test_snippet_exports_socket_when_gpg_agent_is_mounted(tmp_path):
    """gpg enabled: the device is attached, so the socket is present and
    the login shell must pick it up.
    """
    agent = _make_agent_socket(tmp_path)

    result = _run_snippet({"XDG_RUNTIME_DIR": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    assert result.stdout == str(agent)


def test_snippet_leaves_ssh_auth_sock_unset_when_socket_is_absent(tmp_path):
    """`gpg.enabled: false` means no gpg-socket device, so
    /run/user/<uid>/gnupg/S.gpg-agent.ssh does not exist. Exporting a
    path to a missing socket breaks ssh-add and shadows any container-
    local agent the user starts.
    """
    result = _run_snippet({"XDG_RUNTIME_DIR": str(tmp_path)})

    assert result.returncode == 0, result.stderr
    assert result.stdout == UNSET_MARKER


def test_snippet_does_not_overwrite_an_existing_ssh_auth_sock(tmp_path):
    """The base profile (and `container.env`, which is documented to be
    able to point SSH_AUTH_SOCK at a different agent) sets the variable
    before the login shell runs. The fallback must not clobber it.
    """
    _make_agent_socket(tmp_path)
    preset = "/run/user/1000/keyring/ssh"

    result = _run_snippet({"XDG_RUNTIME_DIR": str(tmp_path), "SSH_AUTH_SOCK": preset})

    assert result.returncode == 0, result.stderr
    assert result.stdout == preset


def test_snippet_survives_missing_xdg_runtime_dir():
    """PAM normally sets XDG_RUNTIME_DIR, but the snippet falls back to
    /run/user/$(id -u) and must not error when neither path exists.
    """
    result = _run_snippet({})

    assert result.returncode == 0, result.stderr


def test_snippet_leaves_no_helper_variables_behind():
    """profile.d runs in the user's shell — internal temporaries must not
    leak into the interactive environment.
    """
    snippet = _jailbee_env_snippet()
    probe = f'{snippet}\ncompgen -v | grep -c "^_jailbee" || true\n'
    result = subprocess.run(
        ["bash", "-c", probe],
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.stdout.strip() == "0", result.stdout


def test_snippet_points_claude_config_dir_at_the_shared_mount(tmp_path):
    """Claude Code reads its global config from
    `(CLAUDE_CONFIG_DIR || $HOME)/.claude.json`. Setting the variable to the
    value the default already resolves to moves only that file — into
    `~/.claude`, which is the shared directory mount."""
    result = _run_snippet({"HOME": str(tmp_path)}, var="CLAUDE_CONFIG_DIR")

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{tmp_path}/.claude"


def test_snippet_does_not_overwrite_an_existing_claude_config_dir(tmp_path):
    """`container.env` may point a container at a different profile; the
    login-shell fallback must not clobber it, same rule as SSH_AUTH_SOCK."""
    result = _run_snippet(
        {"HOME": str(tmp_path), "CLAUDE_CONFIG_DIR": "/custom/profile"},
        var="CLAUDE_CONFIG_DIR",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "/custom/profile"


def test_install_sh_masks_the_user_socket_units_that_clobber_host_sockets():
    """jailbee bind-mounts the host's own /run/user/<uid>/gnupg and
    /run/user/<uid>/pulse into the container, and linger (enabled further down
    this script) starts a `systemd --user` in there. Its gnupg and pulse socket
    units listen on paths *inside those mounts* and unlink whatever file is
    already there — so the host's gpg-agent sees "socket file has been removed"
    and shuts down, and the container's own agent, which has no smartcard
    access, answers in its place. A YubiKey silently disappears from
    `ssh-add -l` on the host, mid-session, because a container rebooted.

    `--global` because the units belong to the dev user's session, not the
    system manager; masked rather than disabled so an apt upgrade of gnupg
    inside the container cannot re-enable them.

    The read-only mount in `runtime_mounts` is the actual guarantee (it also
    covers gpg's own agent autostart, which never goes through systemd); this
    keeps the container from even trying, so no failed units accumulate in the
    user session.
    """
    script = _install_sh()
    assert "systemctl --global mask" in script
    for unit in (
        "gpg-agent.socket",
        "gpg-agent-ssh.socket",
        "gpg-agent-extra.socket",
        "gpg-agent-browser.socket",
        "dirmngr.socket",
        "keyboxd.socket",
        "pulseaudio.socket",
        "pipewire-pulse.socket",
    ):
        assert unit in script, f"{unit} is not masked by install.sh"
