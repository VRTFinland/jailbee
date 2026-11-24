"""Read live host status inside a container (standard library only)."""

import json
import os
import sys

from jailbee.inbox import transport


def _bytes(value: object) -> str:
    if not isinstance(value, int):
        return "unavailable"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(size) < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "unavailable"


def status(*, as_json: bool = False) -> int:
    """Query the host rather than reading this container's /proc or filesystem."""
    try:
        with transport.Client(
            transport.socket_path(os.environ), retry_for=transport.retry_seconds()
        ) as client:
            result = client.call("host.status", {}, timeout=120.0)
    except TimeoutError:
        print(transport.UNREACHABLE_MESSAGE.replace("jailbee inbox:", "jailbee host:"), file=sys.stderr)
        return transport.EXIT_UNREACHABLE
    except transport.BrokerUnreachable as exc:
        print(str(exc).replace("jailbee inbox:", "jailbee host:"), file=sys.stderr)
        return transport.EXIT_UNREACHABLE
    except transport.BrokerError as exc:
        print(f"jailbee host: {exc.detail or exc.code}", file=sys.stderr)
        return transport.EXIT_UNREACHABLE if exc.code == "store" else 1
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    load = result.get("load1")
    load_text = f"{load:.2f}" if isinstance(load, (int, float)) else "unavailable"
    cpus = result.get("cpus")
    print(f"Load (1 min): {load_text}; {cpus if cpus is not None else 'unavailable'} CPUs")
    print(
        f"Memory: {_bytes(result.get('memory_total'))} total; "
        f"{_bytes(result.get('memory_available'))} available"
    )
    print(
        f"Storage pool {result.get('storage_pool') or 'unavailable'}: "
        f"{_bytes(result.get('disk_available'))} free"
    )
    print(f"Running JailBee containers: {result.get('running_containers')}")
    return 0
