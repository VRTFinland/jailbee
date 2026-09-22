"""Autostart steps, the docker registry mirror override, and the terminal
coding-agent models (generic `AgentConfig` plus the Claude-specific
subclass), plus the GitHub CLI integration and the top-level autostart block.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
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
    """A single shell step to run inside a container during autostart,
    listed under `autostart.on_create` or `autostart.on_start`. Steps run
    in list order — the config editor offers reordering for exactly that
    reason.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        default=...,
        description=(
            "Identifier for this step, unique within its trigger (`on_create` or "
            "`on_start`). Steps run in the order they appear in that list; the editor "
            "offers reordering because of it."
        ),
    )
    run: str = Field(
        default=...,
        description=(
            "Shell command run as the dev user, `cd`'d into `working_dir` first. Steps run "
            "in the order they appear in the list; the editor offers reordering because of "
            "it."
        ),
    )
    network: Literal["strict", "loose"] | None = Field(
        default=None,
        description=(
            "Swaps the container's network profile to this mode for the step's duration, "
            "restoring it afterward; null keeps the current profile. Steps run in list "
            "order, so an earlier step's swap can affect a later one — reorder with care."
        ),
    )
    mounts: list[str] = Field(
        default_factory=list,
        description=(
            "`optional_mounts` keys to attach for this step's duration, validated against "
            "`optional_mounts`. Steps run in list order, so a mount attached by an earlier "
            "step is available to later ones."
        ),
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-step environment, merged on top of `autostart.env` (this step's values win "
            "on key collisions). Steps run in the order they appear in the list; the editor "
            "offers reordering because of it."
        ),
    )
    working_dir: str = Field(
        default="",
        description=(
            "Path relative to `repo_dir`; empty (default) means `repo_dir` itself. Steps "
            "run in the order they appear in the list; the editor offers reordering because "
            "of it."
        ),
    )
    background: bool = Field(
        default=False,
        description=(
            "When true, detaches via `setsid` and does not wait for completion, so later "
            "steps in the list start without waiting on it. Steps otherwise run in list "
            "order; the editor offers reordering because of it."
        ),
    )
    timeout: int | None = Field(
        default=None,
        description=(
            "Per-step timeout in seconds, overriding `autostart.step_timeout`; null "
            "(default) uses that default. Steps run in the order they appear in the list; "
            "the editor offers reordering because of it."
        ),
    )
    continue_on_error: bool = Field(
        default=False,
        description=(
            "When true, a non-zero exit from this step warns instead of aborting the "
            "remaining steps in the list. Steps run in order, so later steps only run if "
            "this one didn't abort them (or this is true)."
        ),
    )

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        return _reject_offline(v)


class AutostartChain(BaseModel):
    """A serial sequence of steps inside one stage.

    Chains within a stage run in parallel; steps within a chain run in
    order. A dependency between two steps is expressed by putting them in
    the same chain, or in two different stages.
    """

    model_config = ConfigDict(extra="forbid")
    name: str = Field(
        default=...,
        min_length=1,
        description=(
            "Identifier for this chain, unique within its stage. Chains in one stage run "
            "in parallel. `jailbee autostart status` reports progress by stage and step, "
            "not by this name."
        ),
    )
    steps: list[AutostartStep] = Field(
        default_factory=list,
        description="Steps run in list order; a later step starts only after the previous "
        "one exits.",
    )


class AutostartStage(BaseModel):
    """A milestone in an autostart run.

    A stage owns the container-scoped side effects — the network profile and
    the optional mounts — for the whole time its chains run. Chains and
    steps own only in-container work. That is what makes parallel chains
    safe: nothing swaps the profile or detaches a mount underneath them.
    """

    model_config = ConfigDict(extra="forbid")
    stage: str = Field(
        default=...,
        min_length=1,
        description=(
            "Identifier for this stage, unique within its trigger. Stages run in list "
            "order; a stage starts only when the previous one has finished."
        ),
    )
    network: Literal["strict", "loose"] | None = Field(
        default=None,
        description=(
            "Network profile for the whole stage: switched once on entry, restored once on "
            "exit. Null keeps whatever mode the container is already in. Set it here rather "
            "than on a step — a step-level swap is what this level replaces."
        ),
    )
    mounts: list[str] = Field(
        default_factory=list,
        description=(
            "`optional_mounts` keys attached for the whole stage and detached when it ends. "
            "Validated against `optional_mounts`."
        ),
    )
    detach: bool = Field(
        default=False,
        description=(
            "When true, this stage and every stage after it run in a detached supervisor "
            "process while the CLI hands the session to the user. The first stage carrying "
            "it wins; repeating it later is redundant but legal."
        ),
    )
    chains: list[AutostartChain] = Field(
        default_factory=list,
        description="Chains run in parallel. Mutually exclusive with `steps`.",
    )
    steps: list[AutostartStep] = Field(
        default_factory=list,
        description=(
            "Shorthand for a single chain named `main`, for the common serial stage. "
            "Mutually exclusive with `chains`."
        ),
    )

    @field_validator("network", mode="before")
    @classmethod
    def _no_offline(cls, v: object) -> object:
        # The same guard `AutostartStep.network` carries. The stage is where
        # `network:` belongs now, so a user moving one up from a step must
        # not trade the explanation for a bare Literal error.
        return _reject_offline(v)

    @model_validator(mode="after")
    def _one_shape_only(self) -> AutostartStage:
        if self.chains and self.steps:
            raise ValueError(f"stage '{self.stage}' must set `chains` or `steps`, not both")
        return self

    @model_validator(mode="after")
    def _no_container_scope_on_steps(self) -> AutostartStage:
        for chain in self.all_chains():
            for step in chain.steps:
                if step.network is not None:
                    raise ValueError(
                        f"autostart step '{step.name}' sets `network` inside stage "
                        f"'{self.stage}' — move it to the stage: a stage owns the network "
                        f"profile for all of its chains"
                    )
                if step.mounts:
                    raise ValueError(
                        f"autostart step '{step.name}' sets `mounts` inside stage "
                        f"'{self.stage}' — move them to the stage: a stage owns its mounts "
                        f"for all of its chains"
                    )
        return self

    def all_chains(self) -> list[AutostartChain]:
        """The stage's chains, with the `steps:` shorthand expanded.

        Single accessor so no caller has to remember which of the two
        shapes a given stage used.
        """
        if self.chains:
            return list(self.chains)
        if self.steps:
            return [AutostartChain(name="main", steps=list(self.steps))]
        return []


class DockerRegistryMirrorRepoConfig(BaseModel):
    """Per-repo overrides for the host-global rpardini mirror."""

    model_config = ConfigDict(extra="forbid")
    extra_registries: list[str] = Field(
        default_factory=list,
        description=(
            "Upstream registry hostnames this repo pulls images from that aren't covered by "
            "rpardini's built-in defaults (Docker Hub, registry.k8s.io, gcr.io, quay.io, "
            "ghcr.io). Bare hostname[:port] only — no scheme, no path. `jailbee new` / "
            "`jailbee apply` push the merged list into the mirror's REGISTRIES env."
        ),
    )

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

    A shared directory must never carry an IPC socket, a pid file or a lock.
    A *pathname* AF_UNIX socket is not confined by a network namespace —
    `connect()` resolves the path to an inode, and the inode is in the shared
    mount — and a PID means nothing across a PID namespace, so sharing either
    hands one container the ability to drive a daemon inside another. Codex
    is the case this was found on. Name such subpaths in `private` and each is
    mounted over with a per-container directory.
    """

    model_config = ConfigDict(extra="forbid")
    subpath: str = Field(
        default=...,
        description=(
            "Path segment under `<shared_dir>/` that this mount's host-side source lives "
            "at; also feeds `device_name()` for the underlying Incus device name."
        ),
    )
    path: str = Field(
        default=...,
        description="Container-side target path (absolute or `~`-relative) that `subpath` "
        "is bind-mounted to.",
    )
    type: Literal["dir", "file"] = Field(
        default="dir",
        description=(
            "`dir` (default) bind-mounts a directory; `file` bind-mounts a single file. "
            "`seed` is only valid when this is `file`."
        ),
    )
    seed: str | None = Field(
        default=None,
        description=(
            "Content written once to `path` if it doesn't already exist, for `type: file` "
            "mounts only. Must be left unset for `type: dir` — enforced by a validator."
        ),
    )

    private: list[str] = Field(
        default=[],
        description=(
            "Mount-relative subpaths that must NOT be shared: each is mounted over with "
            "a per-container directory. For runtime state whose sharing breaks the "
            "container boundary — an IPC socket, a pid file, a lock. `dir` mounts only."
        ),
    )

    @model_validator(mode="after")
    def _seed_is_file_only(self) -> AgentSharedMount:
        if self.type == "dir" and self.seed is not None:
            raise ValueError(f"seed is only valid for type: file (subpath {self.subpath!r})")
        return self

    @model_validator(mode="after")
    def _private_is_dir_only(self) -> AgentSharedMount:
        if self.private and self.type != "dir":
            raise ValueError(f"private is only valid for type: dir (subpath {self.subpath!r})")
        for entry in self.private:
            parts = PurePosixPath(entry).parts
            if not entry or entry.startswith("/") or not parts or {"..", "."} & set(parts):
                raise ValueError(
                    f"private subpath {entry!r} must be a relative path inside the mount "
                    f"(subpath {self.subpath!r})"
                )
        return self


class AgentConfig(BaseModel):
    """A terminal coding agent wired into the container lifecycle.

    `install`/`update` are shell command lines run inside the container as the
    dev user through the autostart step pipeline, so each gets a fresh
    `bash -lc` login shell — which is why `~/.local/bin` and
    `~/.npm-global/bin` are on PATH for them.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for this agent: gates its shared mount, the strict-mode egress "
            "add, install/update at `jailbee new` time, and the `jailbee doctor` shared-dir "
            "check. Off by default."
        ),
    )
    autostart: bool = Field(
        default=False,
        description="Launch `command` in a background autostart tmux window. Requires "
        "`enabled` to be true.",
    )
    command: str = Field(
        default="",
        description=(
            "Command line the autostart window execs; also the default source for "
            "`install_check`'s probe. Required (non-empty) when `enabled` is true."
        ),
    )
    install: str | None = Field(
        default=None,
        description="Shell command run at `jailbee new` time when `install_check` fails, "
        "i.e. the binary isn't present yet.",
    )
    install_check: str | None = Field(
        default=None,
        description=(
            "Probe deciding install vs. update. Defaults to `command -v <first token of "
            "command>` — see `effective_install_check`."
        ),
    )
    update: str | None = Field(
        default=None,
        description="Shell command run at `jailbee new` time when `install_check` succeeds "
        "and `auto_update` is true.",
    )
    auto_update: bool = Field(
        default=True,
        description=(
            "When false, leaves an existing install untouched; a missing install is still "
            "installed regardless. Defaults to true."
        ),
    )
    install_network: Literal["strict", "loose"] = Field(
        default="strict",
        description="Network mode for the install/update step only, independent of the "
        "container's own default network mode.",
    )
    shared: list[AgentSharedMount] = Field(
        default_factory=list,
        description="Bind mounts this agent needs to keep its auth/config across "
        "containers — see `AgentSharedMount`.",
    )
    egress_allow: list[str] = Field(
        default_factory=list,
        description="Strict-mode egress allowlist entries added while this agent is "
        "enabled. Same grammar as the top-level `egress_allow`.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables passed to both the install/update step and the "
        "autostart launch step.",
    )
    skills_dir: str | None = Field(
        default=None,
        description=(
            "Container-side directory this agent reads user-level skills from — e.g. "
            "`~/.codex/skills`. When set and covered by a `shared` mount, `jailbee "
            "new`/`jailbee apply` copy jailbee's bundled skills into the shared copy "
            "of it (see `install_jailbee_skills`). Presets set it for the agents with "
            "a skills mechanism; leave unset for agents without one."
        ),
    )
    install_jailbee_skills: bool = Field(
        default=True,
        description=(
            "When true (default), `jailbee new`/`jailbee apply` copy jailbee's bundled "
            "skills (`jailbee-usage`, `jailbee-repo-setup`, `jailbee-pr-review`) into "
            "this agent's shared `skills_dir` so the in-container agent understands "
            "jailbee. Host-side file copy only, no network. Does nothing when "
            "`skills_dir` is unset or no `shared` mount covers it."
        ),
    )

    @field_validator("skills_dir")
    @classmethod
    def _skills_dir_has_no_traversal(cls, v: str | None) -> str | None:
        """Reject an empty `skills_dir` or one carrying `.` / `..` segments.

        The value is joined onto the covering mount's host-side subpath in
        `agent_skills._skills_host_dir`; a `..` there would step outside
        `<shared_dir>` and let the skills copy write and delete arbitrary host
        paths, driven by a repo-committed config. A `~`-relative or absolute
        path is legitimate — only traversal segments are invalid. A dotfile
        such as `~/.config` is not a `.` segment, so this does not
        false-positive on dotfiles.

        Segments are read off the raw string (`split`), not
        `PurePosixPath.parts`: pathlib drops `.` segments, so `./skills` would
        arrive here already normalised to `skills` and slip through.
        """
        if v is None:
            return None
        segments = v.split("/")
        if not v or "." in segments or ".." in segments:
            raise ValueError(
                f"skills_dir {v!r} must be a non-empty path without '.' / '..' segments"
            )
        return v

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

    `enabled` (inherited) does more here than the generic `AgentConfig.enabled`
    switch: it also gates the shared `<shared_dir>/claude` cache mount (see
    `Config.effective_shared_caches`), the `CLAUDE_API_HOSTS` strict-mode
    egress auto-add, the `<shared_dir>/claude` subdir creation on `jailbee
    init`, and the claude-subdir presence check in `jailbee doctor`. When
    enabled, jailbee creates an empty `<shared_dir>/claude` directory as a
    bind-mount source and seeds `<shared_dir>/claude/.claude.json` — the
    golden image exports `CLAUDE_CONFIG_DIR=$HOME/.claude`, so Claude Code
    reads its global config from inside that directory mount. The seed is `{}`
    (a clean first run) unless `seed_onboarding` adopts a login the repo's
    credential group already holds. No host `~/.claude` / `~/.claude.json` is
    read. `autostart`, `command` and `auto_update` are otherwise identical in
    meaning to any other agent's.
    """

    plugins_enabled: bool = Field(
        default=True,
        description=(
            "When true (default), also auto-extends the strict-mode egress allowlist with "
            "`CLAUDE_PLUGIN_HOSTS` (GitHub + npm) so Claude Code's plugin marketplace, "
            "skills and SessionStart hooks load. Set to false to keep the API reachable "
            "while blocking marketplace traffic. Has no effect when `enabled` is false."
        ),
    )
    seed_onboarding: bool = Field(
        default=True,
        description=(
            "When true (default), `jailbee init`/`jailbee apply` mark a *fresh* "
            "`<shared_dir>/claude/.claude.json` as already onboarded, and accept the "
            "trust dialog for the repo's in-container path, whenever this repo's "
            "credential group already holds a login. Claude Code's first-run wizard is "
            "gated on that flag alone and never looks at the mounted credential, so "
            "without this a new container asks for a `/login` the credential has "
            "already answered. With no shared login there is nothing to adopt and the "
            "wizard runs as before. A config home Claude Code has already written is "
            "never touched. Has no effect when `enabled` is false."
        ),
    )
    ai_pr_description: bool = Field(
        default=True,
        description=(
            "When true (default), `jailbee pr` asks the in-container Claude to generate "
            "the PR title and body from the branch's commits and diff, falling back to a "
            "placeholder on failure. Has no effect when `enabled` is false."
        ),
    )
    ai_pr_branch: bool = Field(
        default=True,
        description=(
            "When true (default), `jailbee pr` asks the in-container Claude to propose a "
            "convention-following PR head branch name when opening a new PR. Has no effect "
            "when `enabled` or `ai_pr_description` is false."
        ),
    )
    pr_prompt: str | None = Field(
        default=None,
        max_length=_MAX_PR_PROMPT_LEN,
        description=(
            "Project-specific PR-writing instructions, typically a YAML block scalar in a "
            "repo's `.jailbee/config.yaml`. Embedded in jailbee's own prompt as a section "
            "that outranks the generic guidance, without overriding the JSON response "
            "contract. Capped at 20 000 characters. Has no effect when `enabled` or "
            "`ai_pr_description` is false."
        ),
    )
    ai_pr_model: str | None = Field(
        default="sonnet",
        description=(
            "Model passed to `claude --model` when generating PR text. Defaults to "
            "`sonnet` so description generation doesn't compete with the coding work's own "
            "budget. Accepts an alias or a full model ID; null inherits the container's "
            "default model. Has no effect when `enabled` or `ai_pr_description` is false."
        ),
    )
    ai_pr_timeout: int = Field(
        default=600,
        gt=0,
        description=(
            "Seconds `jailbee pr` gives the in-container Claude to produce PR text before "
            "falling back to a placeholder. Defaults to 600 — generation is an agentic run "
            "whose cost scales with the repo, not just the diff. Raise it for a large tree. "
            "Has no effect when `enabled` or `ai_pr_description` is false."
        ),
    )

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

    `api_tokens` may only be set in `~/.config/jailbee/global.yaml` —
    `load_config` rejects this block in a repo's `.jailbee/config.yaml`
    outright, since committing a repo file with a token would leak it.
    """

    model_config = ConfigDict(extra="forbid")
    enabled: bool = Field(
        default=False,
        description=(
            "Master switch. Off by default; opt in via ~/.config/jailbee/global.yaml. When "
            "false, jailbee skips the api.github.com strict-mode egress add, the GH_TOKEN "
            "autostart injection, and the github doctor checks. The gh binary itself is "
            "always installed in the golden image regardless."
        ),
    )
    api_tokens: dict[str, SecretStr] = Field(
        default_factory=dict,
        description=(
            "Map from `container_prefix` to a fine-grained GitHub PAT, one entry per GitHub "
            "resource owner (org or personal account). Each value is a secret — masked in "
            "`repr(cfg)` / config dumps — and having any entry here requires "
            "`~/.config/jailbee/global.yaml` to be mode 0600; `load_config_from_text` "
            "hard-fails otherwise."
        ),
    )


class Autostart(BaseModel):
    """The top-level autostart block: which shell steps run inside a
    container, and when.
    """

    model_config = ConfigDict(extra="forbid")
    on_create: list[AutostartStep] | list[AutostartStage] = Field(
        default_factory=list,
        description=(
            "Steps run once after `jailbee new` provisions the container. Either a flat list "
            "of steps (one implicit stage) or a list of stages."
        ),
    )
    on_start: list[AutostartStep] | list[AutostartStage] = Field(
        default_factory=list,
        description=(
            "Steps run on every stopped-to-running transition: both `jailbee new` (after "
            "`on_create`) and `jailbee start`. Put one-shot setup in `on_create` and "
            "recurring launches in `on_start` — don't duplicate between the two. Either a "
            "flat list of steps (one implicit stage) or a list of stages."
        ),
    )

    @field_validator("on_create", "on_start", mode="before")
    @classmethod
    def _no_mixed_shapes(cls, v: object) -> object:
        """Reject a trigger that mixes flat steps and stages.

        Pydantic's union would otherwise pick one member and report a
        confusing per-field error on the entries that do not fit.
        """
        if not isinstance(v, list) or not v:
            return v
        shapes = {isinstance(e, dict) and "stage" in e for e in v}
        if len(shapes) > 1:
            raise ValueError(
                "autostart trigger may not mix flat steps and stages — "
                "convert the whole list to stages"
            )
        return v

    step_timeout: int = Field(
        default=600,
        description="Default per-step timeout in seconds, overridable per step via "
        "`AutostartStep.timeout`.",
    )
    env: dict[str, str] = Field(
        default_factory=dict,
        description="Global environment merged into every step; a step's own `env` wins "
        "on key collisions.",
    )
