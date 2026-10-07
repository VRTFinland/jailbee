"""Jailbee's LiteLLM pre-call hook, installed inside the proxy container.

On ``/v1/messages``, chatgpt/ deployments need Claude Code's list-valued
system blocks as a string: LiteLLM sends a list as system *messages*, which
the ChatGPT backend rejects. A string becomes Responses instructions.
The callback also replaces or floors Claude Code's requested effort because
its ``output_config.effort`` overrides deployment defaults in LiteLLM.

Only ``/v1/messages`` requests are rewritten (LiteLLM's ``anthropic_messages``
call type); anything else passes through untouched.

The alias table and the model list come from ``$JAILBEE_LITELLM_HOT_FILE``
(``hot.json``), which the host replaces atomically. The callback re-reads it
while the proxy runs: a task on the proxy's event loop notices a new file,
reconciles LiteLLM's router (``upsert_deployment`` for every listed deployment,
then ``delete_deployment`` for every router deployment the file no longer lists)
and swaps the table in the same call, so no request sees one without the other.
It then writes ``$JAILBEE_LITELLM_ACK_FILE`` = ``{"hot_digest", "error"}``; the
host restarts the instance only when no matching, error-free acknowledgement
appears. A file that cannot be read or applied changes nothing and is reported.
The file is required at start: without it no request would be flattened and
every one would fail upstream, so a missing variable stops the proxy.

``hot.json``'s optional ``env.names`` lists the API keys its deployments name
as ``os.environ/NAME``. Before reconciling, the reload reads exactly those from
``$JAILBEE_LITELLM_ENV_FILE`` (the instance's systemd ``EnvironmentFile``) into
``os.environ``, so a new or rotated key needs no restart. ``env.digest`` is
there only so a rotated value changes the file; the callback never reads it.

This module needs only the standard library and LiteLLM.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any

from litellm.integrations.custom_logger import CustomLogger

_ORDER = ("low", "medium", "high", "xhigh", "max")
_MESSAGES_CALL_TYPE = "anthropic_messages"
_POLL_SECONDS = 0.5
_log = logging.getLogger("jailbee_callback")


@dataclass(frozen=True)
class HotFile:
    table: dict[str, Any]
    models: list[dict[str, Any]]
    digest: str
    env_names: tuple[str, ...] = ()


def _required_env(name: str, why: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is not set: {why} Run `jailbee litellm up`.")
    return value


def read_hot(path: str) -> HotFile:
    with open(path, "rb") as f:
        raw = f.read()
    data = json.loads(raw)
    table, models = data["callback"], data["models"]
    if (
        not isinstance(table, dict)
        or not isinstance(models, list)
        or not all(isinstance(m, dict) for m in models)
    ):
        raise ValueError("expected a `callback` object and a `models` list of objects")
    env = data.get("env", {})
    names = env.get("names", []) if isinstance(env, dict) else None
    if not isinstance(names, list) or not all(isinstance(n, str) for n in names):
        raise ValueError("expected `env.names` to be a list of variable names")
    return HotFile(table, models, hashlib.sha256(raw).hexdigest(), tuple(names))


def read_env(path: str, names: tuple[str, ...]) -> dict[str, str]:
    """The named variables from the instance's systemd ``EnvironmentFile``.

    jailbee writes it (``render_instance_env``): ``NAME=value`` or
    ``NAME='value'``, a quoted value holding no quote, backslash or newline.
    Split on the first ``=`` only: keys may end in ``=``. A missing name
    raises, naming it; no message carries a value.
    """
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        raise ValueError(f"{path} is not valid UTF-8") from None
    wanted = set(names)
    values: dict[str, str] = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if not sep or name not in wanted:
            continue
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1]
        values[name] = value
    missing = sorted(wanted - values.keys())
    if missing:
        raise ValueError(f"{path} does not define {', '.join(missing)}")
    return values


def _file_digest(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _signature(path: str) -> tuple[int, int, int] | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _router() -> Any | None:
    from litellm.proxy import proxy_server

    return proxy_server.llm_router


_ENV_PREFIX = "os.environ/"


def _resolve_env(value: Any, model_name: object) -> Any:
    """Copy `value` with every ``os.environ/NAME`` string replaced by the variable.

    LiteLLM resolves these when it loads config.yaml, but not on the
    ``upsert_deployment`` path, which would send the literal as the API key.
    An unset variable raises; the message names the variable, never its value.
    """
    if isinstance(value, str) and value.startswith(_ENV_PREFIX):
        name = value[len(_ENV_PREFIX) :]
        if not name:
            raise ValueError(f"deployment {model_name!r}: empty environment variable name")
        resolved = os.environ.get(name)
        if resolved is None:
            raise ValueError(f"deployment {model_name!r}: environment variable {name} is not set")
        return resolved
    if isinstance(value, dict):
        return {k: _resolve_env(v, model_name) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env(v, model_name) for v in value]
    return value


def _safe_failure(exc: Exception, what: str) -> ValueError:
    """A message about `exc` that cannot echo a value: resolved secrets are in scope.

    Pydantic's str() includes ``input_value=...``; keep only each error's location
    and type, or just the exception class.
    """
    detail = type(exc).__name__
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            detail = "; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ()))}: {e.get('type')}" for e in errors()
            )
        except Exception:  # a hostile errors() must not bring the value back
            detail = type(exc).__name__
    return ValueError(f"{what}: {detail}")


def reconcile_router(router: Any, models: list[dict[str, Any]]) -> None:
    """Bring the router's deployments to exactly `models`.

    Every entry is validated before anything changes, so a bad one leaves the
    router as it was. Deployments are addressed by ``model_info.id``.
    """
    from litellm.types.router import Deployment

    wanted: dict[str, Any] = {}
    for entry in models:
        dep_id = (entry.get("model_info") or {}).get("id")
        if not dep_id:
            raise ValueError(f"deployment {entry.get('model_name')!r} has no model_info.id")
        if "litellm_params" in entry:
            entry = {
                **entry,
                "litellm_params": _resolve_env(entry["litellm_params"], entry.get("model_name")),
            }
        try:
            wanted[dep_id] = Deployment(**entry)
        except Exception as exc:  # the entry holds resolved secrets; see _safe_failure
            raise _safe_failure(exc, f"deployment {entry.get('model_name')!r} is invalid") from None
    present = {
        info["id"] for item in router.model_list if (info := item.get("model_info") or {}).get("id")
    }
    for dep_id, deployment in wanted.items():
        try:
            router.upsert_deployment(deployment)
        except Exception as exc:
            raise ValueError(f"upsert of {dep_id!r} failed: {type(exc).__name__}") from None
    for dep_id in sorted(present - set(wanted)):
        try:
            router.delete_deployment(dep_id)
        except Exception as exc:
            raise ValueError(f"delete of {dep_id!r} failed: {type(exc).__name__}") from None


def _entry(model: object, table: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(model, str):
        return None
    found = table.get("aliases", {}).get(model)
    if found is not None:
        return dict(found)
    if model.startswith("claude-") and table.get("catch_all"):
        return dict(table["catch_all"])
    return None


def transform(data: dict[str, Any], table: dict[str, Any]) -> dict[str, Any]:
    """Apply the alias entry without mutating the original request or table."""
    entry = _entry(data.get("model"), table)
    if entry is None:
        return data
    out = dict(data)
    system = out.get("system")
    if entry.get("chatgpt") and isinstance(system, list):
        out["system"] = "\n\n".join(
            str(block.get("text", ""))
            for block in system
            if isinstance(block, dict) and block.get("type") == "text"
        )
    fixed, floor = entry.get("effort"), entry.get("min_effort")
    if fixed or floor:
        config = dict(data.get("output_config") or {})
        current = config.get("effort")
        if fixed:
            config["effort"] = fixed
        elif current not in _ORDER or _ORDER.index(current) < _ORDER.index(floor):
            config["effort"] = floor
        out["output_config"] = config
    return out


class JailbeeCallback(CustomLogger):  # type: ignore[misc]  # LiteLLM's base is untyped
    def __init__(self) -> None:
        super().__init__()
        self._hot_path = _required_env(
            "JAILBEE_LITELLM_HOT_FILE",
            "the jailbee callback has no alias table, so it would forward every "
            "request unflattened.",
        )
        self._ack_path = _required_env(
            "JAILBEE_LITELLM_ACK_FILE", "the proxy could not report what it loaded."
        )
        self._env_path = _required_env(
            "JAILBEE_LITELLM_ENV_FILE", "the proxy could not reload its API keys."
        )
        # Signature first: a push between the stat and the read is then seen as a
        # change by the next poll, instead of never being read.
        self._seen = _signature(self._hot_path)
        hot = read_hot(self._hot_path)  # required: a missing or invalid file stops the proxy
        self._table = hot.table
        self._pending: HotFile | None = hot  # not yet reconciled with a router
        self._acked: tuple[str | None, str | None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._start_watcher()

    def _start_watcher(self) -> None:
        if self._task is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # imported outside the loop; the first request starts it
            return
        self._task = loop.create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            try:
                self.reload_once(_router())
            except Exception:  # the watcher must outlive any one bad reload
                _log.exception("jailbee hot reload failed")
            await asyncio.sleep(_POLL_SECONDS)

    def reload_once(self, router: Any | None) -> None:
        """One poll: read a changed file, apply it when a router exists, acknowledge."""
        signature = _signature(self._hot_path)
        if signature != self._seen:
            self._seen = signature
            try:
                self._pending = read_hot(self._hot_path)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                self._pending = None
                self._ack(_file_digest(self._hot_path), f"cannot read {self._hot_path}: {exc}")
        if self._pending is None or router is None:
            return
        hot = self._pending
        if hot.env_names:
            try:
                os.environ.update(read_env(self._env_path, hot.env_names))
            except (OSError, ValueError) as exc:  # read_env's messages never carry a value
                self._pending = None
                self._ack(hot.digest, f"cannot read the proxy environment: {exc}")
                return
        try:
            reconcile_router(router, hot.models)
        except Exception as exc:  # reconcile_router's messages never carry a value
            self._pending = None
            self._ack(hot.digest, f"cannot apply the model list: {exc}")
            return
        self._table, self._pending = hot.table, None
        self._ack(hot.digest, None)

    def _ack(self, digest: str | None, error: str | None) -> None:
        if self._acked == (digest, error):
            return
        directory = os.path.dirname(self._ack_path)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".ack.")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"hot_digest": digest, "error": error}, f)
            os.replace(tmp, self._ack_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        self._acked = (digest, error)

    async def async_pre_call_hook(
        self, user_api_key_dict: Any, cache: Any, data: dict[str, Any], call_type: Any
    ) -> dict[str, Any]:
        self._start_watcher()
        if str(getattr(call_type, "value", call_type)) != _MESSAGES_CALL_TYPE:
            return data
        return transform(data, self._table)


proxy_handler_instance = JailbeeCallback()
