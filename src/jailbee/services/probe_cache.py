"""Atomic, shared cache for container-side service probes."""

import fcntl
import json
import math
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from jailbee.services.local_proc import ProbeResult


class ProbeCache:
    def __init__(
        self, path: Path, *, ttl: float = 5.0, now: Callable[[], float] = time.time
    ) -> None:
        self.path = path
        self.ttl = ttl
        self.now = now

    def _read(self) -> dict[str, dict[str, object]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return {}
        if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
            return {}
        return {k: v for k, v in data["entries"].items() if isinstance(k, str) and isinstance(v, dict)}

    @staticmethod
    def _key(service: str, kind: str, launch_id: str) -> str:
        return json.dumps([service, kind, launch_id], ensure_ascii=True)

    @staticmethod
    def _number(value: object) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    def get(self, service: str, kind: str, launch_id: str) -> ProbeResult | None:
        entry = self._read().get(self._key(service, kind, launch_id), {})
        timestamp = entry.get("timestamp")
        code = entry.get("code")
        stdout = entry.get("stdout")
        stderr = entry.get("stderr")
        timed_out = entry.get("timed_out")
        if (
            not self._number(timestamp)
            or self.now() - float(timestamp) >= self.ttl  # type: ignore[arg-type]  # validated above
            or not (code is None or type(code) is int)
            or not isinstance(stdout, str)
            or not isinstance(stderr, str)
            or type(timed_out) is not bool
        ):
            return None
        return ProbeResult(code, stdout, stderr, timed_out)

    def first_failure(self, service: str, kind: str, launch_id: str) -> float | None:
        value = self._read().get(self._key(service, kind, launch_id), {}).get("first_failed_at")
        return float(value) if self._number(value) else None  # type: ignore[arg-type]  # validated above

    def put(self, service: str, kind: str, launch_id: str, result: ProbeResult) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                now = self.now()
                entries = self._read()
                for key, entry in list(entries.items()):
                    timestamp = entry.get("timestamp")
                    if not self._number(timestamp) or now - float(timestamp) >= self.ttl:  # type: ignore[arg-type]  # validated above
                        failure = entry.get("first_failed_at")
                        if self._number(failure):
                            entries[key] = {"first_failed_at": failure}
                        else:
                            del entries[key]
                key = self._key(service, kind, launch_id)
                previous = entries.get(key, {}).get("first_failed_at")
                failure = None
                if result.code != 0 or result.timed_out:
                    failure = previous if self._number(previous) else now
                entries[key] = {
                    "timestamp": now,
                    "code": result.code,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "timed_out": result.timed_out,
                    "first_failed_at": failure,
                }
                fd, name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as temp:
                        json.dump({"entries": entries}, temp)
                    os.replace(name, self.path)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
