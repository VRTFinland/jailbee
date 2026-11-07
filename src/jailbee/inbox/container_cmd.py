"""Typer adapter for the standard-library orchestration inbox modules."""

from collections.abc import Callable
from typing import Annotated

import typer
from typer._click import Context
from typer._click.core import Parameter, ParameterSource
from typer._click.exceptions import UsageError
from typer.core import TyperGroup

from jailbee.inbox import cli, send, socket_cli
from jailbee.inbox.errors import InboxBadUsage, InboxUsage


class InboxGroup(TyperGroup):
    def parse_args(self, ctx: Context, args: list[str]) -> list[str]:
        if not args and not ctx.resilient_parsing:
            raise UsageError("Missing command.", ctx)
        if args[:1] == ["help"]:
            # The bash-era `jailbee inbox help` alias; keep it working.
            args = ["--help", *args[1:]]
        return super().parse_args(ctx, args)


HELP_SETTINGS = {"help_option_names": ["-h", "--help"]}
inbox_app = typer.Typer(
    cls=InboxGroup,
    context_settings=HELP_SETTINGS,
    help="Wait for, list, read and send orchestration messages.",
    epilog=(
        "Environment: JAILBEE_ORCHESTRATION_SOCKET (broker socket path), "
        "JAILBEE_LITELLM_CONFIG (default /etc/jailbee/litellm.json), JAILBEE_DEFAULT_AGENT."
    ),
)
send_app = typer.Typer(
    cls=InboxGroup,
    context_settings=HELP_SETTINGS,
    help=(
        "Send a request to the host broker and print its id. "
        "Text (body or prompt) comes from --text T, --file PATH or - (stdin); "
        "at most 65536 bytes, not empty. "
        "A first-state refusal prints its reason to stderr and exits 1, even without --wait. "
        "--wait prints other status replies: exit 0 on done/queued/started, 1 on failed. "
        "Exit 3 means no reply (may or may not have run); inspect inbox ls/status before "
        "resending with a new id. Timeout defaults to 30m; none waits indefinitely. "
        "4 = host broker not reachable."
    ),
)
inbox_app.add_typer(send_app, name="send")


def _run(command: Callable[[], int]) -> None:
    from jailbee.inbox import transport

    try:
        code = command()
    except transport.BrokerUnreachable as exc:
        typer.echo(str(exc), err=True)
        code = transport.EXIT_UNREACHABLE
    except transport.BrokerError as exc:
        if exc.code == "store":
            typer.echo(f"{transport.UNREACHABLE_MESSAGE} ({exc.detail or exc.code})", err=True)
            code = transport.EXIT_UNREACHABLE
        else:
            typer.echo(f"{cli.PROG}: {exc.detail or exc.code}", err=True)
            code = 1
    except TimeoutError:
        typer.echo(transport.UNREACHABLE_MESSAGE, err=True)
        code = transport.EXIT_UNREACHABLE
    except InboxBadUsage as exc:
        raise UsageError(str(exc)) from exc
    except InboxUsage as exc:
        typer.echo(f"{cli.PROG}: {exc}", err=True)
        code = 2
    except OSError as exc:
        if exc.__cause__ is not None:
            typer.echo(f"{cli.PROG}: {exc.__cause__}", err=True)
        typer.echo(f"{cli.PROG}: {exc}", err=True)
        code = 1
    except KeyboardInterrupt:
        code = 130
    if code:
        raise typer.Exit(code)


def _nonempty(ctx: typer.Context, param: Parameter, value: str | None) -> str | None:
    if value == "" and ctx.get_parameter_source(param.name or "") != ParameterSource.DEFAULT:
        raise typer.BadParameter("needs a non-empty value")
    return value


def _nonempty_many(value: list[str] | None) -> list[str] | None:
    if value and "" in value:
        raise typer.BadParameter("needs a non-empty value")
    return value


Text = Annotated[str | None, typer.Option("--text", help="Body or prompt text (1-65536 bytes).")]
File = Annotated[
    str | None, typer.Option("--file", callback=_nonempty, help="Read text from PATH.")
]
Source = Annotated[str | None, typer.Argument(help="Use - to read text from stdin.")]
Wait = Annotated[bool, typer.Option("--wait", help="Wait for the matching status reply.")]
Timeout = Annotated[
    str,
    typer.Option(
        "--timeout",
        callback=_nonempty,
        help="Status wait: seconds or <n>s|m|h, or none; needs --wait.",
    ),
]
Child = Annotated[str, typer.Option("--child", callback=_nonempty, help="Child container name.")]
Agent = Annotated[
    str,
    typer.Option(
        "--agent", callback=_nonempty, help="Runner: claude or claude-jb; see inbox runners."
    ),
]
Profile = Annotated[
    str, typer.Option("--profile", callback=_nonempty, help="Runner profile (claude-jb only).")
]
Model = Annotated[
    str,
    typer.Option(
        "--model", callback=_nonempty, help="fable, opus, sonnet or haiku (optional [1m] suffix)."
    ),
]
OnDone = Annotated[
    str, typer.Option("--on-done", callback=_nonempty, help="After accepted done: destroy or keep.")
]
Autostart = Annotated[
    str, typer.Option("--autostart-profile", callback=_nonempty, help="Autostart profile name.")
]
Services = Annotated[
    str | None,
    typer.Option("--services", help="Comma-separated services; an empty list starts none."),
]
Ref = Annotated[str, typer.Option("--ref", callback=_nonempty, help="Source ref or commit.")]
Base = Annotated[str, typer.Option("--base", callback=_nonempty, help="Source base branch.")]
Mounts = Annotated[
    list[str] | None,
    typer.Option("--mount", callback=_nonempty_many, help="Grant a mount; repeatable."),
]
Egress = Annotated[
    list[str] | None,
    typer.Option("--egress", callback=_nonempty_many, help="Grant an egress host; repeatable."),
]


def _send(request: send.SendRequest, source: str | None = None) -> None:
    if source is not None and source != "-":
        raise typer.BadParameter("the text source argument must be -")
    _run(lambda: socket_cli.cmd_send(request))


@inbox_app.command(
    help=(
        "Print the id of the oldest unread message (exit 0); --path is obsolete; "
        "exit 1 on timeout, 4 = host broker not reachable. "
        "Use ls to handle every unread message. Nothing is marked read. "
        "Only one wait may run; a second exits 2."
    )
)
def wait(
    timeout: Annotated[
        str,
        typer.Option(
            "--timeout",
            help=(
                "Seconds or <n>s|m|h (default 30m); 0 checks once, none waits until interrupted."
            ),
        ),
    ] = "30m",
    wake_on: Annotated[
        str | None,
        typer.Option(
            "--wake-on",
            help=(
                "Comma-separated message kinds/status states: note,question,done,blocked,idle,"
                "queued,started,failed,refused. Bare words match either; msg:<kind> or "
                "status:<state> restricts the match. Example: msg:done,question,blocked,idle,"
                "status:failed,status:refused skips delivered-to acks. A child's first message "
                "after each start always wakes."
            ),
        ),
    ] = None,
    quiet_after: Annotated[
        str | None,
        typer.Option(
            "--quiet-after",
            help=(
                "Print quiet-child CHILD (exit 0) after a running, not-done child is silent "
                "for DUR. Its status.activity_at counts as activity. "
                "Needs a fresh broker children view (<=15m)."
            ),
        ),
    ] = None,
    path: Annotated[bool, typer.Option("--path", help="Obsolete; prints the message id.")] = False,
    digest: Annotated[
        str | None,
        typer.Option(
            "--digest",
            help=(
                "Print digest and the status table (exit 0) every DUR while a child is running "
                "and not done. First due DUR after the first wait --digest "
                "(tracked by the broker). "
                "Real messages and quiet-child win; --wake-on does not apply. "
                "Needs a fresh broker children view (<=15m)."
            ),
        ),
    ] = None,
) -> None:
    _run(
        lambda: socket_cli.cmd_wait(
            timeout=timeout, wake_on=wake_on, quiet_after=quiet_after, digest=digest, path=path
        )
    )


@inbox_app.command(
    "ls",
    help=(
        "List unread messages, oldest first: <id> <type> <from or ref> "
        "<kind or state> <first 60 chars of text>."
    ),
)
def ls() -> None:
    _run(socket_cli.cmd_ls)


@inbox_app.command(
    help=(
        "Pretty-print a message and mark it read. "
        "Exit 1 if no unread message has that id, 4 = host broker not reachable."
    )
)
def read(
    ident: Annotated[str | None, typer.Argument(help="Unread message id.")] = None,
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave the message unread in place.")
    ] = False,
) -> None:
    if not ident:
        raise UsageError("read needs a message id")
    _run(lambda: socket_cli.cmd_read(ident, keep=keep))


@inbox_app.command(help="Summarise the orchestration grant available to this container.")
def grant(
    json_output: Annotated[
        bool, typer.Option("--json", help="Print the sanitised grant document.")
    ] = False,
) -> None:
    _run(lambda: socket_cli.cmd_grant(as_json=json_output))


@inbox_app.command(
    help=(
        "List the default runner and runners a spawn or restart may name "
        "(--agent/--profile/--model). Listed model tiers also accept the [1m] suffix."
    )
)
def runners(
    json_output: Annotated[bool, typer.Option("--json", help="Print one array.")] = False,
) -> None:
    _run(lambda: socket_cli.cmd_runners(as_json=json_output))


@inbox_app.command(
    help=(
        "List each child's name, running (container state), latest agent session "
        "and done (accepted done) from the broker."
    )
)
def children(
    json_output: Annotated[bool, typer.Option("--json", help="Print one array.")] = False,
) -> None:
    _run(lambda: socket_cli.cmd_children(as_json=json_output))


@inbox_app.command(
    help=(
        "Show broker children: NAME STATE AGENT ACTIVE CPU MEM HEAD DIRTY LONGEST "
        "LAST-MSG (ages; unknown values print ?). LAST-MSG: up = last message from the "
        "child, down = last delivery to it. Exit 1 for unavailable view or "
        "unknown --child, 4 = host broker not reachable. "
        "Use instead of messaging a child to ask how it is doing."
    )
)
def status(
    child: Annotated[str | None, typer.Option("--child", help="Show only this child.")] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print items plus last_from_child_at.")
    ] = False,
) -> None:
    if child is not None and child.startswith("--"):
        raise UsageError("--child needs a name")
    _run(lambda: socket_cli.cmd_status(child=child, as_json=json_output))


@send_app.command("message", help="Send a message to the orchestrator or a named child.")
def message(
    source: Source = None,
    text: Text = None,
    file: File = None,
    to: Annotated[str, typer.Option("--to", callback=_nonempty, help="Recipient container.")] = "",
    kind: Annotated[
        str, typer.Option("--kind", callback=_nonempty, help="note, question, done or blocked.")
    ] = "",
    reply_to: Annotated[
        str, typer.Option("--reply-to", callback=_nonempty, help="Message id being answered.")
    ] = "",
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(
        send.SendRequest(
            type="message",
            text=text,
            file=file,
            stdin=source == "-",
            to=to,
            kind=kind,
            reply_to=reply_to,
            wait=wait,
            timeout=timeout,
        ),
        source,
    )


@send_app.command(
    help=(
        "Spawn a fork, a new branch (--new), or a PR (--pr). --branch is required except "
        "for --pr: the broker derives the branch from the PR and ignores any supplied "
        "branch. --base needs --new. --warm/--no-warm apply to forks only and replace "
        "fork.warm_paths."
    )
)
def spawn(
    source: Source = None,
    text: Text = None,
    file: File = None,
    branch: Annotated[
        str, typer.Option("--branch", callback=_nonempty, help="Child branch; optional for --pr.")
    ] = "",
    new: Annotated[bool, typer.Option("--new", help="Create a new branch.")] = False,
    base: Base = "",
    ref: Ref = "",
    pr: Annotated[str, typer.Option("--pr", callback=_nonempty, help="Positive PR number.")] = "",
    mounts: Mounts = None,
    loose: Annotated[bool, typer.Option("--loose", help="Grant loose network access.")] = False,
    egress: Egress = None,
    note: Annotated[str, typer.Option("--note", callback=_nonempty, help="Host-side note.")] = "",
    on_done: OnDone = "",
    autostart_profile: Autostart = "",
    services: Services = None,
    agent: Agent = "",
    profile: Profile = "",
    model: Model = "",
    warm: Annotated[
        list[str] | None,
        typer.Option("--warm", callback=_nonempty_many, help="Warm a fork path; repeatable."),
    ] = None,
    no_warm: Annotated[bool, typer.Option("--no-warm", help="Do not warm fork paths.")] = False,
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(
        send.SendRequest(
            type="spawn",
            text=text,
            file=file,
            stdin=source == "-",
            branch=branch,
            new=new,
            base_branch=base,
            ref=ref,
            pr=pr,
            mounts=mounts or [],
            loose=loose,
            egress=egress or [],
            note=note,
            on_done=on_done,
            autostart_profile=autostart_profile,
            services=services,
            agent=agent,
            profile=profile,
            model=model,
            warm=warm or [],
            no_warm=no_warm,
            wait=wait,
            timeout=timeout,
        ),
        source,
    )


@send_app.command("import", help="Import the child's committed branch to the host.")
def import_child(child: Child = "", wait: Wait = False, timeout: Timeout = "") -> None:
    _send(send.SendRequest(type="import", child=child, wait=wait, timeout=timeout))


@send_app.command(
    help=(
        "Update a child from the parent or --ref/--base. "
        "--merge merges after a conflict check; a conflict refuses the update."
    )
)
def update(
    child: Child = "",
    ref: Ref = "",
    base: Base = "",
    merge: Annotated[
        bool, typer.Option("--merge", help="Merge the update into the child's branch.")
    ] = False,
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(
        send.SendRequest(
            type="update",
            child=child,
            ref=ref,
            base_branch=base,
            merge=merge,
            wait=wait,
            timeout=timeout,
        )
    )


@send_app.command(
    help=(
        "Restart a child with a prompt. --resume continues a recorded agent session "
        "(see inbox children). --agent-only relaunches an idle RUNNING child's agent "
        "only: no boot/on_start, no extra slot; cannot use --autostart-profile or --services."
    )
)
def restart(
    source: Source = None,
    text: Text = None,
    file: File = None,
    child: Child = "",
    on_done: OnDone = "",
    autostart_profile: Autostart = "",
    services: Services = None,
    agent_only: Annotated[
        bool, typer.Option("--agent-only", help="Relaunch only the agent.")
    ] = False,
    resume: Annotated[
        str, typer.Option("--resume", callback=_nonempty, help="Recorded session UUID or last.")
    ] = "",
    agent: Agent = "",
    profile: Profile = "",
    model: Model = "",
    default_runner: Annotated[
        bool, typer.Option("--default-runner", help="Clear the child's runner override.")
    ] = False,
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(
        send.SendRequest(
            type="restart",
            text=text,
            file=file,
            stdin=source == "-",
            child=child,
            on_done=on_done,
            autostart_profile=autostart_profile,
            services=services,
            agent_only=agent_only,
            resume=resume,
            agent=agent,
            profile=profile,
            model=model,
            default_runner=default_runner,
            wait=wait,
            timeout=timeout,
        ),
        source,
    )


@send_app.command(
    help=(
        "Refresh services in a child (prepare + restart); "
        "elevated prepares stop and resume its agent."
    )
)
def refresh(
    child: Child = "",
    services: Services = None,
    force: Annotated[bool, typer.Option("--force", help="Force service refresh.")] = False,
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(
        send.SendRequest(
            type="refresh", child=child, services=services, force=force, wait=wait, timeout=timeout
        )
    )


@send_app.command(help="Stop a child container.")
def stop(child: Child = "", wait: Wait = False, timeout: Timeout = "") -> None:
    _send(send.SendRequest(type="stop", child=child, wait=wait, timeout=timeout))


@send_app.command(help="Destroy a child container.")
def destroy(
    child: Child = "",
    force: Annotated[bool, typer.Option("--force", help="Force destruction.")] = False,
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(send.SendRequest(type="destroy", child=child, force=force, wait=wait, timeout=timeout))


@send_app.command(
    help=(
        "Change a child's rights at runtime. A grant needs --for DUR (s|m|h); revokes "
        "need no duration. A child may only revoke its own rights; an orchestrator names "
        "--child. Refused while autostart runs in the child."
    )
)
def rights(
    child: Child = "",
    loose: Annotated[bool, typer.Option("--loose", help="Grant loose network access.")] = False,
    mounts: Mounts = None,
    egress: Egress = None,
    duration: Annotated[
        str, typer.Option("--for", callback=_nonempty, help="Grant duration (s|m|h).")
    ] = "",
    revoke_loose: Annotated[
        bool, typer.Option("--revoke-loose", help="Revoke loose network access.")
    ] = False,
    revoke_mounts: Annotated[
        list[str] | None,
        typer.Option("--revoke-mount", callback=_nonempty_many, help="Revoke a mount; repeatable."),
    ] = None,
    revoke_egress: Annotated[
        list[str] | None,
        typer.Option(
            "--revoke-egress", callback=_nonempty_many, help="Revoke an egress host; repeatable."
        ),
    ] = None,
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(
        send.SendRequest(
            type="rights",
            child=child,
            loose=loose,
            mounts=mounts or [],
            egress=egress or [],
            duration=duration,
            revoke_loose=revoke_loose,
            revoke_mounts=revoke_mounts or [],
            revoke_egress=revoke_egress or [],
            wait=wait,
            timeout=timeout,
        )
    )


@send_app.command(
    help=(
        "Publish exactly one staged issue or PR outbox JSON manifest "
        "(bare file name, not a progress file)."
    )
)
def publish(
    issue: Annotated[
        str, typer.Option("--issue", callback=_nonempty, help="Issue outbox JSON file name.")
    ] = "",
    pr: Annotated[
        str, typer.Option("--pr", callback=_nonempty, help="PR outbox JSON file name.")
    ] = "",
    wait: Wait = False,
    timeout: Timeout = "",
) -> None:
    _send(send.SendRequest(type="publish", issue=issue, pr=pr, wait=wait, timeout=timeout))
