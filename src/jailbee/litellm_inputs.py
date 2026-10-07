"""Host files `jailbee litellm up` reads besides `global.yaml`.

- `~/.config/jailbee/litellm/secrets.env` (0600, `NAME=value` lines): API keys
  that routes name in `api_key`, or that `extra` names as `os.environ/NAME`.
  Only referenced names are read out, and they go only into the environment of the
  instances whose config references them (`litellm_render.render_instance_env`).
- the `litellm.extra` fragment: raw LiteLLM config merged into every instance.

Neither is read at config load, so a missing secret breaks `up` (and shows in
`doctor`), not every command. No message here carries a value from either
file: both hold keys.
"""

from __future__ import annotations

import stat
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from jailbee.config.models_litellm import check_secret_name
from jailbee.global_config import default_global_config_path
from jailbee.litellm_render import CATCH_ALL, env_references
from jailbee.paths import expand_path

if TYPE_CHECKING:
    from jailbee.config.models_litellm import LiteLLMConfig

_MAPPINGS = ("general_settings", "litellm_settings", "router_settings", "environment_variables")


class LiteLLMInputError(ValueError):
    """A secrets file or `extra` fragment that `up` cannot use."""


@dataclass(frozen=True)
class HostInputs:
    secrets: dict[str, str]
    extra: dict[str, object] | None


def secrets_path() -> Path:
    return default_global_config_path().parent / "litellm" / "secrets.env"


def _reserved(name: str, origin: str) -> LiteLLMInputError | None:
    try:
        check_secret_name(name)
    except ValueError as error:
        return LiteLLMInputError(f"litellm.extra {origin}: {error}")
    return None


def check_extra(fragment: dict[str, object], origin: str) -> None:
    """Refuse a fragment that would replace or redefine what jailbee owns.

    The merge lets scalars win, so a non-mapping `general_settings` would drop
    the master key and a non-list `callbacks` would drop the jailbee callback.
    """
    for key in _MAPPINGS:
        if key in fragment and not isinstance(fragment[key], dict):
            raise LiteLLMInputError(f"litellm.extra {origin}: {key} must be a mapping")
    models = fragment.get("model_list", [])
    if not isinstance(models, list):
        raise LiteLLMInputError(f"litellm.extra {origin}: model_list must be a list")
    settings = fragment.get("litellm_settings")
    if isinstance(settings, dict) and not isinstance(settings.get("callbacks", []), list):
        raise LiteLLMInputError(
            f"litellm.extra {origin}: litellm_settings.callbacks must be a list"
        )
    for entry in models:
        name = entry.get("model_name") if isinstance(entry, dict) else None
        if isinstance(name, str) and (name.startswith(("jb-", "jb.")) or name == CATCH_ALL):
            raise LiteLLMInputError(
                f"litellm.extra {origin} defines model {name!r}: "
                f"`jb-*`, `jb.*` and `{CATCH_ALL}` are jailbee's"
            )
    general = fragment.get("general_settings")
    if isinstance(general, dict) and "master_key" in general:
        raise LiteLLMInputError(
            f"litellm.extra {origin} sets general_settings.master_key; jailbee generates it"
        )
    env = fragment.get("environment_variables")
    names = [*(env if isinstance(env, dict) else {}), *env_references(fragment)]
    for name in names:
        problem = _reserved(str(name), origin)
        if problem is not None:
            raise problem


def _read(path: Path, label: str) -> str:
    """The file's text, or an error naming why without quoting a byte of it.

    A codec error's text shows the offending byte; both files hold keys.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise LiteLLMInputError(f"cannot read {label}: it is not UTF-8 text") from None
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise LiteLLMInputError(f"cannot read {label}: {reason}") from None


def load_extra(cfg: LiteLLMConfig) -> dict[str, object] | None:
    if cfg.extra is None:
        return None
    path = expand_path(cfg.extra)
    text = _read(path, f"litellm.extra {path}")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as error:
        # PyYAML's text quotes the offending line, which may hold a key.
        mark = getattr(error, "problem_mark", None)
        where = f" at line {mark.line + 1}" if mark is not None else ""
        raise LiteLLMInputError(f"litellm.extra {path} is not valid YAML{where}") from None
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LiteLLMInputError(f"litellm.extra {path} must be a mapping")
    check_extra(raw, str(path))
    return raw


def referenced_secrets(
    cfg: LiteLLMConfig,
    extra: dict[str, object] | None,
    scopes: Iterable[LiteLLMConfig] = (),
) -> list[str]:
    names = {
        r.api_key for view in (cfg, *scopes) for r in view.effective_routes().values() if r.api_key
    }
    names |= env_references(extra or {})
    return sorted(names)


def _parse(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, line in enumerate(_read(path, str(path)).splitlines(), start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        name, sep, value = text.removeprefix("export ").partition("=")
        name = name.strip()
        if not sep or not name or not name.isidentifier():
            raise LiteLLMInputError(f"{path}:{number}: expected NAME=value")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if any(c in value for c in "'\\\0"):
            raise LiteLLMInputError(
                f"{path}:{number}: the value of {name} contains a quote, backslash or NUL, "
                "which the proxy's environment file cannot carry"
            )
        values[name] = value
    return values


def _referenced_by(
    names: Iterable[str],
    host: LiteLLMConfig,
    scopes: Sequence[LiteLLMConfig],
    labels: Sequence[str],
) -> str:
    """` (named by <file>, ...)` for the repo overrides that introduce a use of `names`.

    A scope is the host config merged with one repo file, so a route that only
    `global.yaml` sets shows up in every scope. A file is blamed only for a
    route whose `api_key` it changes or adds relative to the host's.
    """
    wanted = set(names)
    host_keys = {n: r.api_key for n, r in host.effective_routes().items()}
    files = sorted(
        label
        for label, scope in zip(labels, scopes, strict=False)
        if any(
            r.api_key in wanted and host_keys.get(n) != r.api_key
            for n, r in scope.effective_routes().items()
        )
    )
    return f" (named by {', '.join(files)})" if files else ""


def load_secrets(
    cfg: LiteLLMConfig,
    extra: dict[str, object] | None,
    path: Path | None = None,
    *,
    scopes: Iterable[LiteLLMConfig] = (),
    scope_labels: Sequence[str] = (),
) -> dict[str, str]:
    """The referenced secrets by name. Raises `LiteLLMInputError` naming the fix.

    `scope_labels` names each of `scopes` (the repo override file it came
    from), so a missing secret says which file asks for it.
    """
    scopes = tuple(scopes)
    names = referenced_secrets(cfg, extra, scopes)
    if not names:
        return {}
    path = path or secrets_path()
    if not path.exists():
        raise LiteLLMInputError(
            f"routes name {', '.join(names)} but {path} does not exist: create it with "
            "NAME=value lines and `chmod 600` it"
            f"{_referenced_by(names, cfg, scopes, scope_labels)}"
        )
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise LiteLLMInputError(
            f"{path} has insecure permissions (0{mode:03o}); run `chmod 600 {path}`"
        )
    values = _parse(path)
    missing = [n for n in names if not values.get(n)]
    if missing:
        raise LiteLLMInputError(
            f"{path} does not define {', '.join(missing)}"
            f"{_referenced_by(missing, cfg, scopes, scope_labels)}"
        )
    return {n: values[n] for n in names}


def load_host_inputs(
    cfg: LiteLLMConfig,
    scopes: Iterable[LiteLLMConfig] = (),
    scope_labels: Sequence[str] = (),
) -> HostInputs:
    extra = load_extra(cfg)
    secrets = load_secrets(cfg, extra, scopes=scopes, scope_labels=scope_labels)
    return HostInputs(secrets=secrets, extra=extra)
