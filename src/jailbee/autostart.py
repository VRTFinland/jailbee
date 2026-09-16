"""Autostart orchestration: run a trigger's stages inside a container.

A *stage* owns the container-scoped side effects — the network profile and
the optional mounts — for as long as its chains run. Chains inside a stage
run in parallel; steps inside a chain run in order. A legacy flat step list
is normalized into single-chain stages by ``autostart_plan.normalize_stages``,
so the old config shape keeps its exact ordering and its per-step ``mounts``.

``background: True`` runs a step in a detached tmux window and returns
immediately. A sync step blocks until it exits — a single-chain stage waits
on ``tmux wait-for`` (``tmux.run_step``), a multi-chain stage polls its
sentinels (``tmux.poll_steps``), because one wait-for channel cannot serve
several steps at once.
"""

from __future__ import annotations

import os
import shlex
import signal
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal

from jailbee import tmux
from jailbee.autostart_plan import AGENTS_STAGE as AGENTS_STAGE
from jailbee.autostart_plan import AutostartPlan as AutostartPlan
from jailbee.autostart_plan import AutostartTrigger as AutostartTrigger
from jailbee.autostart_plan import plan_autostart as plan_autostart
from jailbee.config import AutostartChain, AutostartStage, AutostartStep, Config
from jailbee.incus import Incus
from jailbee.mounts import add_optional_mount, remove_optional_mount
from jailbee.tmux import TmuxStepError
from jailbee.tui import info, success, warn

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from types import FrameType

    from jailbee.background import AutostartJob

# How long the multi-chain driver waits between sentinel sweeps. One
# `incus exec` covers every in-flight step, so this is the whole polling
# cost of a stage, however many chains it runs.
_POLL_INTERVAL_SEC = 0.5

# rc → short human-readable cause. Anything else falls back to "exit code N".
_EXIT_HINTS: dict[int, str] = {
    126: "command not executable",
    127: "command not found",
    130: "interrupted (SIGINT)",
    137: "killed (SIGKILL / out of memory?)",
    143: "terminated (SIGTERM)",
}


class AutostartStepError(RuntimeError):
    """An autostart step failed. Renders to a user-facing message that
    names the container and step and points at ``jailbee tmux`` for inspection,
    so CLI callers don't need to render a Python traceback for what is
    almost always a config / script issue inside the container.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` /
    ``pytest.raises(RuntimeError)`` paths keep working.
    """

    def __init__(
        self,
        *,
        container: str,
        step_name: str,
        reason: str,
        exit_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        self.container = container
        self.step_name = step_name
        self.reason = reason
        self.exit_code = exit_code
        self.original = original
        super().__init__(self._render())

    def _render(self) -> str:
        if self.reason == "exit":
            hint = _EXIT_HINTS.get(self.exit_code or -1)
            cause = f"{hint} (exit {self.exit_code})" if hint else f"exit code {self.exit_code}"
        elif self.reason == "timeout":
            cause = "timed out"
        elif self.reason == "died_early":
            cause = "exited immediately after launch"
        elif self.reason == "crashed":
            cause = "tmux session lost — exit code unknown"
        else:
            cause = self.reason
        return (
            f"Autostart step '{self.step_name}' failed in '{self.container}': {cause}.\n"
            f"  Inspect the failed window:  jailbee tmux {self.container}"
        )


class AutostartCancelled(Exception):
    """A detached autostart run was asked to stop — `jailbee autostart cancel`.

    Deliberately an ordinary `Exception` rather than `SystemExit`: on its way
    out it must be caught by the worker's `except Exception`, which marks the
    job row failed carrying this message, so a cancelled run never looks
    in-flight afterwards. It still passes through
    `_run_chains_in_parallel`'s `except BaseException` first, which interrupts
    the steps in flight before the stage unwinds.
    """

    def __init__(self, *, container: str, signum: int) -> None:
        self.container = container
        self.signum = signum
        super().__init__(f"Autostart run for '{container}' cancelled (signal {signum}).")


@contextmanager
def _cancel_on_sigterm(container: str) -> Iterator[None]:
    """Turn SIGTERM into :class:`AutostartCancelled` for the duration of a run.

    `jailbee autostart cancel` signals the supervisor, and SIGTERM's default
    action terminates the process without unwinding: `run_stage`'s ``finally``
    (unmount, compare-and-swap network restore) and `run_detached`'s (clearing
    ``user.jailbee.autostart_in_progress``) would both be skipped, leaving
    optional mounts attached and the container on the stage's network mode.
    Raising instead makes cancellation take the same route a failing stage
    already takes.

    Installed here rather than in `cli._autostart_worker` so it covers both
    processes that ever run detached stages — the spawned supervisor and the
    background worker finishing its own continuation in-process.

    **One-shot.** The handler puts the previous handling back before it
    raises, so the unwind it starts cannot itself be cancelled: that unwind —
    interrupting the step, unmounting, restoring the network, clearing the
    flag — runs inside ``finally`` blocks, where a second raise would skip
    exactly the cleanup the first signal exists to perform. A caller who
    really wants the process gone signals again and gets SIGTERM's default.

    The previous handler is also restored on the way out, because on the
    in-process path the worker has more to do afterwards (it marks its row and
    exits) and must not keep a handler that raises into unrelated code. Both
    the install and the ``yield`` sit inside that ``finally``'s ``try``, so a
    signal arriving in the instant between them cannot escape leaving a
    raising handler behind.

    A handler can only be installed from the main thread. Nothing calls this
    off one today; should something start to, `signal.signal` raises
    ``ValueError`` and the run continues with SIGTERM's default handling —
    losing clean cancellation is not a reason to fail the run itself.
    """
    # Read rather than taken from `signal.signal`'s return value: the handler
    # closes over it, and it has to be bound before the handler can possibly
    # run — which is the moment the install returns.
    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum: int, frame: FrameType | None) -> None:
        signal.signal(signal.SIGTERM, previous)
        raise AutostartCancelled(container=container, signum=signum)

    installed = False
    try:
        try:
            signal.signal(signal.SIGTERM, handler)
            installed = True
        except ValueError:
            pass  # not the main thread — SIGTERM keeps its default handling
        yield
    finally:
        if installed:
            signal.signal(signal.SIGTERM, previous)


def has_graphical_session() -> bool:
    """Return True if the host has a Wayland or X display set."""
    return bool(os.environ.get("WAYLAND_DISPLAY")) or bool(
        os.environ.get("DISPLAY"),
    )


def _github_token_step(cfg: Config) -> AutostartStep | None:
    """Return an AutostartStep that writes /etc/profile.d/jailbee-github.sh
    with `export GH_TOKEN=<this repo's PAT>`, or None when no token applies.

    Returns None when:
      - github.enabled is false
      - cfg.container_prefix is not a key in github.api_tokens
      - the resolved token is empty after strip (validate_runtime would have
        flagged this; defensive guard)

    The step needs no network of its own (writing a file inside the
    container does not), so it leaves the container's current profile
    alone. It is executed by ``inject_github_token`` *before* the user's
    autostart steps (each runs in a fresh ``bash -lc`` login shell that
    sources /etc/profile.d), so on_start steps invoking `gh` see GH_TOKEN.
    """
    if not cfg.github.enabled:
        return None
    secret = cfg.github.api_tokens.get(cfg.container_prefix)
    if secret is None:
        return None
    token = secret.get_secret_value().strip()
    if not token:
        return None
    # Autostart steps run as the dev user. /etc/profile.d/ requires root,
    # so we pipe through `sudo tee` — the dev user has passwordless sudo
    # baked into the golden image (provision/install.sh:74-77).
    return AutostartStep(
        name="github-token",
        run=(
            f"printf 'export GH_TOKEN=%s\\n' {shlex.quote(token)} "
            "| sudo tee /etc/profile.d/jailbee-github.sh > /dev/null "
            "&& sudo chmod 0644 /etc/profile.d/jailbee-github.sh"
        ),
        network=None,
    )


def inject_github_token(
    cfg: Config,
    incus: Incus,
    container: str,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Write /etc/profile.d/jailbee-github.sh into ``container`` when the github
    integration applies; no-op otherwise.

    GH_TOKEN is auto-enabled *infrastructure*, not one of the user's
    configured autostart commands — like the Claude install, it must land
    regardless of ``--no-autostart`` so `gh` works in every container. It is
    therefore injected outside ``run_autostart`` and BEFORE the user's
    on_start steps: each step runs in a fresh ``bash -lc`` login shell that
    sources /etc/profile.d, so writing the file first makes GH_TOKEN visible
    to any step that calls `gh`.

    Re-run on every boot path (`jailbee new`, `jailbee start`/`restart`, `jailbee apply`)
    so a rotated PAT in config is picked up. No-op when github.enabled is off
    or no token applies to this container's prefix.

    ``mirror_endpoint`` is accepted for call-site symmetry with the other
    boot-path helpers but is not forwarded: the token step declares no
    ``network``, so there is no profile round-trip whose /etc/hosts pin
    would need re-writing. Switching the profile is a *stage's* job now
    (see ``run_stage``), and this step is not part of a stage.
    """
    step = _github_token_step(cfg)
    if step is None:
        return
    tmux.ensure_session(incus, container, start_dir=repo_dir)
    info(f"Injecting GH_TOKEN into {container}")
    _apply_step(cfg, incus, container, step, repo_dir)


def agent_autostart_steps(cfg: Config) -> list[AutostartStep]:
    """One backgrounded tmux window per agent with `autostart`.

    Appended (not prepended) to on_start so user steps finish first, and
    ordered with `claude` last (see `agents.enabled_agent_specs`) because
    `_attach_tmux` selects the *last* one of these by name
    (`tmux.select_window`) when it lands you in the session — not because
    tmux itself would otherwise focus the most-recently-created window.

    Each step's network is left unset (`None`): the agent's egress hosts
    are already folded into `effective_egress_allow()` when the agent is
    enabled (see `agents.enabled_agent_specs`), so the container's current
    network mode is already the right one.

    `continue_on_error` is True: an agent is an optional integration, so a
    launch failure (binary never installed) degrades to a warning instead of
    failing the whole `jailbee new`.

    An agent whose `command` is empty after stripping is skipped rather than
    emitted as a step: `run=f"exec {spec.command}"` would otherwise produce
    a bare `exec ` that fails opaquely in the tmux window.
    `validate_runtime` already reports `enabled=true` with an empty command
    as a config issue; this is defense-in-depth, not the primary check.

    The agent's `env` is copied onto the step, so `agents.<name>.env` reaches
    the launched binary via tmux's `-e` flags. `_apply_step` layers it over
    `autostart.env`, so a per-agent key wins over the global one. It is the
    same mapping the install/update step gets (see `agents._ensure_one`), which
    is why Claude's `JAILBEE_CLAUDE_AUTO_UPDATE` flag needs no separate wiring.
    """
    from jailbee.agents import enabled_agent_specs

    return [
        AutostartStep(
            name=spec.name,
            run=f"exec {spec.command}",
            background=True,
            continue_on_error=True,
            env=dict(spec.env),
        )
        for spec in enabled_agent_specs(cfg)
        if spec.autostart and spec.command.strip()
    ]


def run_autostart(
    cfg: Config,
    incus: Incus,
    container: str,
    trigger: AutostartTrigger,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
    override: Literal["wait", "no_wait"] | None = None,
    already_detached: bool = False,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> AutostartPlan:
    """Run this trigger's blocking stages and return the plan.

    The returned plan's ``detached`` list is what the caller hands to the
    supervisor; it is empty unless a stage is marked ``detach: true`` (or
    ``--no-wait`` was passed), so every existing caller keeps today's
    fully-blocking behaviour and can ignore the return value.

    ``mirror_endpoint=(ip, port)`` is forwarded to every transient
    ``switch_network`` call a stage triggers, so the strict-mode
    ``jailbee-registry-mirror.incus`` row in /etc/hosts survives an
    autostart-driven ``strict → loose → strict`` round-trip.
    """
    # The synthetic per-agent steps (empty when no agent has autostart on)
    # are the planner's to place — claude sorts last (`enabled_agent_specs`)
    # because `_attach_tmux` selects the last one of these by name when it
    # lands you in the session. The github-token step is NOT injected here:
    # it's infrastructure, not a
    # user autostart command, so it's written by ``inject_github_token``
    # independently of --no-autostart.
    agent_steps = agent_autostart_steps(cfg) if trigger == AutostartTrigger.ON_START else []
    plan = plan_autostart(
        cfg.autostart,
        trigger,
        agent_steps=agent_steps,
        override=override,
        already_detached=already_detached,
    )
    if not plan.blocking:
        return plan

    total = sum(len(s.all_chains()) for s in plan.blocking)
    info(f"Running {len(plan.blocking)} autostart stage(s), {total} chain(s) in {container}")

    # The loose-revert timer (see loose_revert.py) skips containers
    # carrying this flag, so a stage that swaps the network profile
    # mid-autostart doesn't race the auto-revert path. Cleared in
    # ``finally`` so a stage failure still releases the lock — but only
    # when nothing is left to run: with detached stages pending, the
    # supervisor owns the flag from here and re-stamps it with its own pid.
    #
    # "Pending" means *actually handed off*, which is why the hand-off is
    # recorded only once ``run_stages`` has returned: a blocking stage that
    # raises never reaches the hand-off — the exception propagates past every
    # caller's ``on_detach`` (`lifecycle.new_container`,
    # `cli._post_start_actions`), so no supervisor is ever spawned. Keeping
    # the flag then leaves the literal ``"1"`` behind with no owning process,
    # and `loose_revert._autostart_holds` honours that forever: the container
    # is exempt from TTL auto-revert for the rest of its life. That is the
    # hole in the unsafe direction the pid stamping exists to prevent.
    handed_off = False
    incus.config_set(container, "user.jailbee.autostart_in_progress", "1")
    try:
        run_stages(
            cfg,
            incus,
            container,
            plan.blocking,
            repo_dir,
            mirror_endpoint=mirror_endpoint,
            on_progress=on_progress,
        )
        handed_off = bool(plan.detached)
    finally:
        if not handed_off:
            incus.config_unset(container, "user.jailbee.autostart_in_progress")

    success("Autostart complete" if not plan.detached else "Autostart: handing off to background")
    return plan


def run_detached(
    cfg: Config,
    incus: Incus,
    spec: AutostartJob,
    *,
    on_phase: Callable[[str], None] | None = None,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> None:
    """Run every stage the foreground deferred, in this (detached) process.

    The supervisor's whole algorithm, kept out of `cli._autostart_worker` so
    it is reachable without a `CliRunner`. ``cfg`` must already carry the
    *effective* autostart block from ``spec`` — the caller grafts it, because
    on the create path that block is the target branch's, not the host
    checkout's.

    ``on_phase(stage_name)`` is called before each stage, so the caller can
    record it on the job row; ``on_progress`` is forwarded to the executor.
    Neither is required.

    Owns the in-progress flag for the whole run: it re-stamps
    ``user.jailbee.autostart_in_progress`` with *this* process's pid (the
    foreground left the literal ``"1"``, which `loose_revert` reads as "held
    forever") and clears it in ``finally``. A crash that skips the ``finally``
    is still safe — the pid in the key stops existing.

    Cancellable for its whole length: SIGTERM — what `jailbee autostart
    cancel` sends — raises :class:`AutostartCancelled` instead of killing the
    process outright, so the stage's mounts come off, its network mode is put
    back, the flag is cleared and the caller can mark the run cancelled. See
    :func:`_cancel_on_sigterm`.
    """
    from jailbee.autostart_plan import TRIGGER_ORDER

    with _cancel_on_sigterm(spec.container_name):
        incus.config_set(
            spec.container_name, "user.jailbee.autostart_in_progress", str(os.getpid())
        )
        try:
            # Inside the `try`, deliberately: a job file carrying a `from_trigger`
            # this build does not know raises here, and that run must still reach
            # the `finally` rather than exit with the flag left stamped.
            #
            # The foreground ran this trigger's blocking stages and stopped at the
            # boundary; everything from here, in this trigger and every later one,
            # is ours. `already_detached=True` is what makes the planner return the
            # whole remaining list rather than re-splitting it.
            start_at = TRIGGER_ORDER.index(AutostartTrigger(spec.from_trigger))
            for i, trigger in enumerate(TRIGGER_ORDER[start_at:]):
                agent_steps = (
                    agent_autostart_steps(cfg) if trigger == AutostartTrigger.ON_START else []
                )
                plan = plan_autostart(
                    cfg.autostart,
                    trigger,
                    agent_steps=agent_steps,
                    # The override the foreground split on: `_boundary` branches
                    # on it, so recomputing without it can disagree with what was
                    # actually deferred. Inert for a later trigger, where
                    # `already_detached` short-circuits the boundary to 0.
                    override=spec.override,
                    # The trigger we resumed into keeps its own split (its blocking
                    # half already ran); every later trigger is detached in full.
                    already_detached=i > 0,
                )
                # `plan.detached` alone, deliberately: under `already_detached`
                # the planner's boundary is 0 on every branch, so `plan.blocking`
                # is provably empty. Adding it "for safety" would silently absorb
                # a future planner change instead of failing loudly on it.
                for stage in plan.detached:
                    if on_phase is not None:
                        on_phase(stage.stage)
                    run_stages(
                        cfg,
                        incus,
                        spec.container_name,
                        [stage],
                        spec.repo_dir,
                        mirror_endpoint=spec.mirror_endpoint,
                        on_progress=on_progress,
                        # Compare-and-swap, unlike the foreground path: a detached
                        # stage can finish long after the user ran `jailbee net` by
                        # hand, and a blind restore would undo their choice.
                        cas_restore=True,
                    )
        finally:
            incus.config_unset(spec.container_name, "user.jailbee.autostart_in_progress")


def run_stages(
    cfg: Config,
    incus: Incus,
    container: str,
    stages: list[AutostartStage],
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
    on_progress: Callable[[str, str, str], None] | None = None,
    cas_restore: bool = False,
) -> None:
    """Run ``stages`` in order. A stage starts only when the previous one ends."""
    for stage in stages:
        run_stage(
            cfg,
            incus,
            container,
            stage,
            repo_dir,
            mirror_endpoint=mirror_endpoint,
            on_progress=on_progress,
            cas_restore=cas_restore,
        )


def run_stage(
    cfg: Config,
    incus: Incus,
    container: str,
    stage: AutostartStage,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
    on_progress: Callable[[str, str, str], None] | None = None,
    cas_restore: bool = False,
) -> None:
    """Run one stage: switch once, mount once, run the chains, undo.

    This is the only function that touches container-wide state during an
    autostart run. Chains are started together and driven by polling, so no
    profile swap or mount change can happen underneath a running step.

    ``cas_restore`` picks how the entry network mode is put back:

    * ``False`` — the foreground path, and what the synchronous executor
      always did: restore unconditionally, warning if the switch fails.
    * ``True`` — compare-and-swap. A *detached* stage may finish long after
      the user ran `jailbee net` by hand, so the mode is re-read and the
      restore skipped unless it is still the one this stage set. The
      detached supervisor passes ``True``; nothing in the blocking path
      needs it, because nobody can race a stage the CLI is blocking on.
    """
    from jailbee.lifecycle import current_network_mode, switch_network

    entry_mode = current_network_mode(cfg, incus, container)
    switched_to: str | None = None
    if stage.network is not None and entry_mode is not None and entry_mode != stage.network:
        switch_network(cfg, incus, container, stage.network, mirror_endpoint=mirror_endpoint)
        switched_to = stage.network

    mounted: list[str] = []
    info(f"  → stage: {stage.stage} [dim](net: {stage.network or entry_mode or 'unknown'})[/dim]")
    try:
        for m in stage.mounts:
            add_optional_mount(cfg, incus, container, m)
            mounted.append(m)
        _run_chains(cfg, incus, container, stage, repo_dir, on_progress=on_progress)
    finally:
        for m in reversed(mounted):
            try:
                remove_optional_mount(cfg, incus, container, m)
            except Exception as e:
                # Log-and-continue: a missing/already-removed device shouldn't
                # mask the underlying step failure or block subsequent cleanup.
                warn(f"Failed to unmount '{m}' from {container}: {e}")
        if switched_to is not None and entry_mode is not None:
            now_mode = current_network_mode(cfg, incus, container) if cas_restore else switched_to
            if now_mode == switched_to:
                try:
                    switch_network(
                        cfg, incus, container, entry_mode, mirror_endpoint=mirror_endpoint
                    )
                except Exception as e:
                    warn(f"Failed to restore network to '{entry_mode}' on {container}: {e}")
            else:
                info(
                    f"    ↳ leaving network as '{now_mode}' — changed since "
                    f"stage '{stage.stage}' started"
                )


def _run_chains(
    cfg: Config,
    incus: Incus,
    container: str,
    stage: AutostartStage,
    repo_dir: str,
    *,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> None:
    """Run every chain in ``stage``, picking a driver on the chain count.

    A stage with a single chain has nothing to overlap, so it keeps the
    blocking ``tmux.run_step`` call the flat executor always used (see
    ``_apply_step``). That is the path every legacy config takes, and
    `tests/test_autostart.py` pins its every observable: ``run_step``'s
    kwargs, the per-step mount round-trip, the elapsed log line and the
    ``TmuxStepError`` mapping.

    Two or more chains cannot share one ``tmux wait-for`` channel, so they
    are launched together and driven by sentinel polling instead, with
    their timeouts tracked host-side.
    """
    chains = stage.all_chains()
    if not chains:
        return

    tmux.ensure_session(incus, container, start_dir=repo_dir)
    if len(chains) <= 1:
        _run_chain_serially(
            cfg, incus, container, stage, chains[0], repo_dir, on_progress=on_progress
        )
    else:
        _run_chains_in_parallel(
            cfg, incus, container, stage, chains, repo_dir, on_progress=on_progress
        )


def _run_chain_serially(
    cfg: Config,
    incus: Incus,
    container: str,
    stage: AutostartStage,
    chain: AutostartChain,
    repo_dir: str,
    *,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> None:
    """Run one chain's steps one at a time, blocking on each."""
    for step in chain.steps:
        if on_progress is not None:
            on_progress(stage.stage, step.name, "start")
        try:
            _apply_step(cfg, incus, container, step, repo_dir)
        except AutostartCancelled:
            # A cancellation is not a step failure, and `continue_on_error`
            # must never swallow one: leave the step's progress entry
            # dangling (`jailbee autostart status` reads that as interrupted,
            # which is what happened) and let it out.
            raise
        except Exception as e:
            if on_progress is not None:
                on_progress(stage.stage, step.name, "fail")
            if step.continue_on_error:
                warn(f"Step '{step.name}' failed (continue_on_error): {e}")
                continue
            raise
        if on_progress is not None:
            on_progress(stage.stage, step.name, "ok")


def _run_chains_in_parallel(
    cfg: Config,
    incus: Incus,
    container: str,
    stage: AutostartStage,
    chains: list[AutostartChain],
    repo_dir: str,
    *,
    on_progress: Callable[[str, str, str], None] | None = None,
) -> None:
    """Drive every chain in ``stage`` concurrently until all are done.

    One step per chain is in flight at a time. A chain whose step fails
    stops advancing; sibling chains are *not* killed — a half-finished
    install leaves worse state than a finished one — but no further steps
    are launched in them either, and the stage fails once the in-flight
    ones have exited.
    """
    by_name = {c.name: c for c in chains}
    cursors = {c.name: 0 for c in chains}
    in_flight: dict[str, tmux.StepHandle] = {}
    step_by_name: dict[str, AutostartStep] = {}
    chain_by_step: dict[str, str] = {}
    failure: AutostartStepError | None = None
    started: dict[str, float] = {}

    def record_failure(
        name: str,
        *,
        reason: str,
        exit_code: int | None = None,
        original: BaseException | None = None,
    ) -> None:
        """Remember the first failure; the stage raises it once it is quiet."""
        nonlocal failure
        if failure is None:
            failure = AutostartStepError(
                container=container,
                step_name=name,
                reason=reason,
                exit_code=exit_code,
                original=original,
            )

    def launch_next(chain: AutostartChain) -> None:
        i = cursors[chain.name]
        if i >= len(chain.steps):
            return
        step = chain.steps[i]
        cursors[chain.name] = i + 1
        timeout = step.timeout if step.timeout is not None else cfg.autostart.step_timeout
        env = {**cfg.autostart.env, **step.env, "REPO_DIR": repo_dir}
        cwd = repo_dir if not step.working_dir else f"{repo_dir}/{step.working_dir}"
        step_by_name[step.name] = step
        chain_by_step[step.name] = chain.name
        started[step.name] = time.monotonic()
        info(f"    → step: {step.name}")
        if on_progress is not None:
            on_progress(stage.stage, step.name, "start")
        # Per-step mounts are legacy (flat form only) but still honoured.
        for m in step.mounts:
            add_optional_mount(cfg, incus, container, m)
        try:
            handle = tmux.launch_step(
                incus,
                container,
                name=step.name,
                command=step.run,
                env=env,
                cwd=cwd,
                background=step.background,
                timeout=timeout,
            )
        except TmuxStepError as e:
            # `launch_step` raises only for a background step that died
            # inside its probe window. Report it like any other failed step
            # so the error type and `continue_on_error` hold here too.
            _finish_step(cfg, incus, container, step, -1, stage, on_progress, started)
            if step.continue_on_error:
                warn(f"Step '{step.name}' failed (continue_on_error): {e}")
                launch_next(chain)
            else:
                record_failure(step.name, reason=e.reason, exit_code=e.exit_code, original=e)
            return
        if step.background:
            # Fire-and-forget: the chain advances immediately.
            _finish_step(cfg, incus, container, step, 0, stage, on_progress, started)
            launch_next(chain)
            return
        in_flight[step.name] = handle

    try:
        for chain in chains:
            launch_next(chain)

        while in_flight:
            done = tmux.poll_steps(incus, container, list(in_flight.values()))
            for name, rc in done.items():
                if in_flight.pop(name, None) is None:
                    # A sentinel for a step this driver is not tracking. The
                    # loader guarantees unique step names per trigger, but
                    # `run_stage` is also callable on stages built in code
                    # (the detached supervisor's), which never went through
                    # it — so skip the stray name instead of raising and
                    # abandoning the steps that *are* in flight.
                    continue
                step = step_by_name[name]
                _finish_step(cfg, incus, container, step, rc, stage, on_progress, started)
                if rc != 0 and not step.continue_on_error:
                    record_failure(name, reason="exit", exit_code=rc)
                    continue
                if rc != 0:
                    warn(f"Step '{name}' failed (continue_on_error): exit {rc}")
                if failure is None:
                    launch_next(by_name[chain_by_step[name]])

            now = time.monotonic()
            for name, handle in list(in_flight.items()):
                # Every handle here carries a real deadline: a background step
                # never enters `in_flight` (it is fire-and-forget above), and
                # a zero deadline is exactly the "already expired" case.
                if now >= handle.deadline:
                    tmux.interrupt_step(incus, container, handle)
                    in_flight.pop(name)
                    _finish_step(
                        cfg, incus, container, step_by_name[name], -1, stage, on_progress, started
                    )
                    record_failure(name, reason="timeout")

            if in_flight:
                time.sleep(_POLL_INTERVAL_SEC)
    except BaseException:
        # Anything unexpected — a mount that won't attach, a non-tmux error
        # out of `launch_step`, a raising `on_progress`, or a Ctrl-C — must
        # not leave steps running: `run_stage`'s `finally` is about to
        # unmount and flip the network profile, and doing that underneath a
        # live step is the one thing the stage design promises not to do.
        for handle in list(in_flight.values()):
            try:
                tmux.interrupt_step(incus, container, handle)
            except Exception as e:  # best-effort: never mask the real error
                warn(f"Failed to interrupt '{handle.name}' in {container}: {e}")
        raise

    if failure is not None:
        raise failure


def _finish_step(
    cfg: Config,
    incus: Incus,
    container: str,
    step: AutostartStep,
    rc: int,
    stage: AutostartStage,
    on_progress: Callable[[str, str, str], None] | None,
    started: dict[str, float],
) -> None:
    """Report one finished step and undo its own (legacy) mounts.

    A step's `mounts` only ever appear in the flat form — the stage form
    bans them — but they are still honoured there, so they are attached
    around the individual step exactly as `_apply_step` does it, including
    the warn-and-continue cleanup.
    """
    for m in reversed(step.mounts):
        try:
            remove_optional_mount(cfg, incus, container, m)
        except Exception as e:
            warn(f"Failed to unmount '{m}' from {container}: {e}")
    elapsed = time.monotonic() - started.get(step.name, time.monotonic())
    info(f"    ↳ {step.name}: {elapsed:.1f}s (exit {rc})")
    if on_progress is not None:
        on_progress(stage.stage, step.name, "ok" if rc == 0 else "fail")


def _apply_step(
    cfg: Config,
    incus: Incus,
    container: str,
    step: AutostartStep,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
    manage_network: bool = False,
) -> None:
    """Run one step to completion, blocking on its tmux window.

    The serial driver's unit of work. What a step owns itself is handled
    here: its legacy per-step ``mounts``, its elapsed log line and the
    mapping of ``TmuxStepError`` onto ``AutostartStepError``.

    ``manage_network`` is off for every step that belongs to a stage: the
    profile is then the *stage's* to switch and restore (see ``run_stage``),
    and a second swap here would fight it. It is on for the one caller that
    runs a step outside any stage — ``agents._ensure_one``, whose
    install/update commands may need ``loose`` to reach the registry before
    an autostart stage exists. ``mirror_endpoint`` is forwarded to those
    swaps so the strict-mode mirror row in /etc/hosts survives the
    round-trip; it is unused when ``manage_network`` is off.
    """
    from jailbee.lifecycle import current_network_mode, switch_network

    mounted: list[str] = []
    prev_network: str | None = None
    if manage_network and step.network is not None:
        current = current_network_mode(cfg, incus, container)
        if current is not None and current != step.network:
            prev_network = current
            switch_network(cfg, incus, container, step.network, mirror_endpoint=mirror_endpoint)

    # Announced before the step, not only after it: a stage built from four
    # flat steps would otherwise print nothing between its header and each
    # step's completion, so a long `npm install` reads as a hang. The
    # profile is not named here — it belongs to the stage, which prints it.
    info(f"    → step: {step.name}")
    t0 = time.monotonic()
    try:
        for m in step.mounts:
            add_optional_mount(cfg, incus, container, m)
            mounted.append(m)

        env = {**cfg.autostart.env, **step.env, "REPO_DIR": repo_dir}
        cwd = repo_dir if not step.working_dir else f"{repo_dir}/{step.working_dir}"
        timeout = step.timeout if step.timeout is not None else cfg.autostart.step_timeout

        try:
            tmux.run_step(
                incus,
                container,
                name=step.name,
                command=step.run,
                env=env,
                cwd=cwd,
                background=step.background,
                timeout=timeout,
            )
        except TmuxStepError as e:
            raise AutostartStepError(
                container=container,
                step_name=step.name,
                reason=e.reason,
                exit_code=e.exit_code,
                original=e,
            ) from e
        except AutostartCancelled:
            # The `finally` below is about to unmount this step's devices, and
            # the stage's is about to flip the network back — under a step
            # still running, which is the one thing the stage design promises
            # not to do (see `_run_chains_in_parallel`'s abort path, which
            # interrupts its handles for the same reason). The serial driver
            # holds no handle, but the window name is derived from the step
            # name, so the same C-c reaches it. Best-effort and non-blocking,
            # exactly like the parallel path: it narrows the window in which
            # cleanup races a live step, it does not close it.
            tmux.interrupt_window(incus, container, tmux.window_for(step.name))
            raise
    finally:
        for m in reversed(mounted):
            try:
                remove_optional_mount(cfg, incus, container, m)
            except Exception as e:
                # Log-and-continue: a missing/already-removed device shouldn't
                # mask the underlying step failure or block subsequent cleanup.
                warn(f"Failed to unmount '{m}' from {container}: {e}")
        if prev_network is not None:
            try:
                switch_network(cfg, incus, container, prev_network, mirror_endpoint=mirror_endpoint)
            except Exception as e:
                warn(f"Failed to restore network to '{prev_network}' on {container}: {e}")
        elapsed = time.monotonic() - t0
        info(f"    ↳ {step.name}: {elapsed:.1f}s")


def maybe_warn_no_gui() -> None:
    """Print a friendly warning when autostart wants GUI but there's no session."""
    if not has_graphical_session():
        warn("No graphical session detected — GUI app launches skipped")
