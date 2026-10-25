"""Autostart steps, the docker registry mirror override, and the terminal
coding-agent models (generic `AgentConfig` plus the Claude-specific
subclass), plus the GitHub CLI integration and the top-level autostart block.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from jailbee.config.common import CONTAINER_USERNAME
from jailbee.config.models_net import _reject_offline

# An agent name becomes a tmux window name and a `jailbee doctor` label —
# kept to the safest common subset of what both accept. It does *not* reach
# any shared-mount device name: those derive from each `shared[].subpath` via
# `device_name()`. The two only coincide because every shipped preset happens
# to name its subpath after the agent. (The host-wide instructions mount is the
# exception: its device is `agent-instructions-<device_name(agent)>`.)
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


# An exact registry hostname. Each `extra_registries` entry becomes a key in
# the mirror's nginx `hostnames` maps (registry-mirror/nginx.conf), where `*`,
# a leading `.` and `~` are patterns — `*.amazonaws.com` would admit every host
# under it. No `:port` either: the mirror matches CONNECT by host and fetches
# upstream on 443, so another port cannot be served.
REGISTRY_HOSTNAME_RE = re.compile(
    r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*", re.IGNORECASE
)


class DockerRegistryMirrorRepoConfig(BaseModel):
    """Per-repo overrides for the host-global rpardini mirror."""

    model_config = ConfigDict(extra="forbid")
    extra_registries: list[str] = Field(
        default_factory=list,
        description=(
            "Upstream registry hostnames this repo pulls images from that aren't covered by "
            "rpardini's built-in defaults (Docker Hub, registry.k8s.io, gcr.io, quay.io, "
            "ghcr.io). The mirror refuses every registry outside the defaults and this "
            "list, so a pull from an unlisted one fails. Exact hostnames only — no "
            "wildcard, port, scheme or path. `jailbee new` / `jailbee apply` push the "
            "merged list into the mirror's REGISTRIES env."
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
                raise ValueError(f"registry must be a bare hostname, not a URL/path: {raw!r}")
            host, sep, _port = raw.partition(":")
            if sep and REGISTRY_HOSTNAME_RE.fullmatch(host):
                raise ValueError(
                    f"registry on a non-default port is not supported by the mirror "
                    f"(it serves registries on 443 only): {raw!r}"
                )
            if not REGISTRY_HOSTNAME_RE.fullmatch(raw):
                raise ValueError(
                    f"must be an exact registry hostname — no wildcard, leading or "
                    f"trailing dot, or pattern: {raw!r}"
                )
        return v


# `pr.prompt` ships to the container as an environment variable inside
# jailbee's own prompt. The cap is a sanity bound, not a model context limit:
# it turns a pasted-in-by-accident file into a config error instead of an
# agent invocation that fails opaquely and silently falls back.
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


class AgentGlobalInstructions(BaseModel):
    """Where an agent reads host-wide instructions inside the container.

    `dir` is mounted whole, read-only, from jailbee's host-level staging
    directory (`agent_instructions.staging_dir`), which holds one file named
    `file`: `~/.config/jailbee/AGENTS.md` under the agent's own name. A
    directory, not a file, because a single-file bind mount pins the inode
    and an editor's write-and-rename save would leave the container on the
    old content. Hence `dir` must be a directory jailbee owns outright — see
    `AgentConfig._global_instructions_outside_shared`.
    """

    model_config = ConfigDict(extra="forbid")
    dir: str = Field(
        default=...,
        description=(
            "Container-side directory (absolute or `~`-relative) jailbee mounts "
            "read-only with the host-wide instructions; Claude's preset uses "
            "`/etc/claude-code`. Must not overlap one of the agent's `shared` mounts, "
            "and must be a dedicated directory (not `/`, `/etc`, `/usr`, `/home` "
            "or the home directory)."
        ),
    )
    file: str = Field(
        default=...,
        description="File name the agent reads the instructions under, e.g. `CLAUDE.md`.",
    )

    @field_validator("dir")
    @classmethod
    def _dir_is_rooted_without_traversal(cls, v: str) -> str:
        # Raw-string segments preserve `.` for validation; pathlib drops it.
        segments = v.split("/")
        if not (v.startswith("/") or v == "~" or v.startswith("~/")):
            raise ValueError(f"global_instructions.dir {v!r} must be absolute or start with '~/'")
        if "\x00" in v:
            raise ValueError("global_instructions.dir must not contain a NUL byte")
        if "." in segments or ".." in segments:
            raise ValueError(f"global_instructions.dir {v!r} must not contain '.' / '..' segments")
        return v

    @field_validator("file")
    @classmethod
    def _file_is_bare_name(cls, v: str) -> str:
        if not v or v in (".", "..") or "/" in v or "\x00" in v:
            raise ValueError(f"global_instructions.file {v!r} must be a bare file name")
        return v


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
    headless: str | None = Field(
        default=None,
        description=(
            "Shell command line that runs this agent once, non-interactively, and prints "
            "its answer — what `jailbee pr` uses to write PR text. Run in a `bash -lc` "
            "login shell in the repo directory. The prompt is in `$JAILBEE_PR_PROMPT` and "
            "the model, empty when none applies, in `$JAILBEE_PR_MODEL`; read both from the "
            "environment, never interpolate them. Presets set it for the agents with a "
            "one-shot mode; leave unset for agents without one."
        ),
    )
    prompt_args: str | None = Field(
        default=None,
        description=(
            "Shell words appended to `command` (one space) for a launch that carries an "
            "initial prompt. Read the prompt from `$JAILBEE_INITIAL_PROMPT_FILE` through "
            "the shell, never interpolate it. Unset means this agent cannot take an "
            "initial prompt."
        ),
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
            "skills (`jailbee-usage`, `jailbee-repo-setup`, `jailbee-pr-review`, "
            "`jailbee-issue-management`, `jailbee-bug-report`, `jailbee-orchestration`, "
            "`jailbee-orchestration-child`) into "
            "this agent's shared `skills_dir` so the in-container agent understands "
            "jailbee. Host-side file copy only, no network. Does nothing when "
            "`skills_dir` is unset or no `shared` mount covers it."
        ),
    )
    global_instructions: AgentGlobalInstructions | None = Field(
        default=None,
        description=(
            "Where this agent reads host-wide instructions. When set, jailbee "
            "mounts `~/.config/jailbee/AGENTS.md` (renamed to `file`) read-only "
            "at `dir` in every container. Presets set it for the agents wired so "
            "far (claude); leave unset otherwise."
        ),
    )

    @field_validator("headless")
    @classmethod
    def _headless_not_blank(cls, v: str | None) -> str | None:
        """A blank command would run nothing and report a confusing empty answer."""
        if v is not None and not v.strip():
            raise ValueError("headless must be a non-empty command line, or unset")
        return v

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

    @model_validator(mode="after")
    def _global_instructions_outside_shared(self) -> Self:
        """Keep the dedicated read-only instructions mount disjoint from shared state."""
        if self.global_instructions is None:
            return self

        home = f"/home/{CONTAINER_USERNAME}"

        def parts(path: str) -> tuple[str, ...]:
            if path == "~":
                path = home
            elif path.startswith("~/"):
                path = f"{home}/{path[2:]}"
            return PurePosixPath(posixpath.normpath(path)).parts

        target = parts(self.global_instructions.dir)
        # The mount hides whatever lies under it, so a system directory or the
        # home directory itself is never a sensible destination.
        if target in {("/",), ("/", "etc"), ("/", "usr"), ("/", "home"), parts("~")}:
            raise ValueError(
                f"global_instructions.dir {self.global_instructions.dir!r} is too broad; "
                "choose a dedicated directory such as `/etc/claude-code`"
            )
        for mount in self.shared:
            base = parts(mount.path)
            # Either direction: inside a shared mount, or containing one.
            if target[: len(base)] == base or base[: len(target)] == target:
                raise ValueError(
                    f"global_instructions.dir {self.global_instructions.dir!r} overlaps "
                    f"the shared mount {mount.path!r}; choose a directory jailbee owns outright"
                )
        return self

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
            "`CLAUDE_PLUGIN_HOSTS` (GitHub) so Claude Code's plugin marketplace, "
            "skills and SessionStart hooks load. The npm registry is not included; "
            "add `registry.npmjs.org:443` to `egress_allow` if a plugin needs it. "
            "Set to false to keep the API reachable while blocking marketplace "
            "traffic, which also closes GitHub as an upload path. Has no effect "
            "when `enabled` is false."
        ),
    )
    agent_view: bool = Field(
        default=False,
        description=(
            "When false (default), containers get `CLAUDE_CODE_DISABLE_AGENT_VIEW=1`, which "
            "turns off Claude Code's agent view (`claude agents`, `--bg`, `/background`) and "
            "the on-demand background daemon behind it. The daemon keeps its lock file in "
            "the `~/.claude` every container of the repo shares, so daemons in two "
            "containers take the lock from each other and background jobs die. Set to true "
            "if you run one container of the repo at a time. Has no effect when `enabled` "
            "is false."
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


class PrConfig(BaseModel):
    """`pr:` — how `jailbee pr` writes a pull request's title, body and branch name.

    Replaces the `ai_pr_*` / `pr_prompt` fields that used to hang off the Claude
    agent: they describe `jailbee pr`, and which agent does the writing is now a
    choice (`agent`), not an assumption.
    """

    model_config = ConfigDict(extra="forbid")
    agent: str = Field(
        default="auto",
        description=(
            "Which in-container agent writes the PR text. `auto` (default) picks the "
            "repo's own agent: among the enabled agents the ones with `autostart` on, "
            "else all enabled ones, Claude first and then the first with a `headless` "
            "command — and `claude-jb` instead of `claude` when `litellm.autostart` is "
            "on. Name an agent (`claude`, `codex`, ...) to pin it, or `claude-jb` to run "
            "Claude through the LiteLLM wrapper. The agent must be enabled and have a "
            "`headless` command; otherwise `jailbee pr` warns and falls back to a "
            "placeholder."
        ),
    )
    ai_description: bool = Field(
        default=True,
        description=(
            "When true (default), `jailbee pr` asks the in-container agent to generate "
            "the PR title and body from the branch's commits and diff, falling back to a "
            "placeholder on failure."
        ),
    )
    ai_branch: bool = Field(
        default=True,
        description=(
            "When true (default), `jailbee pr` asks the in-container agent to propose a "
            "convention-following PR head branch name when opening a new PR. Has no "
            "effect when `ai_description` is false."
        ),
    )
    prompt: str | None = Field(
        default=None,
        max_length=_MAX_PR_PROMPT_LEN,
        description=(
            "Project-specific PR-writing instructions, typically a YAML block scalar in a "
            "repo's `.jailbee/config.yaml`. Embedded in jailbee's own prompt as a section "
            "that outranks the generic guidance, without overriding the JSON response "
            "contract. Capped at 20 000 characters. Has no effect when `ai_description` "
            "is false."
        ),
    )
    model: str | None = Field(
        default=None,
        description=(
            "Model passed to the agent's `--model` when generating PR text. Left unset, "
            "Claude (and `claude-jb`) use `sonnet` so description generation doesn't "
            "compete with the coding work's own budget, and every other agent uses its "
            "own default. Accepts an alias or a full model ID; an explicit null inherits "
            "the container's default model on every agent. Has no effect when "
            "`ai_description` is false."
        ),
    )
    timeout: int = Field(
        default=600,
        gt=0,
        description=(
            "Seconds `jailbee pr` gives the in-container agent to produce PR text before "
            "falling back to a placeholder. Defaults to 600 — generation is an agentic "
            "run whose cost scales with the repo, not just the diff. Raise it for a "
            "large tree. Has no effect when `ai_description` is false."
        ),
    )

    @field_validator("agent")
    @classmethod
    def _agent_is_a_name(cls, v: str) -> str:
        """`auto` or an agent name: the value picks a config entry, never a command."""
        if not _AGENT_NAME_RE.fullmatch(v):
            raise ValueError(
                f"must be `auto` or an agent name (lowercase letters, digits and `-`), got {v!r}"
            )
        return v

    @field_validator("model")
    @classmethod
    def _reject_non_model_value(cls, v: str | None) -> str | None:
        """A model name is a single token — reject anything that isn't one.

        The value reaches the agent through an environment variable, so embedded
        flags could never be executed as such. The check exists to turn a typo
        or a misunderstanding into a config error, rather than a non-zero agent
        exit that `generate_pr_text` reports only as a failed generation. Use
        `null`, not an empty string, to inherit the container's own default
        model.
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

    The token is per repo: `token`, in the host-local file. The deprecated
    `api_tokens` map may only be set in `~/.config/jailbee/global.yaml`.
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
            "Deprecated, removed in 2.0.0: set `token` in each repo's host-local file "
            "instead (`jailbee config migrate --apply` moves these entries). Map from "
            "`container_prefix` to a fine-grained GitHub PAT with the same read-only "
            "scopes as `token`. Each value is a secret — masked in `repr(cfg)` / config "
            "dumps — and having any entry here requires `~/.config/jailbee/global.yaml` "
            "to be mode 0600; `load_config_from_text` hard-fails otherwise."
        ),
    )
    token: SecretStr | None = Field(
        default=None,
        description=(
            "This repo's fine-grained GitHub PAT, injected into its containers. Grant only "
            "Contents: Read, Issues: Read, Pull requests: Read, and Metadata: Read — every "
            "GitHub write (issue create/edit/comment/label/close/reopen, PR review/comment/"
            "description) goes through the host-side outbox commands (`jailbee issue "
            "apply`, `jailbee review apply`, `jailbee outbox apply`) instead, authenticated "
            "by the host's own `gh`, never this token. Valid only in the host-local file "
            "`~/.config/jailbee/repos/<container_prefix>.yaml`, which must then be mode "
            "0600; wins over this repo's deprecated `api_tokens` entry."
        ),
    )

    def token_for(self, container_prefix: str) -> SecretStr | None:
        """The token this repo's containers get: `token`, else its `api_tokens` entry."""
        return self.token if self.token is not None else self.api_tokens.get(container_prefix)


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
