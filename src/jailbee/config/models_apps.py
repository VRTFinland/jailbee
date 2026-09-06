"""User-defined GUI applications: the `apps:` config mapping.

Builtin applications (the browsers, the JetBrains IDEs) are *not* modelled
here — they have their own typed config blocks and their own extras, such as
profile pools. This model is for everything else: an AppImage, a vendor
binary outside PATH, a wrapper script.
"""

from __future__ import annotations

import re
import shlex

from pydantic import BaseModel, ConfigDict, Field, field_validator

APP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
"""Legal `apps:` key.

An app name becomes a command word, a log-file path segment
(`/tmp/jailbee-app-<name>.log`) and a dashboard action verb, so it is
restricted to characters that need no quoting in any of the three.
"""


class AppEntry(BaseModel):
    """One user-defined GUI application."""

    model_config = ConfigDict(extra="forbid")
    command: list[str] = Field(
        description=(
            "Container-side command to run. A string is split with shell quoting "
            "rules; a list is taken as-is. The first element is an absolute container "
            "path or a name on the container's PATH — it is not resolved on the host."
        ),
    )
    args: list[str] = Field(
        default=[],
        description=(
            "Extra arguments appended after `command`. Arguments passed on the command "
            "line are appended after these."
        ),
    )
    cwd: str = Field(
        default="repo",
        description=(
            "Working directory inside the container: `repo` (the checkout), `home` "
            "(the dev user's home), or an absolute container path."
        ),
    )
    env: dict[str, str] = Field(
        default={},
        description=(
            "Extra environment variables, merged over the GUI environment jailbee "
            "already supplies (HOME, DISPLAY, WAYLAND_DISPLAY, XDG_RUNTIME_DIR)."
        ),
    )
    description: str = Field(
        default="",
        description="One-line summary shown in `jailbee apps ls`.",
    )
    top_level: bool = Field(
        default=False,
        description=(
            "Promote this app to a top-level command, so `jailbee <name>` launches it. "
            "A built-in command of the same name always wins; a name that collides "
            "with one is a config error."
        ),
    )
    autostart: bool = Field(
        default=False,
        description="Launch this app after autostart steps complete.",
    )

    @field_validator("command", mode="before")
    @classmethod
    def _split_command(cls, v: object) -> object:
        if isinstance(v, str):
            return shlex.split(v)
        return v

    @field_validator("command")
    @classmethod
    def _command_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("apps.<name>.command must not be empty")
        return v

    @field_validator("cwd")
    @classmethod
    def _cwd_is_keyword_or_absolute(cls, v: str) -> str:
        if v in ("repo", "home") or v.startswith("/"):
            return v
        raise ValueError(f"invalid cwd: {v!r}. Use 'repo', 'home', or an absolute container path.")
