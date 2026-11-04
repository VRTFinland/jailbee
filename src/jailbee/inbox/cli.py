"""Standard-library command entry point for the inbox port."""

import fcntl
import json
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from jailbee.inbox.errors import InboxBadUsage, InboxUsage
from jailbee.inbox.render import (
    SaturatedNumber,
    clean_c0c1,
    clean_c1,
    jq_tostring,
    ls_line,
    pretty,
    render_status,
    sanitize_json_value,
    tsv,
)

PROG = "jailbee inbox"
USAGE = (
    "Usage: jailbee inbox wait [--timeout DUR|none] [--wake-on LIST] [--quiet-after DUR]\n"
    "                          [--digest DUR]\n"
    "       jailbee inbox ls\n"
    "       jailbee inbox read <id> [--keep]\n"
    "       jailbee inbox runners [--json]\n"
    "       jailbee inbox children [--json]\n"
    "       jailbee inbox status [--child C] [--json]\n"
    "       jailbee inbox send <type> [options]\n"
    "\n"
    "  wait   Print the path of the oldest unread message (exit 0), waiting up to\n"
    "         DUR (seconds, or <n>s|m|h; default 30m) for one to arrive; exit 1\n"
    "         on timeout. --timeout 0 checks once; --timeout none waits until a message\n"
    "         arrives or the process is interrupted (use it for a background waiter).\n"
    "         After an arrival, wait briefly for the burst to settle; then use ls to\n"
    "         handle every unread message.\n"
    "         --wake-on LIST filters comma-separated message kinds/status states\n"
    "         (note,question,done,blocked,idle,queued,started,failed,refused). A bare\n"
    "         word matches a message kind or a status state; msg:<kind> matches only a\n"
    "         message, status:<state> only a status reply (e.g. msg:done,question,\n"
    "         blocked,idle,status:failed,status:refused skips `delivered to` acks).\n"
    "         A child's first message after each start always wakes. Nothing is marked read.\n"
    "         --quiet-after DUR prints `quiet-child CHILD` (exit 0) when a running,\n"
    "         not-done child is silent that long; needs fresh broker children.json (<=15m).\n"
    "         A child's own activity (status.activity_at) counts as not silent.\n"
    "         --digest DUR prints `digest` and the `status` table (exit 0) when DUR has\n"
    "         passed since the last digest and a child is running and not done; the\n"
    "         first digest is due DUR after the first `wait --digest` (stamp: digest-at).\n"
    "         A real message or quiet-child wakeup wins; --wake-on does not apply.\n"
    "         Needs fresh broker children.json (<=15m).\n"
    "         Only one wait may run; a second exits 2. Concurrent send --wait is\n"
    "         safe: wait skips its reserved statuses until that sender exits.\n"
    "  ls     One line per unread message, oldest first:\n"
    "         <id> <type> <from or ref> <kind or state> <first 60 chars of text>\n"
    "  read   Pretty-print message <id> and move it to inbox/read/ (--keep leaves\n"
    "         it in place); exit 1 if no unread message has that id.\n"
    "\n"
    "  runners  List the default runner and the runners a spawn or restart may name\n"
    "         (--agent/--profile/--model of send). --json prints one array.\n"
    "         Listed model tiers also accept the [1m] suffix.\n"
    "\n"
    "  children [--json]  Each child: name, running (container state), latest agent\n"
    "         session, done (accepted done) — from the broker's children.json.\n"
    "\n"
    "  status [--child C] [--json]  One line per child from the broker's children.json:\n"
    "         NAME STATE AGENT ACTIVE CPU MEM HEAD DIRTY LONGEST LAST-MSG (ages; unknown\n"
    "         values print ?). LAST-MSG is up = last message from the child, down = last\n"
    "         delivery to it. --json prints the items plus last_from_child_at. Exit 1\n"
    "         for an unavailable/invalid snapshot or unknown --child. Use it instead of\n"
    "         messaging a child to ask how it is doing.\n"
    "\n"
    "  send   Write a manifest for the orchestrator's host into outbox/ and print\n"
    "         its id. --wait [--timeout DUR|none] waits for its status (default 30m):\n"
    "         exit 0 on done/queued/started, 1 on refused/failed, 3 on timeout.\n"
    "         Text (body or prompt) comes from --text T, --file PATH or -\n"
    "         (stdin); at most 65536 bytes, not empty.\n"
    "           message  [--to C] [--kind note|question|done|blocked]\n"
    "                    [--reply-to ID] + text\n"
    "           spawn    [--branch B] [--new [--base B] | --ref REF | --pr N]\n"
    "                    [--mount M]... [--loose] [--egress H]... [--note T]\n"
    "                    [--on-done destroy|keep] [--autostart-profile NAME] [--"
    "services LIST] [runner] + text\n"
    "                    [--warm PATH]... | [--no-warm] (fork only; replaces fork.warm_paths)\n"
    "                    --branch is required except for --pr; the broker derives\n"
    "                    the branch from the PR and ignores any supplied branch.\n"
    "           refresh  --child C --services LIST [--wait]  Refresh services in"
    " a child (prepare + restart); elevated prepares stop and resume its agent.\n"
    "           restart  --child C [--on-done destroy|keep] [--autostart-profile"
    " NAME] [--services LIST]\n"
    "                    [--agent-only] [--resume ID|last] [runner | --default-r"
    "unner] + text\n"
    "                    --resume continues a recorded agent session (see children).\n"
    "                    --agent-only relaunches an idle RUNNING child's agent only;\n"
    "                    no boot/on_start, no extra slot, cannot use --autostart"
    "-profile or --services.\n"
    "                    --services selects comma-separated services; an empty l"
    "ist starts none.\n"
    "           update   --child C [--ref REF | --base BRANCH] [--merge]\n"
    "                    --merge merges the update into the child's branch after a\n"
    "                    conflict check; a conflict refuses it.\n"
    "           import | stop   --child C\n"
    "           destroy  --child C [--force]\n"
    "           rights   [--child C] [--loose] [--mount M]... [--egress H]... --for DUR\n"
    "                    [--revoke-loose] [--revoke-mount M]... [--revoke-egress H]...\n"
    "                    `send rights` changes a child's rights at runtime. A gr"
    "ant needs --for DUR (s|m|h); revokes need no duration. A\n"
    "                    child may only revoke its own rights; an orchestrator names\n"
    "                    --child. Refused while autostart runs in the child.\n"
    "         runner = --agent claude|claude-jb [--profile P (claude-jb only)]\n"
    "                  [--model fable|opus|sonnet|haiku (optional [1m])]; see `runners`.\n"
    "                  --default-runner (restart only) clears the child's runner.\n"
    "\n"
    "Environment: JAILBEE_ORCHESTRATION_DIR (default ~/.jailbee/orchestration),\n"
    "JAILBEE_LITELLM_CONFIG (default /etc/jailbee/litellm.json), JAILBEE_DEFAULT_AGENT,\n"
    "JAILBEE_INBOX_POLL (poll seconds without inotifywait; default 5),\n"
    "JAILBEE_INBOX_SETTLE (quiet seconds after an arrival; default 1, 0 disables).\n"
    "JAILBEE_INBOX_INOTIFY=0 forces polling.\n"
)


@dataclass
class Settings:
    base: Path
    inbox: Path
    outbox: Path
    poll: float
    settle: float


def load_settings(environ: Mapping[str, str]) -> Settings:
    base = Path(
        environ.get("JAILBEE_ORCHESTRATION_DIR")
        or (environ.get("HOME", "") + "/.jailbee/orchestration")
    )
    settle = environ.get("JAILBEE_INBOX_SETTLE") or "1"
    poll = environ.get("JAILBEE_INBOX_POLL") or "5"
    pattern = r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)"
    if not re.fullmatch(pattern, settle):
        raise InboxUsage(
            f"JAILBEE_INBOX_SETTLE must be a non-negative number of seconds, got '{settle}'"
        )
    if not re.fullmatch(pattern, poll) or not re.search(r"[1-9]", poll):
        raise InboxUsage(f"JAILBEE_INBOX_POLL must be a positive number of seconds, got '{poll}'")
    return Settings(base, base / "inbox", base / "outbox", float(poll), float(settle))


def parse_duration(text: str) -> int:
    match = re.fullmatch(r"([0-9]+)([smh]?)", text)
    if not match:
        raise InboxUsage(f"invalid duration '{text}' (use seconds or <n>s|m|h)")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def parse_timeout(text: str) -> int | None:
    return None if text == "none" else parse_duration(text)


def unread_files(inbox: Path) -> list[Path]:
    return sorted(
        p
        for p in inbox.glob("*.json")
        if not p.name.startswith(".") and not p.is_symlink() and p.is_file()
    )


def _parse_number(text: str) -> Decimal | SaturatedNumber:
    mantissa, _, exponent = text.lower().partition("e")
    digits = mantissa.lstrip("-").replace(".", "")
    first = next((i for i, digit in enumerate(digits) if digit != "0"), len(digits))
    sign = "-" if text.startswith("-") else ""
    if exponent:
        exp = (
            int(exponent)
            if len(exponent.lstrip("+-0")) < 12
            else (10**12 if not exponent.startswith("-") else -(10**12))
        )
        adjusted = exp + len(mantissa.lstrip("-").split(".")[0]) - first - 1
        if first == len(digits):
            if exp > 999999999:
                return SaturatedNumber(sign + "0E+999999999")
            if exp < -1147483646:
                return SaturatedNumber(sign + "0E-1147483646")
        elif adjusted > 999999999:
            return SaturatedNumber(sign + "1.7976931348623157e+308")
        else:
            fraction_length = len(mantissa.partition(".")[2])
            drop = -1147483646 - (exp - fraction_length)
            if drop > 0:
                significant = digits[first:]
                if drop > len(significant):
                    return SaturatedNumber(sign + "0E-1147483646")
                kept = significant[:-drop] or "0"
                if significant[-drop] >= "5":
                    # Round half away from zero, preserving the lower-bound scale.
                    kept = str(int(kept) + 1)
                return Decimal((int(bool(sign)), tuple(int(c) for c in kept), -1147483646))
    return Decimal(text)


def _load(path: Path) -> object:
    return json.loads(path.read_text(), parse_float=_parse_number, parse_int=_parse_number)


def message_id(path: Path) -> str | None:
    try:
        doc = _load(path)
        if isinstance(doc, dict):
            ident: object = doc.get("id")
            if isinstance(ident, str):
                return ident
    except (OSError, ValueError, UnicodeError):
        pass
    return None


def _warning(path: Path) -> None:
    print(f"{PROG}: skipping unreadable message {path}", file=sys.stderr)


def cmd_ls(settings: Settings, args: list[str]) -> int:
    if args:
        raise InboxUsage("ls takes no arguments")
    for path in unread_files(settings.inbox):
        try:
            doc = _load(path)
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            print(ls_line(doc))
        except (OSError, ValueError, UnicodeError, TypeError):
            _warning(path)
    return 0


def _clean_json(value: object) -> object:
    if isinstance(value, str):
        return clean_c1(value)
    if isinstance(value, list):
        return [_clean_json(v) for v in value]
    if isinstance(value, dict):
        return {clean_c1(k): _clean_json(v) for k, v in value.items()}
    return value


def cmd_read(settings: Settings, args: list[str]) -> int:
    ident = ""
    keep = False
    for arg in args:
        if arg == "--keep":
            keep = True
        elif arg.startswith("-"):
            raise InboxUsage(f"unknown option '{arg}' (see --help)")
        elif ident:
            raise InboxUsage("read takes one id")
        else:
            ident = arg
    if not ident:
        raise InboxUsage("read needs a message id")
    for path in unread_files(settings.inbox):
        fid = message_id(path)
        if fid is None:
            _warning(path)
            continue
        if fid != ident:
            continue
        try:
            output = pretty(_clean_json(_load(path)))
            if not keep:
                dest = settings.inbox / "read"
                dest.mkdir(parents=True, exist_ok=True)
                path.rename(dest / path.name)
        except (OSError, ValueError, UnicodeError):
            continue
        print(output)
        return 0
    print(f"{PROG}: no unread message with id {ident}", file=sys.stderr)
    return 1


def _load_snapshot(settings: Settings) -> tuple[Path, dict[str, Any]] | None:
    """Read and validate children.json; print the reason and return None on failure."""
    path = settings.base / "children.json"
    if not path.is_file():
        print(f"{PROG}: children.json is unavailable", file=sys.stderr)
        return None
    try:
        doc = _load(path)
        if (
            not isinstance(doc, dict)
            or isinstance(doc.get("version"), bool)
            or doc.get("version") != 1
            or isinstance(doc.get("updated_at"), bool)
            or not isinstance(doc.get("updated_at"), (int, float, Decimal))
            or not isinstance(doc.get("children"), list)
        ):
            raise ValueError("invalid snapshot")
    except (OSError, ValueError, UnicodeError):
        print(f"{PROG}: invalid children.json", file=sys.stderr)
        return None
    if int(time.time()) - doc["updated_at"] > 900:
        print(f"{PROG}: children.json heartbeat is stale (over 15 minutes old)", file=sys.stderr)
    return path, doc


def cmd_children(settings: Settings, args: list[str]) -> int:
    if args not in ([], ["--json"]):
        raise InboxBadUsage("children accepts only --json")
    loaded = _load_snapshot(settings)
    if loaded is None:
        return 1
    path, doc = loaded
    children = doc["children"]
    line = max(1, len(path.read_text().rstrip().splitlines()))
    if args:
        print(pretty(children))
    else:
        for child in children:
            if child is None:
                child = {}
            if not isinstance(child, dict):
                print(
                    f"jq: error (at {path}:{line}): Cannot index {_jq_type(child)} "
                    'with string "name"',
                    file=sys.stderr,
                )
                return 5
            name = child.get("name")
            session = child.get("session")
            session = "-" if session is None or session is False else session
            for value in (name, session):
                if isinstance(value, (dict, list)):
                    print(
                        f"jq: error (at {path}:{line}): {_jq_type(value)} ({jq_tostring(value)}) "
                        "is not valid in a csv row",
                        file=sys.stderr,
                    )
                    return 5
            print(
                tsv(
                    [
                        "" if name is None else jq_tostring(name),
                        jq_tostring(child.get("running")),
                        jq_tostring(session),
                        jq_tostring(child.get("done") is True),
                    ]
                )
            )
    return 0


def cmd_status(settings: Settings, args: list[str]) -> int:
    from jailbee.inbox.waiting import last_from_child

    child: str | None = None
    as_json = False
    rest = iter(args)
    for arg in rest:
        if arg == "--json":
            as_json = True
        elif arg == "--child":
            child = next(rest, None)
            if child is None or child.startswith("--"):
                raise InboxBadUsage("--child needs a name")
        else:
            raise InboxBadUsage("status accepts only --child C and --json")
    loaded = _load_snapshot(settings)
    if loaded is None:
        return 1
    children: list[object] = list(loaded[1]["children"])
    if child is not None:
        children = [c for c in children if isinstance(c, dict) and c.get("name") == child]
        if not children:
            print(f"{PROG}: unknown child '{clean_c0c1(child)}'", file=sys.stderr)
            return 1
    from_child = last_from_child(settings.base)
    if as_json:
        items: list[object] = []
        for item in children:
            if isinstance(item, dict):
                name = item.get("name")
                stamp = from_child.get(name) if isinstance(name, str) else None
                item = {**item, "last_from_child_at": stamp}
            items.append(item)
        print(pretty(sanitize_json_value(items)))
    else:
        print(render_status(children, now=time.time(), from_child=from_child))
    return 0


def _jq_type(value: object) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal, SaturatedNumber)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


class JqError(Exception):
    """A jq-compatible data error."""


def runners_json(settings: Settings) -> list[dict[str, object]]:
    config = Path(os.environ.get("JAILBEE_LITELLM_CONFIG") or "/etc/jailbee/litellm.json")
    try:
        grant = _load(settings.base / "grant.json")
    except (OSError, ValueError, UnicodeError):
        grant = None
    cfg: object = None
    if os.access(config, os.R_OK):
        try:
            cfg = _load(config)
        except (OSError, ValueError, UnicodeError):
            print(
                f"{PROG}: cannot read {config} (not valid JSON); no claude-jb runners listed",
                file=sys.stderr,
            )
    runner = None
    source = None
    if isinstance(grant, dict):
        candidate = grant.get("default_runner")
        if (
            isinstance(candidate, dict)
            and isinstance(candidate.get("agent"), str)
            and isinstance(grant.get("default_runner_source"), str)
            and all(
                candidate.get(k) is None or isinstance(candidate.get(k), str)
                for k in ("profile", "model")
            )
        ):
            runner = {k: clean_c1(v) if isinstance(v, str) else v for k, v in candidate.items()}
            source = clean_c1(grant["default_runner_source"])
    result: list[dict[str, object]] = [
        {
            "runner": runner,
            "default": True,
            "agent": clean_c1(os.environ.get("JAILBEE_DEFAULT_AGENT") or "unknown"),
            "source": source,
        }
    ]
    if not shutil.which("claude"):
        return result
    tiers = ["fable", "opus", "sonnet", "haiku"]
    result.append({"runner": {"agent": "claude"}, "models": tiers})
    if isinstance(cfg, dict) and isinstance(cfg.get("profiles"), dict):
        for name, profile in sorted(cfg["profiles"].items()):
            if profile is not None and not isinstance(profile, dict):
                raise JqError(f'Cannot index {_jq_type(profile)} with string "tiers"')
            aliases = profile.get("tiers") if isinstance(profile, dict) else None
            if aliases is None or aliases is False:
                aliases = {}
            if isinstance(aliases, list):
                aliases = {str(i): v for i, v in enumerate(aliases)}
            if not isinstance(aliases, dict):
                raise JqError(f"{_jq_type(aliases)} ({jq_tostring(aliases)}) has no keys")
            aliases = {k: clean_c1(jq_tostring(v)) for k, v in aliases.items() if k in tiers}
            result.append(
                {
                    "runner": {"agent": "claude-jb", "profile": clean_c1(name)},
                    "models": sorted(aliases),
                    "aliases": aliases,
                    "default_profile": name == cfg.get("default_profile"),
                }
            )
    return result


def cmd_runners(settings: Settings, args: list[str]) -> int:
    for arg in args:
        if arg != "--json":
            raise InboxUsage(f"unknown argument '{arg}' (see --help)")
    try:
        entries = runners_json(settings)
    except JqError as exc:
        print(f"jq: error (at <unknown>): {exc}", file=sys.stderr)
        return 5
    if args:
        print(pretty(entries))
        return 0
    first = entries[0]
    runner = first["runner"]
    if runner is None:
        print(f"default: omit runner ({clean_c0c1(str(first['agent']))})")
    else:
        assert isinstance(runner, dict)
        line = "default: " + clean_c0c1(runner["agent"])
        if runner.get("profile") is not None:
            line += " -P " + clean_c0c1(runner["profile"])
        if runner.get("model") is not None:
            line += " " + clean_c0c1(runner["model"])
        print(line + " (from " + clean_c0c1(str(first["source"])) + ")")
    labels = []
    for entry in entries[1:]:
        r = entry["runner"]
        assert isinstance(r, dict)
        labels.append(
            "claude"
            if r["agent"] == "claude"
            else "claude-jb -P "
            + clean_c0c1(r["profile"])
            + (" (default)" if entry["default_profile"] else "")
        )
    width = max(map(len, labels), default=0)
    for label, entry in zip(labels, entries[1:], strict=True):
        models = entry["models"]
        assert isinstance(models, list)
        aliases = entry.get("aliases")
        assert aliases is None or isinstance(aliases, dict)
        values = " ".join(
            m if aliases is None else m + "=" + clean_c0c1(aliases[m]) for m in models
        )
        print(label.ljust(width) + "  models: " + values)
    return 0


def cmd_wait(settings: Settings, args: list[str]) -> int:
    from jailbee.inbox.waiting import run_wait, valid_wake_token

    timeout: int | None = 1800
    wake: frozenset[str] | None = None
    quiet = 0
    digest = 0
    index = 0
    while index < len(args):
        option = args[index]
        if option not in ("--timeout", "--wake-on", "--quiet-after", "--digest"):
            raise InboxUsage(f"unknown argument '{option}' (see --help)")
        if index + 1 == len(args):
            raise InboxUsage(f"{option} needs a value")
        value = args[index + 1]
        if option == "--timeout":
            timeout = parse_timeout(value)
        elif option == "--quiet-after":
            quiet = parse_duration(value)
            if not quiet:
                raise InboxUsage("--quiet-after must be positive")
        elif option == "--digest":
            digest = parse_duration(value)
            if not digest:
                raise InboxUsage("--digest must be positive")
        else:
            tokens = value.split(",")
            if not all(valid_wake_token(token) for token in tokens):
                raise InboxUsage(
                    "--wake-on needs comma-separated message kinds or status states "
                    "(optionally msg:<kind> or status:<state>)"
                )
            wake = frozenset(tokens)
        index += 2
    settings.inbox.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.base / "wait.lock", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InboxUsage("another process is already waiting in this container") from None
        rc, line = run_wait(settings, timeout=timeout, wake=wake, quiet=quiet, digest=digest)
        if line == "digest":
            return _print_digest(settings)
        if line is not None:
            print(line)
        return rc
    finally:
        os.close(fd)


def _print_digest(settings: Settings) -> int:
    from jailbee.inbox.waiting import last_from_child, write_digest_stamp

    now = time.time()
    try:
        write_digest_stamp(settings.base, now)
    except OSError as exc:
        print(f"jailbee inbox: could not record the digest time: {exc}", file=sys.stderr)
    print("digest")
    loaded = _load_snapshot(settings)
    children: list[object] = list(loaded[1]["children"]) if loaded else []
    print(render_status(children, now=now, from_child=last_from_child(settings.base)))
    return 0


def main(argv: list[str]) -> int:
    try:
        settings = load_settings(os.environ)
        if not argv or argv[0] not in {
            "wait",
            "ls",
            "read",
            "runners",
            "children",
            "status",
            "send",
            "help",
            "-h",
            "--help",
        }:
            print(USAGE, end="", file=sys.stderr)
            return 2
        if argv[0] in {"help", "-h", "--help"}:
            print(USAGE, end="")
            return 0
        commands = {
            "wait": cmd_wait,
            "ls": cmd_ls,
            "read": cmd_read,
            "children": cmd_children,
            "status": cmd_status,
            "runners": cmd_runners,
        }
        if argv[0] in commands:
            return commands[argv[0]](settings, argv[1:])
        if argv[0] == "send":
            from jailbee.inbox.send import PublishError, parse_send, publish

            try:
                request = parse_send(argv[1:])
                ident = publish(settings, request)
                print(ident, flush=True)
                if request.wait:
                    from jailbee.inbox.waiting import wait_for_status

                    return wait_for_status(settings, ident, parse_timeout(request.timeout or "30m"))
            except (PublishError, OSError) as exc:
                if exc.__cause__ is not None:
                    print(f"{PROG}: {exc.__cause__}", file=sys.stderr)
                print(f"{PROG}: {exc}", file=sys.stderr)
                return 1
            return 0
        raise InboxUsage("not implemented")
    except InboxBadUsage as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        print(f"see: {PROG} --help", file=sys.stderr)
        return 2
    except InboxUsage as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
