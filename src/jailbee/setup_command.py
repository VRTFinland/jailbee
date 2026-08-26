"""`jailbee setup` — the post-install steps on the user's own machine.

`jailbee init` prepares a *repo*: Incus profiles, the ACL, shared dirs.
This module prepares the *user*, with the three steps `make install` used to
inline and that a `uv tool install jailbee` therefore never performed:

* **completions** — a completion script per shell, per console script
  (`jailbee` *and* `jb`; Click derives the env var from the invoked name, so
  one script cannot serve both),
* **timer** — the singleton `jailbee-net-refresh` user timer that keeps the
  egress pool fresh and expires `jailbee net loose` TTLs,
* **skills** — jailbee's bundled Claude Code skills in the host's
  `~/.claude/skills`.

Every step is idempotent, and every step has a probe that costs one `stat`
and never shells out: `jailbee doctor` reports them, and `consume_hint`
prints a one-shot hint from the commands users run daily.

Host prerequisites — Incus itself, the firewall, UID delegation — are
deliberately *not* here. They need root, they are already diagnosed by
`jailbee doctor`, and they are documented end to end in
`docs/installation.md`, which this command points at when it finishes.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee.tui import info, success, warn

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.db.models import HostSetupState

StepKey = Literal["completions", "timer", "skills"]

STEP_KEYS: tuple[StepKey, ...] = ("completions", "timer", "skills")
"""Iteration and display order, and the order steps are offered in."""

STEP_TITLES: dict[StepKey, str] = {
    "completions": "shell completions",
    "timer": "egress refresh timer",
    "skills": "Claude Code skills (host)",
}

PROG_NAMES: tuple[str, ...] = ("jailbee", "jb")
"""Both console scripts `pyproject.toml` installs."""

SUPPORTED_SHELLS: tuple[str, ...] = ("bash", "zsh", "fish")
"""Shells whose completion scripts Typer can render and we know where to put."""

ZSHRC_LINE = "fpath+=~/.zfunc; autoload -Uz compinit; compinit"
"""What `~/.zfunc/_jailbee` needs to be found. Typer's own wording, kept
verbatim so a user who ran `jailbee --install-completion` before does not
end up with two near-identical lines."""

DOCS_URL = "https://github.com/VRTFinland/jailbee/blob/main/docs/installation.md"


@dataclass(frozen=True)
class StepStatus:
    """One step's key, human title, whether it is in place, and why.

    `detail` is shown to the user in `jailbee setup`, in `jailbee doctor` and
    in the first-run hint, so it names paths rather than summarising them:
    "missing: /home/x/.zfunc/_jb" is actionable, "not installed" is not.
    """

    key: StepKey
    title: str
    installed: bool
    detail: str


# --------------------------------------------------------------------------
# shell detection
# --------------------------------------------------------------------------


def _shellingham_name() -> str | None:
    """The current shell per shellingham, or `None` if it cannot tell.

    Typer's own detection, reached through a private helper because Typer
    exposes no public equivalent (the same tradeoff `tests/test_completion_e2e.py`
    documents). Wrapped broadly: shellingham raises on an unrecognised parent
    process, and a failed guess must degrade to `$SHELL`, not to a traceback.
    """
    try:
        from typer._completion_shared import _get_shell_name

        return _get_shell_name()
    except Exception:
        return None


def detect_shell() -> str | None:
    """The shell to install completions for, or `None` if we cannot tell.

    shellingham inspects the *parent process*, which is right for an
    interactive `jailbee setup` (a bash session under a zsh login shell
    completes in bash) and useless under `make`, where the parent is `sh`.
    `$SHELL` — the login shell, which `make` does not export over — is the
    fallback that keeps `make install` working without forcing `SHELL :=
    /bin/bash` as the Makefile once had to.
    """
    for name in (_shellingham_name(), Path(os.environ.get("SHELL", "")).name):
        if name is not None and name in SUPPORTED_SHELLS:
            return name
    return None


# --------------------------------------------------------------------------
# completions
# --------------------------------------------------------------------------


def _xdg_dir(var: str, *fallback: str) -> Path:
    raw = os.environ.get(var)
    return Path(raw) if raw else Path.home().joinpath(*fallback)


def completion_path(shell: str, prog_name: str) -> Path:
    """Where `shell` looks for `prog_name`'s completion script.

    bash and fish autoload by filename from a well-known directory, so
    installing there needs no rc edit at all. zsh autoloads `_<name>` from
    the fpath, which is why it — alone — needs `ZSHRC_LINE`.

    Note these are *not* the paths `--install-completion` uses: Typer sources
    its bash script from `~/.bashrc` instead, and jailbee prefers the
    directory bash-completion already scans.
    """
    if shell == "bash":
        data = _xdg_dir("XDG_DATA_HOME", ".local", "share")
        return data / "bash-completion" / "completions" / prog_name
    if shell == "zsh":
        return Path.home() / ".zfunc" / f"_{prog_name}"
    if shell == "fish":
        return _xdg_dir("XDG_CONFIG_HOME", ".config") / "fish" / "completions" / f"{prog_name}.fish"
    raise ValueError(f"unsupported shell: {shell}")


def _complete_var(prog_name: str) -> str:
    """Click's own convention — `jb` reads `_JB_COMPLETE`, not jailbee's."""
    return "_{}_COMPLETE".format(prog_name.replace("-", "_").upper())


def _completion_targets(shells: Sequence[str]) -> list[Path]:
    return [completion_path(shell, prog) for shell in shells for prog in PROG_NAMES]


def install_completions(shells: Sequence[str]) -> list[Path]:
    """Write a completion script per shell per console script; return the paths.

    `get_completion_script` is Typer's own renderer, imported lazily and from
    a private module: it is the only way to get the script as *text* (the
    public `--install-completion` insists on writing it, and on editing the
    user's rc). Lazy so a Typer that moved it cannot break the probes above,
    which every `jailbee ls` runs.
    """
    from typer._completion_shared import get_completion_script

    written: list[Path] = []
    for shell in shells:
        for prog in PROG_NAMES:
            path = completion_path(shell, prog)
            path.parent.mkdir(parents=True, exist_ok=True)
            script = get_completion_script(
                prog_name=prog, complete_var=_complete_var(prog), shell=shell
            )
            path.write_text(f"{script}\n")
            written.append(path)
    return written


def completions_status(shells: Sequence[str]) -> StepStatus:
    """Installed only when every requested shell has both scripts."""
    title = STEP_TITLES["completions"]
    if not shells:
        return StepStatus(
            key="completions",
            title=title,
            installed=False,
            detail="shell not detected — pass --shell bash|zsh|fish",
        )
    targets = _completion_targets(shells)
    missing = [p for p in targets if not p.exists()]
    if missing:
        return StepStatus(
            key="completions",
            title=title,
            installed=False,
            detail="missing: " + ", ".join(str(p) for p in missing),
        )
    dirs = list(dict.fromkeys(str(p.parent) for p in targets))
    return StepStatus(
        key="completions",
        title=title,
        installed=True,
        detail=f"{len(targets)} scripts in " + ", ".join(dirs),
    )


def zshrc_path() -> Path:
    return Path.home() / ".zshrc"


def zshrc_line_present() -> bool:
    rc = zshrc_path()
    return rc.is_file() and ZSHRC_LINE in rc.read_text()


def _ensure_zshrc_line(confirm: Callable[[str, bool], bool] | None) -> None:
    """Offer to add `ZSHRC_LINE` to `~/.zshrc`, or print it.

    The one step that edits a file jailbee does not own, so it is asked for
    separately — and never done non-interactively: `--yes` is what `make
    install` and scripts run, and a scripted install must not rewrite a
    login shell's config behind the user's back.
    """
    if zshrc_line_present():
        return
    rc = zshrc_path()
    if confirm is not None and confirm(f"Add the compinit line to {rc}?", True):
        existing = rc.read_text() if rc.is_file() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        rc.write_text(f"{existing}{ZSHRC_LINE}\n")
        success(f"Added the compinit line to {rc}")
        return
    info(f"zsh needs this line in {rc} for completions to load:")
    info(f"    {ZSHRC_LINE}")


# --------------------------------------------------------------------------
# refresh timer
# --------------------------------------------------------------------------


def timer_status() -> StepStatus:
    """Probed by a `stat` on the unit file, never `systemctl is-active`.

    This runs from `jailbee ls`, so it must not fork. Whether the timer is
    *running* — and whether its `ExecStart` still points at this `jailbee` —
    is `jailbee doctor`'s job, which may take its time.
    """
    from jailbee.init_command import NET_REFRESH_TIMER, systemd_user_dir

    path = systemd_user_dir() / NET_REFRESH_TIMER
    if not path.exists():
        return StepStatus(
            key="timer",
            title=STEP_TITLES["timer"],
            installed=False,
            detail=f"missing: {path}",
        )
    return StepStatus(key="timer", title=STEP_TITLES["timer"], installed=True, detail=str(path))


# --------------------------------------------------------------------------
# host Claude skills
# --------------------------------------------------------------------------


def skills_status() -> StepStatus:
    """Installed when every bundled skill has a directory in `~/.claude/skills`.

    An install that ships no skills at all has nothing to owe, so it reports
    installed — the alternative is a hint nobody can ever satisfy.
    """
    from jailbee.claude_skills import bundled_skill_names, host_skills_dir

    title = STEP_TITLES["skills"]
    names = bundled_skill_names()
    dest = host_skills_dir()
    if not names:
        return StepStatus(
            key="skills", title=title, installed=True, detail="no skills bundled in this install"
        )
    missing = [n for n in names if not (dest / n).is_dir()]
    if missing:
        return StepStatus(
            key="skills",
            title=title,
            installed=False,
            detail=f"missing in {dest}: " + ", ".join(missing),
        )
    return StepStatus(key="skills", title=title, installed=True, detail=f"{len(names)} in {dest}")


# --------------------------------------------------------------------------
# status, and running the steps
# --------------------------------------------------------------------------


def status_for(key: StepKey, shells: Sequence[str]) -> StepStatus:
    if key == "completions":
        return completions_status(shells)
    if key == "timer":
        return timer_status()
    return skills_status()


def setup_status(shells: Sequence[str]) -> list[StepStatus]:
    """Every step's status, in `STEP_KEYS` order."""
    return [status_for(key, shells) for key in STEP_KEYS]


def _install(
    key: StepKey, shells: Sequence[str], confirm: Callable[[str, bool], bool] | None
) -> None:
    if key == "completions":
        written = install_completions(shells)
        success(f"Installed {len(written)} completion scripts")
        for path in written:
            info(f"    {path}")
        if "zsh" in shells:
            _ensure_zshrc_line(confirm)
        info("Open a new shell to activate.")
        return
    if key == "timer":
        from jailbee.init_command import install_systemd_units

        install_systemd_units()
        return
    from jailbee.claude_skills import host_skills_dir, install_host_skills

    written = install_host_skills()
    success(f"Installed {len(written)} skills in {host_skills_dir()}")


def run_setup(
    *,
    keys: Sequence[StepKey] = STEP_KEYS,
    shells: Sequence[str],
    confirm: Callable[[str, bool], bool] | None,
) -> list[StepKey]:
    """Run the selected steps, returning the keys that actually ran.

    With `confirm` set, each step is offered: defaulting to *yes* when it is
    missing and to *no* when it is already in place, so a re-run does not
    silently rewrite a working install. With `confirm` as `None` — what
    `--yes` passes — every selected step runs, and nothing is asked.
    """
    ran: list[StepKey] = []
    for key in STEP_KEYS:
        if key not in keys:
            continue
        status = status_for(key, shells)
        if status.installed:
            success(f"{status.title}: installed ({status.detail})")
        else:
            warn(f"{status.title}: {status.detail}")
        if confirm is not None:
            verb = "Refresh" if status.installed else "Install"
            if not confirm(f"{verb} {status.title}?", not status.installed):
                continue
        _install(key, shells, confirm)
        ran.append(key)
    return ran


# --------------------------------------------------------------------------
# the one-shot first-run hint
# --------------------------------------------------------------------------


def _load_state(session: Session) -> HostSetupState:
    """The singleton row, created in memory (not committed) when absent."""
    from jailbee.db.models import HostSetupState

    row = session.get(HostSetupState, 1)
    if row is None:
        row = HostSetupState(id=1)
    return row


def record_setup(session: Session, version: str, *, now: datetime) -> None:
    """Note that `jailbee setup` ran, which also silences the hint for good."""
    row = _load_state(session)
    row.setup_at = now
    row.setup_version = version
    session.add(row)
    session.commit()


def consume_hint(session: Session, *, shells: Sequence[str], now: datetime) -> list[str]:
    """Lines naming the missing setup steps — once, ever. Then `[]`.

    Called from the commands users run daily, so it is deliberately blunt
    about not repeating itself: the shown timestamp is written the first time
    it fires, and `jailbee setup` having run at all silences it too. A user
    who ran setup and declined a step has decided; a user of long standing
    whose install predates this hint sees it at most once. `jailbee doctor`
    is where the state stays visible afterwards.
    """
    row = _load_state(session)
    if row.setup_at is not None or row.hint_shown_at is not None:
        return []
    pending = [s for s in setup_status(shells) if not s.installed]
    if not pending:
        return []
    row.hint_shown_at = now
    session.add(row)
    session.commit()
    lines = ["Post-install steps that have not been done on this machine:"]
    lines.extend(f"    - {s.title}: {s.detail}" for s in pending)
    lines.append("    Run `jb setup` to install them; `jb doctor` reports them later.")
    lines.append(f"    Host setup (Incus, firewall, UID delegation): {DOCS_URL}")
    lines.append("    (shown once)")
    return lines
