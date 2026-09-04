"""Autostart steps, the docker registry mirror override, and the terminal
coding-agent models (generic `AgentConfig` plus the Claude-specific
subclass), plus the GitHub CLI integration and the top-level autostart block.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from jailbee.config.models_net import _reject_offline

# An agent name becomes a tmux window name and a `jailbee doctor` label —
# kept to the safest common subset of what both accept. It does *not* reach
# any Incus device name: those derive from each `shared[].subpath` via
# `device_name()`. The two only coincide because every shipped preset happens
# to name its subpath after the agent.
_AGENT_NAME_RE = re.compile(r"[a-z0-9-]+")


class AutostartStep(BaseModel):
    """A single shell step to run inside a container during autostart.

    `network` swaps the container's network profile for the duration of the
    step (and restores it afterwards). `mounts` lists `optional_mounts` keys
    to attach for the step's duration. `background: True` detaches via
    `setsid` and does not wait for completion.
    """

    model_config = ConfigDict(extra="forbid")
    name: str
    run: str
    network: Literal["strict", "loose"] | None = None
    mounts: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    working_dir: str = ""
    background: bool = False
    timeout: int | None = None
    continue_on_error: bool = False

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        return _reject_offline(v)


class DockerRegistryMirrorRepoConfig(BaseModel):
    """Per-repo overrides for the host-global rpardini mirror.

    Currently only ``extra_registries``: a list of upstream registry hostnames
    that this repo pulls images from but which aren't covered by rpardini's
    built-in defaults (Docker Hub, registry.k8s.io, gcr.io, quay.io, ghcr.io).
    The strings are hostnames (optionally with ``:port``) — no scheme, no
    path. Empty by default; ``jailbee new`` / ``jailbee apply`` push the merged list
    into the mirror's REGISTRIES env on each run.
    """

    model_config = ConfigDict(extra="forbid")
    extra_registries: list[str] = Field(default_factory=list)

    @field_validator("extra_registries")
    @classmethod
    def _validate_hostnames(cls, v: list[str]) -> list[str]:
        for raw in v:
            if not raw or raw != raw.strip():
                raise ValueError(f"empty / whitespace-padded registry hostname: {raw!r}")
            if any(c.isspace() for c in raw):
                raise ValueError(f"registry hostname must not contain whitespace: {raw!r}")
            if "/" in raw or "://" in raw:
                raise ValueError(
                    f"registry must be a bare hostname[:port], not a URL/path: {raw!r}"
                )
        return v


# `claude.pr_prompt` ships to the container as an environment variable inside
# jailbee's own prompt. The cap is a sanity bound, not a model context limit:
# it turns a pasted-in-by-accident file into a config error instead of a
# `claude` invocation that fails opaquely and silently falls back.
_MAX_PR_PROMPT_LEN = 20_000


class AgentSharedMount(BaseModel):
    """One bind-mount an agent needs to keep its auth/config across containers.

    Share the minimum surface that avoids re-authentication. Caches, chat
    histories and logs are per-branch working state and must stay
    per-container; a generically-named file (e.g. `~/.env`) must never be
    shared, because the mount would collide with unrelated tools and leak
    their secrets between containers.
    """

    model_config = ConfigDict(extra="forbid")
    subpath: str
    path: str
    type: Literal["dir", "file"] = "dir"
    seed: str | None = None

    @model_validator(mode="after")
    def _seed_is_file_only(self) -> AgentSharedMount:
        if self.type == "dir" and self.seed is not None:
            raise ValueError(f"seed is only valid for type: file (subpath {self.subpath!r})")
        return self


class AgentConfig(BaseModel):
    """A terminal coding agent wired into the container lifecycle.

    `install`/`update` are shell command lines run inside the container as the
    dev user through the autostart step pipeline, so each gets a fresh
    `bash -lc` login shell — which is why `~/.local/bin` and
    `~/.npm-global/bin` are on PATH for them.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    autostart: bool = False
    command: str = ""
    install: str | None = None
    install_check: str | None = None
    update: str | None = None
    auto_update: bool = True
    install_network: Literal["strict", "loose"] = "strict"
    shared: list[AgentSharedMount] = Field(default_factory=list)
    egress_allow: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    def effective_install_check(self) -> str:
        """The command that decides install-vs-update.

        Defaults to `command -v <first token of command>`: the binary's own
        name, not the full command line, so flags in `command` don't leak into
        the probe.
        """
        if self.install_check:
            return self.install_check
        binary = self.command.split()[0] if self.command.strip() else ""
        return f"command -v {binary}"


class ClaudeAgentConfig(AgentConfig):
    """`agents.claude` — the generic fields plus Claude-only integrations.

    `enabled`, `autostart`, `command` and `auto_update` are inherited: their
    semantics are identical to any other agent's. `enabled` gates the shared
    `<shared_dir>/claude` cache mount (see `Config.effective_shared_caches`),
    the `CLAUDE_API_HOSTS` strict-mode egress auto-add, the
    `<shared_dir>/claude` subdir creation on `jailbee init`, and the
    claude-subdir presence check in `jailbee doctor`. When enabled, jailbee
    creates an empty `<shared_dir>/claude` directory as a bind-mount source
    and seeds `<shared_dir>/claude/.claude.json` with `{}` — the golden image
    exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, so Claude Code reads its
    global config from inside that directory mount, and Claude Code inside
    the first container runs its onboarding flow from a clean state. No host
    `~/.claude` / `~/.claude.json` is read.

    - `plugins_enabled`: when true (default), `effective_egress_allow`
      also appends `CLAUDE_PLUGIN_HOSTS` (GitHub + npm) so that Claude
      Code's plugin marketplace, skills and SessionStart hooks load in
      strict-mode containers. Set to false to keep the API reachable
      while blocking marketplace traffic. Has no effect when `enabled`
      is false.
    - `install_jailbee_skills`: when true (default, requires `enabled`), `jailbee new`
      and `jailbee apply` copy jailbee's bundled Claude skills (`jailbee-usage`,
      `jailbee-repo-setup`) into the shared `<shared_dir>/claude/skills/` so the
      in-container Claude understands jailbee and can help with `.jailbee/config.yaml`
      edits. Host-side file copy only — no network. Has no effect when `enabled`
      is false. The pre-1.0 key name (`install_gie_skills`) is not accepted at
      all — `_check_retired_keys`/`_RETIRED_KEYS_CLAUDE` raises a `ConfigError`
      naming this key as the replacement, under both the legacy `claude:` and
      the `agents.claude` spelling.
    - `ai_pr_description`: when true (default, requires `enabled`),
      `jailbee pr` asks the in-container Claude CLI to generate the
      PR title and body from the branch's commits and diff, falling back to
      a placeholder if generation fails. Has no effect when `enabled` is
      false.
    - `ai_pr_branch`: when true (default, requires `enabled`), `jailbee pr` asks
      the in-container Claude to propose a convention-following PR head branch
      name when opening a new PR; has no effect when `enabled` is false.
    - `pr_prompt`: project-specific PR-writing instructions, typically set in a
      repo's `.jailbee/config.yaml` as a YAML block scalar. They are embedded in
      jailbee's own prompt as a delimited section that explicitly outranks the
      generic guidance, so a project can dictate the title and body shape
      without having to restate the JSON response contract `_parse_pr_text`
      depends on. Capped at 20 000 characters so a pathological value fails at
      config load rather than inside the container. Has no effect when
      `enabled` or `ai_pr_description` is false.
    - `ai_pr_model`: the model `jailbee pr` passes to `claude --model` when
      generating the PR text. Defaults to `sonnet`: writing a PR description is
      a bounded summarisation job, and pinning it means the generation does not
      compete for the same budget as the coding work that just happened in the
      container. Accepts an alias (`sonnet`, `opus`, `haiku`) or a full model
      ID; `null` omits the flag entirely so the container's own default model
      applies. `haiku` is a valid choice but has a smaller context window than
      the alternatives, so a large cumulative diff may not fit. Has no effect
      when `enabled` or `ai_pr_description` is false.
    - `ai_pr_timeout`: seconds `jailbee pr` gives the in-container Claude to
      produce the PR text before giving up and falling back to a placeholder.
      Defaults to 600. Generation is an agentic run, not one model call — it
      reads the log, the cumulative diff, the PR template and the branch's spec
      across a dozen-plus turns, so cost scales with the repository, not just
      with the diff. Measured in jailbee's own repo on a 21-file diff: 129s.
      Raise it for a large tree, or when `claude.pr_prompt` asks for work that
      takes longer. Has no effect when `enabled` or `ai_pr_description` is
      false.
    """

    plugins_enabled: bool = True
    install_jailbee_skills: bool = True
    ai_pr_description: bool = True
    ai_pr_branch: bool = True
    pr_prompt: str | None = Field(default=None, max_length=_MAX_PR_PROMPT_LEN)
    ai_pr_model: str | None = "sonnet"
    ai_pr_timeout: int = Field(default=600, gt=0)

    @field_validator("ai_pr_model")
    @classmethod
    def _reject_non_model_value(cls, v: str | None) -> str | None:
        """A model name is a single token — reject anything that isn't one.

        The value reaches `claude --model` through an environment variable, so
        embedded flags could never be executed as such. The check exists to
        turn a typo or a misunderstanding into a config error, rather than a
        non-zero `claude` exit that `generate_pr_text` reports only as a failed
        generation. Use `null`, not an empty string, to inherit the container's
        own default model.
        """
        if v is None:
            return None
        if not v.strip() or len(v.split()) != 1:
            raise ValueError(
                f"must be a single model name or alias (e.g. 'sonnet', "
                f"'claude-haiku-4-5'), or null to inherit the container "
                f"default; got {v!r}"
            )
        return v.strip()


class GithubConfig(BaseModel):
    """GitHub CLI (gh) integration inside containers.

    - `enabled`: master switch. Defaults to false; opt-in via
      ~/.config/jailbee/global.yaml. When false, jailbee skips:
        * the api.github.com:443 strict-mode egress auto-add,
        * the /etc/profile.d/jailbee-github.sh autostart write,
        * the github doctor checks.
      gh binary itself is always installed in the golden image
      (parallels claude.enabled vs the ensure-claude.sh runtime step).
    - `api_tokens`: map from `container_prefix` to a fine-grained PAT.
      One entry per GitHub resource owner (org or personal account).
      Value is a SecretStr so accidental repr / config-dump masks it.
      Permitted only at the global config layer (~/.config/jailbee/global.yaml);
      see load_config's placement constraint.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    api_tokens: dict[str, SecretStr] = Field(default_factory=dict)


class Autostart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    on_create: list[AutostartStep] = Field(default_factory=list)
    on_start: list[AutostartStep] = Field(default_factory=list)
    step_timeout: int = 600
    env: dict[str, str] = Field(default_factory=dict)
