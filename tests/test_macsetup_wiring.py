"""Tests for the Mac wiring step (ssh config, key, bridge config). Fully mocked."""

from __future__ import annotations

import subprocess as _sp
from pathlib import Path

import yaml

from jailbee.macsetup.engine import MacPaths, SetupContext, SetupOptions, SetupState
from jailbee.macsetup.steps_wiring import WiringStep, ensure_include_line


def _cp(rc: int = 0, out: str = "", err: str = "") -> _sp.CompletedProcess[str]:
    return _sp.CompletedProcess([], rc, stdout=out, stderr=err)


class FakeAC:
    def __init__(self) -> None:
        self.runs: list[dict[str, object]] = []

    def machine_run(self, name, argv, *, root=False, input=None):  # type: ignore[no-untyped-def]
        self.runs.append({"argv": argv, "input": input})
        return _cp()


def keygen_runner(argv: list[str], **kwargs: object) -> _sp.CompletedProcess[str]:
    """Plays ssh-keygen: writes the key pair at the -f path."""
    path = Path(argv[argv.index("-f") + 1])
    path.write_text("PRIVATE\n")
    Path(str(path) + ".pub").write_text("ssh-ed25519 AAAAFAKE jailbee-mac-setup\n")
    return _cp()


def make(
    tmp_path: Path, *, mode: str = "mac-repos", gui: bool = False
) -> tuple[WiringStep, SetupContext, FakeAC]:
    ac = FakeAC()
    paths = MacPaths(
        tmp_path / "cfg", tmp_path / "cfg" / "s.json", tmp_path / "k", tmp_path / "cfg" / "ssh"
    )
    ctx = SetupContext(
        ac=ac,  # type: ignore[arg-type]
        state=SetupState(options=SetupOptions(mode=mode, gui=gui, machine="jb")),  # type: ignore[arg-type]
        paths=paths,
        interactive=True,
        confirm=lambda q: True,
        log=lambda line: None,
    )
    step = WiringStep(
        user="me",
        user_ssh_config=tmp_path / "dotssh" / "config",
        macos_yaml=tmp_path / "cfg" / "macos.yaml",
        container_binary="/opt/homebrew/bin/container",
        runner=keygen_runner,
    )
    return step, ctx, ac


def test_include_line_added_once(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.write_text("Host other\n    User x\n")
    ensure_include_line(config, "~/.config/jailbee/mac/ssh/config")
    ensure_include_line(config, "~/.config/jailbee/mac/ssh/config")
    text = config.read_text()
    assert text.count("Include ~/.config/jailbee/mac/ssh/config") == 1
    assert text.startswith("Include ~/.config/jailbee/mac/ssh/config\n")
    assert "Host other" in text


def test_include_line_created_with_private_mode_when_file_missing(tmp_path: Path) -> None:
    config = tmp_path / "dotssh" / "config"
    ensure_include_line(config, "~/x")
    assert config.read_text() == "Include ~/x\n"
    assert oct(config.stat().st_mode & 0o777) == "0o600"


def test_authorized_key_append_is_idempotent_command(tmp_path: Path) -> None:
    step, ctx, ac = make(tmp_path)
    step.run(ctx)
    run = next(r for r in ac.runs if r["input"] and "AAAAFAKE" in str(r["input"]))
    assert "grep -qxF" in " ".join(run["argv"])  # type: ignore[arg-type]
    assert "chmod 600" in " ".join(run["argv"])  # type: ignore[arg-type]


def test_ssh_config_uses_machine_run_proxy_and_quotes_paths(tmp_path: Path) -> None:
    step, ctx, _ = make(tmp_path)
    step.run(ctx)
    text = (ctx.paths.ssh_dir / "config").read_text()
    assert "Host jailbee-vm" in text
    assert "User me" in text
    assert (
        "ProxyCommand /opt/homebrew/bin/container machine run -n jb -i -- nc 127.0.0.1 22" in text
    )
    assert f"IdentityFile {ctx.paths.ssh_dir / 'id_ed25519'}" in text


def test_key_is_not_regenerated(tmp_path: Path) -> None:
    step, ctx, _ = make(tmp_path)
    step.run(ctx)
    key = ctx.paths.ssh_dir / "id_ed25519"
    key.write_text("MY-OWN-KEY\n")
    step.run(ctx)
    assert key.read_text() == "MY-OWN-KEY\n"


def test_macos_yaml_written_for_apple_backend_and_colima_config_backed_up(tmp_path: Path) -> None:
    step, ctx, _ = make(tmp_path, mode="isolated", gui=True)
    yaml_path = tmp_path / "cfg" / "macos.yaml"
    yaml_path.parent.mkdir(parents=True, exist_ok=True)
    yaml_path.write_text("transport: [colima, ssh]\n")
    step.run(ctx)
    written = yaml.safe_load(yaml_path.read_text())
    assert written == {
        "backend": "apple",
        "machine": "jb",
        "user": "me",
        "mode": "isolated",
        "gui": True,
    }
    assert (yaml_path.parent / "macos.yaml.colima.bak").read_text() == "transport: [colima, ssh]\n"


def test_fingerprint_follows_mode_gui_and_machine(tmp_path: Path) -> None:
    step, ctx, _ = make(tmp_path)
    one = step.fingerprint(ctx)
    ctx.state.options.gui = True
    assert step.fingerprint(ctx) != one


def test_proxy_command_quotes_a_binary_path_with_spaces(tmp_path: Path) -> None:
    step, ctx, _ = make(tmp_path)
    step._container = "/Applications/My Tools/container"
    step.run(ctx)
    text = (ctx.paths.ssh_dir / "config").read_text()
    assert "ProxyCommand '/Applications/My Tools/container' machine run -n jb -i" in text


def test_gui_host_block_jumps_through_the_vm_only_when_gui_is_on(tmp_path: Path) -> None:
    step, ctx, _ = make(tmp_path, gui=False)
    step.run(ctx)
    assert "Host jailbee-ssh" not in (ctx.paths.ssh_dir / "config").read_text()
    step, ctx, _ = make(tmp_path / "g", gui=True)
    step.run(ctx)
    text = (ctx.paths.ssh_dir / "config").read_text()
    block = text.split("Host jailbee-ssh", 1)[1]
    assert "HostName 127.0.0.1" in block and "Port 8022" in block
    assert "User jailbee" in block
    assert "ProxyJump jailbee-vm" in block
    assert "HostKeyAlias jailbee-machine-jb" in block
    assert "StrictHostKeyChecking accept-new" in block
