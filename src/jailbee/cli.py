"""CLI entry point for jailbee."""

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, Literal, NamedTuple

import typer
import yaml

from jailbee import __version__, completion, table_format
from jailbee.config import ConfigError, load_config, load_config_unsanitized
from jailbee.global_config import (
    GlobalConfig,
    default_global_config_path,
    load_global_config,
)
from jailbee.paths import find_repo_config
from jailbee.tui import (
    confirm_destroy_risk,
    error,
    error_plain,
    info,
    success,
    success_plain,
    warn,
    warn_plain,
)

app = typer.Typer(
    name="jailbee",
    help="Manage isolated development environments using Incus.",
    no_args_is_help=True,
)

config_app = typer.Typer(
    name="config",
    help="Configuration commands.",
    no_args_is_help=True,
)
app.add_typer(config_app)


ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Path to config.yaml"),
]


def _resolve_config_path(path: Path | None) -> Path:
    if path is not None:
        return path
    return find_repo_config()


def _now() -> datetime:
    """Wallclock helper, factored for test mocking."""
    return datetime.now(UTC)


def _is_tty() -> bool:
    """Whether stdin is a terminal, factored for test mocking.

    `CliRunner` replaces `sys.stdin` inside `invoke()`, so a test cannot patch
    `sys.stdin.isatty` and have it reach the command — hence the indirection.
    """
    return sys.stdin.isatty()


def _record_upgrade_action(cfg: "Config", action: Literal["base_build", "apply"]) -> None:
    """Record that `action` just ran successfully in this repo.

    Bookkeeping for `jailbee.upgrade`'s advice, and a courtesy like the advice
    itself: a state-DB problem must not turn a successful `base build` into a
    failed command, hence the broad except.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.upgrade import record

    try:
        with Session(get_engine()) as session:
            record(session, cfg.container_prefix, action, __version__, now=_now())
    except Exception:  # bookkeeping only; must never fail a successful command
        return


def _advise_upgrade(cfg: "Config") -> None:
    """Print any pending `base build` / `apply` advice for this repo.

    Non-blocking and never interactive: it must work with no TTY, and in
    background `jailbee new` it prints from the foreground parent rather than
    into the detached job's log where nobody reads it.

    Wrapped broadly on purpose — a locked state DB, a schema surprise, an
    unreadable row: none of it may take down the command the user actually
    ran. Advice is a courtesy.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.tui import hint
    from jailbee.upgrade import advice_lines

    try:
        with Session(get_engine()) as session:
            lines = advice_lines(session, cfg.container_prefix, __version__, now=_now())
        hint(lines)
    except Exception:  # advice is a courtesy; must never fail the command
        return


def _advise_setup() -> None:
    """Print the one-shot post-install hint, if it has anything left to say.

    Same contract as `_advise_upgrade`: stderr, non-interactive, and wrapped
    broadly because a courtesy must never take down the command the user
    actually ran. `consume_hint` is what makes this fire at most once — the
    probes it runs are `stat`s, so this costs nothing on the commands it
    decorates.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.setup_command import consume_hint, detect_shell
    from jailbee.tui import hint

    try:
        shell = detect_shell()
        with Session(get_engine()) as session:
            lines = consume_hint(session, shells=[shell] if shell else [], now=_now())
        hint(lines)
    except Exception:  # the hint is a courtesy; must never fail the command
        return


def _record_setup_run() -> None:
    """Record that `jailbee setup` ran, silencing the hint for good.

    Recorded even when every step was declined: the user has seen the state
    and decided, and re-asking on the next `jailbee ls` would be nagging.
    """
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.setup_command import record_setup

    try:
        with Session(get_engine()) as session:
            record_setup(session, __version__, now=_now())
    except Exception:  # bookkeeping only; must never fail a successful command
        return


def _job_engine() -> "Engine | None":
    """The state-DB engine used for background-job tracking, or None.

    Same contract as the advice helpers above, one step earlier: a job row is
    bookkeeping for `jailbee ls` / `jailbee job`, not part of the container, so
    an unwritable state dir or a database that refuses to open must cost the
    user the tracking — not the create/destroy the worker exists to run.
    """
    from jailbee.db import get_engine

    try:
        return get_engine()
    except Exception as e:  # bookkeeping only; never abort the operation
        # `warn_plain`: a DBAPI error body carries `[SQL: ...]` /
        # `[parameters: ...]`, which Rich markup would silently eat.
        warn_plain(f"Job tracking unavailable ({e}) — the operation itself continues")
        return None


def _track_job(
    engine: "Engine | None",
    work: "Callable[[Session], None]",
    what: str,
) -> None:
    """Run one job-row write, warning instead of raising when the DB says no.

    Every phase update, failure record and cleanup of a detached worker goes
    through here, because the write is bookkeeping and the operation around it
    is not: a locked database, a read-only state dir or a vanished table must
    not abort a container create halfway.

    Not hypothetical. A *concurrently running older* jailbee is enough: older
    `db._ensure_schema` versions dropped and recreated every table they knew
    about whenever they met a database newer than their own
    `CURRENT_SCHEMA_VERSION`, and re-checked on every connection. A long-lived
    process from such a build (a dashboard left open across an upgrade) reset
    the schema under a live worker, whose next phase update then died on
    `no such table: background_op` and, unguarded, took the whole
    `jailbee new` with it. That reset is gone — a newer database is now used
    as-is — but the worker must survive one regardless, because the process
    doing it is the *old* build, which no fix here can reach.
    """
    if engine is None:
        return

    from sqlmodel import Session

    try:
        with Session(engine) as session:
            work(session)
    except Exception as e:  # bookkeeping only; never abort the operation
        warn_plain(f"Job tracking: could not {what} ({e})")  # see `_job_engine`


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the jailbee version and exit.",
        ),
    ] = False,
) -> None:
    # No docstring: `help=` on the Typer() above is the command's help text,
    # and a docstring here would silently replace it.
    pass


@app.command()
def version() -> None:
    """Show the jailbee version."""
    typer.echo(__version__)


@config_app.command("show")
def config_show(
    config: ConfigOption = None,
    layer: Annotated[
        str,
        typer.Option(
            "--layer",
            help="Which config layer to print: global | repo | effective (default).",
            autocompletion=completion.complete_choices("global", "repo", "effective"),
        ),
    ] = "effective",
) -> None:
    """Print the loaded configuration as YAML."""
    if layer not in {"global", "repo", "effective"}:
        error(f"--layer must be one of: global, repo, effective. Got: {layer!r}")
        raise typer.Exit(2)

    if layer == "global":
        gpath = default_global_config_path()
        info(f"# Global config: {gpath}")
        if not gpath.exists():
            return
        typer.echo(gpath.read_text(), nl=False)
        return

    if layer == "repo":
        path = _resolve_config_path(config)
        info(f"# Repo config: {path}")
        if not path.exists():
            return
        typer.echo(path.read_text(), nl=False)
        return

    # effective (default) — current behaviour
    path = _resolve_config_path(config)
    cfg = _load_or_exit(config)
    info(f"# Effective config (merged from global + {path})")
    data = cfg.model_dump(mode="json")

    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.egress_scope import effective_repo_entries

    # The `effective` layer promises the list that is actually enforced, so
    # host-local repo overrides belong here. `--layer repo|global` print raw
    # files and stay untouched. Container-scope extras are not part of a repo
    # config layer — `jailbee net egress ls` is the view that shows those.
    with Session(get_engine()) as session:
        data["egress_allow"] = effective_repo_entries(cfg, session)
    data["shared_caches"] = [c.model_dump(mode="json") for c in cfg.effective_shared_caches()]
    data["host_mounts"] = [m.model_dump(mode="json") for m in cfg.effective_host_mounts()]
    # Dump each agent through its own (possibly subclassed) model rather than
    # relying on `cfg.model_dump()`'s dict[str, AgentConfig] field type: that
    # would serialise every entry — including `agents.claude`, which is a
    # ClaudeAgentConfig — through the base AgentConfig shape and silently
    # drop the Claude-only fields (plugins_enabled, install_jailbee_skills, …).
    data["agents"] = {name: agent.model_dump(mode="json") for name, agent in cfg.agents.items()}
    typer.echo(yaml.safe_dump(data, sort_keys=False))


@config_app.command("validate")
def config_validate(config: ConfigOption = None) -> None:
    """Validate the configuration file (schema + runtime paths)."""
    from jailbee.global_config import global_config_issues

    path = _resolve_config_path(config)
    try:
        # `load_config_unsanitized`, not `load_config`: ordinary loading now
        # recovers from a typo in the repo's own `ls:`/`dashboard:` blocks
        # (see `config.load_config`), but the one command whose job is
        # validating config should still catch it, with the allowed names
        # listed — `validate_runtime()` below only sees that if it gets the
        # raw, unrecovered blocks.
        cfg = load_config_unsanitized(path)
    except ConfigError as e:
        # `error_plain`: a validator message can carry square brackets — the
        # `host_ports` name rule quotes the regex `[a-z0-9][a-z0-9-]*` — and
        # `error` would read them as Rich style tags and silently delete the
        # rule the message exists to state.
        error_plain(str(e))
        raise typer.Exit(1) from e
    success(f"Schema OK: {path}")

    issues = cfg.validate_runtime()

    # The global `ls:`/`dashboard:` blocks are otherwise invisible to this
    # command: `load_config` only merges the Config-level subset of
    # `global.yaml`, and ordinary loading (`_load_global`) now recovers from
    # a column typo instead of raising — but the one command whose job is
    # validating config should still catch it, with the allowed names
    # listed, same as it does for the repo's own blocks above.
    try:
        issues += global_config_issues(default_global_config_path())
    except ConfigError as e:
        # `error_plain`: a validator message can carry square brackets — the
        # `host_ports` name rule quotes the regex `[a-z0-9][a-z0-9-]*` — and
        # `error` would read them as Rich style tags and silently delete the
        # rule the message exists to state.
        error_plain(str(e))
        raise typer.Exit(1) from e

    if not issues:
        success("Runtime paths OK")
        return

    for issue in issues:
        warn(issue)
    raise typer.Exit(2)


@config_app.command("init")
def config_init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing config.")] = False,
    global_: Annotated[
        bool,
        typer.Option(
            "--global",
            help="Write the per-user ~/.config/jailbee/global.yaml instead of the repo config.",
        ),
    ] = False,
) -> None:
    """Create .jailbee/config.yaml (default) or ~/.config/jailbee/global.yaml (--global)."""
    from jailbee.config_init import write_global_template, write_template

    if global_:
        try:
            path = write_global_template(force=force)
        except FileExistsError as e:
            error(str(e))
            raise typer.Exit(1) from e
        success(f"Wrote {path}")
        return

    try:
        path = write_template(Path.cwd(), force=force)
    except FileExistsError as e:
        error(str(e))
        raise typer.Exit(1) from e
    success(f"Wrote {path}")

    global_path = default_global_config_path()
    if not global_path.exists():
        info(
            f"Hint: no {global_path} found. Run `jailbee config init --global` "
            f"to create one (carries personal mounts, IDE preference, etc.)."
        )


@app.command()
def init(config: ConfigOption = None) -> None:
    """Initialize Incus profiles, ACL, and shared directories (first-time setup).

    Errors if profiles already exist — use `jailbee apply` to update from
    current config.
    """
    from jailbee.docker_daemon import mirror_wanted
    from jailbee.incus import Incus
    from jailbee.init_command import run_init

    cfg = _load_or_exit(config)

    incus = Incus()
    gcfg = _load_global()

    mirror_endpoint: tuple[str, int] | None = None
    if mirror_wanted(cfg, gcfg):
        from jailbee.docker_daemon import compute_mirror_endpoint

        try:
            mirror_endpoint = compute_mirror_endpoint(incus, gcfg)
        except ValueError as e:
            # Not fatal: `init` is documented to run before `registry up`, and
            # the ACL's mirror rule is written by the next `apply` / refresh.
            warn(f"{e} Continuing — run 'jailbee apply' after 'jailbee registry up'.")

    try:
        run_init(cfg, incus, mirror_endpoint=mirror_endpoint)
    except RuntimeError as e:
        error(str(e))
        raise typer.Exit(1) from e

    # `run_init` writes exactly what `apply` writes — profiles, ACL, shared
    # dirs — so it satisfies an `apply` upgrade note just as `apply` does.
    # Recorded here, at the point that work is known to have succeeded, and
    # not at the end of the command: the steps below (systemd units, repo
    # registration) are not part of what the note is about, and a freshly
    # inited repo must not be told to `jailbee apply` for changes `jailbee
    # init` just made.
    _record_upgrade_action(cfg, "apply")

    # Install the singleton refresh timer + register this repo so it
    # gets refreshed on the next 60s tick (and stays up to date going
    # forward).
    from sqlmodel import Session

    from jailbee import egress_pool
    from jailbee.db import get_engine
    from jailbee.init_command import install_systemd_units

    install_systemd_units()

    with Session(get_engine()) as session:
        egress_pool.register_repo(session, cfg)

    # Linger keeps the timer firing when no user session is open.
    # Inform but don't enforce — needs root.
    from jailbee.setup_command import linger_tip

    linger_tip()


@app.command()
def setup(
    yes: Annotated[
        bool,
        typer.Option("--yes", "-y", help="Install every selected step without asking"),
    ] = False,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            help="Limit to these steps (repeatable): completions, timer, skills",
            autocompletion=completion.complete_choices("completions", "timer", "skills"),
        ),
    ] = None,
    shell: Annotated[
        list[str] | None,
        typer.Option(
            "--shell",
            help="Shells to install completions for (repeatable); default: the detected one",
            autocompletion=completion.complete_choices("bash", "zsh", "fish"),
        ),
    ] = None,
) -> None:
    """Set up this machine: shell completions, the refresh timer, Claude skills.

    The machine-level counterpart to `jailbee init`, which sets up a repo.
    These are the steps a `uv tool install jailbee` cannot perform for you:
    completion scripts for `jailbee` and `jb`, the `jailbee-net-refresh` user
    timer (egress pool refresh and `jailbee net loose` TTL expiry), and
    jailbee's Claude Code skills in `~/.claude/skills`.

    Interactive by default and idempotent, so re-run it after upgrading
    jailbee. `--yes` installs everything without asking, which is what
    `make install` runs.

    Host prerequisites — Incus, the firewall, UID delegation — are not done
    here: run `jailbee doctor` and follow docs/installation.md.
    """
    from jailbee import setup_command as sc

    if only:
        unknown = [key for key in only if key not in sc.STEP_KEYS]
        if unknown:
            error(f"Unknown step: {', '.join(unknown)} (known: {', '.join(sc.STEP_KEYS)})")
            raise typer.Exit(2)
    keys = [key for key in sc.STEP_KEYS if not only or key in only]

    if shell:
        unsupported = [name for name in shell if name not in sc.SUPPORTED_SHELLS]
        if unsupported:
            error(
                f"Unsupported shell: {', '.join(unsupported)} "
                f"(supported: {', '.join(sc.SUPPORTED_SHELLS)})"
            )
            raise typer.Exit(2)
        shells = list(shell)
    else:
        detected = sc.detect_shell()
        shells = [detected] if detected is not None else []

    def ask(question: str, default: bool) -> bool:
        return typer.confirm(question, default=default)

    ran = sc.run_setup(keys=keys, shells=shells, confirm=None if yes else ask)

    if "timer" in ran:
        sc.linger_tip()
    _record_setup_run()

    info("")
    info("Next: `jb doctor` checks the host — Incus, firewall, UID delegation.")
    info(f"      Host setup end to end: docs/installation.md ({sc.DOCS_URL})")


@app.command()
def apply(
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip restart confirmation")] = False,
    no_restart: Annotated[
        bool,
        typer.Option(
            "--no-restart",
            help="Update profiles/ACL/hosts/proxy but never restart containers",
        ),
    ] = False,
) -> None:
    """Apply current config: profiles, ACL, /etc/hosts, dockerd proxy, restarts.

    Replaces `jailbee init --reapply` and `jailbee net refresh`. Idempotent and
    cheap to re-run — if nothing changed, no work is done and no prompt
    is shown.
    """
    from jailbee.apply import run_apply
    from jailbee.incus import Incus
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)
    gcfg = _load_global()
    incus = Incus()

    try:
        result = run_apply(
            cfg,
            incus,
            gcfg,
            assume_yes=yes,
            no_restart=no_restart,
        )
    except Exception as e:
        error(str(e))
        raise typer.Exit(1) from e

    if result.restarted:
        success(f"Restarted: {', '.join(short_name(cfg, n) for n in result.restarted)}")
    if not any(
        [
            result.profiles_changed,
            result.acl_changed,
            result.hosts_repinned,
            result.docker_proxy_reapplied,
            result.offline_migrated,
            result.ports_changed,
        ]
    ):
        info("Configuration already up to date.")
    elif not result.restart_failures and not result.restarted:
        success("Apply complete.")

    for name, err in result.restart_failures:
        error(f"Restart failed: {short_name(cfg, name)}: {err}")

    for name, err in result.port_failures:
        error_plain(f"Port forwards on {short_name(cfg, name)}: {err}")

    if result.fully_successful:
        _record_upgrade_action(cfg, "apply")

    if not result.fully_successful:
        raise typer.Exit(1)


# ---- Lifecycle commands ----


@app.command("ls")
def list_cmd(
    all_repos: Annotated[
        bool, typer.Option("--all", help="Show containers from all repos.")
    ] = False,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help=(
                "Comma-separated list of fields to show. Allowed: name, "
                "full_name, repo, mode, base, state, created, job, network, "
                "ttl, loose_until, ip, memory_limit, mem, wt, ahead_diff, "
                "ahead_count, conflict, local_diff, local_count, git_status, "
                "pr."
            ),
        ),
    ] = None,
    submodules: Annotated[
        bool,
        typer.Option(
            "--submodules/--no-submodules",
            help="Show per-submodule sub-rows (auto-on in repos with submodules).",
        ),
    ] = True,
    config: ConfigOption = None,
) -> None:
    """List managed containers."""
    from jailbee.incus import Incus
    from jailbee.lifecycle import (
        list_containers,
        ls_field_specs,
        repo_has_submodules,
        submodule_sub_rows,
    )
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    _advise_upgrade(cfg)
    _advise_setup()
    show_submodules = submodules and repo_has_submodules(cfg)

    containers = list_containers(
        cfg, Incus(), all_repos=all_repos, with_git_status=True, with_background=True
    )
    now = _now()
    all_fields = ls_field_specs(now=now, all_repos=all_repos, show_submodules=show_submodules)
    # `_load_global()` rather than `load_global_config()` directly: a
    # host-level validation error in global.yaml (bad YAML, a malformed
    # docker_registry_mirror, ...) passes `load_config` (which only merges
    # the Config-level subset) and would otherwise escape as an uncaught
    # ConfigError traceback — Typer's standalone mode does not catch it. The
    # helper renders it as `✗ <msg>` and exits 1 like every other config
    # error. A typo'd column name is not fatal here (or anywhere but `jailbee
    # config validate`) — the helper already recovered from it and just
    # warns.
    columns = cfg.effective_ls_columns(_load_global())
    # An explicit --fields flag beats the configured `fields` list outright: if
    # the config's selection narrowed the candidates first, a flag naming a
    # column outside that selection would have nothing left to pick from.
    # `hide` never removes candidates (only default_table), so it stays
    # unconditional here — the flag can still reach a hidden column by name.
    #
    # `columns.fields` only stands in for the *table*'s default column set —
    # it's a personal display preference (see ColumnConfig's docstring), not
    # a machine-readable contract, so it must not silently narrow
    # `--format json` for a script that expects the built-in default_json
    # fields. Skip it outside table mode; an explicit --fields flag still
    # applies to json above via the `fields is not None` check.
    #
    # `apply_column_config` also clears `show_if` on a `columns.fields`
    # selection: naming a column in `ls: {fields: [...]}` is as explicit a
    # request as passing --fields, so it renders even if no container would
    # otherwise show it (e.g. `pr` with nothing open). See that function's
    # docstring — this is the one place that rule lives.
    all_fields = table_format.apply_column_config(
        all_fields,
        fields=None if fields is not None or fmt != "table" else columns.fields,
        hide=columns.hide,
    )

    table_format.emit(
        containers,
        all_fields,
        fmt=fmt,
        fields=fields,
        console=console,
        title="jailbee containers" if fmt == "table" else None,
        empty_message="[dim](no containers found)[/dim]",
        sub_rows=submodule_sub_rows if show_submodules else None,
    )


@app.command("new")
def new_cmd(
    container_branch: Annotated[
        str | None,
        typer.Argument(
            metavar="NAME",
            help=(
                "Name of the new environment. Used as the git branch inside "
                "the container's clone, and slugified (/ -> -) into the "
                "container name. Point it at an existing branch to review it. "
                "Required unless --current is used. In --mount mode it is the "
                "container name (no slashes), not a branch."
            ),
            autocompletion=completion.complete_branch,
        ),
    ] = None,
    base: Annotated[
        str | None,
        typer.Argument(
            help="Optional base branch to fork from. Must already exist in the source repo.",
            autocompletion=completion.complete_branch,
        ),
    ] = None,
    current: Annotated[
        bool,
        typer.Option(
            "--current",
            help=(
                "Use the host repo's currently checked-out branch. Without a "
                "NAME positional, it becomes the branch to work on. With a "
                "NAME positional, it becomes the base for the new branch. "
                "Mutually exclusive with the BASE positional."
            ),
        ),
    ] = False,
    name: Annotated[str | None, typer.Option("--name")] = None,
    network: Annotated[str, typer.Option("--net")] = "",
    memory: Annotated[str | None, typer.Option("--memory")] = None,
    cpu: Annotated[int | None, typer.Option("--cpu")] = None,
    from_base: Annotated[str | None, typer.Option("--from-base")] = None,
    no_clone: Annotated[bool, typer.Option("--no-clone")] = False,
    no_autostart: Annotated[bool, typer.Option("--no-autostart")] = False,
    mount: Annotated[
        bool,
        typer.Option(
            "--mount",
            "-m",
            help=(
                "Bind-mount the host repo RW into the container instead of "
                "cloning. The positional argument is the container name "
                "(not a branch). Incompatible with --base, --no-clone, and "
                "names containing '/'."
            ),
        ),
    ] = False,
    attach: Annotated[
        str | None,
        typer.Option(
            "--attach",
            help=(
                "After creation, attach to the container: 'shell', 'tmux', "
                "or 'none'. Overrides the `after_new` config setting."
            ),
            autocompletion=completion.complete_choices("shell", "tmux", "none"),
        ),
    ] = None,
    no_attach: Annotated[
        bool,
        typer.Option(
            "--no-attach",
            help="Do not attach after creation (equivalent to --attach none).",
        ),
    ] = False,
    tmux_flag: Annotated[
        bool,
        typer.Option(
            "--tmux",
            help=(
                "After creation, attach to the container's autostart tmux "
                "session. Shorthand for '--attach tmux'; forces foreground "
                "creation even when `new.background` is set."
            ),
        ),
    ] = False,
    shell_flag: Annotated[
        bool,
        typer.Option(
            "--shell",
            help=(
                "After creation, open an interactive shell in the container. "
                "Shorthand for '--attach shell'; forces foreground creation "
                "even when `new.background` is set."
            ),
        ),
    ] = False,
    pr: Annotated[
        int | None,
        typer.Option(
            "--pr",
            help=(
                "GitHub PR number to fetch and check out. The PR's head is "
                "fetched into the source repo (as refs/jailbee/pr/<N>/head, "
                "leaving your branches untouched) and the container's clone "
                "is checked out at that exact commit. Requires the 'gh' CLI."
            ),
        ),
    ] = None,
    no_fetch: Annotated[
        bool,
        typer.Option(
            "--no-fetch",
            help=(
                "Skip 'git fetch' for the PR head; use the copy already on "
                "the host (refs/jailbee/pr/<N>/head from an earlier run, else a "
                "local branch of the head's name). Only valid with --pr."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help=(
                "Skip confirmation prompts: reusing an existing branch, and "
                "accepting a target branch whose autostart config widens "
                "network access."
            ),
        ),
    ] = False,
    background: Annotated[
        bool,
        typer.Option(
            "--background",
            "-b",
            help=(
                "Create the container detached in the background and return "
                "the shell immediately. Track progress with `jailbee ls`. "
                "Overrides the `new.background` config setting."
            ),
        ),
    ] = False,
    no_background: Annotated[
        bool,
        typer.Option(
            "--no-background",
            help="Force foreground creation, overriding `new.background`.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Create a new container for a branch.

    Examples:

      jailbee new feat/foo                  # new branch off default (e.g. dev)
      jailbee new feat/foo feat/wip-bar     # new branch off feat/wip-bar
      jailbee new feat/foo --current        # new branch off host's current branch
      jailbee new feat/jokufeat             # check out existing branch for review
      jailbee new --current                 # use host repo's currently checked-out branch
      jailbee new --pr 1234                 # check out PR #1234 for review
      jailbee new --pr 1234 --no-fetch      # use already-fetched local branch
      jailbee new mysmoke --mount           # mount mode: positional is the container
                                        # name; host repo is bind-mounted RW
    """
    from jailbee.autostart import AutostartStepError
    from jailbee.docker_daemon import mirror_wanted
    from jailbee.git import get_current_branch
    from jailbee.incus import Incus
    from jailbee.lifecycle import NewContainerOptions, new_container, short_name

    cfg = _load_or_exit(config)
    _advise_upgrade(cfg)
    _advise_setup()

    # --tmux/--shell are shorthands for `--attach <mode>` that additionally
    # force foreground creation. All four attach flags are mutually
    # exclusive: each states a different intent for the same decision.
    attach_flags = [
        flag
        for flag, given in (
            ("--attach", attach is not None),
            ("--no-attach", no_attach),
            ("--tmux", tmux_flag),
            ("--shell", shell_flag),
        )
        if given
    ]
    if len(attach_flags) > 1:
        error(f"{', '.join(attach_flags)} are mutually exclusive.")
        raise typer.Exit(2)
    if attach is not None and attach not in ("shell", "tmux", "none"):
        error(f"--attach must be one of 'shell', 'tmux', 'none'; got {attach!r}.")
        raise typer.Exit(2)
    if network == "offline":
        from jailbee.config import OFFLINE_REMOVED_MSG

        error(OFFLINE_REMOVED_MSG)
        raise typer.Exit(2)

    attach_flag = attach_flags[0] if attach_flags else None
    if no_attach:
        attach_mode: str = "none"
    elif attach is not None:
        attach_mode = attach
    elif tmux_flag:
        attach_mode = "tmux"
    elif shell_flag:
        attach_mode = "shell"
    else:
        attach_mode = cfg.after_new
    # An attach asked for on the command line beats a config-driven
    # `new.background`; the `after_new` config default yields to it instead.
    wants_attach = attach_flag is not None and attach_mode != "none"

    if background and no_background:
        error("--background and --no-background are mutually exclusive.")
        raise typer.Exit(2)
    if background and wants_attach:
        error(f"--background cannot be combined with {attach_flag} (the creation is detached).")
        raise typer.Exit(2)
    if no_background:
        run_in_background = False
    elif background:
        run_in_background = True
    elif wants_attach:
        # "Put me in this container when it's ready" only a foreground run
        # can honour, so an explicit attach overrides `new.background`
        # rather than conflicting with it.
        run_in_background = False
    else:
        run_in_background = cfg.new.background

    if run_in_background:
        # A config-driven after_new default can't apply to a detached run.
        # After the rule above this is the only path that drops an attach
        # silently — an explicitly requested one always wins or errors.
        attach_mode = "none"

    if no_fetch and pr is None:
        error("--no-fetch requires --pr.")
        raise typer.Exit(2)
    # The PR head commit the container's clone is pinned to; see
    # `pr.pr_head_ref` for why a branch name would not do.
    pr_clone_commit: str | None = None
    if pr is not None:
        if container_branch is not None:
            error("--pr derives the branch from the PR; do not also pass a positional NAME.")
            raise typer.Exit(2)
        if base is not None:
            error("--pr derives the branch from the PR; --base is not applicable.")
            raise typer.Exit(2)
        if current:
            error("--pr derives the branch from the PR; --current is not applicable.")
            raise typer.Exit(2)
        if mount:
            error("--pr is incompatible with --mount (mount mode shares the host working tree).")
            raise typer.Exit(2)
        if no_clone:
            error("--pr requires cloning the PR's branch; --no-clone is not applicable.")
            raise typer.Exit(2)

        from jailbee import pr as pr_module

        try:
            pr_info = pr_module.resolve_pr(cfg.repo_root, pr, remote=cfg.upstream_remote)
        except pr_module.PrError as e:
            error(str(e))
            raise typer.Exit(2) from e

        if pr_info.state == "CLOSED":
            warn(f"PR #{pr} is CLOSED (not merged). Continuing.")
        elif pr_info.state == "MERGED":
            warn(f"PR #{pr} is already MERGED. Continuing.")

        if no_fetch:
            try:
                pr_clone_commit = pr_module.resolve_pr_head_sha(cfg.repo_root, pr_info)
            except pr_module.PrError as e:
                error(str(e))
                raise typer.Exit(2) from e
            info(
                f"PR #{pr} '{pr_info.head_ref}' at {pr_clone_commit[:12]} "
                f"(--no-fetch: using the host's copy)"
            )
        else:
            try:
                fetch_result = pr_module.fetch_pr_head(
                    cfg.repo_root, pr_info, remote=cfg.upstream_remote
                )
            except pr_module.PrError as e:
                error(str(e))
                raise typer.Exit(2) from e
            pr_clone_commit = fetch_result.new_sha
            if fetch_result.updated:
                info(f"PR #{pr} '{pr_info.head_ref}' fetched ({fetch_result.new_sha[:12]})")
            else:
                info(f"PR #{pr} '{pr_info.head_ref}' already at {fetch_result.new_sha[:12]}")

            # Refresh the PR's base branch too. `new_container` seeds the
            # container's `refs/jailbee/base/<base>` from the host's
            # `refs/remotes/origin/<base>`, and a stale tip there inflates
            # `jailbee ls` AHEAD by every base-branch commit made since the host
            # last fetched. Best-effort: a base branch deleted upstream must
            # not block the container.
            base_sha = pr_module.fetch_base_ref(
                cfg.repo_root, pr_info.base_ref, remote=cfg.upstream_remote
            )
            if base_sha is None:
                warn(
                    f"Could not fetch PR base branch '{pr_info.base_ref}' from origin; "
                    f"AHEAD numbers in `jailbee ls` may be inflated."
                )
            else:
                info(f"PR #{pr} base '{pr_info.base_ref}' at {base_sha[:12]}")

        container_branch = pr_info.head_ref

    if current:
        if base is not None:
            error(
                "--current and the BASE positional are mutually exclusive "
                "(--current already designates the base)."
            )
            raise typer.Exit(2)
        resolved = get_current_branch(cfg.repo_root)
        if resolved is None:
            error(
                f"Cannot resolve --current: repo at {cfg.repo_root} is in detached HEAD "
                "or git is unavailable."
            )
            raise typer.Exit(2)
        if container_branch is None:
            container_branch = resolved
        else:
            base = resolved
    elif container_branch is None:
        if mount:
            error(
                "Missing NAME argument. In --mount mode, provide a container "
                "name (e.g. `jailbee new mysmoke --mount`)."
            )
        else:
            error("Missing NAME argument. Provide a name to work on or use --current.")
        raise typer.Exit(2)

    if mount:
        if base is not None:
            error(
                "--base is for clone mode (creating a branch off an existing one); "
                "not applicable with --mount."
            )
            raise typer.Exit(2)
        if no_clone:
            error("--mount already skips cloning; --no-clone is redundant.")
            raise typer.Exit(2)
        if current:
            error("--current is a clone-mode concept; not applicable with --mount.")
            raise typer.Exit(2)
        # The positional was resolved above; in mount mode it is the
        # container name directly (never a branch). Reject slashes so users
        # don't accidentally pass a branch-style name.
        assert container_branch is not None
        if "/" in container_branch:
            error(
                f"in mount mode the positional is a container name; got '{container_branch}'. "
                "Use e.g. 'feat-foo' instead of 'feat/foo'."
            )
            raise typer.Exit(2)
    else:
        if not (cfg.repo_root / ".git").exists():
            error(
                f"host path is not a git repo: {cfg.repo_root}. "
                "Use `jailbee new <name> --mount` to create a mount-mode container "
                "instead."
            )
            raise typer.Exit(2)

    # If the user typed `jailbee new <name>` and a branch of that name already
    # exists in the source repo, the container checks it out instead of
    # branching. Make that visible and confirm — `-y` skips the prompt for
    # scripted use. With a base the branch is still reused (the base becomes
    # the container's base branch, not a fork point), so the prompt names the
    # base rather than suggesting one. In `--pr` mode the head was just
    # fetched on purpose, so the prompt would always fire redundantly.
    if not mount and not no_clone and pr is None:
        from jailbee.git import branch_exists_in_source

        assert container_branch is not None
        if branch_exists_in_source(cfg.repo_root, cfg.upstream_remote, container_branch):
            info(f"Branch '{container_branch}' already exists in source repo.")
            question = (
                f"Use existing branch '{container_branch}'?"
                if base is None
                else f"Use existing branch '{container_branch}' with base '{base}'?"
            )
            if not yes and not typer.confirm(question, default=True):
                hint = (
                    "Aborted. Pick a different name, or pass a base branch: "
                    f"'jailbee new <newname> {container_branch}'."
                    if base is None
                    else f"Aborted. Pick a different name to branch off '{base}' instead."
                )
                info(hint)
                raise typer.Exit(0)

    incus = Incus()

    gcfg = _load_global()

    net_mode = network or cfg.defaults.network

    mirror_endpoint: tuple[str, int] | None = None
    mirror_ca_path: Path | None = None
    if mirror_wanted(cfg, gcfg):
        from jailbee.docker_daemon import compute_mirror_endpoint

        problem: str | None = None
        try:
            mirror_endpoint = compute_mirror_endpoint(incus, gcfg)
        except ValueError as e:
            problem = str(e)
        else:
            ca = gcfg.docker_registry_mirror.data_dir / "ca" / "ca.crt"
            if ca.is_file():
                mirror_ca_path = ca
            else:
                problem = (
                    f"Mirror CA cert not found at {ca}. "
                    f"Run 'jailbee registry up' and wait for the proxy to start."
                )
        if problem is not None:
            # Half a mirror is not a mirror: without the CA the container
            # cannot trust the proxy, so drop the endpoint too and treat both
            # failures identically.
            mirror_endpoint = None
            if net_mode == "strict":
                error(problem)
                raise typer.Exit(1)
            warn(
                f"{problem} Continuing without it — in loose mode the mirror is "
                f"only a pull cache. Enable it later with "
                f"'jailbee registry up && jailbee apply'."
            )

    if mount:
        full_name = name or f"{cfg.container_prefix}-{container_branch}"
        opts = NewContainerOptions(
            container_branch="",
            name=full_name,
            network=net_mode,
            memory=memory or cfg.defaults.memory,
            cpu=cpu or cfg.defaults.cpu,
            from_base=from_base or cfg.golden.alias,
            clone=False,
            autostart=not no_autostart,
            mirror_endpoint=mirror_endpoint,
            mirror_ca_path=mirror_ca_path,
            base=None,
            mount=True,
            assume_yes=yes,
        )
    else:
        opts = NewContainerOptions(
            container_branch=container_branch,
            name=name,
            network=net_mode,
            memory=memory or cfg.defaults.memory,
            cpu=cpu or cfg.defaults.cpu,
            from_base=from_base or cfg.golden.alias,
            clone=not no_clone,
            autostart=not no_autostart,
            mirror_endpoint=mirror_endpoint,
            mirror_ca_path=mirror_ca_path,
            base=base,
            base_branch_label=pr_info.base_ref if pr is not None else None,
            pr=pr,
            # A fork's head, not merely "a PR": an internal PR's head is a
            # branch in this repo's own origin.
            untrusted_head=pr is not None and pr_info.is_cross_repository,
            clone_commit=pr_clone_commit,
            assume_yes=yes,
        )

    # Register this repo with the refresh timer and resolve the pool
    # *before* creating the container so that the new container's
    # first /etc/hosts pin (done inside new_container) sees fresh IPs.
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import egress_pool
    from jailbee.db import get_engine

    with Session(get_engine()) as session:
        egress_pool.register_repo(session, cfg)
        refresh_result = egress_pool.refresh_pool(
            cfg,
            gcfg,
            incus,
            session,
            now=datetime.now(UTC),
        )
    if refresh_result.status == "dns_error":
        error(f"Initial egress pool resolution failed: {refresh_result.error}")
        raise typer.Exit(1)
    if refresh_result.status == "partial":
        warn(f"Some egress hostnames failed to resolve: {refresh_result.error}")

    if run_in_background:
        from datetime import datetime as _dt

        from jailbee.lifecycle import derive_container_name

        container_name = opts.name or derive_container_name(cfg, opts.container_branch)
        if incus.exists(container_name):
            error(f"Container '{short_name(cfg, container_name)}' already exists")
            raise typer.Exit(2)

        opts = _preflight_background_new(cfg, opts)

        from jailbee import background as bg
        from jailbee.db import get_engine, state_dir

        log_dir = state_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
        log_path = log_dir / f"{container_name}-{stamp}.log"

        job = bg.op_to_job(opts, container_name=container_name, log_path=str(log_path))
        job_file = log_dir / f"{container_name}-{stamp}.job.json"
        job_file.write_text(json.dumps(job))

        worker_argv = [
            sys.executable,
            "-m",
            "jailbee",
            "_new-worker",
            "--job",
            str(job_file),
            "--config",
            str(_resolve_config_path(config)),
        ]
        # Deliberately not closed here: the handle is inherited by the
        # detached child as its stdout/stderr; the parent returns immediately
        # and the fd is released on exit.
        log_fh = open(log_path, "w")
        proc = subprocess.Popen(
            worker_argv,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            cwd=str(cfg.repo_root),
        )

        # Guarded like the worker's own writes, and for the same reason with
        # one twist: the worker is already running by now, so a failed insert
        # must cost the user only `jailbee ls`'s JOB column — never the
        # container that is being built behind it.
        _track_job(
            _job_engine(),
            lambda s: bg.start_job(
                s,
                container_name=container_name,
                container_prefix=cfg.container_prefix,
                branch=None if opts.mount else opts.container_branch,
                pid=proc.pid,
                log_path=str(log_path),
                now=_dt.now().astimezone(),
            ),
            "record the new job row",
        )

        success(
            f"🌱 '{short_name(cfg, container_name)}' is being created in the background "
            f"(pid {proc.pid}). Track with: jailbee ls — log: {log_path}"
        )
        return

    try:
        created = new_container(cfg, incus, opts)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(2) from e
    except AutostartStepError as e:
        # Container was created (and may already be running); the autostart
        # itself failed. The container is left intact so the user can attach
        # and debug — destroy explicitly if they don't want it.
        error(str(e))
        raise typer.Exit(1) from e

    success(f"Container '{short_name(cfg, created)}' created and started")

    _finalize_new(cfg, incus, created, launch_gui=not no_autostart)

    if attach_mode == "tmux":
        raise typer.Exit(_attach_tmux(cfg, incus, created))
    if attach_mode == "shell":
        raise typer.Exit(_attach_shell(cfg, incus, created))


def _preflight_background_new(
    cfg: "Config",
    opts: "NewContainerOptions",
) -> "NewContainerOptions":
    """Do the asking a detached worker cannot, before there is one.

    A background `jailbee new` hands provisioning to a worker with no stdin. Any
    confirmation left for that worker can only ever be answered "no", and the
    user finds out from a log file after the job has already failed — which is
    how a checkout lagging `origin/<default_branch>` used to break `jailbee new`
    outright. So the host-side work that can raise a question happens here, in
    the terminal the user is still sitting at: resolve the clone ref (fetching
    if configured), read the target branch's autostart, report both
    comparisons, and ask about a genuine privilege widening.

    Returns `opts` carrying what was settled: the accepted ref (so the worker
    does not ask again — nor treat the answer as covering a commit that landed
    in the meantime) and `autofetch_done`, so the fetch is not repeated.
    Exits 2 on decline, having created nothing and recorded no job.
    """
    from dataclasses import replace

    from jailbee.lifecycle import (
        _stdin_is_interactive,
        assess_branch_autostart,
        resolve_clone_ref,
    )
    from jailbee.tui import default_confirm

    try:
        clone_ref = resolve_clone_ref(cfg, opts, autofetch=cfg.new.autofetch)
        assessment = assess_branch_autostart(cfg, opts, clone_ref)
    except ValueError as e:
        # Same failures `new_container` raises for a bad ref or a failed
        # fetch — better here, in the terminal, than in a worker log.
        error(str(e))
        raise typer.Exit(2) from e

    verdict = assessment.verdict
    approved_ref: str | None = None
    if verdict is not None and verdict.prompts and not opts.assume_yes:
        if not _stdin_is_interactive():
            error(
                "The target branch's autostart config widens privileges beyond "
                f"{verdict.baseline_source}, which needs confirmation — and there is "
                "no terminal to ask on. Re-run with --yes to accept it, or edit the "
                "branch's .jailbee/config.yaml."
            )
            raise typer.Exit(2)
        if not default_confirm("Provision with the branch's widened privileges?"):
            error("Aborted: declined the target branch's autostart config. Nothing was created.")
            raise typer.Exit(2)
        approved_ref = assessment.ref

    return replace(opts, approved_autostart_ref=approved_ref, autofetch_done=True)


def _finalize_new(
    cfg: "Config",
    incus: "IncusType",
    created: str,
    *,
    launch_gui: bool,
) -> None:
    """Post-create steps shared by the synchronous path and the worker:
    launch IDE/Chrome if configured. Does NOT attach a shell/tmux —
    callers handle that. (The PR label is persisted inside `new_container`,
    before autostart, so it survives an autostart failure — not here.)
    """
    from jailbee.autostart import has_graphical_session, maybe_warn_no_gui

    if launch_gui:
        launch_ide = cfg.jetbrains.enabled and cfg.jetbrains.autostart
        launch_chrome = cfg.chrome.enabled and cfg.chrome.autostart
        if launch_ide or launch_chrome:
            if not has_graphical_session():
                maybe_warn_no_gui()
            else:
                from jailbee.gui import open_chrome, open_ide

                if launch_ide:
                    open_ide(cfg, incus, created, cfg.jetbrains.ide)
                if launch_chrome:
                    open_chrome(cfg, incus, created, cfg.chrome.url)


@app.command("_new-worker", hidden=True)
def _new_worker(
    job: Annotated[Path, typer.Option("--job", help="Path to the JSON job file.")],
    config: ConfigOption = None,
) -> None:
    """Internal: run a `jailbee new` operation detached, tracking phase in SQLite.

    Spawned by `new_cmd` when background mode is active. Not for direct use.
    """
    import json
    import traceback

    from jailbee import background
    from jailbee.incus import Incus
    from jailbee.lifecycle import new_container

    cfg = _load_or_exit(config)
    incus = Incus()

    data = json.loads(job.read_text())
    opts, container_name, _log_path = background.job_to_opts(data)

    engine = _job_engine()

    def _on_phase(phase: str) -> None:
        _track_job(
            engine,
            lambda s: background.set_phase(s, container_name, phase, now=_now()),
            f"record phase '{phase}'",
        )

    try:
        created = new_container(
            cfg,
            incus,
            opts,
            on_phase=_on_phase,
            # Detached worker: there is no stdin to prompt on, so decline
            # rather than block. A backstop, not the mechanism — anything
            # answerable was already answered by `_preflight_background_new`
            # and carried here in `assume_yes` / `approved_autostart_ref`.
            # Reaching it means the branch moved between the two, and the
            # failure names that.
            confirm_fn=lambda _msg: False,
        )
        _finalize_new(cfg, incus, created, launch_gui=opts.autostart)
    except Exception as e:
        traceback.print_exc()
        # `msg`, not `str(e)` inside the lambda: `e` is unbound once the
        # except block ends, and a closure over it would be a trap for the
        # next reader even though the call happens inside the block.
        msg = str(e)
        _track_job(
            engine,
            lambda s: background.fail_job(s, container_name, msg, now=_now()),
            "mark the job failed",
        )
        raise typer.Exit(1) from e

    _track_job(
        engine,
        lambda s: background.delete_job(s, container_name),
        "clear the finished job row",
    )


@app.command("_destroy-worker", hidden=True)
def _destroy_worker(
    name: Annotated[str, typer.Option("--name", help="Full container name to destroy.")],
    force: Annotated[bool, typer.Option("--force")] = False,
    config: ConfigOption = None,
) -> None:
    """Internal: destroy a container detached, tracking phase in SQLite.

    Spawned by `destroy` when background mode is active. Not for direct use.
    """
    import traceback

    from jailbee import background
    from jailbee.incus import Incus
    from jailbee.lifecycle import destroy_container

    cfg = _load_or_exit(config)
    incus = Incus()
    engine = _job_engine()

    def _on_phase(phase: str) -> None:
        _track_job(
            engine,
            lambda s: background.set_phase(s, name, phase, now=_now()),
            f"record phase '{phase}'",
        )

    try:
        destroy_container(cfg, incus, name, force=force, on_phase=_on_phase)
    except Exception as e:
        traceback.print_exc()
        msg = str(e)  # see `_new_worker`: `e` is unbound after the block
        _track_job(
            engine,
            lambda s: background.fail_job(s, name, msg, now=_now()),
            "mark the job failed",
        )
        raise typer.Exit(1) from e

    _track_job(
        engine,
        lambda s: background.delete_job(s, name),
        "clear the finished job row",
    )


@app.command("_boot-worker", hidden=True)
def _boot_worker(
    name: Annotated[str, typer.Option("--name", help="Full container name to boot.")],
    restart: Annotated[
        bool,
        typer.Option("--restart", help="Reboot a running container instead of starting it."),
    ] = False,
    no_autostart: Annotated[bool, typer.Option("--no-autostart")] = False,
    config: ConfigOption = None,
) -> None:
    """Internal: boot a container detached, tracking phase in SQLite.

    Spawned by `start` / `restart` when background mode is active. Not for
    direct use.
    """
    import traceback

    from jailbee import background
    from jailbee.incus import Incus
    from jailbee.lifecycle import boot_container

    cfg = _load_or_exit(config)
    incus = Incus()
    engine = _job_engine()

    try:
        boot_container(cfg, incus, name, restart=restart)
        # Only once the container is up, and only when there is an autostart
        # run to report: with --no-autostart the rest is a hosts pin and a
        # token write, and `starting` stays the honest phase for those.
        if not no_autostart:
            _track_job(
                engine,
                lambda s: background.set_phase(s, name, background.PHASE_AUTOSTART, now=_now()),
                f"record phase '{background.PHASE_AUTOSTART}'",
            )
        _post_start_actions(cfg, incus, name, no_autostart=no_autostart)
    except typer.Exit:
        # `_post_start_actions` reports an autostart failure itself and exits.
        # Its `str()` is the exit code, which would leave the row saying "1",
        # so point at the log that does carry the detail.
        _track_job(
            engine,
            lambda s: background.fail_job(s, name, "boot failed — see the worker log", now=_now()),
            "mark the job failed",
        )
        raise
    except Exception as e:
        traceback.print_exc()
        msg = str(e)  # see `_new_worker`: `e` is unbound after the block
        _track_job(
            engine,
            lambda s: background.fail_job(s, name, msg, now=_now()),
            "mark the job failed",
        )
        raise typer.Exit(1) from e

    _track_job(
        engine,
        lambda s: background.delete_job(s, name),
        "clear the finished job row",
    )


def _attach_shell(cfg: "Config", incus: "IncusType", name: str, user: str = "dev") -> int:
    """Open an interactive shell as ``user`` in the running container.

    Lands in the in-container clone (``/home/dev/<repo>``) by default.
    If the clone doesn't exist (e.g. ``jailbee new --no-clone``), falls back
    to the user's home dir so the shell still opens.
    """
    import shlex

    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir

    repo_dir = container_repo_dir(cfg, incus, name)
    # `cd ... 2>/dev/null` keeps the shell usable when the clone is
    # missing — `;` (not `&&`) means `exec bash -l` always runs.
    inner = f"cd {shlex.quote(repo_dir)} 2>/dev/null; exec bash -l"
    # Route through `incus exec --user` instead of wrapping in `sudo -i`:
    # sudo's login mode wipes env vars not in sudoers' env_keep, which
    # silently drops user-defined `container.env` entries set on the
    # base profile. `incus exec --user` preserves them.
    if user == "root":
        uid, gid, home, username = 0, 0, "/root", "root"
    else:
        uid = cfg.container_user.uid
        gid = cfg.container_user.gid
        home = f"/home/{CONTAINER_USERNAME}"
        username = CONTAINER_USERNAME
    # `incus exec --user UID` does not derive USER/LOGNAME from /etc/passwd;
    # PAM/sudo would normally set them. Set them explicitly so prompt
    # frameworks and other tools that read $USER behave correctly.
    return incus.exec_interactive(
        name,
        ["bash", "-c", inner],
        uid=uid,
        gid=gid,
        env={"HOME": home, "USER": username, "LOGNAME": username},
        # Load supplementary groups (kvm, docker, …) so group-based device
        # access works in the shell. `incus exec --user/--group` alone does
        # not initgroups; see Incus.exec_interactive / device_groups.
        init_groups=True,
    )


def _attach_tmux(cfg: "Config", incus: "IncusType", name: str) -> int:
    """Attach to the autostart tmux session, creating it on demand."""
    from jailbee.autostart import agent_autostart_steps
    from jailbee.config import CONTAINER_USERNAME
    from jailbee.lifecycle import container_repo_dir
    from jailbee.tmux import SESSION_NAME, ensure_session, select_window

    ensure_session(incus, name, start_dir=container_repo_dir(cfg, incus, name))
    steps = agent_autostart_steps(cfg)
    if steps:
        # Best-effort: the window may have died; fall through to the
        # default focus rather than blocking the attach.
        select_window(incus, name, steps[-1].name)
    # See `_attach_shell` for why we route through `incus exec --user`
    # instead of `sudo -i`.
    return incus.exec_interactive(
        name,
        ["tmux", "attach", "-t", SESSION_NAME],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
        env={
            "HOME": f"/home/{CONTAINER_USERNAME}",
            "USER": CONTAINER_USERNAME,
            "LOGNAME": CONTAINER_USERNAME,
        },
        init_groups=True,
    )


@app.command()
def shell(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    user: Annotated[
        str,
        typer.Option(
            "--user",
            "-u",
            help="dev or root",
            autocompletion=completion.complete_choices("dev", "root"),
        ),
    ] = "dev",
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Don't ask for confirmation when the container's background "
            "job failed or is still unfinished — attach straight away.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Open an interactive shell in the container.

    Lands in the in-container clone (``/home/dev/<repo>``) by default.
    If the clone doesn't exist (e.g. ``jailbee new --no-clone``), falls back
    to the user's home dir so the shell still opens.
    """
    if user not in ("dev", "root"):
        error(f"Invalid user: {user} (must be 'dev' or 'root')")
        raise typer.Exit(2)

    cfg = _load_or_exit(config)
    _advise_upgrade(cfg)
    _advise_setup()
    incus, name = _resolve_attachable(cfg, name, force=force, attach_cmd="shell")
    raise typer.Exit(_attach_shell(cfg, incus, name, user))


@app.command()
def tmux(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Don't ask for confirmation when the container's background "
            "job failed or is still unfinished — attach straight away.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Attach to the autostart tmux session inside the container."""
    cfg = _load_or_exit(config)
    incus, name = _resolve_attachable(cfg, name, force=force, attach_cmd="tmux")
    raise typer.Exit(_attach_tmux(cfg, incus, name))


@app.command("dashboard")
def dashboard_cmd(
    interval: Annotated[
        float | None, typer.Option("--interval", "-i", help="Base-state refresh seconds.")
    ] = None,
    git_interval: Annotated[
        float, typer.Option("--git-interval", help="Git-status refresh seconds.")
    ] = 10.0,
    no_git: Annotated[bool, typer.Option("--no-git", help="Disable git-status probing.")] = False,
    gui: Annotated[
        bool, typer.Option("--gui", help="Launch the graphical (Qt) dashboard.")
    ] = False,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground",
            help="Run the GUI attached to this terminal instead of detaching to the background.",
        ),
    ] = False,
) -> None:
    """Live, auto-refreshing view of jailbee containers across all repos.

    Reads every RegisteredRepo plus the current directory's repo (if any),
    grouped by repo. In the TUI: ↑/↓ to move, Enter for the action menu, r to
    refresh, q to quit. Pass --gui (or run `jailbee gui`) for the Qt desktop app.
    """
    raise typer.Exit(
        _run_dashboard(
            interval=interval,
            git_interval=git_interval,
            no_git=no_git,
            gui=gui,
            foreground=foreground,
        )
    )


@app.command("gui")
def gui_cmd(
    interval: Annotated[
        float | None, typer.Option("--interval", "-i", help="Base-state refresh seconds.")
    ] = None,
    git_interval: Annotated[
        float, typer.Option("--git-interval", help="Git-status refresh seconds.")
    ] = 10.0,
    no_git: Annotated[bool, typer.Option("--no-git", help="Disable git-status probing.")] = False,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground",
            help="Run the GUI attached to this terminal instead of detaching to the background.",
        ),
    ] = False,
) -> None:
    """Launch the graphical (Qt) dashboard — alias for `jailbee dashboard --gui`."""
    raise typer.Exit(
        _run_dashboard(
            interval=interval,
            git_interval=git_interval,
            no_git=no_git,
            gui=True,
            foreground=foreground,
        )
    )


def _run_dashboard(
    *, interval: float | None, git_interval: float, no_git: bool, gui: bool, foreground: bool
) -> int:
    """Shared dispatch for `dashboard` and `gui`: pick the TUI or Qt frontend."""
    from jailbee.config import ConfigNotFoundError
    from jailbee.incus import Incus

    try:
        cwd_config: Path | None = find_repo_config()
    except ConfigNotFoundError:
        cwd_config = None

    if gui:
        try:
            import jailbee.qtui.app as qtui_app
        except ImportError:
            # error_plain, not error: Rich reads `[gui]` as a style tag and
            # drops it, so the command printed would be `install 'jailbee'`.
            error_plain(
                "The graphical dashboard requires PySide6 (the optional 'gui' extra).\n"
                "Install it with:  uv tool install 'jailbee[gui]'\n"
                "(or:  pipx install 'jailbee[gui]')\n"
                "From a jailbee repo checkout:  make install"
            )
            return 1

        if foreground:
            return qtui_app.run(
                Incus(),
                cwd_config=cwd_config,
                interval=interval,
                git_interval=git_interval,
                no_git=no_git,
            )

        if qtui_app.preflight(cwd_config) is None:
            error("No repos registered and no .jailbee/config.yaml in the current directory.")
            return 1

        log_path = "/tmp/jailbee-gui.log"
        child_argv = [
            sys.executable,
            "-m",
            "jailbee",
            "gui",
            "--foreground",
            "--git-interval",
            str(git_interval),
        ]
        if interval is not None:
            child_argv += ["--interval", str(interval)]
        if no_git:
            child_argv.append("--no-git")
        with open(log_path, "ab") as logf:
            subprocess.Popen(
                child_argv,
                stdin=subprocess.DEVNULL,
                stdout=logf,
                stderr=logf,
                start_new_session=True,
            )
        info(f"Launched jailbee dashboard GUI in the background (logs: {log_path}).")
        return 0

    from jailbee import dashboard

    return dashboard.run(
        Incus(),
        cwd_config=cwd_config,
        interval=interval if interval is not None else 3.0,
        git_interval=git_interval,
        no_git=no_git,
    )


if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.engine import Engine
    from sqlmodel import Session

    from jailbee import claude_pool
    from jailbee.background import ClearOutcome
    from jailbee.config import Config, LooseAutoRevert
    from jailbee.db.models import BackgroundJob
    from jailbee.incus import Incus as IncusType
    from jailbee.lifecycle import ContainerInfo, NewContainerOptions, ResolvedContainer
    from jailbee.pr_flow import PrState
    from jailbee.submodule_pr import SubCandidate
    from jailbee.sync import (
        BridgePlan,
        FetchResult,
        LocalBranchUpdate,
        PublishResult,
        PushResult,
        SourcePref,
    )


def _resolve_existing(
    cfg: "Config",
    name: str | None,
) -> tuple["IncusType", str]:
    """Resolve a container name, prompting interactively if omitted.

    See lifecycle.resolve_container_for_interactive for the behavior
    matrix. ValueError is translated to typer.Exit(1).
    """
    from jailbee.incus import Incus
    from jailbee.lifecycle import resolve_container_for_interactive

    incus = Incus()
    try:
        resolved = resolve_container_for_interactive(cfg, incus, name)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from e
    return incus, resolved


def _resolve_existing_detailed(
    cfg: "Config",
    name: str | None,
) -> tuple["IncusType", "ResolvedContainer"]:
    """Like :func:`_resolve_existing`, but reports how the container was chosen.

    Used by the bridge commands that confirm an auto-selected target; see
    :func:`_should_show_plan`.
    """
    from jailbee.incus import Incus
    from jailbee.lifecycle import resolve_container_for_interactive_detailed

    incus = Incus()
    try:
        resolved = resolve_container_for_interactive_detailed(cfg, incus, name)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from e
    return incus, resolved


def _confirm_attach(*, force: bool) -> bool:
    """Whether to attach over a background job that failed or is unfinished.

    Defaults to yes: the user typed ``jailbee shell``/``tmux`` precisely to
    look at a container that misbehaved, and looking changes nothing. The
    question exists only so the warning above it is read before ``tmux
    attach`` takes over the screen.

    ``force`` is an explicit "don't ask". A non-interactive stdin answers the
    same way, because :func:`typer.confirm` would read EOF there and abort an
    attach the caller explicitly requested.
    """
    if force or not sys.stdin.isatty():
        return True
    return typer.confirm("Continue anyway?", default=True)


def _resolve_attachable(
    cfg: "Config",
    name: str | None,
    *,
    force: bool = False,
    attach_cmd: str = "shell",
) -> tuple["IncusType", str]:
    """Resolve a container for an attach command, waiting for in-flight ops.

    Like :func:`_resolve_existing`, but in-flight ``jailbee new --background``
    containers resolve by name and appear in the picker; once resolved, the
    call blocks on a spinner until the background op finishes.

    A broken job never blocks the attach on its own. When the wait ends badly
    but the container is up — the common autostart-failure case, where only
    the ``failed`` job row stands between the user and the tmux window they
    want to read — the failure is reported and :func:`_confirm_attach` asks
    whether to go in anyway (default yes; ``force`` skips the question).

    Ctrl-C out of the wait gets a similar offer, on stricter terms: an
    unfinished container is still worth looking inside, but the interrupt is
    an explicit cancel, so the question is asked even under ``force``,
    defaults to no, and is not asked at all without a TTY.

    Exits 1 without asking when attaching cannot help: no container exists
    yet (a create that died before ``incus init``), or a destroy is actively
    tearing this one down.
    """
    from jailbee import background
    from jailbee.db.models import JOB_BOOT
    from jailbee.incus import Incus
    from jailbee.lifecycle import (
        lookup_background_job,
        resolve_container_for_interactive,
        short_name,
        wait_for_background_ready,
    )
    from jailbee.tui import console

    incus = Incus()
    try:
        resolved = resolve_container_for_interactive(cfg, incus, name, with_background=True)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from e

    short = short_name(cfg, resolved)

    try:
        with console.status(f"⏳ waiting for '{short}' …") as status:
            wait_for_background_ready(
                cfg,
                resolved,
                on_phase=lambda p: status.update(f"⏳ waiting for '{short}' — {p}…"),
            )
    except KeyboardInterrupt:
        if not incus.exists(resolved):
            warn(f"'{short}' is still building in the background; check `jailbee ls`.")
            raise typer.Exit(1) from None
        # A boot job's container was already there; only its post-boot setup
        # is unfinished. Saying "created" about one would be a lie the user
        # could act on (e.g. by destroying it).
        row = lookup_background_job(cfg, resolved)
        doing = "booting" if row is not None and row.op_kind == JOB_BOOT else "being created"
        warn(f"'{short}' is still {doing} in the background — its setup is unfinished.")
        # Ctrl-C is an explicit cancel, so neither `force` nor a missing TTY
        # answers this one on the user's behalf: offer the unfinished
        # container only when someone is at the keyboard to accept it, and
        # default to no, since they just interrupted.
        if not (sys.stdin.isatty() and typer.confirm("Attach anyway?", default=False)):
            raise typer.Exit(1) from None
    except ValueError as e:
        # A dead job (terminal phase, or a worker that vanished) over a
        # container that exists is recoverable by looking inside it. A live
        # destroy job is not — nor is a create that never got as far as a
        # container.
        row = lookup_background_job(cfg, resolved)
        dead_job = row is not None and background.clearable(row.phase, row.pid)
        if not (dead_job and incus.exists(resolved)):
            error(str(e))
            if dead_job:
                info(f"  Nothing was created; clear the job record: jailbee job clear {short}")
            raise typer.Exit(1) from e
        warn(str(e))
        info(f"  The container itself is up — 'jailbee {attach_cmd}' can still reach it.")
        info(f"  Once you're done, clear the stale job record: jailbee job clear {short}")
        if not _confirm_attach(force=force):
            raise typer.Exit(1) from e
    return incus, resolved


def _print_fetch_summary(cfg: "Config", short: str, result: "FetchResult") -> None:
    """Print the same summary `jailbee git fetch` prints, reused by checkout/pull."""
    from jailbee import git as git_helpers

    ref = f"refs/jailbee/{short}/{result.branch}"
    if result.commits_added == 0:
        info(f"{ref}: already up to date ({result.new_oid[:7]}).")
        return

    if result.old_oid is None:
        if result.base_oid is not None:
            info(
                f"{ref}: new ref at {result.new_oid[:7]} "
                f"({result.commits_added} commit(s) ahead of HEAD)."
            )
            range_spec = f"{result.base_oid}..{result.new_oid}"
        else:
            info(f"{ref}: fetched {result.commits_added} commit(s).")
            range_spec = result.new_oid
    else:
        info(
            f"{ref}: {result.old_oid[:7]}..{result.new_oid[:7]} "
            f"({result.commits_added} new commits)"
        )
        range_spec = f"{result.old_oid}..{result.new_oid}"

    for line in git_helpers.log_oneline(cfg.repo_root, range_spec):
        info(f"  {line}")


def _print_publish_progress(cfg: "Config", short: str, publish: "PublishResult") -> None:
    """Report the container fetch, then announce the push about to run.

    Wired into `sync.publish_branch_from_container` as its `on_before_push`
    hook, so everything `jailbee pr` did before the push is on screen before the
    push starts. `git push` inherits its output and prints nothing until the
    remote answers, so without this the terminal's last line is git's own fetch
    output and a push waiting on remote authentication looks like a hung fetch.
    """
    _print_fetch_summary(cfg, short, publish.fetch)
    if publish.dirty:
        warn(f"Container '{short}' has uncommitted changes — they are NOT included in the PR.")
    info(f"Pushing '{publish.publish_name}' to {cfg.upstream_remote}…")


def _post_start_actions(
    cfg: "Config", incus: "IncusType", name: str, *, no_autostart: bool
) -> None:
    """Shared post-boot flow for `start` and `restart`.

    Re-pins /etc/hosts (strict mode), runs autostart with the ON_START
    trigger, and launches Chrome / the IDE if a graphical session is
    available. Caller is responsible for the actual container boot and
    for re-attaching /run/user/<uid> devices beforehand.
    """
    from jailbee.autostart import (
        AutostartStepError,
        AutostartTrigger,
        has_graphical_session,
        inject_github_token,
        maybe_warn_no_gui,
        run_autostart,
    )
    from jailbee.lifecycle import container_repo_dir, current_network_mode

    mirror_endpoint = _mirror_endpoint_or_none(cfg, incus)
    if current_network_mode(cfg, incus, name) == "strict":
        from jailbee.hosts import apply_hosts

        apply_hosts(cfg, incus, name, mirror_endpoint=mirror_endpoint)

    repo_dir = container_repo_dir(cfg, incus, name)

    # GH_TOKEN injection is infrastructure, not a user autostart command —
    # write it before the --no-autostart guard so `gh` works after a plain
    # `jailbee start --no-autostart` (and to pick up a rotated PAT on every boot).
    inject_github_token(cfg, incus, name, repo_dir, mirror_endpoint=mirror_endpoint)

    if no_autostart:
        return

    try:
        run_autostart(
            cfg,
            incus,
            name,
            AutostartTrigger.ON_START,
            repo_dir=repo_dir,
            mirror_endpoint=mirror_endpoint,
        )
    except AutostartStepError as e:
        error(str(e))
        raise typer.Exit(1) from e

    launch_ide = cfg.jetbrains.enabled and cfg.jetbrains.autostart
    launch_chrome = cfg.chrome.enabled and cfg.chrome.autostart
    if launch_ide or launch_chrome:
        if not has_graphical_session():
            maybe_warn_no_gui()
        else:
            from jailbee.gui import open_chrome, open_ide

            if launch_ide:
                open_ide(cfg, incus, name, cfg.jetbrains.ide)
            if launch_chrome:
                open_chrome(cfg, incus, name, cfg.chrome.url)


def _clear_superseded_boot_job(cfg: "Config", full_name: str) -> None:
    """Drop a leftover boot job row after a successful foreground boot.

    A `failed` (or worker-gone) row describes a boot that the one just
    completed supersedes. Without this, `jailbee ls` keeps flagging the
    container and the attach guards keep pointing at `jailbee job clear` even
    though it came up seconds ago. The background path needs nothing:
    `start_job` overwrites the row on spawn and the worker deletes it on
    success.

    Two rows are deliberately left alone:

    - Anything that isn't a *boot*. A failed create means the container's
      setup (clone, credential wiring, first autostart) never finished, which
      a reboot doesn't complete, so its row must survive to keep saying so; a
      destroy job is not this command's business either.
    - A *live* row, which belongs to a worker still writing to the container.
      Hence the guarded `clear_job` — also the reason this is safe to reach
      from `_boot_worker`'s own `_post_start_actions` path.
    """
    from jailbee import background as bg
    from jailbee.db.models import JOB_BOOT
    from jailbee.lifecycle import short_name

    cleared: list[str] = []

    def work(session: "Session") -> None:
        row = bg.get_job(session, full_name)
        if row is None or row.op_kind != JOB_BOOT:
            return
        outcome = bg.clear_job(session, full_name)
        if outcome.cleared:
            cleared.append(outcome.reason)

    _track_job(_job_engine(), work, "clear the superseded boot job row")
    if cleared:
        label = "failed" if cleared[0] == "failed" else "stale"
        info(f"Cleared the {label} boot job record for '{short_name(cfg, full_name)}'.")


def _resolve_boot_background(cfg: "Config", *, background: bool, no_background: bool) -> bool:
    """Three-way resolution of the boot commands' background mode.

    Explicit flags win over `boot.background`, and asking for both at once is
    a usage error rather than a silent precedence rule.
    """
    if background and no_background:
        error("--background and --no-background are mutually exclusive.")
        raise typer.Exit(2)
    if no_background:
        return False
    if background:
        return True
    return cfg.boot.background


def _spawn_boot_worker(
    cfg: "Config",
    config_path: Path,
    full_name: str,
    *,
    restart: bool,
    no_autostart: bool,
) -> None:
    """Spawn a detached `_boot-worker` for one container and record its job row.

    Mirrors the `jailbee destroy --background` spawn: the worker inherits a
    log file as stdout/stderr and runs in its own session so it survives the
    parent shell exiting.

    Refuses while another job for this container is still live. Unlike a
    create or a destroy, a boot is not the last thing to happen to the
    container: two workers would race the same reboot and interleave their
    autostart steps.
    """
    from datetime import datetime as _dt

    from jailbee import background as bg
    from jailbee.db import state_dir
    from jailbee.db.models import JOB_BOOT
    from jailbee.lifecycle import lookup_background_job, short_name

    short = short_name(cfg, full_name)
    row = lookup_background_job(cfg, full_name)
    if row is not None and not bg.clearable(row.phase, row.pid):
        error(
            f"'{short}' already has a background job in flight "
            f"({bg.job_label(row.phase, row.pid, kind=row.op_kind)}, pid {row.pid})."
        )
        info("  Wait for it to finish (`jailbee ls`), or run without --background.")
        raise typer.Exit(1)

    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{full_name}-boot-{stamp}.log"

    worker_argv = [
        sys.executable,
        "-m",
        "jailbee",
        "_boot-worker",
        "--name",
        full_name,
        "--config",
        str(config_path),
    ]
    if restart:
        worker_argv.append("--restart")
    if no_autostart:
        worker_argv.append("--no-autostart")

    # Deliberately not closed here: the handle is inherited by the
    # detached child as its stdout/stderr; the parent returns immediately
    # and the fd is released on exit.
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        worker_argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(cfg.repo_root),
    )

    # Guarded like the other background spawns: the worker is already booting
    # the container, so a failed row costs tracking, not the job.
    _track_job(
        _job_engine(),
        lambda s: bg.start_job(
            s,
            container_name=full_name,
            container_prefix=cfg.container_prefix,
            branch=None,
            pid=proc.pid,
            log_path=str(log_path),
            now=_dt.now().astimezone(),
            op_kind=JOB_BOOT,
        ),
        "record the new job row",
    )

    verb = "restarting" if restart else "starting"
    success(
        f"🔁 '{short}' is {verb} in the background (pid {proc.pid}). "
        f"Track with: jailbee ls — log: {log_path}"
    )


@app.command()
def start(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    no_autostart: Annotated[bool, typer.Option("--no-autostart")] = False,
    background: Annotated[
        bool,
        typer.Option(
            "--background",
            "-b",
            help=(
                "Start the container detached in the background and return the "
                "shell immediately. Track progress with `jailbee ls`. "
                "Overrides the `boot.background` config setting."
            ),
        ),
    ] = False,
    no_background: Annotated[
        bool,
        typer.Option(
            "--no-background",
            help="Force a foreground start, overriding `boot.background`.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Start a stopped container, then run autostart."""
    from jailbee.lifecycle import boot_container, short_name

    cfg = _load_or_exit(config)
    run_in_background = _resolve_boot_background(
        cfg, background=background, no_background=no_background
    )
    incus, name = _resolve_existing(cfg, name)
    if run_in_background:
        _spawn_boot_worker(
            cfg,
            _resolve_config_path(config),
            name,
            restart=False,
            no_autostart=no_autostart,
        )
        return
    # boot_container also re-attaches the /run/user/<uid>/* GUI sockets,
    # detaching first so a stale device from a previous boot can't error
    # the attach out. See runtime_mounts.
    boot_container(cfg, incus, name, restart=False)
    success(f"Started: {short_name(cfg, name)}")

    _post_start_actions(cfg, incus, name, no_autostart=no_autostart)
    _clear_superseded_boot_job(cfg, name)


@app.command()
def stop(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    force: Annotated[bool, typer.Option("--force")] = False,
    config: ConfigOption = None,
) -> None:
    """Stop a running container."""
    from jailbee.lifecycle import short_name
    from jailbee.stopping import stop_container

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    # No force fallback here: this container holds the user's work, so a
    # shutdown that will not finish is reported (with what is blocking it)
    # rather than turned into a power cut behind their back.
    stop_container(incus, name, force=force, label=short_name(cfg, name))
    success(f"Stopped: {short_name(cfg, name)}")


@app.command()
def restart(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    no_autostart: Annotated[bool, typer.Option("--no-autostart")] = False,
    background: Annotated[
        bool,
        typer.Option(
            "--background",
            "-b",
            help=(
                "Restart the container detached in the background and return "
                "the shell immediately. Track progress with `jailbee ls`. "
                "Overrides the `boot.background` config setting."
            ),
        ),
    ] = False,
    no_background: Annotated[
        bool,
        typer.Option(
            "--no-background",
            help="Force a foreground restart, overriding `boot.background`.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Restart a container, then run autostart."""
    from jailbee.lifecycle import boot_container, short_name

    cfg = _load_or_exit(config)
    run_in_background = _resolve_boot_background(
        cfg, background=background, no_background=no_background
    )
    incus, name = _resolve_existing(cfg, name)
    if run_in_background:
        _spawn_boot_worker(
            cfg,
            _resolve_config_path(config),
            name,
            restart=True,
            no_autostart=no_autostart,
        )
        return
    boot_container(cfg, incus, name, restart=True)
    success(f"Restarted: {short_name(cfg, name)}")
    _post_start_actions(cfg, incus, name, no_autostart=no_autostart)
    _clear_superseded_boot_job(cfg, name)


def _destroy_batch(cfg: "Config", incus: "IncusType", names: list[str]) -> None:
    """Destroy each container in turn; print a per-row line and a summary.

    Continues past per-container failures so a single broken container
    doesn't strand the rest. Exits with code 1 if at least one destroy
    failed.
    """
    from jailbee.lifecycle import destroy_container, short_name

    successes: list[str] = []
    failures: list[tuple[str, str]] = []
    for n in names:
        short = short_name(cfg, n)
        try:
            destroy_container(cfg, incus, n, force=True)
            successes.append(short)
            success(f"Destroyed: {short}")
        except ValueError as e:
            failures.append((short, str(e)))
            error(f"{short}: {e}")
    if failures:
        info(f"Destroyed {len(successes)} of {len(names)} container(s); {len(failures)} failed.")
        raise typer.Exit(1)


def _spawn_destroy_worker(cfg: "Config", config_path: Path, full_name: str) -> None:
    """Spawn a detached `_destroy-worker` for one container and record its job row.

    Mirrors the `jailbee new --background` spawn: the worker inherits a log
    file as stdout/stderr and runs in its own session so it survives the
    parent shell exiting.
    """
    from datetime import datetime as _dt

    from jailbee import background as bg
    from jailbee.db import state_dir
    from jailbee.db.models import JOB_DESTROY
    from jailbee.lifecycle import short_name

    log_dir = state_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = _dt.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{full_name}-destroy-{stamp}.log"

    worker_argv = [
        sys.executable,
        "-m",
        "jailbee",
        "_destroy-worker",
        "--name",
        full_name,
        "--force",
        "--config",
        str(config_path),
    ]
    # Deliberately not closed here: the handle is inherited by the
    # detached child as its stdout/stderr; the parent returns immediately
    # and the fd is released on exit.
    log_fh = open(log_path, "w")
    proc = subprocess.Popen(
        worker_argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        cwd=str(cfg.repo_root),
    )

    # Guarded like the `new --background` insert above: the worker is already
    # tearing the container down, so a failed row costs tracking, not the job.
    _track_job(
        _job_engine(),
        lambda s: bg.start_job(
            s,
            container_name=full_name,
            container_prefix=cfg.container_prefix,
            branch=None,
            pid=proc.pid,
            log_path=str(log_path),
            now=_dt.now().astimezone(),
            op_kind=JOB_DESTROY,
        ),
        "record the new job row",
    )

    success(
        f"🗑️  '{short_name(cfg, full_name)}' is being destroyed in the background "
        f"(pid {proc.pid}). Track with: jailbee ls — log: {log_path}"
    )


def _prune_orphan_job(cfg: "Config", name: str) -> str | None:
    """Delete a background job row for ``name`` (raw or repo-prefixed) when no
    container exists for it — e.g. a background ``jailbee new`` that failed before
    ``incus init`` ever created the container. Returns the pruned full name,
    or ``None`` if there was no matching row.
    """
    from sqlmodel import Session

    from jailbee import background as bg
    from jailbee.db import get_engine

    candidates = [name, f"{cfg.container_prefix}-{name}"]
    with Session(get_engine()) as session:
        ops = bg.list_jobs(session, cfg.container_prefix)
        for cand in candidates:
            if cand in ops:
                bg.delete_job(session, cand)
                return cand
    return None


def _warn_before_destroy(cfg: "Config", infos: list["ContainerInfo"]) -> bool:
    """Show what destroying ``infos`` would discard; return True to proceed.

    Gather-and-assess step for the CLI's destroy paths, which already hold
    a `ContainerInfo` per container (probed or not). The shared "print the
    summary and confirm" step lives in `tui.confirm_destroy_risk` so every
    destroy path — this one and `jailbee git pull`'s post-merge cleanup in
    `sync.py` — renders "risky to destroy" the same way.
    """
    from jailbee.destroy_guard import assess, status_is_unknown

    unknown = [c.display_name for c in infos if status_is_unknown(c)]
    summaries = [s for c in infos if (s := assess(cfg, c)) is not None]
    return confirm_destroy_risk(unknown, summaries)


@app.command()
def destroy(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    all_: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Destroy every container in this repo (asks for confirmation unless --force).",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force")] = False,
    background: Annotated[
        bool,
        typer.Option(
            "--background",
            "-b",
            help=(
                "Destroy the container(s) detached in the background and return "
                "the shell immediately. Track progress with `jailbee ls`. "
                "Overrides the `destroy.background` config setting."
            ),
        ),
    ] = False,
    no_background: Annotated[
        bool,
        typer.Option(
            "--no-background",
            help="Force foreground destroy, overriding `destroy.background`.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Destroy a container (stops it first if needed).

    With no arguments, opens an interactive checkbox list of this repo's
    containers (TTY required). With ``--all``, destroys every container
    in this repo after a single confirmation.
    """
    from jailbee.incus import Incus
    from jailbee.lifecycle import (
        destroy_container,
        list_containers,
        resolve_container_name,
        short_name,
    )

    cfg = _load_or_exit(config)

    if name is not None and all_:
        error("--all and a container name are mutually exclusive")
        raise typer.Exit(2)

    if background and no_background:
        error("--background and --no-background are mutually exclusive.")
        raise typer.Exit(2)
    if no_background:
        run_in_background = False
    elif background:
        run_in_background = True
    else:
        run_in_background = cfg.destroy.background

    config_path = _resolve_config_path(config)

    incus = Incus()

    if name is not None:
        try:
            resolved = resolve_container_name(cfg, incus, name)
        except ValueError as e:
            # No live container. It may be an orphaned background job row from
            # a background `jailbee new` that failed before the container was
            # created — clear it so `jailbee ls` stops showing the dead job.
            pruned = _prune_orphan_job(cfg, name)
            if pruned is not None:
                success(
                    f"Cleared background job record for '{short_name(cfg, pruned)}' "
                    f"(no container existed)."
                )
                return
            error(str(e))
            raise typer.Exit(1) from e
        if not force:
            if not typer.confirm(f"Destroy container '{short_name(cfg, resolved)}'?"):
                raise typer.Abort()
            # The single-name path never fetched git status; probe just this
            # one container (one `incus exec`, same timeout as `jailbee ls`).
            info_ = next(
                (c for c in list_containers(cfg, incus) if c.name == resolved),
                None,
            )
            if info_ is None:
                # Vanished from the listing between resolve and here — there
                # is nothing to probe, but silence is never safety: say so
                # explicitly rather than skipping the guard outright.
                if not confirm_destroy_risk([short_name(cfg, resolved)], []):
                    raise typer.Abort()
            else:
                from jailbee.git import get_head_sha
                from jailbee.git_status import probe_container_git

                if info_.state == "Running" and info_.mode != "mount" and info_.repo_dir:
                    info_.git_status = probe_container_git(
                        incus,
                        info_.name,
                        info_.repo_dir,
                        info_.base_branch,
                        cfg.default_branch,
                        uid=cfg.container_user.uid,
                        host_head=get_head_sha(cfg.repo_root),
                    )
                if not _warn_before_destroy(cfg, [info_]):
                    raise typer.Abort()
        if run_in_background:
            _spawn_destroy_worker(cfg, config_path, resolved)
            return
        try:
            destroy_container(cfg, incus, resolved, force=True)
        except ValueError as e:
            error(str(e))
            raise typer.Exit(1) from e
        success(f"Destroyed: {short_name(cfg, resolved)}")
        return

    # `--all --force` never reaches `_warn_before_destroy` below (gated on
    # `not force`), so the git-status probe this fetches it for — one
    # `incus exec` per running container — would be pure waste. The
    # interactive picker always needs it regardless of `--force`: its
    # checkbox rows render the git columns.
    containers = list_containers(cfg, incus, with_git_status=not (all_ and force))
    if not containers:
        info("No containers to destroy.")
        return

    if all_:
        targets = [c.name for c in containers]
        if not force:
            shorts = ", ".join(short_name(cfg, n) for n in targets)
            if not typer.confirm(f"Destroy {len(targets)} container(s) ({shorts})?"):
                raise typer.Abort()
            target_set = set(targets)
            if not _warn_before_destroy(cfg, [c for c in containers if c.name in target_set]):
                raise typer.Abort()
        if run_in_background:
            for n in targets:
                _spawn_destroy_worker(cfg, config_path, n)
            return
        _destroy_batch(cfg, incus, targets)
        return

    from jailbee import tui
    from jailbee.lifecycle import _stdin_is_interactive

    if not _stdin_is_interactive():
        error("no container name given; pass a name, use --all, or run interactively in a TTY")
        raise typer.Exit(1)

    chosen = tui.pick_containers_multi(containers)
    if not chosen:  # None (cancel) or [] (no boxes ticked)
        raise typer.Abort()
    chosen_set = set(chosen)
    # Gated on `not force` like the single-name and `--all` paths: `--force`
    # means "don't ask me anything", and the documented contract (see
    # docs/commands.md) is that it skips the risk summary too.
    if not force and not _warn_before_destroy(cfg, [c for c in containers if c.name in chosen_set]):
        raise typer.Abort()
    if run_in_background:
        for n in chosen:
            _spawn_destroy_worker(cfg, config_path, n)
        return
    _destroy_batch(cfg, incus, chosen)


git_app = typer.Typer(
    name="git",
    no_args_is_help=True,
)
app.add_typer(git_app)


@git_app.callback()
def _git_group() -> None:
    """Host <-> container git bridge.

    Move refs between the host repo and a container's clone. Container
    commits land on the host under refs/jailbee/<short>/<branch>; host
    commits land in the container under refs/jailbee/host/<branch>.

    Bring container work back to the host:

      jailbee git fetch feat-foo            # just import refs
      jailbee git checkout feat-foo         # fetch + ff-checkout to host
      jailbee git pull feat-foo             # fetch + merge into HEAD
      jailbee git pull feat-foo --ff        # ff-only, refuse on divergence
      jailbee git pull feat-foo --cleanup   # merge, destroy container, delete branch
      jailbee pr feat-foo                   # push branch to origin + open draft PR

    Send host updates into a container:

      jailbee git push                          # interactive picker (TTY required)
      jailbee git push feat-foo                 # use configured defaults or prompt
      jailbee git push feat-foo --current       # send host's current branch
      jailbee git push feat-foo --merge         # send + 'git merge' in container
      jailbee git push feat-foo --rebase        # send + 'git rebase' in container
      jailbee git push feat-foo --plain         # send refs only (no merge/rebase)
      jailbee git push feat-foo --from main --force   # reset container branch to host ref
      jailbee git push feat-foo --from main     # pick a different host branch
      jailbee git push feat-foo --pr            # refresh PR head from GitHub, then push

    With one container and no name, push/pull/checkout confirm first
    (confirm.auto_target; --no-confirm to skip).

    Defaults (push.default_action, push.default_source) live in
    global.yaml or .jailbee/config.yaml. See `jailbee config show`.
    """


@git_app.command("fetch")
def fetch(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Override branch detection"),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Fetch commits from a container's clone into refs/jailbee/<short>/<branch>.

    Looks up the container's branch from user.jailbee.branch (set at create
    time) or falls back to the clone's HEAD. The container must be running.

    Examples:

      jailbee git fetch feat-foo              # fetch container's branch
      jailbee git fetch feat-foo -b feat/x    # read feat/x from the container
      jailbee git fetch                       # interactive container picker
    """
    from jailbee import git as git_helpers
    from jailbee import sync
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)
    incus, full = _resolve_existing(cfg, name)
    short = short_name(cfg, full)

    try:
        result = sync.fetch_from_container(cfg, incus, short, branch=branch)
    except (sync.SyncError, git_helpers.GitError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    _print_fetch_summary(cfg, short, result)


# Top-level alias — hidden from `jailbee --help`; full docstring inherited from `fetch`.
app.command(
    "fetch",
    hidden=True,
    help="Alias for `jailbee git fetch`. See `jailbee git fetch --help`.",
)(fetch)


@git_app.command("checkout")
def checkout(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="Which branch to read from the container (default: the one it has checked out)",
        ),
    ] = None,
    as_name: Annotated[
        str | None,
        typer.Option(
            "--as",
            help=(
                "Host branch to check out onto (default: the container's branch, "
                "or its PR head branch when the container has one)."
            ),
        ),
    ] = None,
    confirm: Annotated[
        bool | None,
        typer.Option(
            "--confirm/--no-confirm",
            help="Show a plan block and ask before checking out, when jailbee "
            "picked the container itself (single candidate, no name given). "
            "Defaults to confirm.auto_target.",
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Fetch + check out the container's branch on the host (ff-only).

    Creates a new local branch from `refs/jailbee/<short>/<branch>` if absent;
    otherwise fast-forwards the existing one. On divergence, points the
    user at `jailbee git pull`.

    The two branch flags name opposite sides: -b picks what is read *from*
    the container, --as names the branch written *on the host*.

    When jailbee picks the container itself — one eligible container and no name
    given — the checkout is confirmed first: a block naming the container's
    branch, the host branch, both tips and the action, then [Y/n]. Turn it
    off for one run with --no-confirm, or repo-wide with confirm.auto_target.

    Examples:

      jailbee git checkout feat-foo            # ff-checkout to host branch
      jailbee git checkout feat-foo -b feat/x  # read feat/x from the container
      jailbee git checkout feat-foo --as alt   # land it on host branch 'alt'
      jailbee git checkout --no-confirm        # skip the auto-target confirmation
    """
    from jailbee import git as git_helpers
    from jailbee import sync
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)
    incus, resolved = _resolve_existing_detailed(cfg, name)
    full = resolved.name
    short = short_name(cfg, full)

    if _should_show_plan(
        cfg, auto_selected=resolved.auto_selected, flag=confirm
    ) and not _is_mount_mode(incus, full):
        _confirm_plan_if_buildable(
            lambda: sync.plan_checkout(cfg, incus, short, branch=branch, as_name=as_name)
        )

    try:
        result = sync.checkout_from_container(cfg, incus, short, branch=branch, as_name=as_name)
    except (sync.SyncError, git_helpers.GitError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    _print_fetch_summary(cfg, short, result.fetch)
    success(f"Now on '{result.branch}' at {result.head_oid[:7]}.")


def _emit_conflict_report(exc: Exception) -> None:
    """If *exc* is a ``MergeConflictError``, print its submodule report block.

    Shared by the pull and push paths — both resolve submodule gitlinks the
    same way, so both report them the same way.
    """
    from jailbee import sync
    from jailbee.tui import console

    if isinstance(exc, sync.MergeConflictError):
        block = sync.render_submodule_report(conflict=exc.report)
        if block:
            console.print(block)


def _do_single_pull(
    cfg: "Config",
    incus: "IncusType",
    short: str,
    *,
    branch: str | None,
    ff_only: bool,
    into: str | None,
    allow_checkout: bool,
    destroy_policy: Literal["prompt", "always", "never"],
    branch_policy: Literal["prompt", "always", "never"],
) -> None:
    """Run one container's pull + summary + cleanup.

    Raises ``sync.SyncError`` or ``git_helpers.GitError`` on merge
    failure (the caller decides batch behaviour). Cleanup failures are
    printed as warnings and do not raise.
    """
    from jailbee import sync

    result = sync.merge_from_container(
        cfg, incus, short, branch=branch, ff_only=ff_only, into=into, allow_checkout=allow_checkout
    )

    _print_bridge_direction(
        result.branch, "container", result.into_branch or "(detached HEAD)", "host"
    )
    _print_fetch_summary(cfg, short, result.fetch)
    mode_suffix = " (fast-forward)" if ff_only else ""
    success(
        f"Merged '{result.branch}' from container '{short}' into "
        f"'{result.into_branch or '(detached HEAD)'}'{mode_suffix}."
    )
    info(f"HEAD now at {result.head_oid[:7]}.")
    if allow_checkout and result.into_branch:
        info(f"Now on '{result.into_branch}'.")

    cleanup_result = sync.run_post_merge_cleanup(
        cfg,
        incus,
        short,
        result,
        destroy_policy=destroy_policy,
        branch_policy=branch_policy,
    )
    if cleanup_result.skipped_reason:
        warn(f"Skipping cleanup: {cleanup_result.skipped_reason}.")
        info(f"Run 'jailbee destroy {short}' manually if you want to remove it.")
    if cleanup_result.destroyed:
        info(f"Destroyed container '{short}'.")
    if cleanup_result.deleted_branch:
        info(f"Deleted local branch '{result.branch}'.")
    if cleanup_result.cleanup_error:
        warn(f"Cleanup did not fully complete: {cleanup_result.cleanup_error}")

    report = sync.render_submodule_report(
        moves=sync.compute_submodule_moves(cfg.repo_root, result.pre_merge_head, result.head_oid)
    )
    if report:
        from jailbee.tui import console

        console.print(report)


# Top-level alias — hidden from `jailbee --help`; full docstring inherited from `checkout`.
app.command(
    "checkout",
    hidden=True,
    help="Alias for `jailbee git checkout`. See `jailbee git checkout --help`.",
)(checkout)


@git_app.command("pull")
def pull(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Override branch detection"),
    ] = None,
    ff: Annotated[
        bool,
        typer.Option(
            "--ff",
            help="Fast-forward only (no merge commit). Refuses to merge "
            "if the host branch has diverged from the container's branch.",
        ),
    ] = False,
    into: Annotated[
        str | None,
        typer.Option(
            "--into",
            help="Host branch to merge into. Defaults to the container's "
            "base branch (user.jailbee.base_branch).",
        ),
    ] = None,
    current: Annotated[
        bool,
        typer.Option(
            "--current",
            help="Merge into the host's currently checked-out branch "
            "(mirror of `jailbee git push --current`). Mutually exclusive "
            "with --into.",
        ),
    ] = False,
    checkout: Annotated[
        bool,
        typer.Option(
            "--checkout",
            help="After the pull, check out the branch that was merged into, "
            "so host HEAD ends on it. Refuses if the host tree is dirty "
            "when a branch switch is needed.",
        ),
    ] = False,
    cleanup: Annotated[
        bool,
        typer.Option(
            "--cleanup",
            help="After a successful pull, force destroy + branch "
            "delete (both policies → 'always'). Mutually exclusive "
            "with --no-cleanup.",
        ),
    ] = False,
    no_cleanup: Annotated[
        bool,
        typer.Option(
            "--no-cleanup",
            help="After a successful pull, do NOT destroy the container "
            "or delete the merged host branch — even if the config "
            "default says always. Mutually exclusive with --cleanup.",
        ),
    ] = False,
    confirm: Annotated[
        bool | None,
        typer.Option(
            "--confirm/--no-confirm",
            help="Show a plan block and ask before merging, when jailbee picked "
            "the container itself (single candidate, no name given). "
            "Defaults to confirm.auto_target.",
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Fetch + merge the container's branch into its base branch.

    The container acts as a remote: this command is the host-side
    counterpart to `jailbee git push`.

    By default merges into the container's recorded base branch
    (``user.jailbee.base_branch``). Use ``--into <branch>`` to target a
    different host branch. If the target branch is not currently checked
    out, use ``--checkout`` to check it out and merge — host HEAD stays
    on the target afterwards (refuses if the host tree is dirty). In a
    multi-select batch, HEAD ends on the last container's target.

    By default creates an explicit merge commit (`--no-ff`). With `--ff`,
    runs `git merge --ff-only` instead: fast-forwards HEAD to the container's
    tip and refuses if histories have diverged. Conflicts leave the working
    tree in merge state — resolve and `git commit` manually.

    Post-merge cleanup (destroy container, delete merged host branch)
    is controlled by the `pull:` config block (`destroy_container` and
    `delete_branch`, each one of `prompt | always | never`). `--cleanup`
    forces both steps to `always`; `--no-cleanup` forces both to
    `never`. Cleanup failures are warnings, not errors.

    With no arguments and a TTY, opens a multi-select picker (space to
    toggle, Enter to confirm). Selected containers are pulled in order
    with the same flags. The batch stops at the first failed pull —
    remaining containers are listed but not attempted.

    When jailbee picks the container itself — one eligible container and no name
    given — the pull is confirmed first: a block naming the container's
    branch, the host target branch, both tips and the action, then [Y/n].
    Turn it off for one run with --no-confirm, or repo-wide with
    confirm.auto_target.

    Examples:

      jailbee git pull                             # multi-select picker (TTY)
      jailbee git pull feat-foo                    # merge with explicit merge commit
      jailbee git pull feat-foo --ff               # ff-only, refuse on divergence
      jailbee git pull feat-foo --into dev         # merge into 'dev' instead of base branch
      jailbee git pull feat-foo --current         # merge into the host's checked-out branch
      jailbee git pull feat-foo --checkout         # check out base branch and merge, stay on it
      jailbee git pull feat-foo --cleanup          # force destroy + delete branch
      jailbee git pull feat-foo --no-cleanup       # skip destroy + branch delete
      jailbee git pull feat-foo --ff --cleanup     # ff-only + force cleanup
      jailbee git pull --no-confirm                # skip the auto-target confirmation
    """
    from jailbee import git as git_helpers
    from jailbee import sync
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)

    if cleanup and no_cleanup:
        error("--cleanup and --no-cleanup are mutually exclusive.")
        raise typer.Exit(1)

    if current and into is not None:
        error("--current and --into are mutually exclusive.")
        raise typer.Exit(2)

    destroy_policy: Literal["prompt", "always", "never"]
    branch_policy: Literal["prompt", "always", "never"]
    if cleanup:
        destroy_policy = "always"
        branch_policy = "always"
    elif no_cleanup:
        destroy_policy = "never"
        branch_policy = "never"
    else:
        destroy_policy = cfg.pull.destroy_container
        branch_policy = cfg.pull.delete_branch

    if current:
        from jailbee.git import get_current_branch

        resolved_current = get_current_branch(cfg.repo_root)
        if resolved_current is None:
            error(
                f"Cannot resolve --current: repo at {cfg.repo_root} is in "
                "detached HEAD or git is unavailable. Pass --into <branch>."
            )
            raise typer.Exit(2)
        into = resolved_current

    if name is None:
        from jailbee import tui
        from jailbee.incus import Incus
        from jailbee.lifecycle import _stdin_is_interactive, list_containers

        if _stdin_is_interactive():
            incus = Incus()
            all_containers = list_containers(cfg, incus, with_git_status=True)
            pullable = [c for c in all_containers if c.mode != "mount"]
            if not pullable:
                error("No containers eligible for pull (all in mount mode).")
                raise typer.Exit(1)
            selected: list[str]
            if len(pullable) == 1:
                only_full = pullable[0].name
                info(f"Only one eligible container; pulling from '{short_name(cfg, only_full)}'.")
                selected = [only_full]
            else:
                picked = tui.pick_containers_multi(
                    pullable,
                    message="Select containers to pull into host:",
                )
                if picked is None:
                    raise typer.Abort()
                if not picked:
                    info("Nothing selected.")
                    return
                selected = picked

            if _should_show_plan(cfg, auto_selected=len(pullable) == 1, flag=confirm):
                _confirm_plan_if_buildable(
                    lambda: sync.plan_pull(
                        cfg,
                        incus,
                        short_name(cfg, selected[0]),
                        branch=branch,
                        into=into,
                        ff_only=ff,
                    )
                )

            for idx, full in enumerate(selected):
                short = short_name(cfg, full)
                try:
                    _do_single_pull(
                        cfg,
                        incus,
                        short,
                        branch=branch,
                        ff_only=ff,
                        into=into,
                        allow_checkout=checkout,
                        destroy_policy=destroy_policy,
                        branch_policy=branch_policy,
                    )
                except (sync.SyncError, git_helpers.GitError) as exc:
                    error(str(exc))
                    _emit_conflict_report(exc)
                    remaining = selected[idx + 1 :]
                    if remaining:
                        not_attempted = ", ".join(short_name(cfg, n) for n in remaining)
                        error(f"Stopping batch; {len(remaining)} not attempted: {not_attempted}.")
                    raise typer.Exit(1) from exc
            return

    incus, resolved = _resolve_existing_detailed(cfg, name)
    full = resolved.name
    short = short_name(cfg, full)

    if _should_show_plan(
        cfg, auto_selected=resolved.auto_selected, flag=confirm
    ) and not _is_mount_mode(incus, full):
        _confirm_plan_if_buildable(
            lambda: sync.plan_pull(cfg, incus, short, branch=branch, into=into, ff_only=ff)
        )

    try:
        _do_single_pull(
            cfg,
            incus,
            short,
            branch=branch,
            ff_only=ff,
            into=into,
            allow_checkout=checkout,
            destroy_policy=destroy_policy,
            branch_policy=branch_policy,
        )
    except sync.SyncError as exc:
        error(str(exc))
        _emit_conflict_report(exc)
        raise typer.Exit(1) from exc
    except git_helpers.GitError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc


# Top-level alias — hidden from `jailbee --help`; full docstring inherited from `pull`.
app.command(
    "pull",
    hidden=True,
    help="Alias for `jailbee git pull`. See `jailbee git pull --help`.",
)(pull)


@git_app.command("retarget")
def retarget(
    name: Annotated[
        str,
        typer.Argument(
            help="Container to re-point.",
            autocompletion=completion.complete_container,
        ),
    ],
    new_base: Annotated[
        str,
        typer.Argument(
            help="New base branch (must exist on host as refs/heads/...).",
            autocompletion=completion.complete_branch,
        ),
    ],
    merge: Annotated[
        bool,
        typer.Option(
            "--merge",
            help="After retargeting, merge the new base into the container's "
            "branch (same as 'jailbee git push <name> --merge').",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Re-point a container at a new base branch.

    For stacked-PR chains: when the parent PR (e.g. feat/a) merges to
    main, re-point the dependent container at main. `jailbee pull`,
    `jailbee git push` and the `jailbee ls` AHEAD ±/MERGE columns follow the
    new base automatically.

    Examples:

      jailbee git retarget feat-b main           # flip label + base ref
      jailbee git retarget feat-b main --merge   # also merge main into the container
    """
    from jailbee import git as git_helpers
    from jailbee import sync
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)
    incus, full = _resolve_existing(cfg, name)
    short = short_name(cfg, full)

    try:
        result = sync.retarget_container(cfg, incus, short, new_base)
    except (sync.SyncError, git_helpers.GitError) as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    old = result.old_base or "(none)"
    success(f"Container '{short}' base branch: '{old}' → '{result.new_base}'.")

    if merge:
        try:
            _do_single_push(cfg, incus, short, source=result.new_base, action="merge")
        except (sync.SyncError, git_helpers.GitError) as exc:
            error(str(exc))
            info(
                f"Base is already retargeted; re-run "
                f"'jailbee git push {short} --merge' to retry the merge."
            )
            raise typer.Exit(1) from exc
    else:
        info(
            f"Run 'jailbee git push {short} --merge' to merge "
            f"'{result.new_base}' into the container."
        )


# Top-level alias — hidden from `jailbee --help`; full docstring inherited from `retarget`.
app.command(
    "retarget",
    hidden=True,
    help="Alias for `jailbee git retarget`. See `jailbee git retarget --help`.",
)(retarget)


def _print_local_branch_update(short: str, upd: "LocalBranchUpdate") -> None:
    """Report what the push did to the container's own `refs/heads/<source>`.

    Silent for the two benign no-ops, which are also the common ones: a branch
    already at the pushed tip, and HEAD's own branch — which `--merge` /
    `--rebase` advance themselves and `ff_container_branch` never touches.
    """
    if upd.status in ("up-to-date", "checked-out"):
        return
    old = upd.old_oid[:7] if upd.old_oid is not None else "?"
    if upd.status == "created":
        info(f"Container's local '{upd.branch}' created at {upd.new_oid[:7]}.")
    elif upd.status == "fast-forwarded":
        info(f"Container's local '{upd.branch}' fast-forwarded {old} -> {upd.new_oid[:7]}.")
    elif upd.status == "diverged":
        warn(
            f"⚠ container's local '{upd.branch}' ({old}) has diverged from the "
            f"pushed ref and was left alone — no container commit is discarded "
            f"here. Reconcile it in 'jailbee shell {short}', or compare against "
            f"refs/jailbee/host/{upd.branch}."
        )
    else:  # "failed"
        warn(
            f"⚠ could not advance the container's local '{upd.branch}'; it is "
            f"stale, so an in-container 'git rebase {upd.branch}' would use the "
            f"wrong base. Use refs/jailbee/host/{upd.branch} instead, or retry "
            f"the push."
        )


def _print_push_summary(short: str, result: "PushResult") -> None:
    if result.old_oid is None:
        delta = "new ref"
    elif result.old_oid == result.new_oid:
        delta = "up to date"
    else:
        delta = f"{result.old_oid[:7]} -> {result.new_oid[:7]}"
    info(
        f"Pushed '{result.source}' ({result.source_ref}) from host into "
        f"container '{short}' as {result.container_ref} ({delta})."
    )
    # A failed fetch only matters when the origin ref is what travelled — it
    # may then be stale. When resolution fell back to refs/heads/<source>
    # (branch not on origin at all, the normal stacked-PR case) the failure
    # had no bearing on what was pushed, and warning would be noise.
    if result.fetch_error is not None and result.source_ref.startswith("refs/remotes/"):
        # `refs/remotes/<remote>/<branch>` — remote names carry no slash, so the
        # third segment is the remote the fetch actually used. Reading it back off
        # the ref keeps the suggested command honest without threading `cfg` in.
        remote = result.source_ref.split("/")[2]
        warn(
            f"⚠ host 'git fetch {remote} {result.source}' failed: "
            f"{result.fetch_error} — pushed {result.source_ref} as the host "
            f"already had it, which may be stale."
        )
    if result.local_only_commits > 0:
        plural = "" if result.local_only_commits == 1 else "s"
        warn(
            f"⚠ host's local '{result.source}' has {result.local_only_commits} "
            f"commit{plural} not in {result.source_ref}; those were not pushed. "
            f"Use --from-local to send the local branch instead."
        )
    if result.local_branch is not None:
        _print_local_branch_update(short, result.local_branch)


def _print_bridge_direction(src: str, src_side: str, dst: str, dst_side: str) -> None:
    """One-line source->target banner so the bridge direction is unmistakable.

    ``src_side`` / ``dst_side`` are ``"host"`` or ``"container"``. Emitted
    before the detailed success lines in push/pull output.
    """
    info(f"{src} ({src_side}) ──▶ {dst} ({dst_side})")


def _is_mount_mode(incus: "IncusType", full: str) -> bool:
    """Whether container `full` is in --mount mode (shares the host tree).

    A bridge plan describes fetch/checkout/merge — none of which apply in
    mount mode, where `assert_container_publishable` (and its callers) refuse
    outright. Used to skip the confirmation block for a mount-mode container
    picked up by auto-selection, rather than asking [Y/n] for an operation
    that is guaranteed to fail.
    """
    return incus.config_get(full, "user.jailbee.mode") == "mount"


def _should_show_plan(cfg: "Config", *, auto_selected: bool, flag: bool | None) -> bool:
    """Whether a bridge command should show its plan block before running.

    Only when jailbee chose the container itself: with a name argument or a picker
    selection the user has already seen what they aimed at. The flag
    (``--confirm`` / ``--no-confirm``) wins over ``confirm.auto_target``.

    Note this does not consider the TTY. Off a TTY the block is still printed —
    the run gets a record of what it did — and only the prompt is skipped; see
    :func:`_confirm_bridge_plan`.
    """
    enabled = cfg.confirm.auto_target if flag is None else flag
    return auto_selected and enabled


def _confirm_bridge_plan(plan: "BridgePlan") -> None:
    """Print a bridge plan and, on a TTY, ask whether to go ahead.

    ``markup=False`` because branch names are user data and may contain Rich
    markup characters. Off a TTY nothing is asked: a confirmation that is on by
    default must not break scripts or background jobs. Declining raises
    ``typer.Abort()`` — nothing has been mutated at that point.
    """
    from jailbee.lifecycle import _stdin_is_interactive
    from jailbee.tui import console, render_bridge_plan

    console.print(render_bridge_plan(plan), markup=False, highlight=False)
    if not _stdin_is_interactive():
        return
    if not typer.confirm("Continue?", default=True):
        raise typer.Abort()


def _confirm_plan_if_buildable(build: "Callable[[], BridgePlan]") -> bool:
    """Build a bridge plan and confirm it, skipping both if it cannot be built.

    The `sync.plan_*` builders degrade every *read* to None or a note, but
    constructing a plan still resolves the container name and reads its state,
    which can raise: `ValueError` for a container that vanished between the
    listing and now, `IncusError` for a daemon hiccup. A preview is not worth
    failing a command over — when it cannot be built the confirmation is
    skipped and the operation proceeds to produce its own precise error.

    Returns True iff the plan was built (and shown/confirmed) — callers that
    hoisted work ahead of the plan (push's pre-fetch) use this to tell whether
    that work's own notes made it in front of the user.
    """
    from jailbee.incus import IncusError

    try:
        plan = build()
    except (ValueError, IncusError):
        return False
    _confirm_bridge_plan(plan)
    return True


def _resolve_push_action(
    cfg: "Config",
    *,
    merge_flag: bool,
    rebase_flag: bool,
    plain_flag: bool,
    force_flag: bool = False,
) -> str | None:
    """Pick the push action. Returns 'merge'/'rebase'/'plain'/'force', or
    None when the caller must open a picker.

    CLI flag wins -> config value wins -> 'ask' defers to picker (None).
    'force' is flag-only: it is never a configured default."""
    if force_flag:
        return "force"
    if merge_flag:
        return "merge"
    if rebase_flag:
        return "rebase"
    if plain_flag:
        return "plain"
    configured = cfg.push.default_action
    if configured == "ask":
        return None
    return configured


def _resolve_push_source(
    cfg: "Config",
    *,
    source_flag: str | None,
    current_flag: bool,
) -> "str | _BaseSource | None":
    """Pick the host source branch. Returns the branch name, the
    ``_BASE_SOURCE`` sentinel (resolved per-container in
    ``_do_single_push``), or None when the caller must open a picker.

    Detached HEAD with --current or config 'current' raises typer.Exit(2)
    with a dimension-specific message."""
    from jailbee.git import detect_default_branch, get_current_branch

    if source_flag is not None:
        return source_flag
    if current_flag:
        resolved = get_current_branch(cfg.repo_root)
        if resolved is None:
            error(
                f"Cannot resolve --current: repo at {cfg.repo_root} is in "
                "detached HEAD or git is unavailable."
            )
            raise typer.Exit(2)
        return resolved
    configured = cfg.push.default_source
    if configured == "default-branch":
        return detect_default_branch(cfg.repo_root, cfg.upstream_remote)
    if configured == "current":
        resolved = get_current_branch(cfg.repo_root)
        if resolved is None:
            error(
                f"push.default_source='current' but repo at {cfg.repo_root} "
                "is in detached HEAD state. Pass --from <branch> or set "
                "config to 'default-branch'."
            )
            raise typer.Exit(2)
        return resolved
    if configured == "base":
        return _BASE_SOURCE
    return None  # configured == 'ask'


def _resolve_push_ref_pref(
    cfg: "Config",
    *,
    origin_flag: bool,
    local_flag: bool,
    source_flag: str | None,
    current_flag: bool,
) -> "SourcePref | None":
    """Pick which host copy of the source branch to push.

    Returns ``"origin"``, ``"local"``, or None to defer to
    ``push.push_from`` (see `sync._resolve_host_source_ref`).

    Flags win. Failing that, a source that *is* the host's checked-out
    branch resolves locally: ``refs/heads/<branch>`` is the work in
    progress and ``origin/<branch>`` can only be older or absent. Every
    other source (the container's base branch, the default branch, an
    explicit ``--from``) defers to config, where origin is the fresher
    copy — a local ``refs/heads/<base>`` advances only on ``git pull``,
    so a plain ``git fetch`` leaves it behind.

    The ``--pr`` path is not handled here at all: `pr.fetch_pr_head` writes
    the head to ``refs/jailbee/pr/<N>/head``, which is neither candidate, so its
    caller passes that ref to the push explicitly and this preference is
    ignored.
    """
    if origin_flag:
        return "origin"
    if local_flag:
        return "local"
    if source_flag is None and (current_flag or cfg.push.default_source == "current"):
        return "local"
    return None


def _pick_push_action() -> str | None:
    """Open a questionary.select for the push action.

    Returns 'merge' / 'rebase' / 'plain', or None if the user cancels."""
    import questionary

    choices = [
        questionary.Choice(title="merge into container's branch", value="merge"),
        questionary.Choice(title="rebase container's branch onto pushed ref", value="rebase"),
        questionary.Choice(title="plain (transport only, no merge/rebase)", value="plain"),
    ]
    result = questionary.select("What to do with the pushed ref?", choices=choices).ask()
    if result is None:
        return None
    return str(result)


class _PrHead:
    """Sentinel for the 'refresh from the container's PR head' source choice.

    Returned by `_pick_push_source` (and matched by `push`) so the source
    picker can offer "refresh from GitHub" without colliding with any real
    branch name.
    """

    __slots__ = ()


_PR_HEAD = _PrHead()


class _BaseSource:
    """Sentinel: resolve the push source from each container's base branch.

    Mirrors `_PrHead`. Resolved per container in `_do_single_push` (the
    batch path applies one configured source to many containers, each with
    its own base branch), so it can't be turned into a concrete branch name
    up front.
    """

    __slots__ = ()


_BASE_SOURCE = _BaseSource()


def _container_base_branch(incus: "IncusType", full: str) -> str | None:
    label = incus.config_get(full, "user.jailbee.base_branch")
    return label if isinstance(label, str) and label else None


def _pr_head_for(incus: "IncusType", full: str) -> tuple[int, str] | None:
    """Return ``(pr_number, head_ref)`` for a PR container, else ``None``.

    Reads the ``user.jailbee.pr`` and ``user.jailbee.branch`` labels set by
    ``jailbee new --pr``. Defensive against non-integer label values (e.g. a
    bare mock in tests, or a corrupt label) — returns ``None`` rather than
    raising so callers can treat the container as non-PR.

    If the ``user.jailbee.pr`` label was never persisted (e.g. a best-effort set
    failed at create time), this returns ``None`` even for a container created
    with ``--pr``.
    """
    raw = incus.config_get(full, "user.jailbee.pr")
    if raw is None:
        return None
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    head_ref = incus.config_get(full, "user.jailbee.branch")
    if not isinstance(head_ref, str) or not head_ref:
        return None
    return (number, head_ref)


def _refresh_pr_source(cfg: "Config", incus: "IncusType", full: str) -> tuple[str, str]:
    """Refresh a PR container's head from the GitHub origin.

    Re-resolves the PR via `gh`, fetches `pull/<N>/head` into
    `refs/jailbee/pr/<N>/head`, and returns `(head_ref, host_ref)`: the branch
    name the container-side ref is labelled with, and the exact host ref the
    push must send. Errors (no PR label, gh or fetch failures) print a
    message and raise typer.Exit(2).
    """
    from jailbee import pr as pr_module
    from jailbee.lifecycle import short_name

    short = short_name(cfg, full)
    head = _pr_head_for(incus, full)
    if head is None:
        error(
            f"Container '{short}' was not created from a PR "
            f"(no user.jailbee.pr label). Use --from <branch>."
        )
        raise typer.Exit(2)
    pr_number, _label_head_ref = head  # label head_ref may be stale; resolve_pr is authoritative

    try:
        pr_info = pr_module.resolve_pr(cfg.repo_root, pr_number, remote=cfg.upstream_remote)
        if pr_info.state != "OPEN":
            warn(f"PR #{pr_number} is {pr_info.state}; refreshing anyway.")
        fetch_result = pr_module.fetch_pr_head(cfg.repo_root, pr_info, remote=cfg.upstream_remote)
    except pr_module.PrError as exc:
        error(str(exc))
        raise typer.Exit(2) from exc

    if not fetch_result.updated:
        info(
            f"PR #{pr_number} '{pr_info.head_ref}' already up to date "
            f"({fetch_result.new_sha[:12]})."
        )
    elif fetch_result.prev_sha is None:
        info(f"PR #{pr_number} '{pr_info.head_ref}' fetched ({fetch_result.new_sha[:12]}).")
    else:
        info(
            f"PR #{pr_number} '{pr_info.head_ref}' refreshed "
            f"({fetch_result.prev_sha[:12]}..{fetch_result.new_sha[:12]})."
        )
    return pr_info.head_ref, fetch_result.ref


def _pick_push_source(
    cfg: "Config",
    *,
    pr_head: tuple[int, str] | None = None,
    base: str | None = None,
) -> "str | _PrHead | None":
    """Open a questionary.select for the push source branch.

    Returns the branch name, the `_PR_HEAD` sentinel, or None if the user
    cancels. When `pr_head` is given (the target is a single PR container),
    the PR head is offered as the first/default choice — selecting it means
    "refresh the PR head from GitHub and push that".

    When `base` is given (the container's base branch, only passed for
    non-PR containers), it is offered as a choice before the default branch,
    provided it differs from both the default branch and the current branch
    (avoiding duplicate entries).

    Without `pr_head` and without `base`, when the current host branch equals
    the default branch (or HEAD is detached) there is only one eligible source
    — skip the picker and return the default branch directly.
    """
    import questionary

    from jailbee.git import detect_default_branch, get_current_branch

    default_branch = detect_default_branch(cfg.repo_root, cfg.upstream_remote)
    current_branch = get_current_branch(cfg.repo_root)

    if (
        pr_head is None
        and base is None
        and (current_branch is None or current_branch == default_branch)
    ):
        info(f"Only one eligible source branch; pushing from '{default_branch}'.")
        return default_branch

    choices: list[questionary.Choice] = []
    if pr_head is not None:
        pr_number, head_ref = pr_head
        choices.append(
            questionary.Choice(
                title=f"{head_ref} (PR #{pr_number} head — refresh from GitHub)",
                value=_PR_HEAD,
            )
        )
    if base is not None and base != default_branch and base != current_branch:
        choices.append(
            questionary.Choice(
                title=f"{base} (container base branch)",
                value=base,
            )
        )
    choices.append(
        questionary.Choice(title=f"{default_branch} (default branch)", value=default_branch)
    )
    if current_branch is not None and current_branch != default_branch:
        choices.append(
            questionary.Choice(
                title=f"{current_branch} (current host branch)",
                value=current_branch,
            )
        )

    result = questionary.select("Push from which host branch?", choices=choices).ask()
    if result is None:
        return None
    if isinstance(result, _PrHead):
        return _PR_HEAD
    return str(result)


@git_app.command("diff")
def git_diff_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Override branch detection"),
    ] = None,
    wt: Annotated[
        bool,
        typer.Option("--wt", help="Show working-tree changes only (HEAD vs WT)."),
    ] = False,
    all_diff: Annotated[
        bool,
        typer.Option("--all", help="Show WT and committed diffs."),
    ] = False,
    stat: Annotated[
        bool,
        typer.Option("--stat", help="Use --stat instead of full patch."),
    ] = False,
    color: Annotated[
        bool | None,
        typer.Option(
            "--color/--no-color",
            help=(
                "Force ANSI colour on or off. Default: colour when stdout is a "
                "TTY. `jailbee dashboard` passes --color because it pipes the "
                "diff into a pager."
            ),
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Show diff between container and host.

    Default: commits in the container that would be brought by
    `jailbee git pull` (3-dot diff against the container's base branch:
    origin/<base_branch>; fallback origin/<default_branch>).
    Use --wt for working-tree-only, --all for both.

    Colour follows stdout by default. `--color` forces it on for a consumer
    that pages the output (a pipe is not a TTY, so autodetection would drop
    exactly the colour a pager can render); `--no-color` forces it off.
    """
    import sys

    from jailbee import sync
    from jailbee.lifecycle import short_name as _short_name

    if wt and all_diff:
        error("--wt and --all are mutually exclusive.")
        raise typer.Exit(2)

    cfg = _load_or_exit(config)
    incus, full = _resolve_existing(cfg, name)
    short = _short_name(cfg, full)

    mode: Literal["committed", "wt", "all"]
    if wt:
        mode = "wt"
    elif all_diff:
        mode = "all"
    else:
        mode = "committed"

    try:
        out = sync.diff_from_container(
            cfg,
            incus,
            short,
            branch=branch,
            mode=mode,
            stat_only=stat,
            color=sys.stdout.isatty() if color is None else color,
        )
    except sync.SyncError as e:
        error(str(e))
        raise typer.Exit(1) from e

    typer.echo(out, nl=False)


# Top-level alias — hidden from `jailbee --help`; full docstring inherited from `git diff`.
app.command(
    "diff",
    hidden=True,
    help="Alias for `jailbee git diff`. See `jailbee git diff --help`.",
)(git_diff_cmd)


class _PushOutcome(NamedTuple):
    """Per-container result for batch push reporting."""

    short: str
    ok: bool
    summary: str


def _print_push_batch_summary(outcomes: list[_PushOutcome]) -> None:
    """Print a per-container ✓/✗ summary table for batch push."""
    if not outcomes:
        return
    info("")
    info("Summary:")
    name_width = max(len(o.short) for o in outcomes)
    for o in outcomes:
        mark = "[green]✓[/green]" if o.ok else "[red]✗[/red]"
        info(f"  {mark} {o.short:<{name_width}}  {o.summary}")
    info("")
    n_ok = sum(1 for o in outcomes if o.ok)
    n_fail = len(outcomes) - n_ok
    info(f"{n_ok} succeeded, {n_fail} failed.")
    if n_fail:
        info("Fix failed containers manually (e.g. 'jailbee shell <name>'), then retry.")


def _do_single_push(
    cfg: "Config",
    incus: "IncusType",
    short: str,
    *,
    source: "str | _BaseSource",
    action: str,
    prefer_ref: "SourcePref | None" = None,
    fetch: bool | None = None,
    source_ref: str | None = None,
) -> str:
    """Run one container's push + immediate per-container output.

    Returns the one-line success summary (also printed to stdout for
    live progress). Raises ``sync.SyncError`` on failure — the caller
    decides whether to abort or continue.

    ``prefer_ref`` / ``fetch`` select which host copy of the source branch
    to push and whether to refresh it first; ``None`` defers to
    ``push.push_from`` / ``push.autofetch``. ``source_ref`` overrides both
    with an exact host ref (a PR head — see ``pr.pr_head_ref``).
    """
    from jailbee import sync
    from jailbee.lifecycle import resolve_container_name

    if isinstance(source, _BaseSource):
        full = resolve_container_name(cfg, incus, short)
        resolved = _container_base_branch(incus, full)
        if resolved is None:
            raise sync.SyncError(
                f"Container '{short}' has no base branch label "
                f"(user.jailbee.base_branch); pass --from <branch> or --current."
            )
        source = resolved

    if action == "merge":
        merge_result = sync.push_and_merge(
            cfg,
            incus,
            short,
            source=source,
            prefer_ref=prefer_ref,
            fetch=fetch,
            source_ref=source_ref,
        )
        _print_bridge_direction(
            merge_result.push.source, "host", merge_result.container_branch, "container"
        )
        _print_push_summary(short, merge_result.push)
        mode = " (fast-forward)" if merge_result.fast_forward_only else ""
        success(
            f"Merged '{merge_result.push.source}' into "
            f"'{merge_result.container_branch}'{mode} inside container '{short}'."
        )
        info(f"Container HEAD now at {merge_result.head_oid[:7]}.")
        return (
            f"merged '{merge_result.push.source}' into "
            f"'{merge_result.container_branch}'{mode} ({merge_result.head_oid[:7]})"
        )
    if action == "rebase":
        rebase_result = sync.push_and_rebase(
            cfg,
            incus,
            short,
            source=source,
            prefer_ref=prefer_ref,
            fetch=fetch,
            source_ref=source_ref,
        )
        _print_bridge_direction(
            rebase_result.push.source, "host", rebase_result.container_branch, "container"
        )
        _print_push_summary(short, rebase_result.push)
        success(
            f"Rebased '{rebase_result.container_branch}' onto "
            f"'{rebase_result.push.source}' inside container '{short}'."
        )
        info(f"Container HEAD now at {rebase_result.head_oid[:7]}.")
        return (
            f"rebased '{rebase_result.container_branch}' onto "
            f"'{rebase_result.push.source}' ({rebase_result.head_oid[:7]})"
        )
    if action == "force":
        reset_result = sync.push_and_reset(
            cfg,
            incus,
            short,
            source=source,
            prefer_ref=prefer_ref,
            fetch=fetch,
            source_ref=source_ref,
        )
        _print_bridge_direction(
            reset_result.push.source, "host", reset_result.container_branch, "container"
        )
        _print_push_summary(short, reset_result.push)
        success(
            f"Reset '{reset_result.container_branch}' to "
            f"'{reset_result.push.source}' inside container '{short}'."
        )
        info(f"Container HEAD now at {reset_result.head_oid[:7]}.")
        if reset_result.discarded_commits > 0:
            old_short = (
                reset_result.old_branch_oid[:7] if reset_result.old_branch_oid else "unknown"
            )
            warn(
                f"⚠ discarded {reset_result.discarded_commits} "
                f"container-only commit(s) (was {old_short})."
            )
        summary = (
            f"reset '{reset_result.container_branch}' to "
            f"'{reset_result.push.source}' ({reset_result.head_oid[:7]})"
        )
        if reset_result.discarded_commits > 0:
            summary += f", discarded {reset_result.discarded_commits}"
        return summary
    # action == "plain"
    push_result = sync.push_to_container(
        cfg,
        incus,
        short,
        source=source,
        prefer_ref=prefer_ref,
        fetch=fetch,
        source_ref=source_ref,
    )
    _print_bridge_direction(push_result.source, "host", push_result.container_ref, "container")
    _print_push_summary(short, push_result)
    success("Push complete.")
    if push_result.old_oid is None:
        return f"pushed '{push_result.source}' -> {push_result.container_ref} (new)"
    return (
        f"pushed '{push_result.source}' -> {push_result.container_ref} "
        f"({push_result.old_oid[:7]}..{push_result.new_oid[:7]})"
    )


@git_app.command("push")
def push(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    source: Annotated[
        str | None,
        typer.Option(
            "--from",
            help="Host branch to send. Defaults to the host repo's default branch.",
        ),
    ] = None,
    merge: Annotated[
        bool,
        typer.Option(
            "--merge",
            help="After push, run 'git merge' in the container against the "
            "pushed ref. Refuses if the container's working tree is dirty.",
        ),
    ] = False,
    rebase: Annotated[
        bool,
        typer.Option(
            "--rebase",
            help="After push, run 'git rebase' in the container onto the "
            "pushed ref. Refuses if the container's working tree is dirty.",
        ),
    ] = False,
    plain: Annotated[
        bool,
        typer.Option(
            "--plain",
            help="Explicit 'transport only': push the ref but do not run "
            "merge or rebase in the container. Used to override a "
            "configured push.default_action.",
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="After push, run 'git reset --hard' in the container so its "
            "current branch and working tree exactly match the pushed ref. "
            "Discards any container-only commits on that branch. Refuses if "
            "the container's working tree is dirty or its current branch "
            "differs from the source. Requires an explicit container name.",
        ),
    ] = False,
    current: Annotated[
        bool,
        typer.Option(
            "--current",
            help="Use the host repo's currently checked-out branch as the "
            "source. Mutually exclusive with --from.",
        ),
    ] = False,
    pr_refresh: Annotated[
        bool,
        typer.Option(
            "--pr",
            help="Refresh this container's PR head from the GitHub origin "
            "and push it. Only valid for containers created with "
            "'jailbee new --pr'. Requires an explicit container name; mutually "
            "exclusive with --from and --current.",
        ),
    ] = False,
    from_origin: Annotated[
        bool,
        typer.Option(
            "--from-origin",
            help="Push the fetched upstream tip "
            "('refs/remotes/<upstream>/<source>') rather than the host's local "
            "branch. Overrides push.push_from and the --current default.",
        ),
    ] = False,
    from_local: Annotated[
        bool,
        typer.Option(
            "--from-local",
            help="Push the host's local 'refs/heads/<source>' even if an "
            "origin-tracking copy exists, and skip the host fetch. Use when "
            "the host has commits not yet pushed to origin.",
        ),
    ] = False,
    fetch: Annotated[
        bool | None,
        typer.Option(
            "--fetch/--no-fetch",
            help="Fetch <source> from the upstream on the host before "
            "resolving the source ref. Defaults to push.autofetch; only "
            "applies when pushing the upstream-tracking ref.",
        ),
    ] = None,
    confirm: Annotated[
        bool | None,
        typer.Option(
            "--confirm/--no-confirm",
            help="Show a plan block and ask before pushing, when jailbee picked "
            "the container itself (single candidate, no name given). "
            "Defaults to confirm.auto_target.",
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Send a host branch into a container's clone (host -> container).

    Mirror of `jailbee git fetch`. The action (merge/rebase/plain) and
    source (default branch or current) can be passed as flags, taken
    from the configured defaults (push.default_action / default_source
    in global.yaml or .jailbee/config.yaml), or chosen interactively when
    those are 'ask'. CLI flags always win over configured defaults.

    With --merge or --rebase, the pushed ref is also applied to the
    container's current branch; conflicts leave the container in
    merge/rebase state for resolution inside `jailbee shell`.

    With --force, the container's current branch and working tree are
    hard-reset to the pushed ref (discarding container-only commits).
    It is single-container only, refuses a dirty tree, and refuses when
    the container's current branch differs from the source. --force is
    flag-only — it is never a configured push.default_action.

    With --pr (PR containers only), the container's PR head is re-fetched
    from the GitHub origin before the push, bringing in commits the PR
    author pushed since the container was created.

    Which *copy* of the source branch travels is a separate question from
    which branch. By default (push.push_from='origin', push.autofetch=true)
    the host fetches origin/<source> and pushes that, because a local
    refs/heads/<base> only advances on `git pull` — after a plain
    `git fetch` it is exactly the stale one. --from-local pushes the
    host's local branch instead (and skips the fetch); --from-origin
    forces the origin ref. --current and --pr always resolve locally,
    since there the local ref is the fresher one by construction.

    With no arguments and a TTY, opens a multi-select picker. Source
    and action are resolved once and applied to every selected
    container. Failures don't stop the batch — a Summary block at the
    end lists ✓/✗ per container and the command exits non-zero if any
    push failed.

    When jailbee picks the container itself — one eligible container and no name
    given — the push is confirmed first: a block naming the source branch,
    the container's branch, both tips and the action, then [Y/n]. Turn it
    off for one run with --no-confirm, or repo-wide with confirm.auto_target.

    Examples:

      jailbee git push                          # multi-select; source/action asked once
      jailbee git push feat-foo                 # honors push.default_* or prompts
      jailbee git push feat-foo --current       # send host's current branch
      jailbee git push feat-foo --merge --current
      jailbee git push feat-foo --from develop --rebase
      jailbee git push feat-foo --plain         # transport only, no apply
      jailbee git push feat-foo --from develop --force   # replace container branch + worktree
      jailbee git push feat-foo --pr            # refresh from the PR head on GitHub
      jailbee git push feat-foo --from-local    # send the host's local branch as-is
      jailbee git push feat-foo --no-fetch      # use origin/<source> without fetching first
      jailbee git push --no-confirm             # skip the auto-target confirmation

    Current defaults: see `jailbee config show` for the active
    push.default_action, push.default_source, push.push_from and
    push.autofetch.
    """
    if sum([merge, rebase, plain, force]) > 1:
        error("--merge, --rebase, --plain, and --force are mutually exclusive.")
        raise typer.Exit(2)
    if force and name is None:
        error(
            "--force requires an explicit container name; it is not "
            "available in the multi-select picker."
        )
        raise typer.Exit(2)
    if source is not None and current:
        error("--from and --current are mutually exclusive.")
        raise typer.Exit(2)
    if pr_refresh and source is not None:
        error("--pr and --from are mutually exclusive.")
        raise typer.Exit(2)
    if pr_refresh and current:
        error("--pr and --current are mutually exclusive.")
        raise typer.Exit(2)
    if from_origin and from_local:
        error("--from-origin and --from-local are mutually exclusive.")
        raise typer.Exit(2)
    if pr_refresh and (from_origin or from_local):
        flag = "--from-origin" if from_origin else "--from-local"
        error(
            f"--pr and {flag} are mutually exclusive: the PR head is fetched "
            "into refs/jailbee/pr/<N>/head and pushed from there, so there is no "
            "local-vs-origin choice to make."
        )
        raise typer.Exit(2)
    if pr_refresh and name is None:
        error("--pr requires an explicit container name (batch mode is not supported with --pr).")
        raise typer.Exit(1)

    from jailbee import git as git_helpers
    from jailbee import sync
    from jailbee.lifecycle import _stdin_is_interactive, short_name

    cfg = _load_or_exit(config)
    ref_pref = _resolve_push_ref_pref(
        cfg,
        origin_flag=from_origin,
        local_flag=from_local,
        source_flag=source,
        current_flag=current,
    )

    if name is None:
        from jailbee import tui
        from jailbee.incus import Incus
        from jailbee.lifecycle import list_containers

        if not _stdin_is_interactive():
            error(
                "No container name given. Pass a name, or run "
                "interactively in a TTY for the container picker."
            )
            raise typer.Exit(1)

        incus = Incus()
        all_containers = list_containers(cfg, incus, with_git_status=True)
        pushable = [c for c in all_containers if c.state == "Running" and c.mode != "mount"]
        if not pushable:
            error(
                "No pushable containers (none running, or all in mount "
                "mode). Start one with 'jailbee start <name>'."
            )
            raise typer.Exit(1)
        selected: list[str]
        if len(pushable) == 1:
            only_full = pushable[0].name
            info(f"Only one eligible container; pushing to '{short_name(cfg, only_full)}'.")
            selected = [only_full]
        else:
            picked = tui.pick_containers_multi(
                pushable,
                message="Select containers to push to:",
            )
            if picked is None:
                raise typer.Abort()
            if not picked:
                info("Nothing selected.")
                return
            selected = picked

        # Resolve source + action ONCE, applied to every container. A PR head
        # selected in the picker also fixes the exact host ref to push.
        pr_source_ref: str | None = None
        resolved_source = _resolve_push_source(
            cfg,
            source_flag=source,
            current_flag=current,
        )
        resolved_action = _resolve_push_action(
            cfg,
            merge_flag=merge,
            rebase_flag=rebase,
            plain_flag=plain,
            # force is always False here — the `force and name is None` guard
            # above rejects --force without an explicit name, so this branch
            # is never reached with force=True. Passed for symmetry only.
            force_flag=force,
        )
        if resolved_source is None:
            # Offer the PR head only when the selection is a single PR
            # container — a multi-select batch has no single coherent PR
            # source (each container has its own head ref).
            pr_head = _pr_head_for(incus, selected[0]) if len(selected) == 1 else None
            # Offer the container base branch for a single non-PR container.
            # For multi-select or PR containers leave base=None.
            _batch_base = (
                _container_base_branch(incus, selected[0])
                if len(selected) == 1 and pr_head is None
                else None
            )
            _picked_source = _pick_push_source(cfg, pr_head=pr_head, base=_batch_base)
            if _picked_source is None:
                raise typer.Abort()
            if isinstance(_picked_source, _PrHead):
                # The head was fetched into jailbee's own ref, so it is pushed by
                # exact ref rather than resolved from a branch name.
                resolved_source, pr_source_ref = _refresh_pr_source(cfg, incus, selected[0])
            else:
                resolved_source = _picked_source
        if resolved_action is None:
            resolved_action = _pick_push_action()
            if resolved_action is None:
                raise typer.Abort()

        # Auto-selection is exactly the `len(pushable) == 1` branch above: with
        # two or more the picker ran, and an explicit name never reaches here
        # (the `name is None` block returns).
        #
        # `resolved_source` may still be the _BaseSource sentinel — plan_push
        # needs a concrete branch, so resolve it the same way _do_single_push
        # does. A container with no base label yields None, and the plan is
        # skipped: the push itself then fails with its own precise error.
        plan_source: str | _BaseSource | None = resolved_source
        if isinstance(plan_source, _BaseSource):
            plan_source = _container_base_branch(incus, selected[0])
        fetch_arg = fetch
        if isinstance(plan_source, str) and _should_show_plan(
            cfg, auto_selected=len(pushable) == 1, flag=confirm
        ):
            # Bound to their own annotated locals because mypy's narrowing
            # (the isinstance above, and resolved_action's `is None` check
            # further up) does not reach inside the lambda below.
            plan_branch: str = plan_source
            plan_action: str = resolved_action
            # Hoist push's best-effort host fetch so the plan shows the tip the
            # push will really send, then tell the push not to fetch again. A
            # PR head needs no fetch at all: `_refresh_pr_source` just wrote
            # the ref the push will send.
            resolved_prefer: SourcePref = ref_pref if ref_pref is not None else cfg.push.push_from
            fetch_note = (
                (False, None)
                if pr_source_ref is not None
                else sync.prefetch_push_source(
                    cfg,
                    source=plan_branch,
                    prefer=resolved_prefer,
                    fetch=fetch,
                )
            )
            plan_built = _confirm_plan_if_buildable(
                lambda: sync.plan_push(
                    cfg,
                    incus,
                    short_name(cfg, selected[0]),
                    source=plan_branch,
                    action=plan_action,
                    prefer_ref=ref_pref,
                    fetch_note=fetch_note,
                    source_ref=pr_source_ref,
                )
            )
            # The plan (if built) already surfaced fetch_note's error as one of
            # its notes — gated the same way plan_push gates it: noise when
            # the source isn't on origin at all (source_ref falls back to
            # refs/heads/<source>), because the failed fetch had no bearing
            # on what the push will send. When the plan couldn't be built,
            # that note would otherwise vanish: the push below runs with
            # fetch=False (the fetch already happened here), so
            # PushResult.fetch_error comes back None no matter what.
            if not plan_built and fetch_note[1] is not None:
                planned_ref = sync.host_source_ref(cfg, plan_branch, prefer=resolved_prefer)
                if planned_ref is not None and planned_ref.startswith("refs/remotes/"):
                    warn(f"⚠ host fetch of origin/{plan_branch} failed: {fetch_note[1]}")
            fetch_arg = False

        outcomes: list[_PushOutcome] = []
        for full in selected:
            short = short_name(cfg, full)
            try:
                summary = _do_single_push(
                    cfg,
                    incus,
                    short,
                    source=resolved_source,
                    action=resolved_action,
                    prefer_ref=ref_pref,
                    fetch=fetch_arg,
                    source_ref=pr_source_ref,
                )
                outcomes.append(_PushOutcome(short=short, ok=True, summary=summary))
            except (sync.SyncError, git_helpers.GitError) as exc:
                # error_plain: `[<container>]` is a style tag to Rich, and
                # dropping it takes the only thing naming which one failed.
                error_plain(f"[{short}] ✗ {exc}")
                _emit_conflict_report(exc)
                outcomes.append(_PushOutcome(short=short, ok=False, summary=str(exc)))

        _print_push_batch_summary(outcomes)
        if any(not o.ok for o in outcomes):
            raise typer.Exit(1)
        return

    incus, full = _resolve_existing(cfg, name)
    short = short_name(cfg, full)

    # Named `single_source` (not `resolved_source` like the batch path above)
    # because this path has a third assignment site: _refresh_pr_source when
    # --pr is given or the picker selects the PR head.
    single_source: str | _BaseSource | None
    # Set whenever the source is a PR head: `pr.fetch_pr_head` put it in
    # `refs/jailbee/pr/<N>/head`, which no branch-name resolution can reach.
    single_source_ref: str | None = None
    if pr_refresh:
        single_source, single_source_ref = _refresh_pr_source(cfg, incus, full)
    else:
        single_source = _resolve_push_source(cfg, source_flag=source, current_flag=current)

    resolved_action = _resolve_push_action(
        cfg,
        merge_flag=merge,
        rebase_flag=rebase,
        plain_flag=plain,
        force_flag=force,
    )

    if single_source is None:
        if not _stdin_is_interactive():
            error(
                "push.default_source is 'ask' but no TTY is available. "
                "Pass --from <branch> or --current, or set "
                "push.default_source in global.yaml."
            )
            raise typer.Exit(1)
        _pr = _pr_head_for(incus, full)
        _base = _container_base_branch(incus, full) if _pr is None else None
        _source_pick = _pick_push_source(cfg, pr_head=_pr, base=_base)
        if _source_pick is None:
            raise typer.Abort()
        if isinstance(_source_pick, _PrHead):
            single_source, single_source_ref = _refresh_pr_source(cfg, incus, full)
        else:
            single_source = _source_pick

    if resolved_action is None:
        if not _stdin_is_interactive():
            error(
                "push.default_action is 'ask' but no TTY is available. "
                "Pass --merge / --rebase / --plain, or set "
                "push.default_action in global.yaml."
            )
            raise typer.Exit(1)
        resolved_action = _pick_push_action()
        if resolved_action is None:
            raise typer.Abort()

    try:
        _do_single_push(
            cfg,
            incus,
            short,
            source=single_source,
            action=resolved_action,
            prefer_ref=ref_pref,
            fetch=fetch,
            source_ref=single_source_ref,
        )
    except (sync.SyncError, git_helpers.GitError) as exc:
        error(str(exc))
        _emit_conflict_report(exc)
        raise typer.Exit(1) from exc


# Top-level alias — hidden from `jailbee --help`; full docstring inherited from `push`.
app.command(
    "push",
    hidden=True,
    help="Alias for `jailbee git push`. See `jailbee git push --help`.",
)(push)


def _adopt_pr_head(
    cfg: "Config",
    incus: "IncusType",
    full: str,
    short: str,
    state: "PrState",
    *,
    yes: bool,
) -> str | None:
    """Confirm pushing a review container's commits to its PR head; return that head.

    A `jailbee new --pr N` container carries `user.jailbee.pr` without
    `user.jailbee.pr_author`. Publishing its commits to PR #N's head branch is a
    legitimate move — the PR is often the user's own, opened from another
    container or machine — but it mutates a PR jailbee did not create, so it is
    confirmed once and then recorded on `user.jailbee.pr_adopted`. Later runs take
    the ordinary update path silently.

    Returns the PR head branch name to publish under, or None when the
    container needs no adoption (no PR label, or already authored/adopted).
    Exits non-zero on a fork PR, on a gh failure, and on a declined or
    unavailable confirmation. (`--as` is rejected by the caller, for every run
    on an update-path container — not just the first.)
    """
    from jailbee import pr as pr_module
    from jailbee.lifecycle import _stdin_is_interactive

    raw = incus.config_get(full, "user.jailbee.pr")
    if not raw:
        return None
    # `user.jailbee.pr_branch` alone does NOT count as adopted: a lone pr_branch
    # means a previous adoption was interrupted between the two label writes,
    # and asking again is the safe outcome.
    if incus.config_get(full, "user.jailbee.pr_author") or incus.config_get(
        full, "user.jailbee.pr_adopted"
    ):
        return None

    # state.read() at the call site has already validated the label.
    number = int(raw)

    try:
        pr_info = pr_module.resolve_pr(cfg.repo_root, number, remote=cfg.upstream_remote)
    except pr_module.PrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    if pr_info.is_cross_repository:
        owner = pr_info.head_repo_owner or "<fork-owner>"
        error(
            f"PR #{number}'s head branch lives in the fork '{owner}'. Pushing to "
            f"origin would create an unrelated branch there and leave the PR "
            f"untouched, so jailbee refuses.\n"
            f"To update a fork PR you need push access to the fork (the PR's "
            f"'maintainer can modify' box); then push by hand:\n"
            f"  jailbee git fetch {short}\n"
            f"  git push git@github.com:{owner}/<repo>.git "
            f"refs/jailbee/{short}/<branch>:refs/heads/{pr_info.head_ref}"
        )
        raise typer.Exit(1)

    if pr_info.state != "OPEN":
        warn(f"PR #{number} is {pr_info.state}; updating its head anyway.")

    # Unconditional — a `--yes` run must also state which PR it is about to
    # mutate, BEFORE the push rather than after it.
    author = f"@{pr_info.author_login}" if pr_info.author_login else "an unknown author"
    info(
        f"Container '{short}' was created from PR #{number} by {author} "
        f"({pr_info.state}); head '{pr_info.head_ref}' → base '{pr_info.base_ref}'."
    )

    if not yes:
        if not _stdin_is_interactive():
            error(
                f"Container '{short}' was created from PR #{number}. Pushing its "
                f"commits to that PR's head needs confirmation — re-run with --yes "
                f"when there is no terminal to ask on."
            )
            raise typer.Exit(1)
        if not typer.confirm(
            f"Push this container's commits to PR #{number}'s head "
            f"'{pr_info.head_ref}'? The PR will be updated.",
            default=False,
        ):
            raise typer.Abort()

    # pr_branch FIRST — see the docstring: an adopted flag without a recorded
    # head name would make the next run publish to the container branch.
    # Best-effort, like the create path's label writes. `number=None` leaves
    # the recorded PR number alone — `new` already wrote it.
    state.record(
        head=pr_info.head_ref,
        author=False,
        adopted=True,
        number=None,
        context=f"Could not record the PR-head decision on '{short}'",
    )

    return pr_info.head_ref


def pr_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    title: Annotated[
        str | None,
        typer.Option("--title", help="PR title (default: last commit's subject on create)"),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option("--body", help="PR body (default: placeholder text on create)"),
    ] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", help="PR base branch (default: the container's base branch)"),
    ] = None,
    ready: Annotated[
        bool | None,
        typer.Option(
            "--ready/--draft",
            help=(
                "Mark the PR ready for review (--ready) or draft (--draft). "
                "Default: draft on create, unchanged on update."
            ),
        ),
    ] = None,
    no_draft: Annotated[
        bool,
        typer.Option("--no-draft", hidden=True, help="Back-compat alias for --ready."),
    ] = False,
    description: Annotated[
        bool,
        typer.Option(
            "--description",
            "-d",
            help="Regenerate the PR description with Claude and apply it (update only).",
        ),
    ] = False,
    web: Annotated[
        bool,
        typer.Option("--web", help="Open the PR in the browser afterwards"),
    ] = False,
    no_ai: Annotated[
        bool,
        typer.Option(
            "--no-ai",
            help=(
                "Skip AI generation of the PR title/body (even when claude.ai_pr_description is on)"
            ),
        ),
    ] = False,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Override branch detection"),
    ] = None,
    as_name: Annotated[
        str | None,
        typer.Option(
            "--as",
            help=(
                "Explicit PR head branch name (overrides AI naming). New PRs only — "
                "rejected once the container has a PR."
            ),
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Force-push the PR head with --force-with-lease (for a "
                "rebased/amended branch); refuses if the remote moved. "
                "Requires an explicit container name."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            help=(
                "Skip the confirmation when pushing a `jailbee new --pr` container's "
                "commits to that PR's head branch."
            ),
        ),
    ] = False,
    open_only: Annotated[
        bool,
        typer.Option(
            "--open",
            help="Open the container's PR in the browser and exit — no push or update.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Create or update a container's GitHub PR.

    Publishes the container's branch to the GitHub origin (fetch to host, then a
    fast-forward-only push under the host's credentials — never forced). When no
    PR exists yet, opens a draft PR; when one does, the push updates it. Optional
    flags update the description and draft/ready state on an existing PR.

    When `claude.enabled` and `claude.ai_pr_description` are on (the default),
    a new PR's title and body are generated by the container's Claude CLI;
    `--no-ai` opts out and `--title`/`--body` override per field. On an existing
    PR the description is left untouched unless you pass `--description`,
    `--title`/`--body`, or accept the interactive prompt.

    On a new PR the head branch name is proposed by Claude (convention-following)
    and confirmed interactively; `--as` overrides it and `--no-ai` keeps the
    container branch name. Once a PR exists, its head is fixed: `--as` is then
    rejected with exit 2 instead of silently pushing elsewhere.

    On a container whose PR jailbee did not create (`jailbee new --pr N`), the first run
    asks before pushing to that PR's head (`--yes` skips), `--force` asks again
    before overwriting it, and the PR's description is never regenerated unless
    you ask for it explicitly.

    Examples:

      jailbee pr feat-foo                  # create a draft PR, or push new commits to it
      jailbee pr feat-foo --ready          # create/mark ready for review
      jailbee pr feat-foo --description    # update: regenerate the description with Claude
      jailbee pr feat-foo --title "New"    # update: set the title
      jailbee pr feat-foo --draft          # update: move the PR back to draft
      jailbee pr feat-foo --as user/x    # explicit PR head branch name
      jailbee pr feat-foo --force          # force-push a rebased/amended branch
      jailbee pr feat-foo --web            # open the PR in the browser
      jailbee pr feat-foo --open           # just open the PR in the browser (no push)
      jailbee pr                           # interactive container picker
    """
    from jailbee import git as git_mod
    from jailbee import pr as pr_mod
    from jailbee import pr_flow, sync
    from jailbee.lifecycle import short_name

    if no_draft:
        ready = True

    if force and name is None:
        error("--force requires an explicit container name (no interactive picker).")
        raise typer.Exit(2)

    cfg = _load_or_exit(config)
    incus, full = _resolve_existing(cfg, name)
    short = short_name(cfg, full)
    scope = pr_flow.PrScope(
        repo_root=cfg.repo_root, remote=cfg.upstream_remote, prefix="", subpath=None
    )
    state = pr_flow.ContainerLabelState(incus, full, short=short)

    if open_only:
        try:
            pr_num = state.read().number
        except pr_flow.MalformedPrLabelError as exc:
            error(str(exc))
            raise typer.Exit(1) from exc
        if pr_num is None:
            error(f"Container '{short}' has no associated PR.")
            raise typer.Exit(1)
        pr_mod.open_pr_in_browser(cfg.repo_root, pr_num)
        return

    # Fail fast on a mount-mode/stopped/no-clone container BEFORE any expensive
    # or interactive work (the gh call, the adoption prompt, AI generation, the
    # branch-name prompt). Same SyncError messages the publish path would raise.
    try:
        sync.assert_container_publishable(cfg, incus, short)
    except sync.SyncError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    try:
        record = state.read()
    except pr_flow.MalformedPrLabelError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc
    is_author = record.author
    stored_pr_branch = record.head
    pr_label = str(record.number) if record.number is not None else None

    # `--as` names the head of a PR still to be created. A container that
    # already has one (authored, adopted, or about to be adopted below — a PR
    # label always leads to the update path) can only push to that PR's head.
    # Checked BEFORE the adoption prompt so a usage error never adopts a PR.
    if as_name is not None and (is_author or stored_pr_branch or pr_label):
        pr_flow.reject_as_on_pr_update(scope, as_name, pr_label)

    # A `jailbee new --pr` container already has a PR. Publishing its commits to
    # that PR's head is allowed once the user confirms; the confirmation is
    # recorded, so from then on this behaves like an authored-PR container.
    adopted_head = _adopt_pr_head(cfg, incus, full, short, state, yes=yes)
    if adopted_head is not None:
        # Use the value in-process rather than re-reading the label: the run
        # must publish to the right head even if the label write failed.
        stored_pr_branch = adopted_head

    # The container branch that will be fetched/published (also feeds the
    # existing-PR lookup below, the AI prompt, and the local-branch reconcile).
    container_branch = branch or incus.config_get(full, "user.jailbee.branch")

    # A container jailbee knows nothing about (`jailbee new <existing-branch>`) can
    # still sit on a branch that already has a PR — adopt it instead of opening
    # a duplicate. `--as` is a deliberate request for a different head, so it
    # skips the lookup entirely.
    if not (is_author or stored_pr_branch or pr_label) and as_name is None:
        found_pr = pr_flow.adopt_existing_pr_for_branch(
            scope, state, branch=container_branch, yes=yes, record_context=f"on '{short}'"
        )
        if found_pr is not None:
            found_number, found_head = found_pr
            # Both in-process, like the adoption above: `pr_label` turns on the
            # foreign-head guards and `stored_pr_branch` selects the update path.
            pr_label = str(found_number)
            stored_pr_branch = found_head

    # A container whose PR jailbee did not create (`jailbee new --pr`, adopted or not).
    # Two things follow: --force needs its own confirmation (it rewrites a
    # branch the PR author may own), and the interactive "regenerate the
    # description?" offer is suppressed — adoption only ever promised commits.
    is_foreign_pr_head = bool(pr_label) and not is_author
    if force and pr_label and not is_author:
        pr_flow.confirm_foreign_force_push(scope, short, pr_label, stored_pr_branch, yes=yes)

    resolved_base = base or incus.config_get(full, "user.jailbee.base_branch")
    if not resolved_base:
        error(
            f"Container '{short}' has no recorded base branch "
            "(user.jailbee.base_branch). Pass --base <branch>."
        )
        raise typer.Exit(1)

    ai_on = cfg.claude.enabled and cfg.claude.ai_pr_description and not no_ai
    plan = pr_flow.resolve_pr_text_and_head(
        cfg,
        incus,
        full,
        scope,
        is_update=bool(record.author or stored_pr_branch),
        stored_head=stored_pr_branch,
        source_branch=container_branch,
        base=resolved_base,
        title=title,
        body=body,
        as_name=as_name,
        no_ai=no_ai,
        status_label=f"Generating PR title/description with Claude in '{short}'…",
    )
    publish_name, ai_text = plan.publish_name, plan.ai_text

    # --- Publish (fetch + push under the chosen name) ---
    # On a foreign PR head the generic push-failure hint's "--as" advice does
    # not apply, so `is_foreign_pr_head` (resolved above) tailors it.
    try:
        publish = sync.publish_branch_from_container(
            cfg,
            incus,
            short,
            branch=branch,
            publish_name=publish_name,
            force=force,
            on_before_push=lambda result: _print_publish_progress(cfg, short, result),
        )
    except sync.SyncError as exc:
        error(str(exc))
        if is_foreign_pr_head:
            info(
                "This container publishes to an existing PR's head branch. A "
                "rejected push usually means the PR author pushed in the "
                f"meantime — bring their commits in with `jailbee git push {short} "
                "--pr --rebase`, then re-run `jailbee pr`. It can also mean you lack "
                "write access to the repository."
            )
        raise typer.Exit(1) from exc

    # The fetch summary and the dirty-tree warning were printed by
    # `_print_publish_progress` before the push, not here.

    # --- Reconcile a local branch to the external name (create path) ---
    if not (is_author or stored_pr_branch) and publish.publish_name != publish.fetch.branch:
        if git_mod.local_branch_exists(cfg.repo_root, publish.fetch.branch):
            if git_mod.local_branch_exists(cfg.repo_root, publish.publish_name):
                warn(
                    f"Local branch '{publish.publish_name}' already exists; leaving "
                    f"'{publish.fetch.branch}' as-is (no rename)."
                )
            else:
                try:
                    git_mod.rename_branch(cfg.repo_root, publish.fetch.branch, publish.publish_name)
                    success(
                        f"Renamed local branch '{publish.fetch.branch}' → "
                        f"'{publish.publish_name}' to match the PR head."
                    )
                    if git_mod.remote_ref_exists(
                        cfg.repo_root, cfg.upstream_remote, publish.publish_name
                    ):
                        git_mod.set_upstream(
                            cfg.repo_root, publish.publish_name, f"origin/{publish.publish_name}"
                        )
                except git_mod.GitError as exc:
                    warn(f"Could not rename local branch: {exc}")

    is_update_path = bool(is_author or stored_pr_branch)
    resolved_title, resolved_body = ("", "")
    if not is_update_path:
        resolved_title, resolved_body = pr_flow.resolve_create_text(
            scope,
            ai_on=ai_on,
            ai_text=ai_text,
            title=title,
            body=body,
            fallback_ref=f"refs/jailbee/{short}/{publish.fetch.branch}",
            publish_name=publish.publish_name,
            origin_label=f"container '{short}'",
        )
    try:
        created = pr_flow.create_or_view_pr(
            scope,
            state,
            is_update=is_update_path,
            head=publish.publish_name,
            base=resolved_base,
            title=resolved_title,
            body=resolved_body,
            draft=ready is not True,
            label="jailbee pr",
            record_context=f"failed to record the PR label on '{short}'",
        )
    except pr_mod.PrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    is_update = is_author or created.already_existed
    update = None
    if is_update:
        update = pr_flow.apply_pr_updates(
            cfg,
            incus,
            full,
            scope,
            number=created.number,
            branch=publish.fetch.branch,
            base=resolved_base,
            title=title,
            body=body,
            description=description,
            ready=ready,
            ai_on=ai_on,
            offer_regen=not is_foreign_pr_head,
        )
    pr_flow.render_pr_outcome(
        scope,
        url=created.url,
        number=created.number,
        is_update=is_update,
        publish_name=publish.publish_name,
        forced=publish.forced,
        ready=ready,
        update=update,
    )
    if web:
        pr_mod.open_pr_in_browser(cfg.repo_root, created.number)


# `jailbee pr` is the visible, canonical command; `jailbee git pr` is a hidden alias.
app.command("pr")(pr_cmd)
git_app.command("pr", hidden=True, help="See `jailbee pr --help`.")(pr_cmd)


submodule_app = typer.Typer(
    name="submodule",
    no_args_is_help=True,
    help="Manage git submodules.",
)
app.add_typer(submodule_app)


def _print_submodule_report(branch: str, report: list[tuple[str, str | None]]) -> None:
    """Print the per-submodule placement outcome, then a summary line."""
    from jailbee.tui import console

    if not report:
        info("No submodules found.")
        return
    for path, current in report:
        if current == branch:
            console.print(f"  {path}  [green]✓ {branch}[/green]")
        elif current is None:
            console.print(f"  {path}  [yellow]⚠ detached[/yellow]")
        else:
            console.print(f"  {path}  [yellow]⚠ on '{current}' (expected '{branch}')[/yellow]")
    success(f"Submodules aligned to '{branch}'.")


def _print_submodule_pr_candidates(candidates: list["SubCandidate"]) -> None:
    """List the submodules that have commits to publish, one per line."""
    from jailbee.tui import console

    width = max((len(c.path) for c in candidates), default=0)
    for c in candidates:
        count = "?" if c.commits is None else str(c.commits)
        console.print(f"  {c.path.ljust(width)}  {count} commits  {c.subject}")


@submodule_app.command("checkout")
def submodule_checkout(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            "-b",
            help="Branch to put the tree on (default: current). On the host this "
            "checks the branch out in the superproject too.",
        ),
    ] = None,
    submodules_only: Annotated[
        bool,
        typer.Option(
            "--submodules-only",
            help="Align submodules without checking -b out in the superproject "
            "(host only: a container's branch is never switched here).",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Put the repo tree — superproject and submodules — on one branch.

    Purely local — moves nothing between host and container (that is
    `jailbee git push`/`pull`).

    With no NAME this works on the host repo: bare, it aligns the submodules
    to the branch already checked out; with -b it checks that branch out in
    the superproject first and then aligns the submodules to it, so one
    command jumps the whole tree. Pass --submodules-only to leave the
    superproject where it is (a deliberate mismatch, or a detached HEAD you
    want to keep).

    With a container NAME, aligns that container's submodules to its branch
    (or -b). A container's branch is its identity, so -b never switches it.

    Examples:

      jailbee submodule checkout               # host, align to current branch
      jailbee submodule checkout -b master     # host, whole tree to master
      jailbee submodule checkout -b master --submodules-only
      jailbee submodule checkout feat-foo      # container 'feat-foo', its branch
    """
    from jailbee import sync
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)

    try:
        if name is None:
            resolved, report = sync.checkout_submodules_on_host(
                cfg,
                branch=branch,
                switch_superproject=branch is not None and not submodules_only,
            )
        else:
            incus, full = _resolve_existing(cfg, name)
            short = short_name(cfg, full)
            resolved, report = sync.checkout_submodules_in_container(
                cfg, incus, short, branch=branch
            )
    except sync.SyncError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    _print_submodule_report(resolved, report)


@submodule_app.command("pr")
def submodule_pr_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    path: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_submodule_path),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="PR title")] = None,
    body: Annotated[str | None, typer.Option("--body", help="PR body")] = None,
    base: Annotated[
        str | None,
        typer.Option("--base", help="PR base branch (default: the submodule's own default)"),
    ] = None,
    ready: Annotated[
        bool | None, typer.Option("--ready/--draft", help="Mark ready for review, or draft.")
    ] = None,
    description: Annotated[
        bool,
        typer.Option("--description", "-d", help="Regenerate the description (update only)."),
    ] = False,
    web: Annotated[bool, typer.Option("--web", help="Open the PR afterwards")] = False,
    no_ai: Annotated[bool, typer.Option("--no-ai", help="Skip AI title/body/branch")] = False,
    branch: Annotated[
        str | None,
        typer.Option("--branch", "-b", help="Branch to publish FROM the submodule"),
    ] = None,
    as_name: Annotated[
        str | None, typer.Option("--as", help="Explicit PR head branch name (new PRs only)")
    ] = None,
    force: Annotated[
        bool, typer.Option("--force", help="Force-push with --force-with-lease")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip confirmations")] = False,
    open_only: Annotated[
        bool, typer.Option("--open", help="Open the submodule's PR and exit")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Create or update a GitHub PR for one submodule.

    Publishes the commits made inside a submodule in a container to that
    submodule's own GitHub repository — a separate repo, so a separate PR from
    the superproject's `jailbee pr`. One PR per run.

    Without PATH, the submodule that has commits ahead of its base is targeted
    automatically; when several do, they are listed and PATH is required (two
    submodules are two repositories and two PRs).

    The base branch comes from the submodule's own `.gitmodules` entry, else its
    `<remote>/HEAD`, else `main`; `--base` overrides. The head branch name is
    `--as`, else Claude's proposal, else the branch the commits came from.

    Examples:

      jailbee submodule pr feat-foo              # auto-target, draft PR
      jailbee submodule pr feat-foo libs/foo     # explicit submodule
      jailbee submodule pr feat-foo --ready      # mark ready for review
      jailbee submodule pr feat-foo --open       # just open it in the browser
    """
    from jailbee import pr as pr_mod
    from jailbee import pr_flow, submodule_pr, sync
    from jailbee.lifecycle import container_repo_dir, short_name

    cfg = _load_or_exit(config)
    incus, full = _resolve_existing(cfg, name)
    short = short_name(cfg, full)

    # --open resolves from the recorded state alone: no preflight, no
    # transport, no gh mutation (mirrors `jailbee pr --open`).
    if open_only:
        target_path = path
        if target_path is None:
            recorded = submodule_pr.recorded_paths(incus, full)
            if not recorded:
                error(f"Container '{short}' has no submodule PR recorded.")
                raise typer.Exit(1)
            if len(recorded) != 1:
                # Same condition as AmbiguousSubmoduleTargetError on the normal
                # path ("disambiguate with PATH") — same exit code, so a script
                # checking $? sees one meaning regardless of --open.
                error(
                    f"Container '{short}' has submodule PRs recorded for "
                    f"{len(recorded)} paths; name one with PATH."
                )
                raise typer.Exit(2)
            target_path = recorded[0]
        record = submodule_pr.SubmodulePrState(incus, full, target_path).read()
        if record.number is None:
            error(f"Submodule '{target_path}' has no PR recorded on '{short}'.")
            raise typer.Exit(1)
        pr_mod.open_pr_in_browser(cfg.repo_root / target_path, record.number)
        return

    try:
        sync.assert_container_publishable(cfg, incus, short)
    except sync.SyncError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    super_base = incus.config_get(full, "user.jailbee.base_branch") or cfg.default_branch
    repo_dir = container_repo_dir(cfg, incus, full)
    try:
        subs = submodule_pr.detect_candidates(
            cfg, incus, full, repo_dir=repo_dir, base_branch=super_base, short=short
        )
        target = submodule_pr.select_target(subs, path)
    except submodule_pr.NoSubmoduleCandidatesError:
        if not subs:
            # "Name one with PATH" is unactionable advice when there is
            # nothing to name — distinguish "no submodules at all" from
            # "submodules exist, none are ahead".
            info(f"Container '{short}' has no submodules.")
        else:
            info(
                f"No submodule in '{short}' has commits ahead of its base — nothing to "
                f"open a PR for. Name one with PATH to publish it anyway."
            )
        return
    except submodule_pr.AmbiguousSubmoduleTargetError as exc:
        error(f"Several submodules in '{short}' have commits to publish:")
        _print_submodule_pr_candidates(exc.candidates)
        info("Pick one: `jailbee submodule pr <container> <path>`.")
        raise typer.Exit(2) from exc
    except submodule_pr.SubmodulePrError as exc:
        error(str(exc))
        raise typer.Exit(
            2 if isinstance(exc, submodule_pr.UnknownSubmodulePathError) else 1
        ) from exc

    subpath = target.path
    source_branch = branch or target.branch

    # Step 2 of the spec's pipeline: transport this submodule's objects to
    # the host BEFORE anything below reads the host sub-repo. For a
    # submodule the host has never seen (added inside the container, or a
    # host clone where `git submodule update --init` never ran for this
    # path), the sub-repo does not exist until this call clones it — see
    # `submodule_pr.transport_submodule_to_host`'s docstring.
    submodule_pr.transport_submodule_to_host(
        cfg, incus, full, short, subpath=subpath, repo_dir=repo_dir
    )

    remote = submodule_pr.resolve_remote(cfg.repo_root, subpath)
    resolved_base = submodule_pr.resolve_base_branch(cfg.repo_root, subpath, override=base)
    scope = pr_flow.PrScope(
        repo_root=cfg.repo_root / subpath,
        remote=remote,
        prefix=f"submodule '{subpath}': ",
        subpath=subpath,
    )
    state = submodule_pr.SubmodulePrState(incus, full, subpath)
    record = state.read()
    pr_label = str(record.number) if record.number is not None else None

    if as_name is not None and (record.author or record.head or pr_label):
        pr_flow.reject_as_on_pr_update(scope, as_name, pr_label)

    if target.commits is None:
        warn(
            f"Could not count submodule '{subpath}''s commits (no base anchor and "
            f"no {remote}/HEAD); publishing what it has."
        )
    if target.gitlink_stale:
        info(
            f"Submodule '{subpath}''s commits are not yet in the superproject's "
            f"gitlink — commit the bump there when this PR is ready."
        )
    if target.dirty:
        warn(f"Submodule '{subpath}' has uncommitted changes — they are NOT in the PR.")

    is_update = bool(record.author or record.head)
    if not is_update and as_name is None:
        found = pr_flow.adopt_existing_pr_for_branch(
            scope,
            state,
            branch=source_branch,
            yes=yes,
            record_context=f"for submodule '{subpath}' on '{short}'",
        )
        if found is not None:
            pr_label = str(found[0])
            is_update = True
            # Build the record in-process rather than re-reading it: `state.record`
            # (called by `adopt_existing_pr_for_branch`) is best-effort, and a
            # failed write would otherwise make `state.read()` hand back a blank
            # `PrRecord` here — `record.head is None` then makes
            # `resolve_pr_text_and_head` treat this as a headless detached
            # submodule and fail with a nonsense usage error, even though the
            # user just confirmed adopting a real PR. Same anti-pattern
            # `jailbee pr`'s adoption path avoids above (see the "Use the value
            # in-process" comment near `_adopt_pr_head`).
            record = pr_flow.PrRecord(number=found[0], head=found[1], author=False, adopted=True)

    is_foreign = bool(pr_label) and not record.author
    if force and pr_label and not record.author:
        pr_flow.confirm_foreign_force_push(scope, short, pr_label, record.head, yes=yes)

    plan = pr_flow.resolve_pr_text_and_head(
        cfg,
        incus,
        full,
        scope,
        is_update=is_update,
        stored_head=record.head,
        source_branch=source_branch,
        base=resolved_base,
        title=title,
        body=body,
        as_name=as_name,
        no_ai=no_ai,
        status_label=f"Generating PR title/description with Claude in '{short}:{subpath}'…",
    )
    publish_name = plan.publish_name
    if publish_name is None:
        error(
            f"Submodule '{subpath}' is detached in '{short}' and no head branch name "
            f"was chosen. Name one with --as, or pass --branch to publish an "
            f"existing submodule branch."
        )
        raise typer.Exit(2)

    # Publish step 4 of the spec: the submodule's own upstream must be a GitHub
    # one, checked BEFORE anything is pushed. `create_pr` validates too, but
    # only after the branch is already on the remote.
    try:
        pr_mod.assert_github_remote(scope.repo_root, remote, label="jailbee submodule pr")
    except pr_mod.PrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    try:
        published = submodule_pr.publish_submodule_branch(
            cfg,
            short,
            subpath=subpath,
            branch=source_branch,
            publish_name=publish_name,
            remote=remote,
            force=force,
        )
    except submodule_pr.SubmodulePrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    ai_on = cfg.claude.enabled and cfg.claude.ai_pr_description and not no_ai
    resolved_title, resolved_body = ("", "")
    if not is_update:
        resolved_title, resolved_body = pr_flow.resolve_create_text(
            scope,
            ai_on=ai_on,
            ai_text=plan.ai_text,
            title=title,
            body=body,
            fallback_ref=published.src_ref,
            publish_name=published.publish_name,
            origin_label=f"container '{short}' submodule '{subpath}'",
        )
    try:
        created = pr_flow.create_or_view_pr(
            scope,
            state,
            is_update=is_update,
            head=published.publish_name,
            base=resolved_base,
            title=resolved_title,
            body=resolved_body,
            draft=ready is not True,
            label="jailbee submodule pr",
            record_context=f"failed to record the PR label for submodule '{subpath}' on '{short}'",
        )
    except pr_mod.PrError as exc:
        error(str(exc))
        raise typer.Exit(1) from exc

    did_update = is_update or created.already_existed
    update = None
    if did_update and source_branch:
        update = pr_flow.apply_pr_updates(
            cfg,
            incus,
            full,
            scope,
            number=created.number,
            branch=source_branch,
            base=resolved_base,
            title=title,
            body=body,
            description=description,
            ready=ready,
            ai_on=ai_on,
            offer_regen=not is_foreign,
        )
    elif did_update:
        # The submodule is detached and no --branch resolved a source: there
        # is no branch to regenerate a description from or a state to toggle
        # against. `render_pr_outcome` defaults a missing `update` to a no-op
        # on the update path, so nothing further is needed here beyond the
        # user-facing warning — and only when the user actually asked for
        # something that needed the missing branch; a bare re-run with no
        # such flag has nothing to silently ignore.
        if description or title is not None or body is not None or ready is not None:
            warn(
                f"{scope.prefix}--description/--title/--body/--ready/--draft "
                f"could not be applied to PR #{created.number}: the submodule "
                f"is detached and no source branch was resolved. Pass --branch "
                f"to select one."
            )
    pr_flow.render_pr_outcome(
        scope,
        url=created.url,
        number=created.number,
        is_update=did_update,
        publish_name=published.publish_name,
        forced=published.forced,
        ready=ready,
        update=update,
    )
    if incus.config_get(full, "user.jailbee.pr"):
        info(
            "Merge this submodule PR first; the superproject PR's gitlink bump "
            "then points at a merged commit."
        )
    if web:
        pr_mod.open_pr_in_browser(scope.repo_root, created.number)


net_app = typer.Typer(
    name="net",
    help="Switch container network mode.",
    no_args_is_help=True,
)
app.add_typer(net_app)


egress_app = typer.Typer(
    name="egress",
    help=(
        "Allow a container — or this host's copy of the repo — to reach a "
        "host in strict mode.\n\n"
        "Also available as the shorter `jailbee egress ...`.\n\n"
        "Overrides are additive: they can widen the allowlist, never narrow "
        "it. Repo-scope overrides are host-local and are NOT committed — use "
        "`jailbee net egress export` to promote one into `.jailbee/config.yaml`."
    ),
    no_args_is_help=True,
)
net_app.add_typer(egress_app)
# Same group at the root as a short alias. Hidden so `jailbee --help` stays
# readable; named explicitly in the group's own help above, because an alias
# nobody is told about is dead code.
app.add_typer(egress_app, name="egress", hidden=True)


def _egress_target(
    name: str | None,
    repo: bool,
    cfg: "Config",
) -> tuple["IncusType", str | None]:
    """Resolve the (incus, container) an egress command acts on.

    `--repo` short-circuits container resolution: a repo-scope change needs
    no container, and prompting for one would be a lie about what it touches.
    """
    from jailbee.incus import Incus

    if repo:
        return Incus(), None
    return _resolve_existing(cfg, name)


def _repin_hosts_quietly(cfg: "Config", incus: "IncusType", name: str) -> None:
    """Best-effort /etc/hosts re-pin after an override change.

    A stopped container has no /etc/hosts to write; an exec failure must not
    make the override itself look like it failed, because it did not.
    """
    from jailbee.hosts import apply_hosts
    from jailbee.incus import IncusError

    try:
        apply_hosts(cfg, incus, name, mirror_endpoint=_mirror_endpoint_or_none(cfg, incus))
    except IncusError as e:
        warn(f"Override stored, but /etc/hosts was not re-pinned on '{name}': {e}")


def _egress_container_mode(cfg: "Config", incus: "IncusType", name: str) -> str:
    """The mode to materialise a container's extra ACL under.

    `apply_container_acl` owns a container-local `eth0` device, and a
    container-local device SHADOWS the assigned network profile. Hardcoding
    `mode="strict"` here would silently pin a currently-loose container back
    to `incusbr0` with the strict allowlist enforced, while `jailbee ls`
    still reported it loose. Same fallback `snapshots.restore_snapshot` uses.
    """
    from jailbee.lifecycle import current_network_mode

    return current_network_mode(cfg, incus, name) or "strict"


@egress_app.command("add")
def egress_add_cmd(
    entry: str,
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    repo: Annotated[
        bool,
        typer.Option("--repo", help="Apply to every container of this repo on this host."),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Allow one host. Scoped to one container unless --repo is given."""
    from sqlmodel import Session

    from jailbee import egress_scope
    from jailbee.db import get_engine
    from jailbee.egress import NetworkResolveError, parse_egress_entry

    cfg = _load_or_exit(config)
    try:
        parse_egress_entry(entry)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(2) from e

    if entry in cfg.effective_egress_allow():
        info(f"'{entry}' is already allowed by your config — nothing to do.")
        return

    # Resolve before storing: an unresolvable host is the user's typo, and
    # they can fix it now.
    try:
        egress_scope.resolve_entries([entry])
    except NetworkResolveError as e:
        error(f"{e}\nNothing was stored.")
        raise typer.Exit(1) from e

    incus, container = _egress_target(name, repo, cfg)
    with Session(get_engine()) as session:
        if repo:
            if not egress_scope.add_repo_extra(session, cfg.container_prefix, entry, now=_now()):
                info(f"'{entry}' is already a repo override — nothing to do.")
                return
            success(f"Added repo override '{entry}'. Run `jailbee apply` to push it.")
            return

        assert container is not None
        extras = egress_scope.container_extras(incus, container)
        if entry in extras:
            info(f"'{entry}' is already an override on '{container}' — nothing to do.")
            return
        egress_scope.set_container_extras(incus, container, [*extras, entry])
        mode = _egress_container_mode(cfg, incus, container)
        egress_scope.apply_container_acl(cfg, incus, container, mode=mode)
    _repin_hosts_quietly(cfg, incus, container)
    success(f"'{container}' may now reach {entry}.")


@egress_app.command("rm")
def egress_rm_cmd(
    entry: str,
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    repo: Annotated[
        bool,
        typer.Option("--repo", help="Remove a repo-scope override."),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Remove an override.

    An entry that exists ONLY in config.yaml must be edited there instead —
    overrides can only widen the allowlist, never narrow config. But an
    entry that has ALSO been stored as an override (typically one promoted
    with `jailbee net egress export` and then pasted, or simply re-added
    on purpose) removes normally: it's the override row that goes away, not
    the config line, so additive-only still holds. Checking "is this also
    stored as an override" needs the repo/container override list, so that
    check runs before the config-sourced refusal, not instead of it.
    """
    from sqlmodel import Session

    from jailbee import egress_scope
    from jailbee.db import get_engine

    cfg = _load_or_exit(config)
    incus, container = _egress_target(name, repo, cfg)
    with Session(get_engine()) as session:
        if repo:
            if not egress_scope.remove_repo_extra(session, cfg.container_prefix, entry):
                if entry in cfg.effective_egress_allow():
                    error(
                        f"'{entry}' comes from your config, not from a repo "
                        f"override — overrides can only widen the allowlist.\n"
                        f"Edit {_resolve_config_path(config)} and run `jailbee apply`."
                    )
                else:
                    error(f"'{entry}' is not a repo override.")
                raise typer.Exit(1)
            success(f"Removed repo override '{entry}'. Run `jailbee apply` to push it.")
            return

        assert container is not None
        extras = egress_scope.container_extras(incus, container)
        if entry not in extras:
            if entry in cfg.effective_egress_allow():
                error(
                    f"'{entry}' comes from your config, not from an override "
                    f"on '{container}' — overrides can only widen the allowlist.\n"
                    f"Edit {_resolve_config_path(config)} and run `jailbee apply`."
                )
            else:
                error(f"'{entry}' is not an override on '{container}'.")
            raise typer.Exit(1)
        egress_scope.set_container_extras(incus, container, [e for e in extras if e != entry])
        mode = _egress_container_mode(cfg, incus, container)
        egress_scope.apply_container_acl(cfg, incus, container, mode=mode)
    _repin_hosts_quietly(cfg, incus, container)
    success(f"'{container}' can no longer reach {entry}.")


@egress_app.command("ls")
def egress_ls_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    config: ConfigOption = None,
) -> None:
    """Show every egress entry that applies, and where it came from.

    With no container name, only repo-scope entries (config + repo-scope
    overrides) are shown — a read command must not prompt for a container.
    Pass a container name (branch or full) to also see its own overrides.
    """
    from sqlmodel import Session

    from jailbee import egress_scope
    from jailbee.db import get_engine
    from jailbee.incus import Incus

    cfg = _load_or_exit(config)
    incus = Incus()
    container: str | None = None
    if name is not None:
        # Resolve through the same resolver add/rm use, so a branch or short
        # name works identically across all four commands — passing the raw
        # argument straight to container_extras() would silently look up the
        # wrong (or no) container and show an empty picture with no error.
        incus, container = _resolve_existing(cfg, name)

    with Session(get_engine()) as session:
        rows = egress_scope.classify_sources(cfg, session, incus, container=container)

    from jailbee.tui import console

    type Row = egress_scope.EntryRow
    all_fields: list[table_format.FieldSpec[Row]] = [
        table_format.FieldSpec(
            name="entry",
            header="ENTRY",
            cell=lambda r: r.entry,
            json=lambda r: r.entry,
        ),
        table_format.FieldSpec(
            name="source",
            header="SOURCE",
            cell=lambda r: r.source,
            json=lambda r: r.source,
        ),
        table_format.FieldSpec(
            name="note",
            header="NOTE",
            cell=lambda r: "redundant — already in config.yaml" if r.redundant else "",
            json=lambda r: "redundant" if r.redundant else "",
            # Only worth a column when at least one row has something to say.
            show_if=lambda rs: any(r.redundant for r in rs),
        ),
    ]

    table_format.emit(
        rows,
        all_fields,
        fmt=fmt,
        fields=None,
        console=console,
        title=f"Egress entries for {cfg.container_prefix}" if fmt == "table" else None,
        empty_message="No egress entries.",
    )
    if container is None:
        from jailbee.tui import hint

        # stderr, not stdout: `--format json`'s output is piped/parsed, and
        # this notice must never land inside it.
        hint(
            [
                "Container-scope overrides are not shown here — pass a "
                "container name, e.g. `jailbee net egress ls <container>`, "
                "to see them too."
            ]
        )


@egress_app.command("export")
def egress_export_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Print overrides as a replacement for the config's `egress_allow:` key.

    With no container name, only repo-scope overrides are promoted — a read
    command must not prompt for a container. Pass a container name to also
    promote its own overrides.
    """
    from sqlmodel import Session

    from jailbee import egress_scope
    from jailbee.db import get_engine
    from jailbee.incus import Incus

    cfg = _load_or_exit(config)
    config_path = _resolve_config_path(config)
    if not config_path.is_file():
        error(f"No repo config at {config_path} — there is no key to replace.")
        raise typer.Exit(1)

    incus = Incus()
    container: str | None = None
    if name is not None:
        # Same resolver add/rm use — see egress_ls_cmd's comment.
        incus, container = _resolve_existing(cfg, name)

    with Session(get_engine()) as session:
        overrides = list(egress_scope.repo_extras(session, cfg.container_prefix))
        if container is not None:
            overrides += egress_scope.container_extras(incus, container)

    typer.echo(
        egress_scope.render_config_block(
            egress_scope.repo_file_egress_allow(config_path),
            overrides,
            prefix=cfg.container_prefix,
        ),
        nl=False,
    )
    if container is None:
        from jailbee.tui import hint

        # stderr, not stdout: stdout here is the literal replacement block —
        # pasted straight into config.yaml — and must stay pure YAML.
        hint(
            [
                "Container-scope overrides are not included — pass a "
                "container name, e.g. `jailbee net egress export "
                "<container>`, to include them too."
            ]
        )


@dataclass(frozen=True)
class _LooseTtl:
    """An explicitly chosen loose TTL.

    Distinguishes "the caller decided" from "fall back to the config
    policy": `_switch(ttl=None)` uses the policy, while
    `_switch(ttl=_LooseTtl(duration=None))` means the user asked for no
    auto-revert at all.
    """

    duration: timedelta | None


def _validate_duration_answer(raw: str) -> bool | str:
    """questionary validator: True when parseable, else the error message."""
    from jailbee.config import parse_loose_ttl

    try:
        parse_loose_ttl(raw)
    except ValueError as e:
        return str(e)
    return True


def _prompt_loose_ttl(default_after: str) -> _LooseTtl | None:
    """Ask how long the container should stay in loose.

    Returns None when the user cancelled. A returned `_LooseTtl` with
    `duration=None` means "no auto-revert".
    """
    import questionary

    from jailbee.config import LOOSE_TTL_PRESETS, parse_loose_ttl

    presets = list(LOOSE_TTL_PRESETS)
    if default_after not in presets:
        presets.insert(0, default_after)

    # Explicit sentinels: `questionary.Choice` treats `value=None` as *unset*
    # and falls back to the title, so a cancel entry with `value=None` would
    # answer the string "cancel" and get parsed as a duration.
    custom = "__custom__"
    cancel = "__cancel__"
    choices = [
        questionary.Choice(
            title=f"{p}  (config default)" if p == default_after else p,
            value=p,
        )
        for p in presets
    ]
    choices.append(questionary.Choice(title="no auto-revert", value="never"))
    choices.append(questionary.Choice(title="custom…", value=custom))
    choices.append(questionary.Choice(title="cancel", value=cancel))

    answer = questionary.select(
        "Keep loose for how long?",
        choices=choices,
        default=default_after,
    ).ask()
    # `None` is Ctrl-C; `cancel` is the menu entry. Both abort.
    if answer is None or answer == cancel:
        return None
    if answer == custom:
        raw = questionary.text(
            "Duration (e.g. 30s, 45m, 4h; max 24h, or `never`):",
            default=default_after,
            validate=_validate_duration_answer,
        ).ask()
        if raw is None:
            return None
        answer = raw
    return _LooseTtl(duration=parse_loose_ttl(answer))


def _switch(
    name: str | None,
    mode: str,
    cfg: "Config",
    *,
    no_revert: bool = False,
    ttl: _LooseTtl | None = None,
    policy: "LooseAutoRevert | None" = None,
) -> None:
    """Switch ``name`` to ``mode`` and maintain the loose TTL labels.

    ``policy`` is the caller-resolved ``cfg.effective_loose_auto_revert(gcfg)``
    (None = auto-revert disabled). It is read only when ``mode == "loose"`` and
    neither ``ttl`` nor ``no_revert`` already decides the TTL, so callers that
    cannot reach that branch (``net strict``) need not resolve it — and no
    caller pays for a global-config read it does not use.
    """
    from jailbee.lifecycle import (
        current_network_mode,
        short_name,
        switch_network,
    )

    incus, resolved = _resolve_existing(cfg, name)
    mirror_endpoint = _mirror_endpoint_or_none(cfg, incus) if mode == "strict" else None
    if mode == "strict" and mirror_endpoint is None:
        # `_mirror_endpoint_or_none` stays silent — it is also the `start` /
        # `restart` path, where the pin is incidental. Here it is not: going
        # strict removes the container's direct route to Docker Hub, so a repo
        # that wants the mirror and cannot reach it ends up with a dockerd
        # proxying to a host it can no longer resolve. `jailbee new --network
        # loose` with the mirror down is a legitimate way to reach this state.
        from jailbee.docker_daemon import mirror_wanted

        if mirror_wanted(cfg, _load_global()):
            warn(
                "Registry mirror unavailable — this strict container gets no "
                "/etc/hosts pin for it, so `docker pull` inside it will fail. "
                "Fix with 'jailbee registry up && jailbee apply'."
            )

    # Capture pre-switch mode for ``loose_revert_to``. None means no
    # recognised jailbee net profile attached — default to "strict" as the
    # safest fallback.
    pre_mode = current_network_mode(cfg, incus, resolved)

    try:
        switch_network(cfg, incus, resolved, mode, mirror_endpoint=mirror_endpoint)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from e

    # Label lifecycle. Only ``loose`` may set labels; everything else
    # clears them so the timer never operates on stale state.
    if mode == "loose":
        if ttl is not None:
            chosen = ttl.duration
        elif no_revert:
            # --no-revert never needs the policy's TTL, so its .duration()
            # must stay unevaluated: a malformed loose_auto_revert.after
            # (e.g. unparseable or >24h) must not break --no-revert.
            chosen = None
        else:
            chosen = policy.duration() if policy is not None else None
        if no_revert or chosen is None:
            incus.config_unset(resolved, "user.jailbee.loose_until")
            incus.config_unset(resolved, "user.jailbee.loose_revert_to")
        else:
            until = _now() + chosen
            # Preserve revert_to if already loose; otherwise capture pre_mode.
            if pre_mode == "loose":
                existing = incus.config_get(resolved, "user.jailbee.loose_revert_to")
                revert_to = existing or "strict"
            else:
                # Only strict remains as a revert target; a pre_mode of None
                # (no recognised net profile attached) lands here too.
                revert_to = "strict"
            incus.config_set(resolved, "user.jailbee.loose_revert_to", revert_to)
            incus.config_set(resolved, "user.jailbee.loose_until", until.isoformat())
    else:
        incus.config_unset(resolved, "user.jailbee.loose_until")
        incus.config_unset(resolved, "user.jailbee.loose_revert_to")

    success(f"Container '{short_name(cfg, resolved)}' is now on network: {mode}")


@net_app.command("strict")
def net_strict(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Switch to strict (egress allowlist)."""
    _switch(name, "strict", _load_or_exit(config))


@net_app.command("loose")
def net_loose(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    config: ConfigOption = None,
    for_: Annotated[
        str | None,
        typer.Option(
            "--for",
            help=(
                "How long to stay in loose before auto-reverting to strict "
                "(e.g. `30s`, `45m`, `4h`; max 24h). `never` skips the "
                "auto-revert, same as --no-revert. Omit it and jailbee asks, or "
                "uses `loose_auto_revert.after` when there is no TTY."
            ),
        ),
    ] = None,
    no_revert: Annotated[
        bool,
        typer.Option(
            "--no-revert",
            help=("Skip the auto-revert TTL — stay in loose until manually switched."),
        ),
    ] = False,
) -> None:
    """Switch to loose (full NAT)."""
    from jailbee.config import format_loose_after, parse_loose_ttl
    from jailbee.lifecycle import _stdin_is_interactive

    if for_ is not None and no_revert:
        error(
            "--for and --no-revert are mutually exclusive "
            "(`--for never` is the same as --no-revert)."
        )
        raise typer.Exit(2)

    cfg = _load_or_exit(config)

    ttl: _LooseTtl | None = None
    # Local annotations aren't evaluated at runtime, so the TYPE_CHECKING-only
    # import suffices here — unlike the quoted parameter annotations above.
    policy: LooseAutoRevert | None = None
    if for_ is not None:
        try:
            ttl = _LooseTtl(duration=parse_loose_ttl(for_))
        except ValueError as e:
            error(str(e))
            raise typer.Exit(2) from e
    elif not no_revert:
        # Resolved once and handed to _switch: the prompt's default and
        # _switch's fallback TTL are the same policy. --for and --no-revert
        # skip this entirely, so neither pays for the global-config read.
        policy = cfg.effective_loose_auto_revert(_load_global())
        # A configured-off policy means there is no auto-revert to schedule,
        # so asking would be misleading.
        if policy is not None and _stdin_is_interactive():
            ttl = _prompt_loose_ttl(format_loose_after(policy.after))
            if ttl is None:
                raise typer.Abort()

    _switch(name, "loose", cfg, no_revert=no_revert, ttl=ttl, policy=policy)


# `jailbee net refresh` + `status` + `unregister` — egress pool control.


@net_app.command("refresh")
def net_refresh_cmd(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Refresh only this repo (debug)"),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit results as JSON"),
    ] = False,
) -> None:
    """Resolve egress allowlist, merge into pool, push ACL/hosts."""
    from dataclasses import asdict
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee import egress_pool
    from jailbee.db import get_engine
    from jailbee.egress_pool import RefreshResult
    from jailbee.incus import Incus

    gcfg = _load_global()
    incus = Incus()
    now = datetime.now(UTC)

    with Session(get_engine()) as session:
        if repo is not None:
            cfg = load_config(repo)
            results: dict[str, RefreshResult] = {
                cfg.container_prefix: egress_pool.refresh_pool(
                    cfg,
                    gcfg,
                    incus,
                    session,
                    now=now,
                ),
            }
        else:
            results = egress_pool.refresh_all(session, gcfg, incus, now=now)

    if json_output:
        out = {k: asdict(v) for k, v in results.items()}
        typer.echo(json.dumps(out, default=str))
        return

    error_seen = False
    for prefix, r in results.items():
        if r.status not in ("ok", "partial"):
            error(f"FAIL {prefix}: {r.error}")
            error_seen = True
            continue
        if r.added or r.removed:
            added_str = ", ".join(f"{h}:{ip}" for h, ip in r.added) or "—"
            info(
                f"pool {prefix}-allowlist: +{len(r.added)} -{len(r.removed)} ({added_str})",
            )
    if error_seen:
        raise typer.Exit(code=1)


@net_app.command("status")
def net_status_cmd() -> None:
    """Show timer health, registered repos, and per-repo pool sizes."""
    import subprocess

    from sqlmodel import Session, select

    from jailbee.db import get_engine
    from jailbee.db.models import PoolIP, RefreshState, RegisteredRepo

    proc = subprocess.run(
        ["systemctl", "--user", "is-active", "jailbee-net-refresh.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    timer_state = proc.stdout.strip() or "unknown"
    typer.echo(f"Timer: jailbee-net-refresh.timer ({timer_state})")
    typer.echo("")

    with Session(get_engine()) as session:
        repos = session.exec(select(RegisteredRepo)).all()
        typer.echo(f"Registered repos: {len(repos)}")
        for repo in repos:
            typer.echo("")
            typer.echo(f"{repo.container_prefix}  ({repo.repo_root})")
            state = session.get(RefreshState, repo.container_prefix)
            if state is not None:
                typer.echo(
                    f"  Last refresh: {state.last_refresh_at.isoformat()} "
                    f"({state.last_refresh_status})",
                )
            else:
                typer.echo("  Last refresh: never")

            pool_rows = session.exec(
                select(PoolIP).where(
                    PoolIP.container_prefix == repo.container_prefix,
                )
            ).all()
            by_host: dict[str, list[PoolIP]] = {}
            for row in pool_rows:
                by_host.setdefault(row.hostname, []).append(row)
            for host, rows in sorted(by_host.items()):
                typer.echo(f"  Pool: {host:30s} → {len(rows)} IPs")

    # Auto-revert: list each loose-mode container, with or without TTL.
    _print_loose_status()
    _print_port_forward_status()
    _print_egress_override_status()


def _print_loose_status() -> None:
    """Render the auto-revert section of `jailbee net status`.

    Best-effort — silently skips when no repo config is reachable from cwd
    or Incus is unavailable.
    """
    from jailbee.incus import Incus
    from jailbee.lifecycle import format_duration_short, list_containers

    try:
        cfg = _load_or_exit(None)
    except typer.Exit:
        return
    try:
        infos = list_containers(cfg, Incus())
    except Exception:
        return

    loose_rows = [i for i in infos if i.network == "loose"]
    if not loose_rows:
        return

    typer.echo("")
    with_ttl = [i for i in loose_rows if i.loose_until is not None]
    header = (
        f"Auto-revert: {len(loose_rows)} container(s) in loose mode with active TTL"
        if with_ttl
        else f"Auto-revert: {len(loose_rows)} container(s) in loose mode (--no-revert)"
    )
    typer.echo(header)

    now = _now()
    for c in loose_rows:
        short = c.display_name
        if c.loose_until is None:
            typer.echo(f"  {short:<14}no expiry          (--no-revert)")
            continue
        delta = c.loose_until - now
        typer.echo(
            f"  {short:<14}expires in {format_duration_short(delta):<10}(→ strict)",
        )


def _print_port_forward_status() -> None:
    """Render the port-forward section of `jailbee net status`.

    Best-effort, like `_print_loose_status`: silent when no repo config is
    reachable from cwd or Incus is unavailable. Forwards belong in this
    command because Incus's forkproxy connects directly into (or out of)
    the container's network namespace, so a forward's traffic never
    traverses the bridge the ACL is attached to. The ACL is deny-by-default
    on both egress and ingress (see `network.py`), so neither direction of
    a forward is filtered by it — which means `net strict` alone no longer
    describes the whole boundary.
    """
    from jailbee import ports
    from jailbee.incus import Incus
    from jailbee.lifecycle import list_containers

    try:
        cfg = _load_or_exit(None)
    except typer.Exit:
        return
    try:
        incus = Incus()
        infos = list_containers(cfg, incus)
        by_container = ports.list_forwards(incus, [i.name for i in infos])
    except Exception:
        return

    rows = [(i, by_container.get(i.name, [])) for i in infos]
    active = [(i, fwds) for i, fwds in rows if fwds]
    if not active:
        return

    total = sum(len(fwds) for _, fwds in active)
    typer.echo("")
    typer.echo(
        f"Port forwards: {total} on {len(active)} container(s) — the network ACL does not see these"
    )
    for info_row, fwds in active:
        for fwd in fwds:
            # The direction word, not an arrow: it is the same word the
            # commands use, and an arrow would raise the very "which way?"
            # question the vocabulary exists to settle.
            typer.echo(
                f"  {info_row.display_name:<14}{fwd.direction:<14}"
                f"{fwd.proto} container {fwd.container.display}  "
                f"host {fwd.host.display}  ({fwd.source})"
            )


def _list_containers_for_status(cfg: "Config", incus: "IncusType") -> list[str]:
    """Container names of this repo. Factored out so tests can patch one symbol."""
    from jailbee.lifecycle import list_containers

    return [c.name for c in list_containers(cfg, incus)]


def _print_egress_override_status() -> None:
    """Render the egress-override section of `jailbee net status`.

    Two separate exits, because they mean different things:

    - No repo config reachable from cwd is ordinary use — `net status` is a
      global command (timer state, every registered repo), routinely run
      outside any one repo checkout — so that case returns silently, like
      `_print_loose_status`'s.
    - Once a repo config is found, the rest of the fetch (container listing,
      the DB session, every per-container `container_extras` call — each of
      which is a real `incus config get` subprocess call) is guarded by its
      own try so a failure there (Incus unreachable, a container destroyed
      between the listing and its own query, the state DB unavailable)
      can't crash the rest of `jailbee net status`. Unlike the sibling
      sections, that failure is NOT silent: this section is the feature's
      audit surface — it reports a widening of a security boundary that
      never passed code review, where the siblings report conveniences —
      so it prints a one-line note on stderr before returning. A security-
      relevant widening that silently stops being reported is worse than a
      noisy status command. Training the user to expect a note on every
      routine no-repo invocation would defeat that purpose, which is why
      the first exit stays silent.
    """
    from sqlmodel import Session

    from jailbee import egress_scope
    from jailbee.db import get_engine
    from jailbee.tui import hint

    try:
        cfg = load_config(find_repo_config())
    except Exception:
        return

    try:
        from jailbee.incus import Incus

        incus = Incus()
        names = _list_containers_for_status(cfg, incus)
        with Session(get_engine()) as session:
            repo_rows = egress_scope.repo_extras(session, cfg.container_prefix)
            per_container = {name: egress_scope.container_extras(incus, name) for name in names}
    except Exception:
        hint(["Could not gather egress-override status for `jailbee net status`."])
        return

    per_container = {k: v for k, v in per_container.items() if v}
    if not repo_rows and not per_container:
        return

    typer.echo("")
    typer.echo("Egress overrides (host-local, not in git):")
    for entry in repo_rows:
        typer.echo(f"  repo {cfg.container_prefix}: {entry}")
    for name, entries in sorted(per_container.items()):
        typer.echo(f"  {name}: {', '.join(entries)}")


@net_app.command("unregister")
def net_unregister_cmd(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repo (default: cwd)"),
    ] = None,
    prefix: Annotated[
        str | None,
        typer.Option("--prefix", help="Target container_prefix directly"),
    ] = None,
) -> None:
    """Remove the cwd (or --repo) registration from the refresh registry."""
    from sqlmodel import Session

    from jailbee.db import get_engine
    from jailbee.db.models import RegisteredRepo

    target_prefix: str
    if prefix is not None:
        target_prefix = prefix
    else:
        cfg = load_config(repo or Path.cwd())
        target_prefix = cfg.container_prefix

    with Session(get_engine()) as session:
        row = session.get(RegisteredRepo, target_prefix)
        if row is None:
            typer.echo(f"not registered: {target_prefix}")
            return
        session.delete(row)
        session.commit()
        typer.echo(f"unregistered: {target_prefix}")


@net_app.command("install")
def net_install_cmd() -> None:
    """Install (or refresh) the jailbee-net-refresh user systemd timer + service.

    Idempotent: safe to re-run after upgrading jailbee. Called by ``make install``
    so the timer stays in sync with the unit template shipped in the package.
    """
    from jailbee.init_command import install_systemd_units

    install_systemd_units()


job_app = typer.Typer(
    name="job",
    help="Inspect and clear background job state (`jailbee new/destroy --background`).",
    no_args_is_help=True,
)
app.add_typer(job_app)


def _jobs_for_repo(cfg: "Config", *, all_repos: bool) -> dict[str, "BackgroundJob"]:
    """Job rows for this repo, or for every repo with ``all_repos``."""
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine

    with Session(get_engine()) as session:
        if all_repos:
            return background.list_all_jobs(session)
        return background.list_jobs(session, cfg.container_prefix)


@job_app.command("ls")
def job_ls(
    all_repos: Annotated[
        bool,
        typer.Option("--all-repos", help="Show jobs from every repo, not just this one."),
    ] = False,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help="Comma-separated fields: name, repo, kind, phase, pid, age, error, log.",
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """List in-flight and failed background jobs."""
    from jailbee import jobs
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    rows_by_name = _jobs_for_repo(cfg, all_repos=all_repos)
    table_format.emit(
        [rows_by_name[name] for name in sorted(rows_by_name)],
        jobs.job_field_specs(now=_now(), all_repos=all_repos),
        fmt=fmt,
        fields=fields,
        console=console,
        title="jailbee background jobs" if fmt == "table" else None,
        empty_message="[dim](no background jobs)[/dim]",
    )


@job_app.command("log")
def job_log(
    name: Annotated[
        str,
        typer.Argument(
            help="Container whose job log to print.",
            autocompletion=completion.complete_container,
        ),
    ],
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Keep printing as the worker writes.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Print the worker log of a background job."""
    from pathlib import Path as _Path

    from jailbee import jobs
    from jailbee.lifecycle import lookup_background_job, short_name
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    row = lookup_background_job(cfg, name)
    if row is None:
        error(f"no background job for '{name}'")
        raise typer.Exit(1)

    path = _Path(row.log_path)
    if not path.is_file():
        error(f"log file for '{short_name(cfg, row.container_name)}' is gone: {path}")
        raise typer.Exit(1)

    if follow:
        jobs.follow_log(path, write=lambda chunk: console.out(chunk, end=""))
        return
    console.out(path.read_text(errors="replace"), end="")


def _report_clear(cfg: "Config", full_name: str, outcome: "ClearOutcome") -> None:
    """Print the user-facing result of one clear attempt."""
    from jailbee.lifecycle import short_name

    short = short_name(cfg, full_name)
    if outcome.reason == "failed":
        success(f"Cleared failed {outcome.kind} job for '{short}' (container untouched).")
        return
    if outcome.reason == "stale":
        success(
            f"Cleared stale {outcome.kind} job for '{short}' "
            f"(worker gone at phase '{outcome.phase}')."
        )
        return
    if outcome.reason == "missing":
        error(f"no background job for '{short}'")
        return
    error(
        f"'{short}' job is still running "
        f"({outcome.kind}, phase={outcome.phase}, pid {outcome.pid})."
    )
    info(f"  Watch it:  jailbee job log {short} --follow")
    info(f"  Give up:   kill {outcome.pid}, then jailbee job clear {short}")


@job_app.command("clear")
def job_clear(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Container whose job record to clear.",
            autocompletion=completion.complete_container,
        ),
    ] = None,
    all_: Annotated[
        bool, typer.Option("--all", help="Clear every clearable job in this repo.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Acknowledge a dead background job. The container is never touched.

    Clears a job whose phase is ``failed`` or whose worker process is gone.
    A job whose worker is still alive is refused — clearing it would leave a
    running worker building a container jailbee no longer tracks.
    """
    from sqlmodel import Session

    from jailbee import background
    from jailbee.db import get_engine
    from jailbee.lifecycle import lookup_background_job, short_name

    if name is not None and all_:
        error("--all and a container name are mutually exclusive")
        raise typer.Exit(2)

    cfg = _load_or_exit(config)

    if name is not None:
        row = lookup_background_job(cfg, name)
        if row is None:
            error(f"no background job for '{name}'")
            raise typer.Exit(1)
        full_name = row.container_name
        with Session(get_engine()) as session:
            outcome = background.clear_job(session, full_name)
        _report_clear(cfg, full_name, outcome)
        raise typer.Exit(0 if outcome.cleared else 1)

    rows = _jobs_for_repo(cfg, all_repos=False)
    if not all_:
        if not rows:
            error("no background jobs in this repo")
            raise typer.Exit(1)
        error("no container name given; pass a name or --all. Known jobs:")
        for full_name in sorted(rows):
            row = rows[full_name]
            label = background.job_label(row.phase, row.pid, kind=row.op_kind)
            info(f"  {short_name(cfg, full_name)}  ({row.op_kind}, phase={label})")
        raise typer.Exit(1)

    if not rows:
        info("No background jobs to clear.")
        return
    for full_name in sorted(rows):
        with Session(get_engine()) as session:
            outcome = background.clear_job(session, full_name)
        _report_clear(cfg, full_name, outcome)


# ---- Base (golden image) commands ----

base_app = typer.Typer(
    name="base",
    help="Manage the golden base image.",
    no_args_is_help=True,
)
app.add_typer(base_app)


@base_app.command("build")
def base_build_cmd(config: ConfigOption = None) -> None:
    """Build the golden image (long operation, takes several minutes)."""
    from jailbee.golden import build_golden_image
    from jailbee.incus import Incus, IncusError

    cfg = _load_or_exit(config)
    try:
        build_golden_image(cfg, Incus())
    except IncusError as e:
        # A provisioning failure carries apt's own stdout and stderr, which
        # is the diagnosis. Typer installs no global handler, so without this
        # it arrives wrapped in a Rich traceback that pushes the useful part
        # off the top of the screen.
        error(str(e))
        raise typer.Exit(1) from e

    _record_upgrade_action(cfg, "base_build")


@base_app.command("prune")
def base_prune_cmd(
    all_repos: Annotated[
        bool, typer.Option("--all", help="Prune archives for all registered repos.")
    ] = False,
    days: Annotated[
        int | None,
        typer.Option("--days", help="Only prune archives older than N days."),
    ] = None,
    yes_to_all: Annotated[bool, typer.Option("--yes-to-all")] = False,
    config: ConfigOption = None,
) -> None:
    """Prune dated archive golden images (`<alias>-YYYY-MM-DD`).

    The live base image is always kept. In-use archives are skipped. With
    `--all`, prunes archives for every registered repo, not just the current
    one.
    """
    from datetime import UTC, datetime, timedelta

    from jailbee.golden import find_all_archived_images, find_archived_images
    from jailbee.incus import Incus, IncusError
    from jailbee.maintenance import humanize

    incus = Incus()
    if all_repos:
        from sqlmodel import Session, select

        from jailbee.db import get_engine
        from jailbee.db.models import RegisteredRepo

        with Session(get_engine()) as session:
            repos = session.exec(select(RegisteredRepo)).all()
        base_aliases = sorted({f"{r.container_prefix}-base" for r in repos})
        archives = find_all_archived_images(incus, base_aliases)
    else:
        cfg = _load_or_exit(config)
        archives = find_archived_images(cfg, incus)

    if days is not None:
        cutoff = datetime.now(UTC).date() - timedelta(days=days)
        archives = [a for a in archives if a.date < cutoff]
    if not archives:
        info("Nothing to prune.")
        return

    total = sum(a.size_bytes for a in archives)
    info(f"Found {len(archives)} archived image(s), {humanize(total)} total:")
    for a in archives:
        info(f"  - {a.alias}  ({a.date.isoformat()}, {humanize(a.size_bytes)})")

    if not yes_to_all and not typer.confirm(
        f"Delete these {len(archives)} image(s) ({humanize(total)})?"
    ):
        info("Aborted.")
        return

    for a in archives:
        try:
            incus.image_delete(a.alias)
            success(f"Deleted {a.alias}")
        except IncusError as e:
            if "in use" in str(e).lower():
                warn(f"Skipped {a.alias}: image is in use")
            else:
                warn(f"Skipped {a.alias}: {e}")


@base_app.command("usage")
def base_usage_cmd(
    all_repos: Annotated[
        bool, typer.Option("--all", help="Show usage for all registered repos.")
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Show disk usage of golden base images (live image + dated archives)."""
    from rich.table import Table

    from jailbee.golden import gather_golden_usage
    from jailbee.incus import Incus
    from jailbee.maintenance import humanize
    from jailbee.tui import console

    incus = Incus()
    if all_repos:
        from sqlmodel import Session, select

        from jailbee.db import get_engine
        from jailbee.db.models import RegisteredRepo

        with Session(get_engine()) as session:
            repos = session.exec(select(RegisteredRepo)).all()
        base_aliases = sorted({f"{r.container_prefix}-base" for r in repos})
    else:
        cfg = _load_or_exit(config)
        base_aliases = [cfg.golden.alias]

    usages = [
        u
        for u in gather_golden_usage(incus, base_aliases)
        if u.live_size_bytes is not None or u.archives
    ]
    if not usages:
        info("No golden images found.")
        return

    table = Table(title="Golden image disk usage")
    table.add_column("IMAGE")
    table.add_column("DATE")
    table.add_column("SIZE", justify="right")

    grand_total = 0
    grand_prunable = 0
    for u in usages:
        subtotal = 0
        prunable = 0
        if u.live_size_bytes is not None:
            table.add_row(u.base_alias, "[dim]live[/dim]", humanize(u.live_size_bytes))
            subtotal += u.live_size_bytes
        for a in u.archives:
            table.add_row(a.alias, a.date.isoformat(), humanize(a.size_bytes))
            subtotal += a.size_bytes
            prunable += a.size_bytes
        table.add_row(
            f"[bold]{u.base_alias} subtotal[/bold]",
            "",
            f"[bold]{humanize(subtotal)}[/bold]  (prunable {humanize(prunable)})",
        )
        table.add_section()
        grand_total += subtotal
        grand_prunable += prunable

    table.add_row(
        "[bold]Total[/bold]",
        "",
        f"[bold]{humanize(grand_total)}[/bold]  (prunable {humanize(grand_prunable)})",
    )
    console.print(table)


# ---- Registry mirror commands ----


def _load_or_exit(config_path: Path | None) -> "Config":
    """Load config or exit cleanly with error.

    A typo'd column name in the repo's own ``ls``/``dashboard`` blocks is a
    personal display preference, not a reason to break this command: `load_config`
    already recovered from it (dropped names, defaults restored where nothing
    valid remained) before returning here — this just prints what it fixed
    as a warning, tagged with the config file so it isn't confused with the
    equivalent global-layer warning (see ``_load_global``). `jailbee config
    validate` is the one place such a typo is still an error (it calls
    `load_config_unsanitized` directly, bypassing this function).
    """
    try:
        path = _resolve_config_path(config_path)
        cfg = load_config(path)
    except ConfigError as e:
        # `error_plain`: a validator message can carry square brackets — the
        # `host_ports` name rule quotes the regex `[a-z0-9][a-z0-9-]*` — and
        # `error` would read them as Rich style tags and silently delete the
        # rule the message exists to state.
        error_plain(str(e))
        raise typer.Exit(1) from e
    for w in cfg.column_warnings():
        warn(f"{path}: {w}")
    return cfg


def _load_global() -> GlobalConfig:
    """Load global config; fall back to defaults if file is missing/invalid.

    Genuine host-level schema errors (bad YAML, a malformed
    ``docker_registry_mirror``, ...) are reported and the command exits 1 —
    this matches the treatment of repo-config errors. A typo'd column name
    in the ``ls``/``dashboard`` blocks is different: it is a personal
    display preference, not a reason to break an unrelated command, so
    ``load_global_config`` already recovered from it (dropped names,
    defaults restored where nothing valid remained) before returning here —
    this just prints what it fixed, once, as a warning rather than exiting,
    tagged with the global config file's path so it isn't confused with an
    equivalent warning from the repo layer (see ``_load_or_exit``).
    `jailbee config validate` is the one place such a typo is still an error.
    """
    gpath = default_global_config_path()
    try:
        gcfg, warnings = load_global_config(gpath)
    except ConfigError as e:
        # `error_plain`: a validator message can carry square brackets — the
        # `host_ports` name rule quotes the regex `[a-z0-9][a-z0-9-]*` — and
        # `error` would read them as Rich style tags and silently delete the
        # rule the message exists to state.
        error_plain(str(e))
        raise typer.Exit(1) from e
    for w in warnings:
        warn(f"{gpath}: {w}")
    return gcfg


def _mirror_endpoint_or_none(cfg: "Config", incus: "IncusType") -> tuple[str, int] | None:
    """Resolve the registry mirror's (ip, port) best-effort.

    Returns ``None`` when this repo does not want the mirror or the container
    isn't in a usable state (e.g. not yet started). Used by operations that
    pin `jailbee-registry-mirror.incus` into strict containers' /etc/hosts but
    should NOT abort the operation if the mirror is unavailable — the pin is
    best-effort.
    """
    from jailbee.docker_daemon import compute_mirror_endpoint, mirror_wanted

    gcfg = _load_global()
    if not mirror_wanted(cfg, gcfg):
        return None
    try:
        return compute_mirror_endpoint(incus, gcfg)
    except ValueError:
        return None


registry_app = typer.Typer(
    name="registry",
    help="Docker registry mirror control.",
    no_args_is_help=True,
)
app.add_typer(registry_app)


@registry_app.command("up")
def registry_up_cmd(
    recreate: Annotated[
        bool,
        typer.Option(
            "--recreate",
            help="Delete the mirror container and provision it from scratch. "
            "The host-side cache and CA are preserved.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Start the Docker registry mirror Incus container.

    Repairs a half-provisioned mirror automatically. Use `--recreate` when
    that isn't enough.
    """
    from jailbee.docker_daemon import MIRROR_DNS_NAME
    from jailbee.incus import Incus, IncusError
    from jailbee.registry import registry_up
    from jailbee.tui import status_with_elapsed

    _load_or_exit(config)
    gcfg = _load_global()
    incus = Incus()
    if recreate:
        info(
            "Recreating jailbee-registry-mirror from scratch; "
            "the host-side cache and CA are preserved."
        )
    # A first `registry up` downloads a base image and then installs podman
    # inside the container. Without a live status line that is minutes of
    # silence, which reads as a hang — the same treatment `jailbee new` gives
    # its background wait.
    try:
        with status_with_elapsed("starting the registry mirror") as status:
            registry_up(incus, gcfg, recreate=recreate, on_step=status.update)
    except (IncusError, RuntimeError) as e:
        # Typer installs no global handler, so without this an apt timeout
        # inside the mirror reached the user as a Rich traceback with the
        # whole provisioning script in it. Both are expected failures with
        # messages already written for a human: IncusError from the
        # provisioning exec, RuntimeError from `_ensure_service_active`,
        # which names `--recreate` as the next thing to try.
        error(str(e))
        raise typer.Exit(1) from e
    success(f"Registry mirror running on {MIRROR_DNS_NAME}:{gcfg.docker_registry_mirror.port}")


@registry_app.command("down")
def registry_down_cmd(config: ConfigOption = None) -> None:
    """Stop the Docker registry mirror."""
    from jailbee.incus import Incus
    from jailbee.registry import registry_down

    _load_or_exit(config)
    incus = Incus()
    registry_down(incus)
    success("Registry mirror stopped")


@registry_app.command("status")
def registry_status_cmd(config: ConfigOption = None) -> None:
    """Show registry mirror status."""
    from jailbee.incus import Incus
    from jailbee.registry import registry_status

    _load_or_exit(config)
    status = registry_status(Incus())
    info(f"Registry mirror: {status.value}")


# ---- Mount commands ----


@app.command("mount")
def mount_cmd(
    kind: str,
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Add an optional bind mount (e.g. 'aws') to a container."""
    from jailbee.lifecycle import short_name
    from jailbee.mounts import add_optional_mount

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    try:
        add_optional_mount(cfg, incus, name, kind)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(2) from e
    success(f"Mounted '{kind}' in container '{short_name(cfg, name)}'")


@app.command("unmount")
def unmount_cmd(
    kind: str,
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Remove an optional bind mount from a container."""
    from jailbee.lifecycle import short_name
    from jailbee.mounts import remove_optional_mount

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    try:
        remove_optional_mount(cfg, incus, name, kind)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(2) from e
    success(f"Unmounted '{kind}' from container '{short_name(cfg, name)}'")


# ---- Snapshot commands ----

snapshot_app = typer.Typer(
    name="snapshot",
    help="Snapshot management.",
    no_args_is_help=True,
)
app.add_typer(snapshot_app)


@snapshot_app.command("create")
def snap_create_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    tag: Annotated[str | None, typer.Argument()] = None,
    config: ConfigOption = None,
) -> None:
    """Create a snapshot of a container."""
    from jailbee.lifecycle import short_name
    from jailbee.snapshots import create_snapshot

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    actual = create_snapshot(incus, name, tag)
    success(f"Snapshot '{actual}' created for {short_name(cfg, name)}")


@snapshot_app.command("restore")
def snap_restore_cmd(
    name: Annotated[
        str,
        typer.Argument(autocompletion=completion.complete_container),
    ],
    tag: Annotated[
        str,
        typer.Argument(autocompletion=completion.complete_snapshot),
    ],
    config: ConfigOption = None,
) -> None:
    """Restore a snapshot."""
    from jailbee.lifecycle import short_name
    from jailbee.snapshots import restore_snapshot

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    restore_snapshot(cfg, incus, name, tag)
    success(f"Snapshot '{tag}' restored on {short_name(cfg, name)}")


@snapshot_app.command("ls")
def snap_ls_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help="Comma-separated list of fields to show. Allowed: name, created.",
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """List snapshots."""
    from jailbee.lifecycle import short_name
    from jailbee.snapshots import list_snapshots
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    snaps = list_snapshots(incus, name)
    short = short_name(cfg, name)

    if not snaps and fmt == "table":
        info(f"No snapshots for {short}")
        return

    all_fields: list[table_format.FieldSpec[dict[str, Any]]] = [
        table_format.FieldSpec(
            name="name",
            header="NAME",
            cell=lambda s: str(s.get("name", "?")),
            json=lambda s: s.get("name"),
        ),
        table_format.FieldSpec(
            name="created",
            header="CREATED",
            cell=lambda s: str(s.get("created_at", "?")),
            json=lambda s: s.get("created_at"),
        ),
    ]

    table_format.emit(
        snaps,
        all_fields,
        fmt=fmt,
        fields=fields,
        console=console,
        title=f"Snapshots for {short}" if fmt == "table" else None,
    )


@snapshot_app.command("delete")
def snap_delete_cmd(
    name: Annotated[
        str,
        typer.Argument(autocompletion=completion.complete_container),
    ],
    tag: Annotated[
        str,
        typer.Argument(autocompletion=completion.complete_snapshot),
    ],
    config: ConfigOption = None,
) -> None:
    """Delete a snapshot."""
    from jailbee.lifecycle import short_name
    from jailbee.snapshots import delete_snapshot

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    delete_snapshot(incus, name, tag)
    success(f"Snapshot '{tag}' deleted from {short_name(cfg, name)}")


# ---- Port forwarding commands ----

port_app = typer.Typer(
    name="port",
    help="Forward ports between a container and the host.",
    no_args_is_help=True,
)
app.add_typer(port_app)


def _parse_port(raw: int) -> int:
    """Validate a port before anything reaches Incus.

    Incus accepts an out-of-range port at device-add time and fails only when
    the device starts, so the check has to be here.
    """
    if not 1 <= raw <= 65535:
        raise typer.BadParameter(f"port must be 1..65535, got {raw}")
    return raw


def _parse_proto(raw: str) -> str:
    """Validate a forward's protocol before anything reaches Incus.

    `config.HostPort.proto` restricts `host_ports` entries to tcp/udp; the
    ad-hoc `jailbee port` commands must enforce the same restriction, or a
    value like `sctp` reaches Incus and surfaces only as `ports._translate`'s
    generic fallback error.
    """
    if raw not in ("tcp", "udp"):
        raise typer.BadParameter(f"--proto must be 'tcp' or 'udp', got {raw!r}")
    return raw


def _parse_ip_literal(raw: str, *, option: str) -> str:
    """Validate a forward endpoint address before anything reaches Incus.

    Mirrors `config.HostPort`'s own `_validate_address`. Without this, a
    hostname (or any other non-IP value) reaches `ports._probe_free_port` /
    `ports.host_port_free`, which open a raw socket and let the resulting
    `OSError`/`gaierror` escape uncaught for `--host-port auto`, or get
    swallowed by `host_port_free`'s `except OSError: return False` into a
    confidently wrong "already in use" diagnosis for an explicit
    `--host-port N`.
    """
    import ipaddress

    try:
        ipaddress.ip_address(raw)
    except ValueError as e:
        raise typer.BadParameter(f"{option} must be an IP literal, not a hostname: {raw!r}") from e
    return raw


@port_app.command("to-container")
def port_to_container_cmd(
    port: Annotated[int, typer.Argument(help="Container-side port to listen on.")],
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    host_port: Annotated[
        int | None,
        typer.Option("--host-port", help="Host-side port. Defaults to PORT."),
    ] = None,
    proto: Annotated[
        str,
        typer.Option(
            "--proto",
            help="tcp (default) or udp.",
            autocompletion=completion.complete_choices("tcp", "udp"),
        ),
    ] = "tcp",
    host_address: Annotated[
        str, typer.Option("--host-address", help="Host address to connect to.")
    ] = "127.0.0.1",
    container_address: Annotated[
        str,
        typer.Option("--container-address", help="Container address to listen on."),
    ] = "127.0.0.1",
    config: ConfigOption = None,
) -> None:
    """Make a host service reachable inside the container.

    The container listens on PORT; connections land on the host. This is a
    hole through `net strict` by construction — see docs/security.md.
    """
    from jailbee import ports
    from jailbee.lifecycle import short_name

    container_port = _parse_port(port)
    resolved_host_port = _parse_port(host_port) if host_port is not None else container_port
    proto = _parse_proto(proto)
    host_address = _parse_ip_literal(host_address, option="--host-address")
    container_address = _parse_ip_literal(container_address, option="--container-address")
    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    try:
        fwd = ports.add_forward(
            incus,
            name,
            direction="to-container",
            proto=proto,
            container_port=container_port,
            host_port=resolved_host_port,
            container_address=container_address,
            host_address=host_address,
        )
    except ports.PortError as e:
        error_plain(str(e))
        raise typer.Exit(1) from e
    # error_plain's counterpart: an IPv6 endpoint's bracketed display
    # (`[fd00::1]:5037`) is otherwise read as a Rich style tag and silently
    # deleted from the line.
    success_plain(
        f"{short_name(cfg, name)}: connecting to {fwd.container.display} "
        f"inside the container now reaches the host's {fwd.host.display} "
        f"({fwd.device})"
    )


@port_app.command("to-host")
def port_to_host_cmd(
    port: Annotated[int, typer.Argument(help="Container-side port to connect to.")],
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    host_port: Annotated[
        str | None,
        typer.Option(
            "--host-port",
            help="Host-side port, or 'auto' to pick a free one. Defaults to PORT.",
        ),
    ] = None,
    proto: Annotated[
        str,
        typer.Option(
            "--proto",
            help="tcp (default) or udp.",
            autocompletion=completion.complete_choices("tcp", "udp"),
        ),
    ] = "tcp",
    host_address: Annotated[
        str, typer.Option("--host-address", help="Host address to listen on.")
    ] = "127.0.0.1",
    container_address: Annotated[
        str,
        typer.Option("--container-address", help="Container address to connect to."),
    ] = "127.0.0.1",
    config: ConfigOption = None,
) -> None:
    """Make a container service reachable on the host.

    The host listens; connections land inside the container. Not available in
    repo config: a host port is machine-wide, so declaring one per repo would
    make the repo's containers fight over it.
    """
    from jailbee import ports
    from jailbee.lifecycle import short_name

    container_port = _parse_port(port)
    proto = _parse_proto(proto)
    host_address = _parse_ip_literal(host_address, option="--host-address")
    container_address = _parse_ip_literal(container_address, option="--container-address")
    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)

    try:
        if host_port == "auto":
            taken = set(ports.declared_host_ports(incus, exclude=name))
            resolved_host_port = ports.allocate_host_port(host_address, taken)
        else:
            if host_port is None:
                resolved_host_port = container_port
            else:
                try:
                    numeric_host_port = int(host_port)
                except ValueError:
                    raise typer.BadParameter(
                        f"--host-port must be a port number or 'auto', got {host_port!r}"
                    ) from None
                # Route through `_parse_port` so a numeric-but-out-of-range value
                # (e.g. `-1`) gets the same "port must be 1..65535" message as
                # `to-container`, instead of the "or 'auto'" message above, which
                # is misleading once the value has already parsed as a number.
                resolved_host_port = _parse_port(numeric_host_port)
            ports.check_host_port(incus, host_address, resolved_host_port, container=name)
        fwd = ports.add_forward(
            incus,
            name,
            direction="to-host",
            proto=proto,
            container_port=container_port,
            host_port=resolved_host_port,
            container_address=container_address,
            host_address=host_address,
        )
    except ports.PortError as e:
        error_plain(str(e))
        raise typer.Exit(1) from e
    # error_plain's counterpart: an IPv6 endpoint's bracketed display
    # (`[fd00::1]:5037`) is otherwise read as a Rich style tag and silently
    # deleted from the line.
    success_plain(
        f"{short_name(cfg, name)}: connecting to {fwd.host.display} on the "
        f"host now reaches {fwd.container.display} inside the container "
        f"({fwd.device})"
    )


@port_app.command("rm")
def port_rm_cmd(
    handle: Annotated[
        str,
        typer.Argument(
            help="Device name, host_ports name, or container port.",
            autocompletion=completion.complete_port_handle,
        ),
    ],
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Remove one port forward from a container."""
    from jailbee import ports
    from jailbee.lifecycle import short_name

    cfg = _load_or_exit(config)
    incus, name = _resolve_existing(cfg, name)
    try:
        fwd = ports.remove_forward(incus, name, handle)
    except ports.PortError as e:
        error(str(e))
        raise typer.Exit(1) from e
    success(f"Removed {fwd.device} from {short_name(cfg, name)}")


@port_app.command("ls")
def port_ls_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help=(
                "Comma-separated fields. Allowed: container, device, "
                "direction, proto, container_endpoint, host_endpoint, source."
            ),
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """List port forwards. With no container, lists every container of the repo.

    Every proxy device is listed, including one added with `incus` directly —
    it shows as source `other`. Hiding those would misreport what the
    container can reach.
    """
    from rich.markup import escape

    from jailbee import ports
    from jailbee.incus import Incus
    from jailbee.lifecycle import list_containers, short_name
    from jailbee.tui import console

    cfg = _load_or_exit(config)

    rows: list[tuple[str, ports.Forward]] = []
    if name is None:
        incus = Incus()
        infos = list_containers(cfg, incus)
        by_container = ports.list_forwards(incus, [i.name for i in infos])
        for info_row in infos:
            for fwd in by_container.get(info_row.name, []):
                rows.append((info_row.display_name, fwd))
        title = f"Port forwards for {cfg.container_prefix}"
    else:
        incus, resolved = _resolve_existing(cfg, name)
        short = short_name(cfg, resolved)
        rows = [(short, fwd) for fwd in ports.forwards_for(incus, resolved)]
        title = f"Port forwards for {short}"

    type Row = tuple[str, ports.Forward]
    all_fields: list[table_format.FieldSpec[Row]] = [
        table_format.FieldSpec(
            name="container",
            header="CONTAINER",
            cell=lambda r: r[0],
            json=lambda r: r[0],
            show_if=lambda rs: len({r[0] for r in rs}) > 1,
        ),
        table_format.FieldSpec(
            name="device",
            header="HANDLE",
            cell=lambda r: r[1].device,
            json=lambda r: r[1].device,
        ),
        table_format.FieldSpec(
            name="direction",
            header="DIRECTION",
            cell=lambda r: r[1].direction,
            json=lambda r: r[1].direction,
        ),
        table_format.FieldSpec(
            name="proto",
            header="PROTO",
            cell=lambda r: r[1].proto,
            json=lambda r: r[1].proto,
        ),
        table_format.FieldSpec(
            name="container_endpoint",
            header="IN CONTAINER",
            # Escaped for the table cell: an IPv6 endpoint's bracketed display
            # (`[fd00::1]:5037`) is otherwise read as a Rich style tag and
            # silently deleted. `json` stays unescaped — it is unstyled,
            # pipeable output (see table_format.emit), not passed through
            # Rich's markup parser.
            cell=lambda r: escape(r[1].container.display),
            json=lambda r: r[1].container.display,
        ),
        table_format.FieldSpec(
            name="host_endpoint",
            header="ON HOST",
            cell=lambda r: escape(r[1].host.display),
            json=lambda r: r[1].host.display,
        ),
        table_format.FieldSpec(
            name="source",
            header="SOURCE",
            cell=lambda r: r[1].source,
            json=lambda r: r[1].source,
        ),
    ]

    table_format.emit(
        rows,
        all_fields,
        fmt=fmt,
        fields=fields,
        console=console,
        title=title if fmt == "table" else None,
        empty_message="No port forwards.",
    )


# ---- GUI launcher commands ----


@app.command("ide")
def ide_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    app_name: Annotated[
        str | None,
        typer.Option(
            "--app",
            help="JetBrains launcher name (e.g. idea, pycharm, webstorm, studio). "
            "Defaults to the repo's `ide:` config value.",
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Don't ask for confirmation when the container's background "
            "job failed or is still unfinished — launch straight away.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Launch JetBrains IDE in the container."""
    from jailbee.gui import open_ide

    cfg = _load_or_exit(config)
    if not cfg.jetbrains.enabled:
        error("JetBrains integration disabled in config (jetbrains.enabled: false).")
        raise typer.Exit(2)
    resolved = app_name or cfg.jetbrains.ide
    incus, name = _resolve_attachable(cfg, name, force=force, attach_cmd="ide")
    open_ide(cfg, incus, name, resolved)


@app.command("chrome")
def chrome_cmd(
    name: Annotated[
        str | None,
        typer.Argument(autocompletion=completion.complete_container),
    ] = None,
    url: Annotated[
        str | None,
        typer.Argument(help="URL to open. Falls back to chrome.url config."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Don't ask for confirmation when the container's background "
            "job failed or is still unfinished — launch straight away.",
        ),
    ] = False,
    config: ConfigOption = None,
) -> None:
    """Launch Chrome in the container."""
    from jailbee.gui import open_chrome

    cfg = _load_or_exit(config)
    if not cfg.chrome.enabled:
        error("Chrome integration disabled in config (chrome.enabled: false).")
        raise typer.Exit(2)
    incus, name = _resolve_attachable(cfg, name, force=force, attach_cmd="chrome")
    open_chrome(cfg, incus, name, url or cfg.chrome.url)


claude_app = typer.Typer(
    name="claude",
    help="Switch which stored Claude Code login this repo's containers use.",
    no_args_is_help=True,
)
app.add_typer(claude_app)


def _claude_ctx(config: Path | None) -> "tuple[Config, GlobalConfig]":
    """The two configs every pool command needs."""
    return _load_or_exit(config), _load_global()


def _claude_fields() -> "list[table_format.FieldSpec[claude_pool.Slot]]":
    from jailbee import table_format

    return [
        table_format.FieldSpec(
            name="account",
            header="ACCOUNT",
            cell=lambda s: f"[bold]{s.name}[/bold]" if s.live else s.name,
            json=lambda s: s.name,
        ),
        table_format.FieldSpec(
            name="org",
            header="ORG",
            cell=lambda s: s.org_hint or "-",
            json=lambda s: s.org_hint,
        ),
        table_format.FieldSpec(
            name="state",
            header="STATE",
            cell=lambda s: "live" if s.live else "parked",
            json=lambda s: "live" if s.live else "parked",
        ),
    ]


def _report_side_effects(change: "claude_pool.PoolChange") -> None:
    """The parts of a pool change that are the same for `use` and `park`."""
    if change.cleared:
        info(f"Recorded account cleared in: {', '.join(change.cleared)}")
    if change.not_cleared:
        warn(
            "Could not clear the recorded account in: "
            f"{', '.join(change.not_cleared)}. Those repos keep naming the previous "
            "account until their config is readable again — authentication is "
            "unaffected."
        )
    if change.live_sessions:
        warn(
            f"A Claude session looks live in: {', '.join(change.live_sessions)}. "
            "The credential swaps on that session's next turn; the account shown "
            "in /status may lag until it restarts."
        )


@claude_app.command("ls")
def claude_ls_cmd(
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option("--fields", help="Comma-separated fields. Allowed: account, org, state."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """List stored Claude logins and which one this repo's containers use."""
    from jailbee import claude_pool
    from jailbee.tui import console

    cfg, gcfg = _claude_ctx(config)
    try:
        slots = claude_pool.list_slots(cfg, gcfg)
    except claude_pool.PoolError as e:
        error(str(e))
        raise typer.Exit(2) from e

    table_format.emit(
        slots,
        _claude_fields(),
        fmt=fmt,
        fields=fields,
        console=console,
        title=f"Claude logins for {claude_pool.holder_dir(cfg)}",
        empty_message=(
            "No stored Claude logins. `jailbee claude park` stores the one in use."
        ),
    )


@claude_app.command("use")
def claude_use_cmd(
    ref: Annotated[
        str, typer.Argument(help="Account email, or the full slot name for an exact match.")
    ],
    config: ConfigOption = None,
) -> None:
    """Switch this repo's containers to a stored login.

    The switch is holder-wide: every repo sharing this credential group moves
    with it. A running Claude session picks the new credential up on its next
    turn — no restart.
    """
    from jailbee import claude_pool
    from jailbee.claude_locks import ClaudeLockTimeout

    cfg, gcfg = _claude_ctx(config)
    try:
        change = claude_pool.switch(cfg, gcfg, ref)
    except (claude_pool.PoolError, ClaudeLockTimeout) as e:
        error(str(e))
        raise typer.Exit(2) from e

    success(f"Switched to {change.activated}")
    if change.parked_as is not None:
        info(f"Parked the previous login as `{change.parked_as}`.")
    _report_side_effects(change)


@claude_app.command("park")
def claude_park_cmd(config: ConfigOption = None) -> None:
    """Store the login in use and leave this repo's holder empty.

    This is how a new account enters the pool: with no credential to find, the
    next `claude` in a container of this holder prompts `/login`, and that
    login lands straight in the holder.
    """
    from jailbee import claude_pool
    from jailbee.claude_locks import ClaudeLockTimeout

    cfg, gcfg = _claude_ctx(config)
    try:
        change = claude_pool.park(cfg, gcfg)
    except (claude_pool.PoolError, ClaudeLockTimeout) as e:
        error(str(e))
        raise typer.Exit(2) from e

    if change.parked_as is None:
        info("Nothing to park: this holder has no stored login.")
        return
    success(f"Parked `{change.parked_as}`")
    _report_side_effects(change)
    info("The next `claude` in a container of this holder will prompt /login.")


@claude_app.command("rm")
def claude_rm_cmd(
    ref: Annotated[str, typer.Argument(help="Account email, or the full slot name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation.")] = False,
    config: ConfigOption = None,
) -> None:
    """Delete a stored login permanently.

    JailBee never contacts Anthropic, so a deleted login can only come back
    through a browser `/login`.
    """
    from jailbee import claude_pool

    cfg, gcfg = _claude_ctx(config)
    try:
        slot = claude_pool.resolve_ref(ref, claude_pool.list_slots(cfg, gcfg))
        if slot.live:
            raise claude_pool.PoolError(
                f"`{slot.name}` is the live account — run `jailbee claude park` first."
            )
    except claude_pool.PoolError as e:
        error(str(e))
        raise typer.Exit(2) from e

    if not yes and not typer.confirm(
        f"Delete stored login `{slot.name}`? It can only come back through a "
        "browser /login.",
        default=False,
    ):
        raise typer.Exit(1)
    try:
        claude_pool.remove_slot(slot)
    except (claude_pool.PoolError, OSError) as e:
        error(f"could not delete `{slot.name}`: {e}")
        raise typer.Exit(2) from e
    success(f"Deleted `{slot.name}` — a browser /login is the only way back")


chrome_pool_app = typer.Typer(
    name="chrome-pool",
    help="Chrome profile pool management.",
    no_args_is_help=True,
)
app.add_typer(chrome_pool_app)


@chrome_pool_app.command("ls")
def chrome_pool_ls_cmd(
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help=(
                "Comma-separated list of fields to show. Allowed: slot, container, "
                "login_data_mtime, size_bytes, size, path."
            ),
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """List Chrome profile pool slots."""
    from datetime import datetime

    from jailbee import chrome_pool
    from jailbee.chrome_pool import SlotInfo
    from jailbee.incus import Incus
    from jailbee.maintenance import humanize
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    slots = chrome_pool.list_slots(cfg, Incus())

    def _mtime_cell(s: SlotInfo) -> str:
        if s.login_data_mtime is None:
            return "-"
        return datetime.fromtimestamp(s.login_data_mtime).isoformat(" ", "seconds")

    def _mtime_json(s: SlotInfo) -> str | None:
        if s.login_data_mtime is None:
            return None
        return datetime.fromtimestamp(s.login_data_mtime).isoformat()

    all_fields: list[table_format.FieldSpec[SlotInfo]] = [
        table_format.FieldSpec(
            name="slot",
            header="SLOT",
            cell=lambda s: s.name,
            json=lambda s: s.name,
        ),
        table_format.FieldSpec(
            name="container",
            header="CONTAINER",
            cell=lambda s: s.container or "[dim](free)[/dim]",
            json=lambda s: s.container,
        ),
        table_format.FieldSpec(
            name="login_data_mtime",
            header="LOGIN DATA MTIME",
            cell=_mtime_cell,
            json=_mtime_json,
        ),
        table_format.FieldSpec(
            name="size",
            header="SIZE",
            cell=lambda s: humanize(s.size_bytes),
            json=lambda s: humanize(s.size_bytes),
            justify="right",
            default_json=False,
        ),
        table_format.FieldSpec(
            name="size_bytes",
            header="SIZE (BYTES)",
            cell=lambda s: str(s.size_bytes),
            json=lambda s: s.size_bytes,
            justify="right",
            default_table=False,
        ),
        table_format.FieldSpec(
            name="path",
            header="PATH",
            cell=lambda s: str(s.path),
            json=lambda s: str(s.path),
            default_table=False,
            default_json=False,
        ),
    ]

    table_format.emit(
        slots,
        all_fields,
        fmt=fmt,
        fields=fields,
        console=console,
        title="Chrome profile pool" if fmt == "table" else None,
        empty_message="[dim](pool is empty)[/dim]",
    )


@chrome_pool_app.command("prune")
def chrome_pool_prune_cmd(config: ConfigOption = None) -> None:
    """Delete all unallocated Chrome profile slots."""
    from jailbee import chrome_pool
    from jailbee.incus import Incus

    cfg = _load_or_exit(config)
    deleted = chrome_pool.prune(cfg, Incus())
    success(f"Pruned {deleted} free slots")


@app.command("exec")
def exec_cmd(
    name: Annotated[
        str,
        typer.Argument(
            help="Container name (short or full).",
            autocompletion=completion.complete_container,
        ),
    ],
    cmd: Annotated[
        list[str],
        typer.Argument(help="Command and args to run as the dev user."),
    ],
    cwd: Annotated[
        str,
        typer.Option(
            "--cwd",
            help="`repo` (default), `home`, or an absolute container path.",
        ),
    ] = "repo",
    config: ConfigOption = None,
) -> None:
    """Run a command in the container as the dev user.

    Examples:
        jailbee exec smoke -- claude
        jailbee exec smoke -- pnpm test
        jailbee exec smoke --cwd home -- ls -la
    """
    import shlex

    from jailbee.config import CONTAINER_USERNAME
    from jailbee.incus import Incus
    from jailbee.lifecycle import container_repo_dir, resolve_container_name

    cfg = _load_or_exit(config)
    incus = Incus()
    try:
        resolved = resolve_container_name(cfg, incus, name)
    except ValueError as e:
        error(str(e))
        raise typer.Exit(1) from e

    if cwd == "home":
        target = f"/home/{CONTAINER_USERNAME}"
    elif cwd == "repo":
        target = container_repo_dir(cfg, incus, resolved)
    else:
        target = cwd

    shell_cmd = " ".join(shlex.quote(a) for a in cmd)
    # Route through `incus exec --user` instead of `sudo -u`: sudo
    # silently filters env vars not in env_keep, dropping any
    # `container.env` entries set on the base profile.
    #
    # A LOGIN shell (`-lc`), like `jailbee shell` and the PR-text bridge use:
    # `incus exec` supplies a bare default PATH, and tools installed per-user
    # live in ~/.local/bin, which only `/etc/profile.d/local-bin.sh` adds.
    # Under plain `bash -c` nothing sources it, so this command's own
    # documented example — `jailbee exec smoke -- claude` — died with
    # "claude: command not found". Verified not to pollute stdout: a
    # non-interactive login shell here emits nothing of its own.
    rc = incus.exec_interactive(
        resolved,
        ["bash", "-lc", f"cd {shlex.quote(target)} && exec {shell_cmd}"],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
        env={
            "HOME": f"/home/{CONTAINER_USERNAME}",
            "USER": CONTAINER_USERNAME,
            "LOGNAME": CONTAINER_USERNAME,
        },
        init_groups=True,
    )
    raise typer.Exit(rc)


# ---- Diagnostics & maintenance ----


@app.command()
def doctor(config: ConfigOption = None) -> None:
    """Run diagnostic checks."""
    from rich.markup import escape
    from rich.table import Table

    from jailbee.doctor import run_checks
    from jailbee.incus import Incus
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    results = run_checks(cfg, Incus(), gcfg=_load_global())

    table = Table(title="Diagnostic checks")
    table.add_column("CHECK")
    table.add_column("STATUS")
    table.add_column("DETAIL")
    for r in results:
        # The STATUS cell is markup, so the whole row is rendered with markup
        # enabled — and details are arbitrary text: exception strings
        # (SQLAlchemy appends `[SQL: ...] [parameters: (...)]`), absolute
        # paths in brackets, the upgrade block's own wording. Unescaped, a
        # bracketed run is silently swallowed as a style tag, or raises
        # MarkupError and takes `doctor` down with it. Escape every detail.
        status = "[green]✓ OK[/green]" if r.ok else "[red]✗ FAIL[/red]"
        table.add_row(escape(r.name), status, escape(r.detail))
    console.print(table)

    if any(not r.ok for r in results):
        raise typer.Exit(1)


@app.command("disk-usage")
def disk_usage_cmd(
    fmt: Annotated[
        str,
        typer.Option(
            "--format",
            "-o",
            help="Output format: table (default) or json.",
            autocompletion=completion.complete_choices("table", "json"),
        ),
    ] = "table",
    fields: Annotated[
        str | None,
        typer.Option(
            "--fields",
            help=(
                "Comma-separated list of fields to show. Allowed: component, "
                "size, size_bytes, path."
            ),
        ),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Show disk usage by component."""
    from jailbee.incus import Incus
    from jailbee.maintenance import DiskRow, gather_disk_usage, humanize
    from jailbee.tui import console

    cfg = _load_or_exit(config)
    gcfg = _load_global()
    rows = gather_disk_usage(cfg, gcfg, Incus())

    all_fields: list[table_format.FieldSpec[DiskRow]] = [
        table_format.FieldSpec(
            name="component",
            header="COMPONENT",
            cell=lambda r: r.component,
            json=lambda r: r.component,
            footer=lambda _rows: "TOTAL",
        ),
        table_format.FieldSpec(
            name="size",
            header="SIZE",
            cell=lambda r: humanize(r.size_bytes),
            json=lambda r: humanize(r.size_bytes),
            justify="right",
            default_json=False,
            footer=lambda rs: humanize(sum(r.size_bytes or 0 for r in rs)),
        ),
        table_format.FieldSpec(
            name="size_bytes",
            header="SIZE (BYTES)",
            cell=lambda r: "n/a" if r.size_bytes is None else str(r.size_bytes),
            json=lambda r: r.size_bytes,
            justify="right",
            default_table=False,
            footer=lambda rs: str(sum(r.size_bytes or 0 for r in rs)),
        ),
        table_format.FieldSpec(
            name="path",
            header="PATH",
            cell=lambda r: r.path,
            json=lambda r: r.path,
        ),
    ]

    table_format.emit(
        rows,
        all_fields,
        fmt=fmt,
        fields=fields,
        console=console,
        title="Disk usage" if fmt == "table" else None,
    )


@app.command()
def prune(
    yes_to_all: Annotated[bool, typer.Option("--yes-to-all")] = False,
    config: ConfigOption = None,
) -> None:
    """Interactively clean up stopped containers older than 30 days."""
    from jailbee.incus import Incus
    from jailbee.lifecycle import destroy_container, short_name
    from jailbee.maintenance import find_stale_stopped

    cfg = _load_or_exit(config)
    incus = Incus()
    stale = find_stale_stopped(cfg, incus, days=30)
    if not stale:
        info("Nothing to prune.")
        return

    info(f"Found {len(stale)} stopped containers older than 30 days:")
    for name in stale:
        info(f"  - {short_name(cfg, name)}")

    if not yes_to_all:
        for name in stale:
            if typer.confirm(f"Destroy '{short_name(cfg, name)}'?"):
                destroy_container(cfg, incus, name, force=True)
                success(f"Destroyed {short_name(cfg, name)}")
            else:
                info(f"Kept {short_name(cfg, name)}")
    else:
        for name in stale:
            destroy_container(cfg, incus, name, force=True)
            success(f"Destroyed {short_name(cfg, name)}")


if __name__ == "__main__":
    app()
