"""Standard-library orchestration inbox command logic."""

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
from typing import TYPE_CHECKING, Any

from jailbee.inbox.errors import InboxUsage
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

if TYPE_CHECKING:
    from jailbee.inbox.send import SendRequest

PROG = "jailbee inbox"


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


def cmd_ls(settings: Settings) -> int:
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


def cmd_read(settings: Settings, ident: str, *, keep: bool = False) -> int:
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


def cmd_children(settings: Settings, *, as_json: bool = False) -> int:
    loaded = _load_snapshot(settings)
    if loaded is None:
        return 1
    path, doc = loaded
    line = max(1, len(path.read_text().rstrip().splitlines()))
    return render_children(doc["children"], where=str(path), line=line, as_json=as_json)


def render_children(children: list[Any], *, where: str, line: int, as_json: bool) -> int:
    """Print the `inbox children` view of a children list (shared by both transports)."""
    if as_json:
        print(pretty(children))
    else:
        for child in children:
            if child is None:
                child = {}
            if not isinstance(child, dict):
                print(
                    f"jq: error (at {where}:{line}): Cannot index {_jq_type(child)} "
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
                        f"jq: error (at {where}:{line}): {_jq_type(value)} ({jq_tostring(value)}) "
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


def cmd_status(settings: Settings, *, child: str | None = None, as_json: bool = False) -> int:
    from jailbee.inbox.waiting import last_from_child

    loaded = _load_snapshot(settings)
    if loaded is None:
        return 1
    from_child = last_from_child(settings.base)
    return render_status_view(list(loaded[1]["children"]), from_child, child=child, as_json=as_json)


def render_status_view(
    children: list[object],
    from_child: Mapping[str, float],
    *,
    child: str | None,
    as_json: bool,
) -> int:
    """Print `inbox status` for a children list (shared by both transports)."""
    if child is not None:
        children = [c for c in children if isinstance(c, dict) and c.get("name") == child]
        if not children:
            print(f"{PROG}: unknown child '{clean_c0c1(child)}'", file=sys.stderr)
            return 1
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


_UNSET: Any = object()


def runners_json(settings: Settings, grant: object = _UNSET) -> list[dict[str, object]]:
    config = Path(os.environ.get("JAILBEE_LITELLM_CONFIG") or "/etc/jailbee/litellm.json")
    if grant is _UNSET:
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


def cmd_grant(settings: Settings, *, as_json: bool = False) -> int:
    path = settings.base / "grant.json"

    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    try:
        grant = json.loads(
            path.read_text(),
            parse_float=_parse_number,
            parse_int=_parse_number,
            parse_constant=reject_constant,
        )
    except FileNotFoundError:
        print(
            f"{PROG}: no orchestration grant here yet (the broker writes it a few seconds "
            "after the grant; if it stays missing, tell the human)",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError, UnicodeError):
        print(f"{PROG}: invalid grant.json", file=sys.stderr)
        return 1
    if not isinstance(grant, dict):
        print(f"{PROG}: invalid grant.json", file=sys.stderr)
        return 1
    return render_grant(grant, as_json=as_json)


def render_grant(grant: dict[str, Any], *, as_json: bool) -> int:
    """Print the grant summary (shared by both transports)."""
    safe = sanitize_json_value(grant)
    if as_json:
        print(pretty(safe))
        return 0

    def text(value: object) -> str:
        if isinstance(value, str):
            return clean_c0c1(value)
        if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
            return str(value)
        return "-"

    operations = grant.get("operations")
    operations_text = (
        ", ".join(text(item) for item in operations) if isinstance(operations, list) else "-"
    )
    grantable = grant.get("grantable")
    grantable = grantable if isinstance(grantable, dict) else {}
    mounts = grantable.get("mounts")
    egress = grantable.get("egress")
    mounts_text = ", ".join(text(item) for item in mounts) if isinstance(mounts, list) else "-"
    egress_text = ", ".join(text(item) for item in egress) if isinstance(egress, list) else "-"
    publish = grant.get("publish")
    publish = publish if isinstance(publish, dict) else {}
    issues = publish.get("issues")
    issue_text = ", ".join(text(item) for item in issues) if isinstance(issues, list) else "-"
    foreign_issues = publish.get("foreign_issues")
    foreign_issues = foreign_issues if isinstance(foreign_issues, dict) else {}
    repos = foreign_issues.get("repos")
    actions = foreign_issues.get("actions")
    foreign_text = (
        "; ".join(
            f"{text(repo)} ({', '.join(text(action) for action in actions)})" for repo in repos
        )
        if isinstance(repos, list) and isinstance(actions, list)
        else "-"
    )
    default_runner = grant.get("default_runner")
    runner_text = "none"
    if isinstance(default_runner, dict):
        agent = text(default_runner.get("agent"))
        runner_text = agent
        if isinstance(default_runner.get("profile"), str):
            runner_text += " -P " + text(default_runner["profile"])
        if isinstance(default_runner.get("model"), str):
            runner_text += " " + text(default_runner["model"])
    print("operations: " + operations_text)
    print("max_running_children: " + text(grant.get("max_running_children")))
    print("grantable mounts: " + mounts_text)
    print("grantable loose: " + ("yes" if grantable.get("loose") is True else "no"))
    print("grantable egress: " + egress_text)
    print("publish issues: " + issue_text)
    print("publish foreign issues: " + foreign_text)
    print("publish pull_requests: " + ("yes" if publish.get("pull_requests") is True else "no"))
    print("publish pr_reviews: " + ("yes" if publish.get("pr_reviews") is True else "no"))
    print("publish max_actions_per_hour: " + text(publish.get("max_actions_per_hour")))
    print("default runner: " + runner_text)
    return 0


def cmd_runners(settings: Settings, *, as_json: bool = False, grant: object = _UNSET) -> int:
    try:
        entries = runners_json(settings, grant)
    except JqError as exc:
        print(f"jq: error (at <unknown>): {exc}", file=sys.stderr)
        return 5
    if as_json:
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


def parse_wait_args(
    timeout: str, wake_on: str | None, quiet_after: str | None, digest: str | None
) -> tuple[int | None, int, int, frozenset[str] | None]:
    from jailbee.inbox.waiting import valid_wake_token

    seconds = parse_timeout(timeout)
    quiet = parse_duration(quiet_after) if quiet_after is not None else 0
    digest_seconds = parse_duration(digest) if digest is not None else 0
    if quiet_after is not None and not quiet:
        raise InboxUsage("--quiet-after must be positive")
    if digest is not None and not digest_seconds:
        raise InboxUsage("--digest must be positive")
    wake = None
    if wake_on is not None:
        tokens = wake_on.split(",")
        if not all(valid_wake_token(token) for token in tokens):
            raise InboxUsage(
                "--wake-on needs comma-separated message kinds or status states "
                "(optionally msg:<kind> or status:<state>)"
            )
        wake = frozenset(tokens)
    return seconds, quiet, digest_seconds, wake


def cmd_wait(
    settings: Settings,
    *,
    timeout: str = "30m",
    wake_on: str | None = None,
    quiet_after: str | None = None,
    digest: str | None = None,
    path: bool = False,
) -> int:
    from jailbee.inbox.waiting import run_wait

    seconds, quiet, digest_seconds, wake = parse_wait_args(timeout, wake_on, quiet_after, digest)
    settings.inbox.mkdir(parents=True, exist_ok=True)
    fd = os.open(settings.base / "wait.lock", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InboxUsage("another process is already waiting in this container") from None
        deadline = None if seconds is None else time.monotonic() + seconds
        warned: set[Path] = set()
        while True:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            rc, line = run_wait(
                settings,
                timeout=remaining,
                wake=wake,
                quiet=quiet,
                digest=digest_seconds,
                warned=warned,
            )
            if line == "digest":
                return _print_digest(settings, warned=warned)
            if line is None:
                return rc
            if path or line.startswith("quiet-child "):
                print(line)
            else:
                ident = message_id(Path(line))
                if ident is None:
                    continue
                print(clean_c0c1(ident))
            return rc
    finally:
        os.close(fd)


def _print_digest(settings: Settings, *, warned: set[Path] | None = None) -> int:
    from jailbee.inbox.waiting import last_from_child, write_digest_stamp

    now = time.time()
    try:
        write_digest_stamp(settings.base, now)
    except OSError as exc:
        print(f"jailbee inbox: could not record the digest time: {exc}", file=sys.stderr)
    print("digest")
    loaded = _load_snapshot(settings)
    children: list[object] = list(loaded[1]["children"]) if loaded else []
    print(render_status(children, now=now, from_child=last_from_child(settings.base, warned)))
    return 0


def cmd_send(settings: Settings, request: "SendRequest") -> int:
    from jailbee.inbox.send import publish, validate
    from jailbee.inbox.waiting import wait_for_status

    request = validate(request)
    ident = publish(settings, request)
    print(ident, flush=True)
    if request.wait:
        result = wait_for_status(settings, ident, parse_timeout(request.timeout or "30m"))
        if result == 3:
            print(
                "jailbee inbox: no reply from the host broker; if this repeats, tell the human "
                "(the broker may not be running)",
                file=sys.stderr,
            )
        return result
    return 0
