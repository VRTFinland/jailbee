"""The agent a spawned child runs: which CLI, which LiteLLM profile, which model tier.

Imported by `protocol`, so it must not import it.
"""

from __future__ import annotations

import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from jailbee.config.models_litellm import PROFILE_NAME_RE

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
