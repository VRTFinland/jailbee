"""`jailbee mac setup | upgrade | doctor | uninstall`."""

from __future__ import annotations

import argparse
import shutil
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from jailbee.mac_container import AppleContainer, AppleContainerError
from jailbee.macsetup.engine import (
    MacPaths,
    SetupContext,
    SetupError,
    SetupOptions,
    SetupState,
    Step,
    ask,
    load_state,
    run_steps,
)
from jailbee.macsetup.steps_gui import GuiStep
from jailbee.macsetup.steps_image import ImageStep
from jailbee.macsetup.steps_jailbee import SCRATCH_DIR, BaseImageStep, JailbeeStep, run_as_user
from jailbee.macsetup.steps_kernel import KernelStep
from jailbee.macsetup.steps_machine import IncusStep, MachineStep, PrepareStep
from jailbee.macsetup.steps_preflight import PreflightStep
from jailbee.macsetup.steps_wiring import SSH_ALIAS, WiringStep

_INCLUDE_LINE = "Include ~/.config/jailbee/mac/ssh/config"
_FALLBACK_BINARIES = ("/opt/homebrew/bin/container", "/usr/local/bin/container")


class VerifyStep:
    """Prove the result with a real container, then say what to type next."""

    id = "verify"
    title = "Verifying with a test container"

    def fingerprint(self, ctx: SetupContext) -> str:
        import uuid

        return uuid.uuid4().hex

    def run(self, ctx: SetupContext) -> None:
        smoke = "mac-setup-smoke"
        run_as_user(
            ctx,
            f"mkdir -p {SCRATCH_DIR} && cd {SCRATCH_DIR} && "
            f"jailbee new {smoke} --mount --no-autostart && jailbee destroy {smoke} --force",
        )
        for line in _next_steps(ctx.state.options):
            ctx.log(line)


def _next_steps(options: SetupOptions) -> list[str]:
    lines = ["", "JailBee is ready."]
    if options.mode == "mac-repos":
        lines += ["  cd ~/code/your-repo", "  jailbee new feat/x          # runs in the machine"]
    else:
        lines += [f"  ssh {SSH_ALIAS}               # JailBee and your repos live in the machine"]
    if options.gui:
        lines += ["  jailbee firefox feat-x      # opens a window on this Mac"]
    lines += ["  jailbee mac doctor          # check every layer; run setup again to upgrade"]
    return lines


def default_steps() -> list[Step]:
    return [
        PreflightStep(),
        KernelStep(),
        ImageStep(),
        MachineStep(),
        PrepareStep(),
        IncusStep(),
        JailbeeStep(),
        WiringStep(),
        GuiStep(),
        BaseImageStep(),
        VerifyStep(),
    ]


def find_container() -> str:
    found = shutil.which("container")
    if found:
        return found
    return next((path for path in _FALLBACK_BINARIES if Path(path).exists()), "container")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jailbee mac")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("setup", "upgrade"):
        p = sub.add_parser(name, help="set up, or bring every layer up to date")
        p.add_argument(
            "--isolated", action="store_true", help="do not share the Mac home with the machine"
        )
        p.add_argument("--machine", help="machine name (default: jailbee)")
        p.add_argument("--cpus", type=int, help="machine CPUs (default: half the Mac)")
        p.add_argument("--memory", help="machine memory, e.g. 8G (default: half the Mac)")
        p.add_argument("--no-gui", action="store_true", help="skip native Mac windows")
        p.add_argument("--yes", "-y", action="store_true", help="answer yes to every question")
    sub.add_parser("doctor", help="report which layers are stale")
    un = sub.add_parser("uninstall", help="remove the machine and what setup created")
    un.add_argument("--yes", "-y", action="store_true")
    return parser


def _interactive() -> bool:
    from jailbee.prompting import is_interactive  # the one TTY test (CLAUDE.md)

    return is_interactive() and sys.stdout.isatty()


def _confirm(question: str) -> bool:
    return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}


def _say(line: str) -> None:
    print(line, flush=True)


def _options_from(args: argparse.Namespace, stored: SetupOptions | None) -> SetupOptions:
    base = stored or SetupOptions()
    changes: dict[str, object] = {"assume_yes": bool(args.yes)}
    if args.isolated:
        changes["mode"] = "isolated"
    if args.machine:
        changes["machine"] = args.machine
    if args.cpus is not None:
        changes["cpus"] = args.cpus
    if args.memory:
        changes["memory"] = args.memory
    if args.no_gui:
        changes["gui"] = False
    return replace(base, **changes)  # type: ignore[arg-type]


def main(
    args: Sequence[str],
    *,
    ac: AppleContainer | None = None,
    paths: MacPaths | None = None,
    steps: Sequence[Step] | None = None,
    interactive: bool | None = None,
    user_ssh_config: Path | None = None,
    macos_yaml: Path | None = None,
) -> int:
    parsed = _parser().parse_args(list(args))
    paths = paths or MacPaths.default()
    ac = ac or AppleContainer(binary=find_container())
    interactive = _interactive() if interactive is None else interactive
    the_steps = list(steps) if steps is not None else default_steps()
    stored = load_state(paths.state_file)

    if parsed.command == "uninstall":
        return _uninstall(parsed, ac, paths, stored, interactive, user_ssh_config, macos_yaml)
    if parsed.command == "doctor":
        return _doctor(ac, paths, stored, the_steps, interactive)

    options = _options_from(parsed, stored.options if stored else None)
    if stored is not None and options.mode != stored.options.mode:
        info = ac.machine(options.machine)
        if info is not None:
            _say(
                f"The machine {options.machine} already exists in {stored.options.mode} mode; "
                "the mode cannot change on an existing machine.\n"
                "Run: jailbee mac uninstall, then set up again."
            )
            return 1
    state = SetupState(
        options=options,
        done=dict(stored.done) if stored else {},
        facts=dict(stored.facts) if stored else {},
    )
    ctx = SetupContext(
        ac=ac, state=state, paths=paths, interactive=interactive, confirm=_confirm, log=_say
    )
    return run_steps(ctx, the_steps)


def _doctor(
    ac: AppleContainer,
    paths: MacPaths,
    stored: SetupState | None,
    steps: Sequence[Step],
    interactive: bool,
) -> int:
    if stored is None:
        _say("Not set up yet. Run: jailbee mac setup")
        return 1
    ctx = SetupContext(
        ac=ac, state=stored, paths=paths, interactive=interactive, confirm=_confirm, log=_say
    )
    stale = 0
    for step in steps:
        if step.id in {"preflight", "verify"}:
            continue
        try:
            current = step.fingerprint(ctx)
        except (KeyError, AppleContainerError):
            _say(f"  {step.id}: never run")
            stale += 1
            continue
        if stored.done.get(step.id) == current:
            _say(f"  {step.id}: ok")
        else:
            _say(f"  {step.id}: stale (setup would redo it)")
            stale += 1
    info = ac.machine(stored.options.machine)
    if info is None or not info.running:
        _say(f"  machine {stored.options.machine}: not running")
        stale += 1
    else:
        _say(f"  machine {stored.options.machine}: running")
    if stale:
        _say("Run: jailbee mac setup")
    return 1 if stale else 0


def _uninstall(
    parsed: argparse.Namespace,
    ac: AppleContainer,
    paths: MacPaths,
    stored: SetupState | None,
    interactive: bool,
    user_ssh_config: Path | None,
    macos_yaml: Path | None,
) -> int:
    if stored is None:
        _say("Nothing to remove: setup has not run (no state file).")
        return 0
    options = replace(stored.options, assume_yes=bool(parsed.yes))
    ctx = SetupContext(
        ac=ac,
        state=SetupState(options=options, done=stored.done, facts=stored.facts),
        paths=paths,
        interactive=interactive,
        confirm=_confirm,
        log=_say,
    )
    machine = stored.facts.get("machine")
    try:
        if not ask(
            ctx,
            f"Remove the machine {machine or '(none)'}, its image, the custom kernel and the SSH "
            "wiring? "
            "Everything inside the machine is deleted.",
            "jailbee mac uninstall --yes",
        ):
            _say("Nothing removed.")
            return 0
    except SetupError as exc:
        _say(str(exc))
        if exc.remedy:
            _say(exc.remedy)
        return exc.exit_code
    removed: list[str] = []
    if machine and ac.machine(machine) is not None:
        for action in (ac.machine_stop, ac.machine_rm):
            try:
                action(machine)
            except AppleContainerError as exc:
                _say(f"warning: {exc}")
        removed.append(f"machine {machine}")
    image = stored.facts.get("image")
    if image:
        try:
            ac.image_rm(image)
            removed.append(f"image {image}")
        except AppleContainerError:
            pass
    kernel = stored.facts.get("kernel_path")
    if kernel and Path(kernel).exists():
        Path(kernel).unlink()
        removed.append("custom kernel")
    _remove_include_line(user_ssh_config or Path.home() / ".ssh" / "config")
    _restore_macos_yaml(macos_yaml)
    shutil.rmtree(paths.ssh_dir, ignore_errors=True)
    paths.state_file.unlink(missing_ok=True)
    removed.append("SSH wiring and setup state")
    _say("Removed: " + ", ".join(removed))
    return 0


def _remove_include_line(config: Path) -> None:
    if not config.exists():
        return
    lines = config.read_text().splitlines(keepends=True)
    kept: list[str] = []
    skip_blank = False
    for line in lines:
        if line.strip() == _INCLUDE_LINE:
            skip_blank = True
            continue
        if skip_blank and not line.strip():
            skip_blank = False
            continue
        skip_blank = False
        kept.append(line)
    config.write_text("".join(kept))


def _restore_macos_yaml(path: Path | None) -> None:
    if path is None:
        from jailbee.macos import _config_path

        path = _config_path()
    backup = path.with_name(path.name + ".colima.bak")
    if backup.exists():
        backup.replace(path)
    elif path.exists():
        path.unlink()
