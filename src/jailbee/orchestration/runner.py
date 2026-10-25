"""The agent a spawned child runs: which CLI, which LiteLLM profile, which model tier.

Imported by `protocol`, so it must not import it.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from jailbee import litellm
from jailbee.config.models_litellm import PROFILE_NAME_RE

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

RUNNER_AGENTS = ("claude", "claude-jb")
RUNNER_TIERS = ("fable", "opus", "sonnet", "haiku")


class Runner(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    agent: Literal["claude", "claude-jb"]
    profile: str | None = None
    model: Literal["fable", "opus", "sonnet", "haiku"] | None = None

    @field_validator("profile")
    @classmethod
    def _profile(cls, v: str | None) -> str | None:
        if v is not None and not PROFILE_NAME_RE.fullmatch(v):
            raise ValueError(
                f"invalid profile name {v!r}: use 1-64 lowercase letters, digits, '-' or '_', "
                "starting with a letter or digit"
            )
        return v

    @model_validator(mode="after")
    def _profile_needs_claude_jb(self) -> Self:
        if self.profile is not None and self.agent != "claude-jb":
            raise ValueError("profile is only valid with agent 'claude-jb'")
        return self

    def to_label(self) -> str:
        data = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(data, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_label(cls, raw: str) -> Self:
        try:
            return cls.model_validate_json(raw)
        except ValidationError as e:
            raise ValueError(f"invalid runner label: {e}") from e

    def describe(self) -> str:
        return " ".join(p for p in (self.agent, self.profile, self.model) if p)


def parse_runner_label(raw: object) -> Runner | None:
    """The runner a label holds; None when it is absent or corrupt."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return Runner.from_label(raw)
    except ValueError:
        return None


def read_runner(incus: Incus, container: str) -> Runner | None:
    """The runner `container` was created with; None when it has none."""
    from jailbee.orchestration.labels import RUNNER

    return parse_runner_label(incus.config_get(container, RUNNER))


def runner_command(command: str, runner: Runner) -> str | None:
    """The configured Claude command rewritten for `runner`; None when it is not Claude's.

    The first word is swapped between `claude` and `claude-jb` by basename (a
    path to either is recognised), the other flags are kept, then `-P` and
    `--model` are appended when the runner names them.
    """
    first, _, rest = command.strip().partition(" ")
    if PurePosixPath(first).name not in RUNNER_AGENTS:
        return None
    if PurePosixPath(first).name != runner.agent:
        first = runner.agent
    parts = [first, rest.strip()]
    if runner.profile:
        parts.append(f"-P {runner.profile}")
    if runner.model:
        parts.append(f"--model {runner.model}")
    return " ".join(p for p in parts if p)


def runner_problems(cfg: Config, incus: Incus, gcfg: GlobalConfig, runner: Runner) -> list[str]:
    """Why `runner` cannot be honoured in this repo; empty when it can.

    A runner that cannot be honoured is a refusal, never a fallback.
    """
    agent = cfg.agents.get("claude")
    if agent is None or not agent.enabled:
        return ["agent `claude` is not enabled in this repo"]
    if not agent.prompt_args:
        return ["agent `claude` has no prompt_args, so it cannot take a prompt"]
    if runner_command(agent.command, runner) is None:
        return [
            "agents.claude.command must start with `claude` or `claude-jb` to take a runner "
            f"(it is `{agent.command}`)"
        ]
    if runner.agent != "claude-jb":
        return []
    view = cfg.litellm_view()
    if not view.config.enabled:
        return ["claude-jb needs LiteLLM enabled in this repo"]
    payload = litellm.container_sync_payload(incus, gcfg, view=view)
    data = payload.get("json") if payload else None
    profiles: dict[str, dict[str, object]] = {}
    default: str | None = None
    if isinstance(data, dict):
        profiles = data.get("profiles") or {}
        default = data.get("default_profile")
    name = runner.profile or default
    if name is None or name not in profiles:
        shown = ", ".join(sorted(profiles)) or "\u2014"
        return [
            f"LiteLLM profile `{name}` is not served (configured: {shown}; "
            "run `jailbee litellm up`)"
        ]
    tiers = profiles[name].get("tiers")
    if runner.model and not (isinstance(tiers, dict) and runner.model in tiers):
        return [f"LiteLLM profile `{name}` has no `{runner.model}` tier"]
    return []
