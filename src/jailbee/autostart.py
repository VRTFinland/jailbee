"""Autostart orchestration: run config-driven shell steps inside container.

Each ``AutostartStep`` is iterated in order. Per-step ``mounts`` are attached
before the step and detached after (best-effort cleanup runs even on failure).
``background: True`` runs the step in a detached tmux window and returns
immediately. Sync steps block via ``tmux wait-for`` until the step exits.
"""

from __future__ import annotations

import os
import shlex
import time
from enum import Enum

from jailbee import tmux
from jailbee.config import AutostartStep, Config
from jailbee.incus import Incus
from jailbee.mounts import add_optional_mount, remove_optional_mount
from jailbee.tmux import TmuxStepError
from jailbee.tui import info, success, warn

# rc → short human-readable cause. Anything else falls back to "exit code N".
_EXIT_HINTS: dict[int, str] = {
    126: "command not executable",
    127: "command not found",
    130: "interrupted (SIGINT)",
    137: "killed (SIGKILL / out of memory?)",
    143: "terminated (SIGTERM)",
}


class AutostartTrigger(Enum):
    ON_CREATE = "on_create"
    ON_START = "on_start"


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
    """
    step = _github_token_step(cfg)
    if step is None:
        return
    tmux.ensure_session(incus, container, start_dir=repo_dir)
    info(f"Injecting GH_TOKEN into {container}")
    _apply_step(cfg, incus, container, step, repo_dir, mirror_endpoint=mirror_endpoint)


def _claude_autostart_step(cfg: Config) -> AutostartStep | None:
    """Return an AutostartStep that runs `cfg.claude.command` in a
    backgrounded tmux window, or None when `claude.autostart` is off.

    Appended (not prepended) to on_start, so user-defined steps finish
    first and `claude` is the last window — most tmux layouts surface
    it as the focused window when `jailbee tmux` attaches. `jailbee tmux`
    additionally calls `select-window` for it.

    The step has no network override: claude.enabled already extends
    strict-mode egress with api.anthropic.com:443 + code.claude.com:443
    + claude.ai:443 + downloads.claude.ai:443 (plus the plugin marketplace
    hosts when claude.plugins_enabled), so the container's current network
    mode is the right one.

    validate_runtime enforces that `claude.autostart` requires
    `claude.enabled`, so we don't re-check here.

    `continue_on_error` is True: claude is an optional integration, so a
    launch failure (e.g. the binary never installed) degrades to a warning
    instead of hard-failing the whole `jailbee new`. The dev container is still
    usable without the claude window.
    """
    if not cfg.claude.autostart:
        return None
    return AutostartStep(
        name="claude",
        run=f"exec {cfg.claude.command}",
        background=True,
        continue_on_error=True,
    )


def run_autostart(
    cfg: Config,
    incus: Incus,
    container: str,
    trigger: AutostartTrigger,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    """Run autostart steps inside ``container`` for ``trigger``.

    ``mirror_endpoint=(ip, port)`` is forwarded to every transient
    ``switch_network`` call a step triggers, so the strict-mode
    ``jailbee-registry-mirror.incus`` row in /etc/hosts survives an
    autostart-driven ``strict → loose → strict`` round-trip.
    """
    if trigger == AutostartTrigger.ON_CREATE:
        steps: list[AutostartStep] = list(cfg.autostart.on_create)
    else:
        # Append the synthetic claude step (no-op when claude.autostart
        # is off) — runs last so its window is the most-recently-created
        # and `jailbee tmux` lands in it. The github-token step is NOT injected
        # here: it's infrastructure, not a user autostart command, so it's
        # written by ``inject_github_token`` independently of --no-autostart.
        steps = list(cfg.autostart.on_start)
        claude_step = _claude_autostart_step(cfg)
        if claude_step is not None:
            steps.append(claude_step)
    if not steps:
        return

    tmux.ensure_session(incus, container, start_dir=repo_dir)
    info(f"Running {len(steps)} autostart step(s) in {container}")

    # The loose-revert timer (see loose_revert.py) skips containers
    # carrying this flag, so steps that swap the network profile
    # mid-autostart don't race the auto-revert path. Cleared in
    # ``finally`` so a step failure still releases the lock.
    incus.config_set(container, "user.jailbee.autostart_in_progress", "1")
    try:
        for step in steps:
            try:
                _apply_step(
                    cfg,
                    incus,
                    container,
                    step,
                    repo_dir,
                    mirror_endpoint=mirror_endpoint,
                )
            except Exception as e:
                if step.continue_on_error:
                    warn(f"Step '{step.name}' failed (continue_on_error): {e}")
                    continue
                raise
    finally:
        incus.config_unset(container, "user.jailbee.autostart_in_progress")

    success("Autostart complete")


def _apply_step(
    cfg: Config,
    incus: Incus,
    container: str,
    step: AutostartStep,
    repo_dir: str,
    *,
    mirror_endpoint: tuple[str, int] | None = None,
) -> None:
    from jailbee.lifecycle import (
        current_network_mode,
        switch_network,
    )

    mounted: list[str] = []
    prev_network: str | None = None
    current = current_network_mode(cfg, incus, container)
    if step.network is not None and current is not None and current != step.network:
        prev_network = current
        switch_network(cfg, incus, container, step.network, mirror_endpoint=mirror_endpoint)
    effective_network = step.network if step.network is not None else current
    info(f"  → step: {step.name} [dim](net: {effective_network or 'unknown'})[/dim]")

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
        warn("No graphical session detected — IDE and Chrome launches skipped")
