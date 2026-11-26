"""Read a ChatGPT subscription's usage limits through the LiteLLM proxy.

Runs inside a container (standard library only). The ChatGPT backend attaches
`x-codex-*` headers with the account's limit windows to every reply; LiteLLM
forwards them prefixed with `llm_provider-` on `/v1/chat/completions` (not on
the Anthropic-format `/v1/messages`). One 1-token request is sent per distinct
tier model of the profile, cheapest first, until a reply carries them: xAI and
API-key routes send none.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = "/etc/jailbee/litellm.json"
TIER_ORDER = ("haiku", "sonnet", "opus", "fable")
_PREFIX = "llm_provider-x-codex-"

# (base_url, key, model) -> the reply's headers with lowercase names, or None.
Fetch = Callable[[str, str, str], Mapping[str, str] | None]


def http_fetch(base_url: str, key: str, model: str) -> Mapping[str, str] | None:
    """POST one 1-token chat completion; the reply's headers, or None on failure."""
    body = json.dumps(
        {"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "."}]}
    ).encode()
    request = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as reply:
            return {name.lower(): value for name, value in reply.headers.items()}
    except (urllib.error.URLError, OSError, ValueError):
        return None


def parse_headers(headers: Mapping[str, str]) -> dict[str, Any] | None:
    """The quota carried by a reply's headers, or None when it carries none."""
    h = {k[len(_PREFIX) :]: v for k, v in headers.items() if k.startswith(_PREFIX)}
    if "primary-used-percent" not in h:
        return None

    def num(name: str) -> int | None:
        try:
            return int(float(h[name]))
        except (KeyError, ValueError):
            return None

    def window(name: str) -> dict[str, int | None] | None:
        if not num(f"{name}-window-minutes"):
            return None
        return {
            "used_percent": num(f"{name}-used-percent"),
            "window_minutes": num(f"{name}-window-minutes"),
            "reset_after_seconds": num(f"{name}-reset-after-seconds"),
            "reset_at": num(f"{name}-reset-at"),
        }

    def flag(name: str) -> bool | None:
        return None if name not in h else h[name].strip().lower() == "true"

    return {
        "plan": h.get("plan-type"),
        "active_limit": h.get("active-limit"),
        "primary": window("primary"),
        "secondary": window("secondary"),
        "credits": {
            "has_credits": flag("credits-has-credits"),
            "unlimited": flag("credits-unlimited"),
        },
    }


def _span(minutes: int) -> str:
    if minutes >= 1440:
        return f"{minutes // 1440}d"
    if minutes >= 60:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def _duration(seconds: int | None) -> str:
    if seconds is None:
        return "?"
    seconds = max(0, seconds)
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h {seconds % 3600 // 60}m"


def render(result: Mapping[str, Any], now: float) -> list[str]:
    """Human-readable lines for one quota result."""
    credits = result["credits"]
    credit_text = (
        "unlimited" if credits["unlimited"] else "yes" if credits["has_credits"] else "none"
    )
    lines = [
        f"plan: {result['plan'] or '?'}  active limit: {result['active_limit'] or '?'}  "
        f"credits: {credit_text}"
    ]
    for name in ("primary", "secondary"):
        w = result[name]
        if w is None:
            continue
        left = w["reset_after_seconds"]
        if left is None and w["reset_at"] is not None:
            left = int(w["reset_at"] - now)
        lines.append(
            f"  {name}: {w['used_percent']}% used of {_span(w['window_minutes'])} window, "
            f"resets in {_duration(left)}"
        )
    return lines


def _fail(message: str) -> int:
    print(f"jailbee litellm quota: {message}", file=sys.stderr)
    return 2


def quota(
    *,
    profile: str | None = None,
    as_json: bool = False,
    config_path: Path | None = None,
    fetch: Fetch = http_fetch,
    now: float | None = None,
) -> int:
    """Print the subscription quota of a claude-jb profile; the exit code."""
    path = config_path or Path(os.environ.get("JAILBEE_LITELLM_CONFIG", DEFAULT_CONFIG))
    try:
        config = json.loads(path.read_text())
    except OSError:
        return _fail(
            f"no LiteLLM proxy configured for this container ({path} missing); "
            "on the host run `jailbee litellm up`, then `jailbee apply` in this repo"
        )
    except ValueError:
        return _fail(f"cannot read {path} (not valid JSON); re-run `jailbee apply` on the host")
    profiles = config.get("profiles") or {}
    name = profile or os.environ.get("JAILBEE_LITELLM_PROFILE") or config.get("default_profile")
    if name not in profiles:
        return _fail(f"unknown profile {name!r}; profiles: {', '.join(profiles) or 'none'}")
    entry = profiles[name]
    try:
        key = Path(entry["key_file"]).read_text().strip()
    except (KeyError, OSError):
        return _fail("cannot read the proxy key; re-run `jailbee apply` on the host")
    if not key:
        return _fail("the proxy key is empty; re-run `jailbee apply` on the host")

    tiers = entry.get("tiers") or {}
    models = list(dict.fromkeys(tiers[t] for t in TIER_ORDER if tiers.get(t)))
    for model in models:
        headers = fetch(entry["base_url"], key, model)
        parsed = parse_headers(headers) if headers is not None else None
        if parsed is not None:
            result = {"profile": name, "model": model, **parsed}
            if as_json:
                print(json.dumps(result, indent=2))
            else:
                print("\n".join(render(result, time.time() if now is None else now)))
            return 0
    print(
        f"jailbee litellm quota: no subscription quota in the replies of profile {name} "
        "(only ChatGPT-subscription routes report one)",
        file=sys.stderr,
    )
    return 1
