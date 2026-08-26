"""Subprocess wrapper for the `incus` CLI.

This is the only module that calls subprocess. All other modules go through
this interface, which makes them unit-testable with mocks.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import yaml


class IncusError(Exception):
    """Raised when an `incus` CLI invocation fails."""


class IncusTimeoutError(IncusError):
    """Raised when an `incus` CLI invocation exceeded its ``timeout``.

    A subclass so that every existing ``except IncusError`` keeps catching
    timeouts unchanged. Callers that can say something useful *specifically*
    about an expiry — as opposed to a missing binary or a non-zero exit —
    catch this first; `pr_ai` does, to point at the transcript the timed-out
    Claude run left behind in the container.
    """


# An argument longer than this, or one spanning lines, is summarised rather
# than echoed in an error message.
_ARG_ECHO_LIMIT = 160


def _render_args(args: list[str]) -> str:
    """Args as a one-line command, with bulky ones summarised.

    `incus exec <name> -- bash -c <script>` carries its entire script as a
    single argument. The registry mirror's provisioning script is some sixty
    lines, and echoing it buried the actual failure under it — twice, once
    per exception in the chain — leaving a screenful of shell in which the
    reason appeared exactly once, at the very end. The caller knows which
    script it passed; what the message has to say is which container and
    why.
    """
    out: list[str] = []
    for arg in args:
        if len(arg) > _ARG_ECHO_LIMIT or "\n" in arg:
            out.append(f"<{len(arg)}-byte script, {arg.count(chr(10)) + 1} lines>")
        else:
            out.append(arg)
    return " ".join(out)


def _missing_binary_error(binary: str) -> IncusError:
    """The `incus` binary is not on PATH — as an IncusError, not an OSError.

    Every caller in the codebase catches ``IncusError`` and nothing else,
    because incus.py is the sole subprocess boundary. An unwrapped
    ``FileNotFoundError`` therefore escapes as a raw traceback out of
    whichever command the user ran — including `jailbee doctor`, whose whole
    job is to *report* a host that isn't set up yet. Normalised here for the
    same reason timeouts are.
    """
    return IncusError(
        f"`{binary}` not found in PATH — Incus does not appear to be installed. "
        f"Install it and re-run."
    )


def _partial_output(exc: subprocess.TimeoutExpired) -> str:
    """Whatever the timed-out command managed to write, as text.

    `TimeoutExpired` carries raw bytes even when the call asked for text
    mode — CPython builds it from the undecoded read buffers — so this
    decodes leniently for the same reason `_run` passes ``errors="replace"``.
    """

    def as_text(raw: object) -> str:
        if raw is None:
            return ""
        if isinstance(raw, bytes):
            return raw.decode(errors="replace").strip()
        return str(raw).strip()

    stdout, stderr = as_text(exc.stdout), as_text(exc.stderr)
    if stdout and stderr:
        return f"{stderr}\n--- stdout ---\n{stdout}"
    return stderr or stdout


class Incus:
    """Typed wrapper around the `incus` CLI.

    Set ``dry_run=True`` to log commands without executing them — useful for
    development debugging. ``binary`` allows overriding the `incus` path.
    """

    def __init__(self, binary: str = "incus", dry_run: bool = False) -> None:
        self.binary = binary
        self.dry_run = dry_run

    # ---- Internal helpers ---------------------------------------------------

    def _run(
        self,
        args: list[str],
        *,
        capture_output: bool = True,
        check: bool = True,
        timeout: int | None = None,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.binary, *args]
        if self.dry_run:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # stdin=DEVNULL so non-interactive `incus exec` (and any other
        # incus subcommand) cannot drain the parent terminal's input
        # buffer. Without this, characters the user typed while a `jailbee`
        # command was still running — intending them for the *next*
        # shell command — got forwarded into the container by
        # `incus exec`'s default stdin-forwarding behavior and lost.
        # The interactive shell path (exec_interactive) is a separate
        # method and still inherits stdin so tmux/bash get keypresses.
        try:
            result = subprocess.run(
                cmd,
                capture_output=capture_output,
                text=True,
                # Decode leniently: `incus exec` streams arbitrary command
                # output, and `git diff --submodule=diff` in particular emits
                # raw file bytes. A latin-1 text file or mislabeled binary puts
                # invalid-UTF-8 bytes on stdout, which strict decoding turns
                # into a UnicodeDecodeError that crashes the whole `jailbee`
                # invocation (e.g. `jailbee diff`). incus's own JSON/YAML output is
                # always UTF-8, so replacement only ever kicks in for pass-through
                # command output, where a U+FFFD beats a traceback.
                errors="replace",
                check=False,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise _missing_binary_error(self.binary) from e
        except subprocess.TimeoutExpired as e:
            # Normalize into IncusError so callers — which only ever catch
            # IncusError (incus.py is the sole subprocess boundary) — handle
            # timeouts uniformly. The git-status probe relies on this to
            # degrade a busy/mid-create container to "?" instead of crashing.
            #
            # Carry the partial output: a command that timed out got *some*
            # way, and how far is the entire diagnosis. A 10-minute
            # provisioning timeout whose message is only "timed out" cannot
            # be told apart from a DNS failure in its first second.
            detail = _partial_output(e)
            message = f"`incus {_render_args(args)}` timed out after {timeout}s"
            raise IncusTimeoutError(f"{message}: {detail}" if detail else message) from e
        if check and result.returncode != 0:
            # Include stdout AND stderr — `incus exec` runs scripts whose
            # progress (echo) goes to stdout, while errors (set -u traps,
            # apt-get warnings) go to stderr. Dropping stdout leaves
            # failures unreadable because the last `==> ...` banner that
            # tells us WHICH step failed lives there.
            stdout = result.stdout.strip() if result.stdout else ""
            stderr = result.stderr.strip() if result.stderr else ""
            detail = stderr
            if stdout:
                detail = f"{stderr}\n--- stdout ---\n{stdout}" if stderr else stdout
            raise IncusError(
                f"`incus {_render_args(args)}` failed (exit {result.returncode}): {detail}"
            )
        return result

    # Optimistic-concurrency retry tuning for instance-config read-modify-writes.
    _ETAG_RETRIES = 5
    _ETAG_BACKOFF = 0.2  # seconds; linear backoff (attempt+1) * _ETAG_BACKOFF

    def _run_retrying_on_etag(
        self, args: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        """`_run`, but retry on a transient optimistic-concurrency (ETag) race.

        `incus config set`, `config device add/remove`, and `profile assign` are
        read-modify-writes against the instance config: Incus GETs the config
        (capturing its ETag), mutates it, and PUTs back with an `If-Match`
        precondition. A freshly-started container churns `volatile.*` keys
        asynchronously as it boots and the network comes up, bumping the ETag
        between the GET and the PUT, so the PUT can fail with
        `Error: ETag doesn't match: <old> vs <new>`.

        That failure is transient and the operations are idempotent — the next
        GET picks up the new ETag — so retry with a small bounded backoff rather
        than aborting the whole `jailbee new`. Non-ETag failures fail fast so real
        errors (e.g. a device that already exists) are never masked.
        """
        for attempt in range(self._ETAG_RETRIES):
            try:
                return self._run(args, **kwargs)
            except IncusError as e:
                if "ETag doesn't match" in str(e) and attempt < self._ETAG_RETRIES - 1:
                    time.sleep(self._ETAG_BACKOFF * (attempt + 1))
                    continue
                raise
        # Unreachable: the loop either returns or raises on the final attempt.
        raise AssertionError("unreachable")

    # ---- Container queries --------------------------------------------------

    def list_containers(
        self,
        *,
        fast: bool = False,
        timeout: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return all containers as a list of dicts.

        ``fast=True`` adds ``--fast``, which skips the per-instance state
        fetch — ``state`` comes back null, so ``ip`` and memory figures are
        unavailable. Verified against Incus 6.0.5: every other top-level key,
        including ``profiles`` and ``config``, is unchanged, so profile-based
        filtering still works. Used by shell completion, which needs names and
        nothing else.

        ``timeout`` bounds the call; `_run` normalises an expiry into
        ``IncusError``. Completion sets it so a wedged daemon cannot hang the
        user's shell.
        """
        args = ["list", "--format", "json"]
        if fast:
            args.append("--fast")
        result = self._run(args, timeout=timeout)
        return json.loads(result.stdout) if result.stdout else []

    def exists(self, name: str) -> bool:
        """Return True if a container with this name exists."""
        return any(c["name"] == name for c in self.list_containers())

    def console_log(self, name: str, *, timeout: int | None = None) -> str:
        """The container's console ring buffer (`incus console --show-log`).

        This is where systemd's own shutdown narration lands — ``A stop job
        is running for …`` — which is the one place that names the unit
        blocking a shutdown. Distinct from ``incus info --show-log``, which
        shows the LXC log rather than the guest's console.
        """
        return self._run(["console", name, "--show-log"], timeout=timeout).stdout

    # ---- Container lifecycle ------------------------------------------------

    def init(self, image: str, name: str) -> None:
        """Create an instance from an image without starting it."""
        self._run(["init", image, name])

    def copy(self, source: str, dest: str) -> None:
        self._run(["copy", source, dest])

    def start(self, name: str) -> None:
        self._run(["start", name])

    def stop(self, name: str, force: bool = False, timeout: int | None = None) -> None:
        """Stop a container; ``timeout`` bounds the clean shutdown, in seconds.

        Omitting ``timeout`` is not the neutral default it looks like: incusd
        turns the CLI's ``--timeout -1`` into **600 seconds**
        (``cmd/incusd/instance_state.go``, ``doInstanceStatePut``), so a
        container whose init never finishes shutting down blocks for ten
        minutes and then fails with ``Failed shutting down instance, status
        is "Running": context deadline exceeded``. Callers that a user is
        watching should pass a budget — see `jailbee.stopping`.

        ``force`` is a power cut and maps to a zero timeout server-side, so
        the two flags are mutually exclusive; ``force`` wins.
        """
        args = ["stop", name]
        if force:
            args.append("--force")
        elif timeout is not None:
            args += ["--timeout", str(timeout)]
        self._run(args)

    def restart(self, name: str) -> None:
        self._run(["restart", name])

    def delete(self, name: str, force: bool = False) -> None:
        args = ["delete", name]
        if force:
            args.append("--force")
        self._run(args)

    # ---- Container exec -----------------------------------------------------

    def _exec_args(
        self,
        name: str,
        cmd: list[str],
        *,
        uid: int | None,
        gid: int | None,
        cwd: str | None,
        env: dict[str, str] | None,
        init_groups: bool,
    ) -> list[str]:
        """Build ``incus exec`` args shared by ``exec`` / ``exec_interactive``.

        When ``init_groups`` is set for a non-root ``uid``, the command runs
        as container root and drops to the user via ``setpriv … --init-groups``
        instead of ``incus exec --user/--group``. Incus's ``--user/--group``
        does NOT call ``initgroups(3)``, so the user's *supplementary* groups
        (e.g. ``kvm``, ``docker``) are dropped and group-based device/file
        access fails; ``setpriv --init-groups`` restores them. ``incus exec``
        still injects the base-profile ``environment.X`` and ``--env`` vars,
        and ``setpriv`` preserves the environment (unlike ``sudo -i``).
        """
        use_setpriv = init_groups and uid is not None and uid != 0
        args = ["exec", name]
        if not use_setpriv:
            if uid is not None:
                args += ["--user", str(uid)]
            if gid is not None:
                args += ["--group", str(gid)]
        if cwd is not None:
            args += ["--cwd", cwd]
        if env:
            for k, v in env.items():
                args += ["--env", f"{k}={v}"]
        if use_setpriv:
            assert uid is not None  # guarded by use_setpriv
            regid = str(gid) if gid is not None else str(uid)
            args += [
                "--",
                "setpriv",
                "--reuid",
                str(uid),
                "--regid",
                regid,
                "--init-groups",
                "--",
                *cmd,
            ]
        else:
            args += ["--", *cmd]
        return args

    def exec(
        self,
        name: str,
        cmd: list[str],
        *,
        uid: int | None = None,
        gid: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int | None = None,
        init_groups: bool = False,
    ) -> str:
        """Run a command inside the container, return stdout.

        ``uid`` and ``gid`` are passed to ``incus exec --user`` /
        ``--group`` (Incus requires numeric IDs, not names). Set
        ``init_groups`` to run with the user's full supplementary groups
        (see ``_exec_args``).
        """
        args = self._exec_args(
            name, cmd, uid=uid, gid=gid, cwd=cwd, env=env, init_groups=init_groups
        )
        result = self._run(args, timeout=timeout)
        return result.stdout

    def exec_interactive(
        self,
        name: str,
        cmd: list[str],
        *,
        uid: int | None = None,
        gid: int | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        init_groups: bool = False,
    ) -> int:
        """Run an interactive command (no stdout capture). Returns exit code.

        ``uid``/``gid``/``cwd``/``env`` map to ``incus exec
        --user/--group/--cwd/--env``. Using these instead of wrapping the
        command in ``sudo`` keeps the env vars Incus injects from the base
        profile (``environment.X``) reachable to the launched shell —
        ``sudo -i`` would otherwise wipe everything not in ``env_keep``.
        Note that ``--user UID`` does NOT read ``/etc/passwd`` to derive
        ``HOME``, so callers that need it must pass it via ``env``.

        Set ``init_groups`` to load the user's supplementary groups (see
        ``_exec_args``) — required for group-based access (kvm, docker, …)
        from interactive sessions.
        """
        args = self._exec_args(
            name, cmd, uid=uid, gid=gid, cwd=cwd, env=env, init_groups=init_groups
        )
        full = [self.binary, *args]
        if self.dry_run:
            return 0
        try:
            return subprocess.run(full, check=False).returncode
        except FileNotFoundError as e:
            raise _missing_binary_error(self.binary) from e

    # ---- Profiles -----------------------------------------------------------

    def profile_create(self, name: str) -> None:
        self._run(["profile", "create", name])

    def profile_delete(self, name: str) -> None:
        self._run(["profile", "delete", name])

    def profile_exists(self, name: str) -> bool:
        result = self._run(["profile", "list", "--format", "json"], check=True)
        profiles = json.loads(result.stdout) if result.stdout else []
        return any(p["name"] == name for p in profiles)

    def profile_set_yaml(self, name: str, yaml_content: str) -> None:
        """Replace a profile's content with the given YAML string."""
        cmd = [self.binary, "profile", "edit", name]
        if self.dry_run:
            return
        try:
            result = subprocess.run(
                cmd,
                input=yaml_content,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as e:
            raise _missing_binary_error(self.binary) from e
        if result.returncode != 0:
            raise IncusError(f"`incus profile edit {name}` failed: {result.stderr.strip()}")

    def profile_config_get(self, name: str, key: str) -> str | None:
        """Return one `config` key of a profile, or None when it is unset.

        Read out of `profile show` rather than `incus profile get`: the
        latter prints an empty line and exits 0 both for an unset key and
        for a key set to the empty string, and the caller here has to tell
        those apart (an explicitly set value is never overwritten).
        """
        parsed = yaml.safe_load(self.profile_show(name)) or {}
        value = (parsed.get("config") or {}).get(key)
        return None if value is None else str(value)

    def profile_config_set(self, name: str, key: str, value: str) -> None:
        """Set a single `config` key on a profile, leaving the rest as-is."""
        self._run(["profile", "set", name, key, value])

    def profile_assign(self, container: str, profiles: list[str]) -> None:
        self._run_retrying_on_etag(["profile", "assign", container, ",".join(profiles)])

    def profile_show(self, name: str) -> str:
        """Return the profile's current state as YAML (raw stdout)."""
        result = self._run(["profile", "show", name])
        return result.stdout

    # ---- Container config ---------------------------------------------------

    def config_set(self, name: str, key: str, value: str) -> None:
        self._run_retrying_on_etag(["config", "set", name, key, value])

    def config_get(self, name: str, key: str) -> str | None:
        """Return the value of a container config key, or None if unset.

        Returns None when the key is unset (Incus prints an empty line on
        stdout) or when the command exits non-zero. Used by `jailbee git fetch` to
        read ``user.jailbee.branch`` persisted at container creation.
        """
        result = self._run(["config", "get", name, key], check=False)
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    def config_unset(self, name: str, key: str) -> None:
        """Remove a container config key. Idempotent — unsetting an absent
        key is a no-op (Incus prints `Error: Config option not found` to
        stderr and exits non-zero, which we swallow).
        """
        self._run(["config", "unset", name, key], check=False)

    def config_device_add(
        self,
        name: str,
        device_name: str,
        device_type: str,
        properties: dict[str, str],
    ) -> None:
        args = ["config", "device", "add", name, device_name, device_type]
        for k, v in properties.items():
            args.append(f"{k}={v}")
        self._run_retrying_on_etag(args)

    def config_device_remove(self, name: str, device_name: str) -> None:
        self._run_retrying_on_etag(["config", "device", "remove", name, device_name])

    # ---- Snapshots ----------------------------------------------------------

    def snapshot_create(self, name: str, snap: str) -> None:
        self._run(["snapshot", "create", name, snap])

    def snapshot_restore(self, name: str, snap: str) -> None:
        self._run(["snapshot", "restore", name, snap])

    def snapshot_delete(self, name: str, snap: str) -> None:
        self._run(["snapshot", "delete", name, snap])

    def snapshot_list(self, name: str, *, timeout: int | None = None) -> list[dict[str, Any]]:
        result = self._run(["snapshot", "list", name, "--format", "json"], timeout=timeout)
        return json.loads(result.stdout) if result.stdout else []

    # ---- Networks / ACLs ----------------------------------------------------

    def network_acl_create(self, name: str) -> None:
        self._run(["network", "acl", "create", name])

    def network_acl_delete(self, name: str) -> None:
        self._run(["network", "acl", "delete", name])

    def network_acl_set_yaml(self, name: str, yaml_content: str) -> None:
        cmd = [self.binary, "network", "acl", "edit", name]
        if self.dry_run:
            return
        try:
            result = subprocess.run(
                cmd,
                input=yaml_content,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as e:
            raise _missing_binary_error(self.binary) from e
        if result.returncode != 0:
            raise IncusError(f"`incus network acl edit {name}` failed: {result.stderr.strip()}")

    def network_acl_exists(self, name: str) -> bool:
        result = self._run(["network", "acl", "list", "--format", "json"])
        acls = json.loads(result.stdout) if result.stdout else []
        return any(a["name"] == name for a in acls)

    def network_acl_show(self, name: str) -> str:
        """Return the ACL's current state as YAML (raw stdout)."""
        result = self._run(["network", "acl", "show", name])
        return result.stdout

    def network_get(self, name: str, key: str) -> str:
        """Get a single network config key. Returns empty string if unset."""
        result = self._run(["network", "get", name, key])
        return result.stdout.strip()

    def network_set(self, name: str, key: str, value: str) -> None:
        """Set a single network config key."""
        self._run(["network", "set", name, key, value])

    def network_exists(self, name: str) -> bool:
        """Return True if a managed network with this name exists."""
        result = self._run(["network", "list", "--format", "json"])
        nets = json.loads(result.stdout) if result.stdout else []
        return any(n["name"] == name for n in nets)

    def network_create(self, name: str, network_type: str = "bridge") -> None:
        """Create a managed Incus network. Caller ensures idempotency."""
        self._run(["network", "create", name, f"--type={network_type}"])

    def network_rename(self, old: str, new: str) -> None:
        """Rename a managed network.

        Incus refuses while the network is in use by any instance or profile,
        surfacing as `IncusError`; callers are expected to have checked.
        """
        self._run(["network", "rename", old, new])

    def network_delete(self, name: str) -> None:
        """Delete a managed network.

        Like `network_rename`, Incus refuses while the network is in use by
        any instance or profile — check `network_used_by` first if you need
        to report *what* is holding it rather than an `IncusError`.
        """
        self._run(["network", "delete", name])

    def network_used_by(self, name: str) -> list[str]:
        """Return the API paths of the objects referencing this network.

        Entries look like ``/1.0/instances/app-feat`` and
        ``/1.0/profiles/app-net-loose`` (possibly with a ``?project=`` query).
        An unknown network reports no users rather than raising, so callers
        can treat "gone" and "unused" alike.
        """
        result = self._run(["network", "list", "--format", "json"])
        nets = json.loads(result.stdout) if result.stdout else []
        for net in nets:
            if net["name"] == name:
                return [str(u) for u in net.get("used_by") or []]
        return []

    # ---- Images -------------------------------------------------------------

    def launch(
        self,
        image: str,
        container_name: str,
        *,
        config: dict[str, str] | None = None,
        network: str | None = None,
    ) -> None:
        """Launch a container from `image` named `container_name`.

        Optional `config` becomes one or more ``-c key=value`` flags, e.g.
        ``config={"security.nesting": "true"}`` is required for systemd-networkd
        to start: on Ubuntu 24.04+ hosts that set
        ``kernel.apparmor_restrict_unprivileged_userns=1``, the unprivileged
        user namespace systemd (>=256) needs for nested workloads and for
        services using ``DynamicUser=``/``PrivateUsers=`` is otherwise blocked.

        Optional `network` overrides the default profile's network (passed
        as ``--network <name>``). Used by `jailbee base build` to attach the
        build container to ``jailbee-loose`` — the default ``incusbr0`` bridge
        carries a strict egress ACL that would block ``archive.ubuntu.com``
        and other distro repos.
        """
        args = ["launch", image, container_name]
        if network:
            args += ["--network", network]
        if config:
            for key, value in config.items():
                args += ["-c", f"{key}={value}"]
        self._run(args)

    def publish(self, container_name: str, alias: str) -> None:
        self._run(["publish", container_name, "--alias", alias])

    def image_alias_rename(self, old_alias: str, new_alias: str) -> None:
        """Rename an image alias (e.g. archive `gisgro-base` as `gisgro-base-YYYY-MM-DD`)."""
        self._run(["image", "alias", "rename", old_alias, new_alias])

    def image_alias_delete(self, alias: str) -> None:
        """Delete an image alias (the underlying image stays unless it had no other refs)."""
        self._run(["image", "alias", "delete", alias])

    def list_images(self) -> list[dict[str, Any]]:
        """Return `incus image list --format json` parsed into a list of dicts."""
        result = self._run(["image", "list", "--format", "json"])
        images: list[dict[str, Any]] = json.loads(result.stdout) if result.stdout else []
        return images

    def list_storage_pools(self) -> list[dict[str, Any]]:
        """Return `incus storage list --format json` parsed into a list of dicts.

        Each entry carries at least ``name``, ``driver`` and ``config`` (whose
        ``source`` is the on-disk root for a ``dir`` pool). Used by disk-usage
        to measure containers on the pool's real path rather than the symlink
        farm under ``/var/lib/incus/containers``.
        """
        result = self._run(["storage", "list", "--format", "json"])
        pools: list[dict[str, Any]] = json.loads(result.stdout) if result.stdout else []
        return pools

    def image_delete(self, ref: str) -> None:
        """Delete an image by alias or fingerprint (removes the image and its
        aliases). Raises IncusError if the image is still in use by a container."""
        self._run(["image", "delete", ref])

    def image_exists(self, alias: str) -> bool:
        return any(
            any(a.get("name") == alias for a in img.get("aliases", []))
            for img in self.list_images()
        )
