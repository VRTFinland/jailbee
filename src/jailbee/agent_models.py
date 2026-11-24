"""Read user-authored model guidance inside a container (standard library only)."""

import json
import os
import sys

from jailbee.inbox import transport


def models(*, as_json: bool = False) -> int:
    """Look up the current host config over the calling container's broker socket."""
    try:
        with transport.Client(
            transport.socket_path(os.environ), retry_for=transport.retry_seconds()
        ) as client:
            result = client.call("agent.models", {}, timeout=120.0)
    except TimeoutError:
        print(transport.UNREACHABLE_MESSAGE.replace("jailbee inbox:", "jailbee agent:"), file=sys.stderr)
        return transport.EXIT_UNREACHABLE
    except transport.BrokerUnreachable as exc:
        print(str(exc).replace("jailbee inbox:", "jailbee agent:"), file=sys.stderr)
        return transport.EXIT_UNREACHABLE
    except transport.BrokerError as exc:
        print(f"jailbee agent: {exc.detail or exc.code}", file=sys.stderr)
        return transport.EXIT_UNREACHABLE if exc.code == "store" else 1
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    policy = result.get("model_policy")
    if policy:
        print(f"Model policy:\n{policy}\n")
    entries = result.get("models")
    if not isinstance(entries, dict) or not entries:
        print("No model map configured. Set agents.models in ~/.config/jailbee/global.yaml; "
              "a repo may override individual fields.")
        return 0
    for name, entry in entries.items():
        print(name)
        if isinstance(entry, dict):
            for key, label in (("label", "Label"), ("use_for", "Use for"), ("avoid_for", "Avoid for"), ("notes", "Notes")):
                if entry.get(key):
                    print(f"  {label}: {entry[key]}")
    return 0
