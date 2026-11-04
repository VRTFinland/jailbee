"""Parse and publish orchestration requests without external dependencies."""

import fcntl
import io
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, BinaryIO

from jailbee.inbox.errors import InboxBadUsage, InboxUsage
from jailbee.inbox.render import pretty

if TYPE_CHECKING:
    from jailbee.inbox.cli import Settings


@dataclass(frozen=True)
class SendRequest:
    type: str
    to: str = ""
    kind: str = ""
    reply_to: str = ""
    branch: str = ""
    new: bool = False
    base_branch: str = ""
    loose: bool = False
    note: str = ""
    child: str = ""
    force: bool = False
    autostart_profile: str = ""
    services: str | None = None
    agent_only: bool = False
    merge: bool = False
    resume: str = ""
    agent: str = ""
    profile: str = ""
    model: str = ""
    default_runner: bool = False
    on_done: str = ""
    wait: bool = False
    timeout: str = ""
    ref: str = ""
    pr: str = ""
    text: str | None = None
    file: str | None = None
    stdin: bool = False
    mounts: list[str] = field(default_factory=list)
    egress: list[str] = field(default_factory=list)
    revoke_mounts: list[str] = field(default_factory=list)
    revoke_egress: list[str] = field(default_factory=list)
    duration: str = ""
    revoke_loose: bool = False
    warm: list[str] = field(default_factory=list)
    no_warm: bool = False


def parse_send(args: list[str]) -> SendRequest:
    from jailbee.inbox.cli import parse_timeout

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise InboxBadUsage(message)

    require(bool(args), "send needs a type")
    t = args[0]
    require(
        t
        in {
            "message",
            "spawn",
            "import",
            "update",
            "restart",
            "refresh",
            "stop",
            "destroy",
            "rights",
        },
        f"unknown message type '{t}'",
    )
    values: dict[str, object] = {"type": t}
    flags = {
        "--merge": "merge",
        "--wait": "wait",
        "--new": "new",
        "--no-warm": "no_warm",
        "--loose": "loose",
        "--revoke-loose": "revoke_loose",
        "--force": "force",
        "--default-runner": "default_runner",
        "--agent-only": "agent_only",
        "-": "stdin",
    }
    options = {
        "--text": "text",
        "--file": "file",
        "--to": "to",
        "--kind": "kind",
        "--reply-to": "reply_to",
        "--branch": "branch",
        "--base": "base_branch",
        "--ref": "ref",
        "--warm": "warm",
        "--pr": "pr",
        "--mount": "mounts",
        "--egress": "egress",
        "--note": "note",
        "--child": "child",
        "--agent": "agent",
        "--profile": "profile",
        "--model": "model",
        "--on-done": "on_done",
        "--timeout": "timeout",
        "--for": "duration",
        "--revoke-mount": "revoke_mounts",
        "--revoke-egress": "revoke_egress",
        "--autostart-profile": "autostart_profile",
        "--services": "services",
        "--resume": "resume",
    }
    lists: dict[str, list[str]] = {
        k: [] for k in ("mounts", "egress", "revoke_mounts", "revoke_egress", "warm")
    }
    i = 1
    while i < len(args):
        opt = args[i]
        if opt in flags:
            values[flags[opt]] = True
            i += 1
            continue
        require(opt in options, f"unknown argument '{opt}'")
        require(i + 1 < len(args), f"{opt} needs a value")
        val = args[i + 1]
        require(bool(val) or opt in {"--text", "--services"}, f"{opt} needs a non-empty value")
        key = options[opt]
        if key in lists:
            lists[key].append(val)
        else:
            values[key] = val
        i += 2
    values.update(lists)
    r = SendRequest(**values)  # type: ignore[arg-type]  # Parsed option values match the dataclass fields.
    require(not (r.warm and r.no_warm), "--warm and --no-warm are mutually exclusive")
    require(not (r.warm or r.no_warm) or (t == "spawn" and not r.new and not r.pr),
            "--warm and --no-warm only apply to fork spawns")
    require(not r.merge or t == "update", "--merge only applies to update")
    if r.resume:
        require(t == "restart", "--resume only applies to restart")
        require(
            bool(
                re.fullmatch(
                    r"last|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", r.resume
                )
            ),
            "--resume must be last or a lowercase UUID",
        )
    if r.agent_only:
        require(t == "restart", "--agent-only only applies to restart")
        require(not r.autostart_profile, "--agent-only cannot be combined with --autostart-profile")
        require(r.services is None, "--agent-only cannot be combined with --services")
    if r.services is not None:
        require(
            t in {"spawn", "restart", "refresh"},
            "--services only applies to spawn, restart and refresh",
        )
    if t == "refresh":
        require(
            r.services is not None and any(s.strip() for s in r.services.split(",")),
            "refresh needs at least one service",
        )
    if r.timeout:
        require(r.wait, "--timeout needs --wait")
        parse_timeout(r.timeout)
    if r.on_done:
        require(t in {"spawn", "restart"}, "--on-done only applies to spawn and restart")
        require(r.on_done in {"destroy", "keep"}, "--on-done must be destroy or keep")
    sources = sum((r.text is not None, r.file is not None, r.stdin))
    if t in {"message", "spawn", "restart"}:
        require(sources == 1, f"{t} needs exactly one of --text, --file or -")
    else:
        require(sources == 0, f"{t} takes no text, file or stdin")
    require(
        t == "message" or not (r.to or r.kind or r.reply_to),
        "--to, --kind and --reply-to only apply to message",
    )
    require(
        t == "spawn"
        or not (
            r.branch
            or r.new
            or r.pr
            or r.note
            or (t != "rights" and (r.loose or r.mounts or r.egress))
        ),
        "--branch, --new, --mount, --loose, --egress and --note only apply to spawn",
    )
    require(
        t in {"spawn", "update"} or not (r.ref or r.base_branch),
        "--ref and --base only apply to spawn and update",
    )
    require(
        t != "update" or not (r.ref and r.base_branch), "--ref and --base are mutually exclusive"
    )
    require(
        t in {"destroy", "refresh"} or not r.force, "--force only applies to destroy and refresh"
    )
    require(
        t in {"spawn", "restart"} or not (r.agent or r.profile or r.model or r.default_runner),
        "--agent, --profile, --model and --default-runner only apply to spawn and restart",
    )
    if r.default_runner:
        require(t == "restart", "--default-runner only applies to restart")
        require(not r.agent, "--default-runner cannot be combined with --agent")
    if r.agent:
        require(r.agent in {"claude", "claude-jb"}, "--agent must be claude or claude-jb")
    else:
        require(not (r.profile or r.model), "--profile and --model need --agent")
    require(not r.profile or r.agent == "claude-jb", "--profile needs --agent claude-jb")
    require(
        not r.model or re.fullmatch(r"(?:fable|opus|sonnet|haiku)(?:\[1m\])?", r.model) is not None,
        "--model must be fable, opus, sonnet or haiku",
    )
    require(
        not r.autostart_profile or t in {"spawn", "restart"},
        "--autostart-profile only applies to spawn and restart",
    )
    if t in {"import", "update", "stop", "destroy", "restart", "refresh"}:
        require(bool(r.child), f"{t} needs --child")
    elif t != "rights":
        require(not r.child, f"--child does not apply to {t}")
    if t == "rights":
        granted = bool(r.loose or r.mounts or r.egress)
        revoked = bool(r.revoke_loose or r.revoke_mounts or r.revoke_egress)
        require(granted or revoked, "rights needs at least one grant or revoke")
        require(not granted or bool(r.duration), "rights grants need --for")
        require(granted or not r.duration, "--for needs a grant")
        require(not (r.loose and r.revoke_loose), "cannot grant and revoke loose")
        for item in r.mounts:
            require(item not in r.revoke_mounts, f"cannot grant and revoke mount {item}")
        for item in r.egress:
            require(item not in r.revoke_egress, f"cannot grant and revoke egress {item}")
    else:
        require(
            not (r.duration or r.revoke_loose or r.revoke_mounts or r.revoke_egress),
            "--for and --revoke-* only apply to rights",
        )
    if t == "spawn":
        require(not r.ref or not (r.pr or r.new), "--ref cannot be combined with --pr or --new")
        if r.pr:
            require(not (r.new or r.base_branch), "--pr cannot be combined with --new or --base")
            require(bool(re.fullmatch(r"[1-9][0-9]*", r.pr)), "--pr must be a positive integer")
            if not r.branch:
                from dataclasses import replace

                r = replace(r, branch="pr-placeholder")
        require(bool(r.branch), "spawn needs --branch")
        require(not r.base_branch or r.new, "--base needs --new")
    if t == "message" and r.kind:
        require(
            r.kind in {"note", "question", "done", "blocked"},
            "--kind must be note, question, done or blocked",
        )
    return r


def build_manifest(r: SendRequest, id_: str, body_name: str | None) -> dict[str, object]:
    m: dict[str, object] = {"version": 2 if r.type == "spawn" else 1, "id": id_, "type": r.type}
    runner: dict[str, object] = {}
    if r.agent:
        runner["runner"] = {
            "agent": r.agent,
            **({"profile": r.profile} if r.profile else {}),
            **({"model": r.model} if r.model else {}),
        }
    elif r.default_runner:
        runner["runner"] = None
    for key, value in (
        ("autostart_profile", r.autostart_profile),
        (
            "services",
            None if r.services is None else [s.strip() for s in r.services.split(",") if s.strip()],
        ),
        ("agent_only", True if r.agent_only else None),
        ("resume", r.resume),
        ("on_done", r.on_done),
    ):
        if value is not None and value != "":
            runner[key] = value
    if r.type == "message":
        m["body_file"] = body_name
        for key, value in (("to", r.to), ("kind", r.kind), ("in_reply_to", r.reply_to)):
            if value:
                m[key] = value
    elif r.type == "rights":
        if r.child:
            m["child"] = r.child
        if r.loose or r.mounts or r.egress:
            m["grant"] = {
                "loose": r.loose,
                "mounts": r.mounts,
                "egress": r.egress,
                "for": r.duration,
            }
        if r.revoke_loose or r.revoke_mounts or r.revoke_egress:
            m["revoke"] = {
                "loose": r.revoke_loose,
                "mounts": r.revoke_mounts,
                "egress": r.revoke_egress,
            }
    elif r.type == "spawn":
        m.update(
            {
                "kind": "pr" if r.pr else "new" if r.new else "fork",
                "branch": r.branch,
                "prompt_file": body_name,
                "rights": {"version": 1, "mounts": r.mounts, "loose": r.loose, "egress": r.egress},
            }
        )
        if r.warm or r.no_warm:
            m["warm_paths"] = [] if r.no_warm else r.warm
        for key, spawn_value in (
            ("base", r.base_branch),
            ("ref", r.ref),
            ("pr", Decimal(r.pr) if r.pr else None),
            ("note", r.note),
        ):
            if spawn_value:
                m[key] = spawn_value
        m.update(runner)
    else:
        m["child"] = r.child
        if r.type == "restart":
            m["prompt_file"] = body_name
        if r.type in {"restart", "refresh"}:
            m.update(runner)
        if r.type in {"refresh", "destroy"} and r.force:
            m["force"] = True
        if r.type == "update":
            if r.ref:
                m["ref"] = r.ref
            if r.base_branch:
                m["base"] = r.base_branch
            if r.merge:
                m["merge"] = True
    return m


def new_id(type_: str) -> str:
    return f"{type_}-{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(2)}"


_reservations: list[int] = []


class PublishError(Exception):
    """A send failed after validation, with exit status 1."""


def publish(settings: "Settings", req: SendRequest) -> str:
    settings.outbox.mkdir(parents=True, exist_ok=True)
    id_ = ""
    for _ in range(2):
        candidate = new_id(req.type)
        if (
            not (settings.outbox / f"{candidate}.json").exists()
            and not (settings.outbox / f"{candidate}.md").exists()
        ):
            id_ = candidate
            break
    if not id_:
        raise PublishError("could not find a free manifest id")
    body_name = f"{id_}.md" if req.type in {"message", "spawn", "restart"} else None
    body_tmp = settings.outbox / f".{id_}.md.tmp"
    json_tmp = settings.outbox / f".{id_}.json.tmp"
    body_path = settings.outbox / f"{id_}.md"
    fd: int | None = None
    moved_body = False
    publishing_manifest = False
    try:
        if body_name:
            source: BinaryIO
            if req.text is not None:
                source = io.BytesIO(req.text.encode())
            elif req.file is not None:
                from pathlib import Path

                path = Path(req.file)
                try:
                    if not path.is_file() or not os.access(path, os.R_OK):
                        raise OSError()
                    source = path.open("rb")
                except OSError:
                    raise InboxUsage(f"cannot read file '{req.file}'") from None
            else:
                source = sys.stdin.buffer
            size = 0
            try:
                with body_tmp.open("wb") as output:
                    while chunk := source.read(65536):
                        size += len(chunk)
                        output.write(chunk)
            finally:
                if not req.stdin:
                    source.close()
            if not size:
                raise InboxUsage(f"the {req.type} text is empty")
            if size > 65536:
                raise InboxUsage(f"the {req.type} text is {size} bytes; the limit is 65536")
        json_tmp.write_text(pretty(build_manifest(req, id_, body_name)) + "\n")
        if req.wait:
            lock_dir = settings.base / "status-waits"
            lock_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(lock_dir / f"{id_}.lock", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InboxUsage("status ref is already reserved") from None
        if body_name:
            body_tmp.replace(body_path)
            moved_body = True
        try:
            publishing_manifest = True
            json_tmp.replace(settings.outbox / f"{id_}.json")
        except OSError as exc:
            raise PublishError("could not write the manifest") from exc
        if fd is not None:
            _reservations.append(fd)
            fd = None
        return id_
    except BaseException:
        # After rename the broker may already have consumed the manifest.
        if moved_body and not (publishing_manifest and not json_tmp.exists()):
            body_path.unlink(missing_ok=True)
        raise
    finally:
        body_tmp.unlink(missing_ok=True)
        json_tmp.unlink(missing_ok=True)
        if fd is not None:
            os.close(fd)
