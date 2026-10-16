"""The git identity a container commits with.

Resolved on the host from the repo's point of view (`git.config_get` in
`cfg.repo_root`, so repo-local config and `includeIf` rules apply) and written
into the container's XDG gitconfig. That file rather than `~/.gitconfig`: git
reads it first, so a `~/.gitconfig` the user mounts by hand still wins, and it
covers submodules and mount-mode containers without touching the host repo's
own `.git/config`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee import git
from jailbee.config.common import CONTAINER_USERNAME
from jailbee.incus import IncusError
from jailbee.tui import warn_plain

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

IDENTITY_KEYS: tuple[str, ...] = ("user.name", "user.email")
SIGNING_KEYS: tuple[str, ...] = ("user.signingkey", "commit.gpgsign", "tag.gpgsign", "gpg.format")

_CONTAINER_HOME = f"/home/{CONTAINER_USERNAME}"
CONTAINER_GITCONFIG = f"{_CONTAINER_HOME}/.config/git/config"

# argv: FILE, then `set KEY VALUE` / `unset KEY` groups. Values arrive as
# positional parameters and are never part of the script text. `git config
# --unset-all` exits 5 when the key is absent, which is the goal anyway.
_WRITE_SCRIPT = """\
f=$1; shift
mkdir -p "${f%/*}" || exit 1
while [ $# -gt 0 ]; do
  case $1 in
    set) git config --file "$f" "$2" "$3" || exit 1; shift 3 ;;
    unset) git config --file "$f" --unset-all "$2"; rc=$?
      [ "$rc" -eq 0 ] || [ "$rc" -eq 5 ] || exit "$rc"; shift 2 ;;
    *) exit 2 ;;
  esac
done
"""


def resolve(cfg: Config) -> dict[str, str | None]:
    """Every key in IDENTITY_KEYS + SIGNING_KEYS, None where nothing applies.

    `git.user` fields win over the host value one at a time. Signing keys are
    only read when `gpg.enabled`: without the agent a signed commit fails.
    """
    overrides = {"user.name": cfg.git.user.name, "user.email": cfg.git.user.email}
    values: dict[str, str | None] = {}
    for key in IDENTITY_KEYS:
        values[key] = overrides[key] or git.config_get(cfg.repo_root, key)
    for key in SIGNING_KEYS:
        values[key] = git.config_get(cfg.repo_root, key) if cfg.gpg.enabled else None
    return values


def write(cfg: Config, incus: Incus, name: str, values: dict[str, str | None]) -> None:
    """Set each non-None value in the container's XDG gitconfig, unset the rest.

    One exec. Raises IncusError when it fails.
    """
    args: list[str] = []
    for key, value in values.items():
        args += ["set", key, value] if value is not None else ["unset", key]
    incus.exec(
        name,
        ["sh", "-c", _WRITE_SCRIPT, "sh", CONTAINER_GITCONFIG, *args],
        uid=cfg.container_user.uid,
        gid=cfg.container_user.gid,
        env={"HOME": _CONTAINER_HOME, "USER": CONTAINER_USERNAME, "LOGNAME": CONTAINER_USERNAME},
    )


def _ssh_key_file(values: dict[str, str | None]) -> bool:
    """SSH signing pointed at a key *file*, which exists on the host only.

    A literal key (`key::…` or `ssh-ed25519 AAAA…`) signs through the agent
    `gpg.enabled` forwards; a path would make every signed commit fail.
    """
    key = values.get("user.signingkey")
    if values.get("gpg.format") != "ssh" or key is None:
        return False
    return not key.startswith(("key::", "ssh-", "ecdsa-", "sk-"))


def _covering_mount(cfg: Config) -> str | None:
    """Container path of a host mount the XDG file would be written through."""
    for mount in cfg.effective_host_mounts():
        path = str(mount.container).rstrip("/")
        if CONTAINER_GITCONFIG == path or CONTAINER_GITCONFIG.startswith(path + "/"):
            return path
    return None


def sync(cfg: Config, incus: Incus, name: str) -> None:
    """Resolve and write the identity. Best-effort: warns, never raises."""
    if cfg.git.identity == "off":
        return
    covered = _covering_mount(cfg)
    if covered is not None:
        # Writing would edit the host's own git config through the bind.
        warn_plain(
            f"Not setting the git identity in {name}: {covered} is a host mount, so "
            f"{CONTAINER_GITCONFIG} is a host file.",
            stderr=True,
        )
        return
    values = resolve(cfg)
    missing = [key for key in IDENTITY_KEYS if values[key] is None]
    if missing:
        warn_plain(
            f"No git {' / '.join(missing)} for {name}: commits there will fail. Set it with "
            f"`git config {missing[0]} …` in {cfg.repo_root}, or under `git.user` in the "
            "host-local config (`jailbee config edit --local`).",
            stderr=True,
        )
    if _ssh_key_file(values):
        warn_plain(
            f"Not copying commit signing to {name}: gpg.format=ssh with a key file "
            f"({values['user.signingkey']}) that only exists on the host. Use a literal "
            "`key::…` signing key to sign through the forwarded agent.",
            stderr=True,
        )
        values.update(dict.fromkeys(SIGNING_KEYS))
    try:
        write(cfg, incus, name, values)
    except (IncusError, OSError) as e:
        warn_plain(f"Could not set the container's git identity: {e}", stderr=True)
