"""Shared parsing and rendering for orchestration inbox commands."""

import json
import os
import re
import shutil
import sys
import time
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from jailbee.inbox.errors import InboxUsage
from jailbee.inbox.render import (
    SaturatedNumber,
    clean_c0c1,
    clean_c1,
    jq_tostring,
    pretty,
    render_status,
    sanitize_json_value,
    tsv,
)

PROG = "jailbee inbox"


def parse_duration(text: str) -> int:
    match = re.fullmatch(r"([0-9]+)([smh]?)", text)
    if not match:
        raise InboxUsage(f"invalid duration '{text}' (use seconds or <n>s|m|h)")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def parse_timeout(text: str) -> int | None:
    return None if text == "none" else parse_duration(text)


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


def _clean_json(value: object) -> object:
    if isinstance(value, str):
        return clean_c1(value)
    if isinstance(value, list):
        return [_clean_json(v) for v in value]
    if isinstance(value, dict):
        return {clean_c1(k): _clean_json(v) for k, v in value.items()}
    return value


def render_children(children: list[Any], *, where: str, line: int, as_json: bool) -> int:
    """Print the `inbox children` view of a children list."""
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


def render_status_view(
    children: list[object],
    from_child: Mapping[str, float],
    *,
    child: str | None,
    as_json: bool,
) -> int:
    """Print `inbox status` for a children list."""
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


def runners_json(grant: object) -> list[dict[str, object]]:
    config = Path(os.environ.get("JAILBEE_LITELLM_CONFIG") or "/etc/jailbee/litellm.json")
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


def render_grant(grant: dict[str, Any], *, as_json: bool) -> int:
    """Print the grant summary."""
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


def cmd_runners(grant: object, *, as_json: bool = False) -> int:
    try:
        entries = runners_json(grant)
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
