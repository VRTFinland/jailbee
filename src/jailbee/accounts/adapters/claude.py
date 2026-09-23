"""Claude Code's account adapter.

`engine.py` reaches Claude only through `ClaudeAdapter`, and what that object
answers with lives here: where a login is recorded, how an account is named,
which siblings of a credential belong to the machine rather than to the
account, and what has to be written beside a credential the engine just moved.
This is the only home of those names: every direct importer reaches them
from here.
"""

from __future__ import annotations

import json
import logging
import shutil
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from jailbee.accounts import engine
from jailbee.accounts.adapters import base
from jailbee.accounts.models import Identity, LiveAccount, slug_for
from jailbee.claude_locks import ClaudeLockTimeoutError, config_lock
from jailbee.config import CONTAINER_USERNAME, ConfigError
from jailbee.tui import choose_shared_credential, success

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from jailbee.accounts.models import Member, Slot
    from jailbee.config import Config
    from jailbee.incus import Incus

log = logging.getLogger(__name__)


CREDENTIAL_FILE = ".credentials.json"
"""The filename Claude Code reads a login from, inside whichever holder it uses."""


CLAUDE_SECURESTORAGE_ENV = "CLAUDE_SECURESTORAGE_CONFIG_DIR"
"""The environment variable Claude Code reads a shared credential from.

Every reader and writer imports this one name — the profile render, its
empty-value guard, and the `jailbee new` repair — so a rename cannot split
`ClaudeAdapter.wiring` from the profile that mirrors it (carryover item 3).
The `environment.` prefix a profile key needs is added by the caller.
"""


def identity_file(home: Path) -> Path:
    """The config file carrying `oauthAccount`, mirroring Claude Code's own
    resolution: the legacy `.config.json` when it exists, else `.claude.json`.
    """
    legacy = home / ".config.json"
    return legacy if legacy.exists() else home / ".claude.json"


def read_account_record(home: Path) -> dict[str, Any] | None:
    """Claude Code's own `oauthAccount` block, or None when there is none.

    Every failure — absent, unreadable, torn, or missing the block — is None.
    Callers treat an unidentified account as a fact to report, not an error:
    a fresh group has no identity anywhere until something has run.

    `UnicodeDecodeError` is in the caught set because it is a `ValueError`, not
    an `OSError`: a write torn mid-character makes `read_text` raise it, and
    that is the same "unreadable file" fact as a torn JSON document.

    Returns the block verbatim, because `Identity` is a lossy reading of it:
    it keeps the email and the organization UUID and drops the rest, and
    `slug_for` truncates the UUID further. Restoring a record after a switch
    has to put back what Claude Code wrote, not what jailbee understood.
    """
    try:
        data = json.loads(identity_file(home).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("oauthAccount")
    return block if isinstance(block, dict) else None


def identity_of(record: dict[str, Any]) -> Identity | None:
    """The `Identity` an `oauthAccount` block names, or None when it names none."""
    email = record.get("emailAddress")
    if not isinstance(email, str) or not email:
        return None
    org = record.get("organizationUuid")
    return Identity(email=email, org_uuid=org if isinstance(org, str) and org else None)


def read_identity(home: Path) -> Identity | None:
    """The account a config home names, or None when it names none."""
    record = read_account_record(home)
    return None if record is None else identity_of(record)


SHARED_CREDENTIAL_KEYS = frozenset(
    {"mcpOAuth", "mcpOAuthClientConfig", "mcpXaaIdp", "mcpXaaIdpConfig", "pluginSecrets"}
)
"""Siblings of `claudeAiOauth` that belong to the machine, not to an account.

They hold OAuth integrations that rotate independently of any login, so on
activation the live copy is authoritative. The list is cswap's
(claude-swap, MIT) `SHARED_CREDENTIAL_KEYS`.
"""

ACCOUNT_CREDENTIAL_KEYS = frozenset({"claudeAiOauth", "trustedDeviceToken"})
"""Account-scoped siblings we know about, named so the probe below does not
flag them. `trustedDeviceToken` is enrolled per (device, account) at login."""

CLAUDE_CREDS_DIRNAME = ".claude-creds"
"""Container-side directory name for a shared Claude credential.

Deliberately not `.claude`: only the credential is shared, and the config home
stays per-repo. Claude Code resolves `.credentials.json` *and*
`.oauth_refresh.lock` from `CLAUDE_SECURESTORAGE_CONFIG_DIR`, so the rotation
lock travels with the credential into this directory — which is what keeps
containers of different repos mutually excluded.
"""

CLAUDE_CREDS_DEVICE = "claude-creds"
"""Name of the `<prefix>-binds` disk device that mounts the shared credential
directory. Its presence on that profile is what `init_command`'s `jailbee
new` repair checks before writing the env key — see
`ensure_claude_credentials_env`.
"""

ACCOUNT_RECORD_KEY = "jailbeeAccount"
"""Where a parked file keeps the `oauthAccount` block of the login it holds.

**Not a ledger.** The identity of a live credential is Claude Code's to record,
in a config home's `oauthAccount` — and `switch` has to invalidate that record,
or every member repo would go on naming the previous account. That leaves a
window in which no file on disk says which account the live credential belongs
to, and a `park` landing in it can only name the file `unknown-<timestamp>`,
losing the one record of what the file contains.

So the record travels *with the grant*: parking copies Claude Code's own
`oauthAccount` into the parked file, and activating writes it back. Because it
lives inside the file whose grant it describes, it cannot be orphaned: moving or
deleting the file moves or deletes the record with it, so `store_dir()` stays
the only state and there is no manifest to repair.

It *can*, however, disagree with the one other place the account is named — the
filename — because a file renamed by hand keeps the record it was written with.
The filename wins: `trusted_record_in` restores a record only while it derives
the slot's own name, and a mismatch degrades to the pre-record behaviour rather
than writing one account's identity under another's name.

Never written to a *live* credential: that file is Claude Code's, and it stays
the shape Claude Code wrote. `compose_credential` strips this key on the way
out. A parked file without it — a login jailbee never parked, or one parked
before this key existed — falls back to invalidating the record, which is what
every switch did before.
"""


def _credential_object(raw: str | None) -> dict[str, Any] | None:
    """Parse a credential file's text, or None when it is not a JSON object.

    A managed `sk-ant-…` API key and any opaque legacy shape land here as
    None, which every caller treats as "activate verbatim".
    """
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def shared_fields(raw: str | None) -> dict[str, Any] | None:
    """The machine-shared fields of a live credential.

    A dict — including `{}` — is authoritative for every allowlisted key: one
    absent here is absent from the machine's current state and must not be
    resurrected from a slot's snapshot. None means there was no JSON credential
    object to read.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    if "claudeAiOauth" in data:
        # An unknown sibling defaults to slot-owned, which fails safe but
        # silently: if Claude Code grows a new *shared* key, that default
        # quietly reintroduces the stale-restore papercut for it. Leave a
        # trace so it gets noticed.
        unrecognized = (
            data.keys() - SHARED_CREDENTIAL_KEYS - ACCOUNT_CREDENTIAL_KEYS - {ACCOUNT_RECORD_KEY}
        )
        if unrecognized:
            log.debug(
                "credential has sibling keys jailbee does not recognize "
                "(a newer Claude Code?), treating them as account-owned: %s",
                sorted(unrecognized),
            )
    return {key: data[key] for key in SHARED_CREDENTIAL_KEYS if key in data}


def compose_credential(target_raw: str, live_shared: dict[str, Any] | None) -> str:
    """The credential to activate, composed from its two owners.

    Shared keys come from `live_shared`; everything else comes from the slot.
    A target that is not a JSON object carrying a login activates unchanged, as
    does any target when there is nothing live to take shared fields from.

    **`live_shared` is filtered, not trusted.** Its only producer,
    `shared_fields`, already returns nothing else — but this is a public
    function, and a caller that passed a whole credential through would write
    the live account's `claudeAiOauth` into the target's file: one account's
    login stored under another's identity, and two files for one lineage. The
    allowlist costs a comprehension and closes that direction for good.

    `ACCOUNT_RECORD_KEY` is stripped on every path that produces a composed
    object, `live_shared is None` included: jailbee's own bookkeeping must not
    reach a file Claude Code reads.
    """
    target = _credential_object(target_raw)
    if target is None or "claudeAiOauth" not in target:
        # Nothing to strip from a blob that is not a credential object, and no
        # shared fields to compose into one.
        return target_raw
    composed = {
        k: v
        for k, v in target.items()
        if k != ACCOUNT_RECORD_KEY and (live_shared is None or k not in SHARED_CREDENTIAL_KEYS)
    }
    if live_shared is not None:
        composed.update({k: v for k, v in live_shared.items() if k in SHARED_CREDENTIAL_KEYS})
    return json.dumps(composed)


def _record_in(raw: str) -> dict[str, Any] | None:
    """The account record a slot's blob carries, or None when it carries none.

    None for every shape that is not a credential object with a dict under
    `ACCOUNT_RECORD_KEY` — a login that entered through `/login`, or one parked
    before the key existed. Callers fall back to invalidating the members'
    record, which is what every switch did before.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    record = data.get(ACCOUNT_RECORD_KEY)
    return record if isinstance(record, dict) else None


def trusted_record_in(slot: Slot, raw: str) -> dict[str, Any] | None:
    """The slot's account record, but only while it agrees with the slot's name.

    **The filename stays authoritative.** Keeping the record beside the grant
    means the account is named twice for one file — in the name and in the
    record — and two records of one fact can differ. A slot renamed by hand,
    which is how `doctor`'s recovery advice works, changes the name and not the
    record; restoring that record would write one account into the members'
    config while the user believed they activated the other, silently.

    So a mismatch is treated as no record at all: the members' recorded account
    is invalidated instead, and Claude Code repopulates it from the credential
    now live. That is the pre-record behaviour — correct, only slower to
    display — so a disagreement costs an optimisation rather than causing a
    wrong write.

    Compared on the *derived* name, so a `~<disambiguator>` is not a mismatch:
    it separates two grants of one account, which by definition share an
    identity.
    """
    record = _record_in(raw)
    if record is None:
        return None
    identity = identity_of(record)
    if identity is None or slug_for(identity) != slot._derived:
        log.debug(
            "slot %s carries a record for a different account; invalidating instead",
            slot.name,
        )
        return None
    return record


def _member_account(
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account a member repo's config home says the holder holds.

    Read from a config home, never from the credential. `authoritative`
    names the members whose config home can be trusted to describe *this
    holder's* login: a repo whose containers span two credential groups
    shares one `~/.claude` between them, so its `oauthAccount` names
    whichever account ran most recently, and naming a parked file from it
    would store one account's grant under another's name. See
    `accounts.groups.authoritative_prefixes`, which is its only producer.

    The calling repo is consulted first among the authoritative ones; any
    of them will do, since they share one login. None means no
    authoritative member names an account — a fresh group, the window a
    `switch` opens before any container has run Claude again (which is what
    `ACCOUNT_RECORD_KEY` exists to close), or a holder no repo resolves to.
    Callers want `live_account`, which falls back to the holder's own note.
    """
    usable = [m for m in found if m.container_prefix in authoritative]
    ordered = sorted(usable, key=lambda m: m.container_prefix != prefer)
    for member in ordered:
        record = read_account_record(member.config_home)
        if record is None:
            continue
        identity = identity_of(record)
        if identity is not None:
            return LiveAccount(identity=identity, record=record)
    return None


def live_session_prefixes(found: Sequence[Member]) -> list[str]:
    """Members that look like they have a Claude Code session running.

    Claude Code writes `<config home>/sessions/<pid>.json` per session, which
    is why this lives here and not in the engine: the layout is Claude's, and
    another agent records a running session somewhere else or not at all. The
    engine only ever asks `AccountAdapter.sessions`.

    The PIDs belong to container namespaces the host cannot check, so a
    leftover file reads as live — this is a warning input, never a refusal.
    """
    busy: list[str] = []
    for member in found:
        try:
            if any((member.config_home / "sessions").glob("*.json")):
                busy.append(member.container_prefix)
        except OSError:
            continue
    return sorted(busy)


def _login_block(raw: str | None) -> dict[str, Any] | None:
    """The `claudeAiOauth` block of credential *text* — `login_of` for a path.

    One definition of "the login inside a credential", so the fingerprint a
    note is written with and the one it is checked against cannot be read out
    of two differently-shaped dicts.

    **This is what `ClaudeAdapter.grant_block` answers with** (~340 lines
    below), and through it every lineage comparison the engine makes:
    `engine.login_of`, `engine._same_grant`, `engine.holds_same_login` and
    `engine.grant_fingerprint` all reach Claude's credential shape here and
    nowhere else. The engine's knowledge of `claudeAiOauth` is this function.
    """
    data = _credential_object(raw)
    if data is None:
        return None
    block = data.get("claudeAiOauth")
    return block if isinstance(block, dict) else None


ACCOUNT_NOTE_FILE = ".jailbee-account.json"
"""Where a holder keeps the account of the login *jailbee* put there.

**Why a second place at all.** Every other identity source is a config home,
and a config home belongs to a *repo*, not to a holder: one `~/.claude` is
shared by every container of the repo whatever group each reads, so it can name
only one account while such a repo has two live logins. For a group no repo
resolves to — the one `jailbee account use -g` exists to fill — there is no
config home to read at all, and a `park` of a login jailbee had itself just
activated could only name the file `unknown-<timestamp>`, losing the one record
of what it contains (`ACCOUNT_RECORD_KEY` documents the same loss for the other
window it closes).

**Why it cannot go stale into a wrong name.** The note carries a fingerprint of
the grant it describes — a digest of `claudeAiOauth.refreshToken`, the same
refresh-token lineage `_same_grant` compares — and `note_account` returns
nothing unless it still matches the credential beside it. A `/login` in a
container mints a new lineage, so a note left over from the previous account
stops being read the moment it stops being true. The digest, never the token:
this file names an account, and a second copy of a secret is exactly what this
module refuses to make elsewhere.

**Trust.** A group holder is mounted into its containers, so a container can
write this file. That is the same trust level as the `.claude.json` every
identity read already comes from, and the account it names goes through
`slug_for` like any other, so a forged note can misname a parked file and can
do nothing else — no new surface.

Not a manifest: it describes the one directory it lives in, so it moves and
dies with the holder, and nothing has to repair it.
"""


def account_note_path(holder: Path) -> Path:
    """Where `holder` keeps its account note."""
    return holder / ACCOUNT_NOTE_FILE


def write_account_note(holder: Path, record: dict[str, Any] | None, credential_raw: str) -> None:
    """Note that `credential_raw` — now the login in `holder` — is `record`'s.

    Removes any existing note when there is nothing to say: no record (a slot
    parked before `ACCOUNT_RECORD_KEY` existed, or one whose record contradicts
    its name) or no fingerprintable grant. Leaving the previous account's note
    in place would be harmless — the fingerprint no longer matches — but a file
    that says something untrue about the directory it sits in is worth deleting
    rather than explaining.

    Best-effort, like `_stamp_account_record` and for the same reason: the
    credential is already in place by the time this runs, and a failure costs a
    future `park` its account name, never the login.
    """
    path = account_note_path(holder)
    fingerprint = engine.grant_fingerprint(CLAUDE, _login_block(credential_raw))
    if record is None or fingerprint is None:
        with suppress(OSError):
            path.unlink(missing_ok=True)
        return
    try:
        engine.atomic_write(path, json.dumps({"account": record, "grant": fingerprint}, indent=2))
    except OSError:
        log.debug("could not note the account of the login in %s", holder, exc_info=True)


def note_account_at(holder: Path) -> LiveAccount | None:
    """The account `holder`'s note names, while it still describes the grant.

    None for every other case: no note, an unreadable or malformed one, one
    whose fingerprint no longer matches the credential beside it, and one whose
    record names no account. A missing credential is a mismatch too, so the
    note a `park` failed to delete cannot name a holder's next login.
    """
    try:
        data = json.loads(account_note_path(holder).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    record = data.get("account")
    grant = data.get("grant")
    if not isinstance(record, dict) or not isinstance(grant, str):
        return None
    live = engine.login_of(CLAUDE, engine.credential_in(CLAUDE, holder))
    if grant != engine.grant_fingerprint(CLAUDE, live):
        return None
    identity = identity_of(record)
    return None if identity is None else LiveAccount(identity=identity, record=record)


def note_account(cfg: Config) -> LiveAccount | None:
    """The account this repo's holder notes, while it still describes the grant."""
    return note_account_at(engine.holder_dir(CLAUDE, cfg))


def account_of(
    holder: Path,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account `holder`'s live credential belongs to, with its record.

    Two sources, and the holder's own note comes first: it is the only one tied
    to the grant being named — `note_account_at` checks its fingerprint against
    the very credential a `park` is about to move — while a config home is tied
    to a repo and merely *usually* describes this holder (see
    `_member_account`). Where both speak they agree; where they disagree the
    note is the one that was verified.

    None means nothing on this host says which account the live credential
    holds. That is a fact to report, not an error: `park` then names the file
    `unknown_slot_name`, and `ls` shows `LIVE_UNIDENTIFIED`.
    """
    return note_account_at(holder) or _member_account(
        found, prefer=prefer, authoritative=authoritative
    )


def live_account(
    cfg: Config,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> LiveAccount | None:
    """The account this repo's holder holds, with its record.

    `account_of` for a caller holding a `Config` rather than a holder — the
    shape `doctor` and `live_identity` want. Not the protocol's entry point:
    that is `ClaudeAdapter.account_at`, which takes the holder directly, so a
    generic caller never has to synthesise a `Config` pointed at one.
    """
    return account_of(
        engine.holder_dir(CLAUDE, cfg), found, prefer=prefer, authoritative=authoritative
    )


def live_identity(
    cfg: Config,
    found: Sequence[Member],
    *,
    prefer: str,
    authoritative: Collection[str],
) -> Identity | None:
    """The identity half of `live_account`, for callers that only display it."""
    account = live_account(cfg, found, prefer=prefer, authoritative=authoritative)
    return None if account is None else account.identity


def invalidate_identity(home: Path) -> bool:
    """Delete `oauthAccount` so Claude Code repopulates it from the credential.

    Returns whether the config home is now consistent — True also when there
    was nothing to delete. False means the file exists but could not be read
    or written; it is **never** overwritten in that case, because a torn
    `.claude.json` still holds the user's projects and MCP servers and an
    `or {}` here would erase them.

    `UnicodeDecodeError` is caught alongside the rest for the reason
    `read_identity` gives, and matters more here: this runs from inside
    `switch` *after* the credential files have moved, so an escaping exception
    would report a failed switch that had already landed.
    """
    path = identity_file(home)
    try:
        with config_lock(home):
            if not path.exists():
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            if "oauthAccount" not in data:
                return True
            del data["oauthAccount"]
            engine.atomic_write(path, json.dumps(data, indent=2))
    except (ClaudeLockTimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


ONBOARDING_KEYS = ("hasCompletedOnboarding", "oauthAccount")
"""The keys whose presence means Claude Code has already run in a config home.

Either one is enough. `hasCompletedOnboarding` is what Claude Code's own
first-run wizard is gated on, and `oauthAccount` is written the moment a login
lands — a config home carrying either is the user's state, never a seed, and
`mark_onboarded` refuses to touch it.
"""


def mark_onboarded(home: Path, *, repo_dir: str) -> bool:
    """Record in `home` that the first-run wizard need not run, and why it can.

    Claude Code's wizard is gated on `hasCompletedOnboarding` alone: it asks
    for a login even when a valid credential is mounted at
    `CLAUDE_SECURESTORAGE_CONFIG_DIR`, because it never looks. A fresh config
    home therefore sends the user through `/login` for an account they are
    already logged into — the whole cost of a new container in a repo whose
    credential group holds a login. Deciding *whether* that credential exists
    is the caller's job (`init_command._seed_claude_json`); this function only
    writes.

    `repo_dir` is the repo's path **inside the container**, where Claude Code
    resolves it; keyed by the host path the trust dialog would still be there
    to answer. Trust is seeded with the same flag as the onboarding skip
    because the two are one decision: the container is the isolation boundary
    the dialog asks about, and one config home is shared by every container of
    the repo, so the first manual accept already covers the rest.

    Returns whether the config home now carries the state. False means the
    file was there but is not a seed — Claude Code has run here, and the
    contract of `invalidate_identity` applies: a file holding the user's
    projects and MCP servers is **never** overwritten. False also covers an
    unreadable file, a lost lock, and a torn one; the caller falls back to the
    `{}` seed it has always written.
    """
    path = identity_file(home)
    try:
        with config_lock(home):
            data: dict[str, Any] = {}
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return False
                if any(key in data for key in ONBOARDING_KEYS):
                    return False
            projects = data.get("projects")
            projects = projects if isinstance(projects, dict) else {}
            entry = projects.get(repo_dir)
            entry = entry if isinstance(entry, dict) else {}
            data["projects"] = {**projects, repo_dir: {**entry, "hasTrustDialogAccepted": True}}
            data["hasCompletedOnboarding"] = True
            engine.atomic_write(path, json.dumps(data, indent=2))
    except (ClaudeLockTimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def restore_identity(home: Path, record: dict[str, Any]) -> bool:
    """Write `record` back as this config home's `oauthAccount`.

    The counterpart to `invalidate_identity`, with the same contract: True when
    the config home is consistent afterwards, False when the file exists but
    could not be read or written — and in that case it is **never** overwritten,
    because a torn `.claude.json` still holds the user's projects and MCP
    servers.

    An absent file is True and left absent, exactly as in `invalidate_identity`:
    creating Claude Code's config here would be a write nobody asked for, and
    Claude Code writes the account itself from the credential it finds.
    """
    path = identity_file(home)
    try:
        with config_lock(home):
            if not path.exists():
                return True
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            if data.get("oauthAccount") == record:
                return True
            data["oauthAccount"] = record
            engine.atomic_write(path, json.dumps(data, indent=2))
    except (ClaudeLockTimeoutError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return True


def _rewrite_identities(
    found: Sequence[Member],
    unreachable: Sequence[str],
    record: dict[str, Any] | None,
    authoritative: Collection[str],
) -> tuple[list[str], list[str]]:
    """Point every member's recorded account at the login now live.

    With `record`, that means writing it: the activated slot carried Claude
    Code's own block (see `ACCOUNT_RECORD_KEY`), so the members can be made
    correct immediately instead of merely not-wrong. Without one — a login
    jailbee never parked, or one parked before the key existed — the record is
    deleted and Claude Code repopulates it on its next run, which is what every
    switch did before.

    **A record is written only into an authoritative member**, for the same
    reason `live_account` reads only those: a repo whose containers span two
    groups shares one config home between them, so stamping this group's
    account into it would make that home name the wrong login for the other
    group's containers — and a later `park` of *that* group would then park it
    under the wrong name, name and record agreeing. Every other member is
    cleared instead, which is always safe: Claude Code repopulates the block
    from whichever credential the container actually reads.

    Reports which members took the change, so the caller can name the ones that
    are still naming the previous account.
    """
    done: list[str] = []
    failed: list[str] = list(unreachable)
    for member in found:
        trusted = member.container_prefix in authoritative
        ok = (
            restore_identity(member.config_home, record)
            if record is not None and trusted
            else invalidate_identity(member.config_home)
        )
        (done if ok else failed).append(member.container_prefix)
    return sorted(done), sorted(failed)


def _stamp_account_record(path: Path, record: dict[str, Any] | None) -> None:
    """Keep `record` inside the newly parked file, best-effort.

    Best-effort on purpose: the login is already safely in the store by the
    time this runs, and a failure here costs a future `park` its account name —
    the state this whole mechanism improves on, never worse than it. Raising
    would report a failed park that had in fact landed, which is the one
    outcome worth avoiding.
    """
    if record is None:
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return
        data[ACCOUNT_RECORD_KEY] = record
        engine.atomic_write(path, json.dumps(data))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.debug("could not record the account of the login parked at %s", path, exc_info=True)


def _creds_env_key() -> str:
    """The profile/config key that names Claude Code's credential directory."""
    return f"environment.{CLAUDE_SECURESTORAGE_ENV}"


def _creds_mount_path() -> str:
    """Where a shared credential directory mounts inside the container."""
    return f"/home/{CONTAINER_USERNAME}/{CLAUDE_CREDS_DIRNAME}"


def _config_home_path() -> str:
    """Claude Code's own config home, the explicit no-group override target."""
    return f"/home/{CONTAINER_USERNAME}/.claude"


def _local_creds_device(incus: Incus, container: str) -> dict[str, str] | None:
    """The container's own instance-local `claude-creds` device, or None.

    Reads `devices` (instance-local), not `expanded_devices` (profile-merged)
    — the question is whether a local override already shadows the profile,
    exactly as `egress_scope._local_eth0` asks for `eth0`. Needed because
    `config_device_override` fails once a local device already exists
    (`incus.py:504`), which a second `set_container_group` call on the same
    container — the feature's whole point — would otherwise hit.
    """
    for raw in incus.list_containers():
        if raw.get("name") == container:
            devices = raw.get("devices") or {}
            device = devices.get(CLAUDE_CREDS_DEVICE)
            return dict(device) if device else None
    return None


class ClaudeAdapter:
    name = "claude"
    credential_file = CREDENTIAL_FILE
    refresh_token_key = "refreshToken"
    live_switch = True

    def config_home(self, cfg: Config) -> Path:
        """This repo's Claude config home on the host — never shared."""
        assert cfg.shared_dir is not None  # set by load_config
        return cfg.shared_dir / "claude"

    def holder_override(self, cfg: Config) -> Path | None:
        # Falsy, not `is None`: an empty group name would resolve the holder to
        # the credential *root* — the parent of `_parked` and of every group.
        if not cfg.credential_group:
            return None
        return engine.group_dir(self.name, cfg.credential_group)

    def grant_block(self, raw: str | None) -> dict[str, Any] | None:
        return _login_block(raw)

    def compose(self, target_raw: str, live_raw: str | None) -> str:
        return compose_credential(target_raw, shared_fields(live_raw))

    def locks(self, holder: Path) -> AbstractContextManager[None]:
        from jailbee.claude_locks import credential_locks

        return credential_locks(holder)

    def account_at(
        self,
        holder: Path,
        found: Sequence[Member],
        *,
        prefer: str,
        authoritative: Collection[str],
    ) -> LiveAccount | None:
        return account_of(holder, found, prefer=prefer, authoritative=authoritative)

    def record_for(self, slot: Slot, raw: str) -> dict[str, Any] | None:
        return trusted_record_in(slot, raw)

    def on_park(self, cfg: Config, holder: Path, parked: Path, account: LiveAccount | None) -> None:
        """Stamp the account into the parked file and retire the holder's note.

        Both belong to the grant that just left: `_stamp_account_record` keeps
        Claude Code's own `oauthAccount` inside the file (see
        `ACCOUNT_RECORD_KEY`), and the note describes a credential this holder
        no longer has.
        """
        _stamp_account_record(parked, None if account is None else account.record)
        with suppress(OSError):
            account_note_path(holder).unlink(missing_ok=True)

    def on_activate(self, holder: Path, record: dict[str, Any] | None, credential_raw: str) -> None:
        write_account_note(holder, record, credential_raw)

    def on_switch(
        self,
        found: Sequence[Member],
        unreachable: Sequence[str],
        record: dict[str, Any] | None,
        authoritative: Collection[str],
    ) -> tuple[list[str], list[str]]:
        return _rewrite_identities(found, unreachable, record, authoritative)

    def sessions(self, found: Sequence[Member]) -> list[str]:
        return live_session_prefixes(found)

    def blockers(self, cfg: Config, incus: Incus, containers: Sequence[str]) -> list[str]:
        """Claude never blocks a switch: it re-reads the credential itself."""
        return []

    def wiring(self, cfg: Config, group_dir: Path | None) -> base.Wiring:
        """The device and env a container needs to read a shared credential.

        An *empty* `CLAUDE_SECURESTORAGE_CONFIG_DIR` is not the same as an
        unset one — Claude Code falls back to `~/.claude` for it, silently
        sending credential writes into the config home mount — so an empty
        value is omitted entirely rather than written.
        """
        if not cfg.claude.enabled or group_dir is None:
            return base.Wiring()
        home = f"/home/{CONTAINER_USERNAME}"
        value = cfg.container.env.get(CLAUDE_SECURESTORAGE_ENV, f"{home}/{CLAUDE_CREDS_DIRNAME}")
        env = {CLAUDE_SECURESTORAGE_ENV: value} if value else {}
        return base.Wiring(
            devices={
                CLAUDE_CREDS_DEVICE: {
                    "type": "disk",
                    "source": str(group_dir),
                    "path": f"{home}/{CLAUDE_CREDS_DIRNAME}",
                }
            },
            env=env,
        )

    def prepare_config_home(self, cfg: Config, home: Path) -> None:
        """Make `home` hold this repo's login, before a container reads it.

        Runs on both `jailbee init` and `jailbee apply` via
        `base.prepare_config_homes`, for the same reason as the shared-mount
        creation: the binds profile names the holder directory as a disk
        source, and Incus rejects every `profile edit`/`profile assign` when a
        source path is missing.

        Four cases, and the two-credential one is the interesting one:

        * holder empty, repo has a credential → **move** it in. A copy would
          give one refresh-token lineage two refreshers, and the first
          rotation silently logs one side out.
        * both hold one → **ask** (`choose_shared_credential`). Exactly one
          login can be shared and the other becomes unused, so the answer is
          the user's; the loser is deleted rather than kept, since nothing
          would ever read it again and a stale grant left in the shared tree
          only invites confusion. Cancelling — or having no TTY to ask on —
          raises the original `ConfigError`, which still names the
          `credentials.repos` opt-out for a user who wants neither shared
          login. Deleting a credential is safe here precisely because the two
          are *independent* grants: two `/login`s to one account each mint
          their own refresh-token lineage, so deleting one leaves the
          survivor's untouched. (Copying a credential blob to two places is
          the operation that logs one side out; deleting one of two grants is
          not.)
        * only the holder holds one → nothing to do; the mount does the rest.
        * neither → nothing to do; the first `/login` in any member lands here.

        A repo that shares no group has `home == holder`, so there is no
        reconciliation to do and this returns immediately.

        Mode 0700: unlike the rest of the shared tree this directory holds a
        live credential, and it lives outside every repo. The container's dev
        user is idmapped to the host user, so 0700 is still readable inside.

        No `.owner` stamp (see `init_command._ensure_shared_owner`): being
        shared by several repos is the entire point here.
        """
        holder = engine.holder_dir(self, cfg)
        if holder == home:
            return

        holder_cred = engine.credential_in(self, holder)
        repo_cred = engine.credential_in(self, home)

        if holder_cred.exists() and repo_cred.exists():
            keep = choose_shared_credential(holder, repo_cred, cfg.container_prefix)
            if keep is None:
                raise ConfigError(
                    f"{holder} already holds a credential, and so does this repo "
                    f"({repo_cred}). Sharing one account means one of the two logins "
                    f"becomes unused, and jailbee will not choose for you. Either "
                    f"delete this repo's copy to adopt the group's login, or point "
                    f"this repo at another group (or `null`) under "
                    f"`credentials.repos` in ~/.config/jailbee/global.yaml."
                )
            if keep == "group":
                repo_cred.unlink()
                success(f"Adopted the group's Claude login; deleted this repo's copy: {repo_cred}")
            else:
                holder_cred.unlink()
                success(f"Replaced the group's Claude login with this repo's: {holder}")

        holder.mkdir(parents=True, exist_ok=True)
        holder.chmod(0o700)

        if not holder_cred.exists() and repo_cred.exists():
            # shutil.move, not Path.rename: `shared_dir` can be overridden to
            # another filesystem, where rename fails with EXDEV.
            shutil.move(str(repo_cred), str(holder_cred))
            success(f"Moved this repo's Claude credential into the shared group dir: {holder}")

    def profile_has_group(self, cfg: Config) -> bool:
        """Whether `<prefix>-binds` carries Claude's shared-credential device.

        `profiles.py` renders it only when the repo itself resolves a group,
        and `config_device_override` fails when there is nothing to override
        (`incus.py:504`). Derived from the config rather than read back from
        Incus so it cannot disagree with what the next `jailbee apply` writes.
        """
        return cfg.claude.enabled and cfg.credential_group is not None

    def set_container_group(
        self,
        cfg: Config,
        incus: Incus,
        container: str,
        group_dir: Path | None,
    ) -> None:
        """Point `container` at `group_dir`'s credential, or back at `~/.claude`.

        `group_dir` is None for the explicit no-group override: remove the
        device and set secure storage to the config home. Writing the key is
        what distinguishes this from `clear_container_group` — the instance
        override outranks the profile, so it must carry the env it needs even
        when the profile has none.

        The env key is written **always**, not only when the repo has no group
        of its own: `ClaudeAdapter.wiring` returns nothing for a group-less
        repo, so the profile carries no such key, and if the repo's group is
        later removed the profile would drop the key out from under a
        still-overridden container.
        """
        if group_dir is None:
            incus.config_device_remove(container, CLAUDE_CREDS_DEVICE, missing_ok=True)
            incus.config_set(container, _creds_env_key(), _config_home_path())
            return

        source = str(group_dir)
        existing = _local_creds_device(incus, container)
        if existing is not None:
            # A local device already shadows the profile (this container has
            # been switched before) — update it in place, since
            # `config_device_override` only works the first time.
            incus.config_device_set(container, CLAUDE_CREDS_DEVICE, {"source": source})
        elif self.profile_has_group(cfg):
            incus.config_device_override(container, CLAUDE_CREDS_DEVICE, {"source": source})
        else:
            incus.config_device_add(
                container,
                CLAUDE_CREDS_DEVICE,
                "disk",
                {"source": source, "path": _creds_mount_path()},
            )
        incus.config_set(container, _creds_env_key(), _creds_mount_path())

    def clear_container_group(self, cfg: Config, incus: Incus, container: str) -> None:
        """Remove the device and unset the env key, restoring inheritance."""
        incus.config_device_remove(container, CLAUDE_CREDS_DEVICE, missing_ok=True)
        incus.config_unset(container, _creds_env_key())


CLAUDE = ClaudeAdapter()
base.register(CLAUDE)
