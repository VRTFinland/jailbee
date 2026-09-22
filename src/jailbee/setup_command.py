"""`jailbee setup` — the post-install steps on the user's own machine.

`jailbee init` prepares a *repo*: Incus profiles, the ACL, shared dirs.
This module prepares the *user*, with the three steps `make install` used to
inline and that a `uv tool install jailbee` therefore never performed:

* **completions** — a completion script per shell, per console script
  (`jailbee` *and* `jb`; Click derives the env var from the invoked name, so
  one script cannot serve both),
* **timer** — the singleton `jailbee-net-refresh` user timer that keeps the
  egress pool fresh and expires `jailbee net loose` TTLs,
* **skills** — jailbee's bundled agent skills for the agents found on the
  host (opt-in, `install_host_skills` in `global.yaml`).

Every step is idempotent, and no step's probe ever shells out: `jailbee
doctor` reports them, and `consume_hint` prints a one-shot hint from the
commands users run daily. Completions and the timer cost one `stat`; the
skills probe additionally reads and validates `global.yaml` for the opt-in
(once — `hint_pending` fires at most once per host) and, when opted in,
scans `PATH` for each preset's binary.

Host prerequisites — Incus itself, the firewall, UID delegation — are
deliberately *not* here. They need root, they are already diagnosed by
`jailbee doctor`, and they are documented end to end in
`docs/installation.md`, which this command points at when it finishes.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from jailbee.tui import info, success_plain, warn_plain

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
    "skills": "agent skills (host)",
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

    `actionable` is false for a step that has nothing it *could* do — the
    host skills with the opt-in off, an install bundling no skills, a host
    with none of the agents. Such a step is `installed` in the only sense
    that matters (it owes nothing, so nothing may nag about it), but saying
    "installed" of it reads as a claim that files are on disk, and offering
    to "Refresh" it offers a no-op. Both callers key off this instead.
    """

    key: StepKey
    title: str
    installed: bool
    detail: str
    actionable: bool = True


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
        success_plain(f"Added the compinit line to {rc}")
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
# host agent skills
# --------------------------------------------------------------------------


def _host_skills_opt_in() -> bool:
    """Whether the user asked `jailbee setup` to manage host-side skills.

    `install_host_skills` in `global.yaml`, default off. Read here rather
    than threaded through every caller, so the hint path (`jailbee ls`),
    `jailbee setup` and `jailbee doctor` cannot disagree about it.

    Tolerant on purpose: a schema-invalid `global.yaml` raises `ConfigError`
    from the loader, and `jailbee setup` is the command a user runs to repair
    exactly such a host. Warn and treat it as opted out rather than letting
    the error escape through the status probes as a traceback.
    """
    from jailbee.config.errors import ConfigError
    from jailbee.global_config import default_global_config_path, load_global_config

    path = default_global_config_path()
    try:
        gcfg, _ = load_global_config(path)
    except ConfigError as exc:
        # `warn_plain`: the error body carries pydantic's bracketed detail,
        # which `warn`'s Rich markup parser would silently swallow.
        warn_plain(f"ignoring {path}: {exc} — host agent skills treated as opted out")
        return False
    return gcfg.install_host_skills


def _orphaned_host_skills() -> list[Path]:
    """Directories still holding bundled skills that nothing refreshes now.

    Before the opt-in existed, `jailbee setup` wrote the skills into the
    host's `~/.claude/skills` unconditionally. Those files survive the
    upgrade and then quietly rot as the bundled set evolves, and the step
    reports `installed` — so the one population that needs to know would
    never be told. Named in the opted-out detail instead.
    """
    from jailbee.agent_skills import bundled_skill_names, host_skill_targets

    names = bundled_skill_names()
    if not names:
        return []
    return [t for t in host_skill_targets() if any((t / n).is_dir() for n in names)]


def skills_status(*, opt_in: bool | None = None) -> StepStatus:
    """Installed when every detected agent has every bundled skill.

    Opt-in governs: with `install_host_skills` off (the default) the step
    reports installed — the containers' skills need no host action, so their
    absence is a preference, not a fault, and neither the first-run hint nor
    `jailbee doctor` may nag about it. An install that ships no skills at
    all, or a host with none of the agents, has nothing to owe either. None
    of the three is `actionable`: there is nothing to install.

    `opt_in` is the caller's already-loaded `install_host_skills`, so a
    command holding a `GlobalConfig` (`jailbee doctor`) answers from it
    rather than from a second read of the same file. `None` — `jailbee
    setup`, which deliberately loads no config — reads it here.
    """
    from jailbee.global_config import default_global_config_path

    title = STEP_TITLES["skills"]
    if opt_in is None:
        opt_in = _host_skills_opt_in()
    if not opt_in:
        detail = f"opt-in: off — set install_host_skills: true in {default_global_config_path()}"
        orphaned = _orphaned_host_skills()
        if orphaned:
            detail = (
                "opt-in: off — jailbee skills already in "
                + ", ".join(str(t) for t in orphaned)
                + " are no longer refreshed; set install_host_skills: true in "
                + str(default_global_config_path())
            )
        return StepStatus(
            key="skills", title=title, installed=True, detail=detail, actionable=False
        )
    from jailbee.agent_skills import bundled_skill_names, host_skill_targets

    names = bundled_skill_names()
    if not names:
        return StepStatus(
            key="skills",
            title=title,
            installed=True,
            detail="no skills bundled in this install",
            actionable=False,
        )
    targets = host_skill_targets()
    if not targets:
        return StepStatus(
            key="skills",
            title=title,
            installed=True,
            detail="no skill-capable agents found on this host",
            actionable=False,
        )
    missing = [
        f"{name} in {target}"
        for target in targets
        for name in names
        if not (target / name).is_dir()
    ]
    if missing:
        return StepStatus(
            key="skills",
            title=title,
            installed=False,
            detail="missing: " + ", ".join(missing),
        )
    return StepStatus(
        key="skills",
        title=title,
        installed=True,
        detail=f"{len(names)} skills in each of " + ", ".join(str(t) for t in targets),
    )


# --------------------------------------------------------------------------
# the optional Qt extra
# --------------------------------------------------------------------------

QT_EXTRA_TITLE = "Qt dashboard (optional)"

QT_EXTRA_COMMAND = "uv tool install 'jailbee[gui]'"


def qt_dashboard_status() -> tuple[bool, str]:
    """Whether `jailbee gui` can run, and what to say about it.

    Deliberately *not* a `StepKey`, because unlike the three steps this one
    cannot be installed from here: the extra lives in jailbee's own
    environment, so installing it means reinstalling the tool that is
    currently running — through whichever of uv, pipx or a hand-made
    virtualenv put it there, which jailbee has no way to know. So
    `jailbee setup --status` and `jailbee doctor` report it and the user runs
    the command, which is also why a missing extra never fails either one.

    `find_spec` rather than an import: this runs from `jailbee doctor`, and
    importing PySide6 to find out whether it exists costs a second and a
    display connection. It raises rather than returns on a package whose
    parent cannot be imported, hence the guard.
    """
    try:
        if find_spec("PySide6") is not None:
            return True, "PySide6 available — `jb gui` works"
    except (ImportError, ValueError):
        pass
    return False, f"not installed — `{QT_EXTRA_COMMAND}` (or pipx) adds `jb gui`"


# --------------------------------------------------------------------------
# status, and running the steps
# --------------------------------------------------------------------------


def status_for(key: StepKey, shells: Sequence[str], *, opt_in: bool | None = None) -> StepStatus:
    if key == "completions":
        return completions_status(shells)
    if key == "timer":
        return timer_status()
    return skills_status(opt_in=opt_in)


def setup_status(shells: Sequence[str], *, opt_in: bool | None = None) -> list[StepStatus]:
    """Every step's status, in `STEP_KEYS` order."""
    return [status_for(key, shells, opt_in=opt_in) for key in STEP_KEYS]


def pending_steps(shells: Sequence[str], *, opt_in: bool | None = None) -> list[StepStatus]:
    """The steps not yet in place, in `STEP_KEYS` order."""
    return [status for status in setup_status(shells, opt_in=opt_in) if not status.installed]


def report_step(status: StepStatus) -> None:
    """Print one step's state, in the wording every caller shares.

    A single renderer on purpose: the interactive run, the read-only listing
    and the hint describe the same three probes, and two copies of the
    phrasing drift the moment one of them gains a detail the other lacks.
    """
    if not status.actionable:
        # "installed (opt-in: off)" reads as a contradiction: the detail
        # already says why there is nothing on disk and nothing owed.
        success_plain(f"{status.title}: {status.detail}")
    elif status.installed:
        success_plain(f"{status.title}: installed ({status.detail})")
    else:
        warn_plain(f"{status.title}: {status.detail}")


def report_status(keys: Sequence[StepKey], shells: Sequence[str]) -> None:
    """Print the state of `keys`, installing nothing and asking nothing.

    What `jailbee setup --status` prints, and the only view that shows all
    three steps at once: `jailbee doctor` reports completions and skills but
    deliberately not the timer, because its egress check says more about that
    one than a file check could.

    The optional Qt extra rides along on a full listing only. It is not one
    of the steps and not something this command can install, so on a
    `--only`-filtered listing — which asked about something else — it is
    noise, and `info` rather than `warn_plain` keeps a missing one from
    reading like a fault.
    """
    opt_in = _host_skills_opt_in() if "skills" in keys else None
    for key in STEP_KEYS:
        if key in keys:
            report_step(status_for(key, shells, opt_in=opt_in))
    if set(keys) == set(STEP_KEYS):
        installed, detail = qt_dashboard_status()
        if installed:
            success_plain(f"{QT_EXTRA_TITLE}: {detail}")
        else:
            info(f"{QT_EXTRA_TITLE}: {detail}")


def _install(
    key: StepKey, shells: Sequence[str], confirm: Callable[[str, bool], bool] | None
) -> None:
    if key == "completions":
        written = install_completions(shells)
        success_plain(f"Installed {len(written)} completion scripts")
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
    from jailbee.agent_skills import host_skill_targets, install_host_skills

    # `run_setup` skips a step that is not `actionable`, which covers being
    # opted out and having no agent on the host — this is the install path
    # proper, reached only when there is something to write.
    targets = host_skill_targets()
    written = install_host_skills(targets)
    success_plain(f"Installed {len(written)} skill directories across {len(targets)} agents")
    for target in targets:
        info(f"    {target}")


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
    opt_in = _host_skills_opt_in() if "skills" in keys else None
    for key in STEP_KEYS:
        if key not in keys:
            continue
        status = status_for(key, shells, opt_in=opt_in)
        if not status.actionable:
            # Nothing to install and nothing to refresh: say so and move on
            # rather than offering a question whose yes is a no-op.
            report_step(status)
            continue
        if key == "completions" and not shells:
            # Nothing can be written, so nothing may be claimed: skip the
            # step outright rather than "installing" an empty set. Always
            # said out loud — it is the reason nothing happened.
            report_step(status)
            continue
        if status.installed:
            report_step(status)
        elif confirm is not None:
            # What is missing is context for the question that follows. With
            # nothing to answer, the installer's own output says it better.
            report_step(status)
        if confirm is not None:
            verb = "Refresh" if status.installed else "Install"
            if not confirm(f"{verb} {status.title}?", not status.installed):
                continue
        _install(key, shells, confirm)
        ran.append(key)
    return ran


def linger_tip() -> None:
    """Print the `enable-linger` tip unless linger is already on.

    A user timer only fires while that user has a login session, or while
    linger is enabled for them. Enabling it needs root, so jailbee informs
    rather than acts — and stays quiet on a host with no `loginctl` at all,
    since this is advice, not a step.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["loginctl", "show-user", os.getenv("USER", ""), "-p", "Linger"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return
    if "Linger=yes" in proc.stdout:
        return
    info("Tip: `sudo loginctl enable-linger $USER` keeps the timer")
    info("     running when no user session is open.")


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


def hint_pending(session: Session, *, shells: Sequence[str], now: datetime) -> list[StepStatus]:
    """The missing setup steps — once, ever. Then `[]`.

    The gate both the printed hint and the interactive offer sit behind, and
    the reason neither repeats: the shown timestamp is written the first time
    *either* fires, and `jailbee setup` having run at all silences both. A
    user who ran setup and declined a step has decided; a user of long
    standing whose install predates this sees it at most once. `jailbee
    doctor` is where the state stays visible afterwards.
    """
    row = _load_state(session)
    if row.setup_at is not None or row.hint_shown_at is not None:
        return []
    pending = pending_steps(shells)
    if not pending:
        return []
    row.hint_shown_at = now
    session.add(row)
    session.commit()
    return pending


def _pending_lines(pending: Sequence[StepStatus]) -> list[str]:
    """The header and one line per missing step — what both blocks open with."""
    return [
        "Post-install steps that have not been done on this machine:",
        *(f"    - {s.title}: {s.detail}" for s in pending),
    ]


def _docs_line() -> str:
    return f"    Host setup (Incus, firewall, UID delegation): {DOCS_URL}"


def consume_hint(session: Session, *, shells: Sequence[str], now: datetime) -> list[str]:
    """Lines naming the missing setup steps — once, ever. Then `[]`.

    The non-interactive half of the pair: what the commands print when they
    cannot stop to ask, either because nothing is watching or because they
    are mid-workflow (`jailbee new` may be heading for a detached worker,
    `jailbee shell` is about to hand the terminal to a container). It names
    the steps, points at `jb setup`, and gets out of the way. `offer_lines`
    is the interactive counterpart.
    """
    pending = hint_pending(session, shells=shells, now=now)
    if not pending:
        return []
    return [
        *_pending_lines(pending),
        "    Run `jb setup` to install them; `jb doctor` reports them later.",
        _docs_line(),
        "    (shown once)",
    ]


def offer_lines(pending: Sequence[StepStatus]) -> list[str]:
    """The same block without a call to action — the question that follows is it.

    No "(shown once)" either. That line exists to tell a user who cannot act
    on the notice that it will not return; a user who is about to be asked
    can act on it right now.
    """
    return [*_pending_lines(pending), _docs_line()]
