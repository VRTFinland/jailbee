"""Subprocess wrapper for the `cswap` (claude-swap) CLI.

This is the only module that runs the `cswap` binary. Everything else goes
through :class:`Cswap`, which makes the policy layer unit-testable with a
mocked ``subprocess.run`` — the same rule ``incus.py`` follows for ``incus``.

`cswap` is an **optional** host dependency. Without it every ``jailbee
claude`` command prints :data:`INSTALL_HINT` and exits, ``jailbee doctor``
reports the absence as information, and no existing jailbee path changes.

Environment
-----------
`cswap` resolves its target the way Claude Code does: the config home is
``CLAUDE_CONFIG_DIR`` (falling back to ``~/.claude``), the global config is
``<config-home>/.claude.json`` and the credential is
``<config-home>/.credentials.json`` (``claude_swap/paths.py``). Pointing that
one variable at ``<shared_dir>/claude`` is therefore enough to make `cswap`
operate on *this repo's* Claude login.

``HOME`` is deliberately left alone, so cswap's own account store resolves
normally to ``${XDG_DATA_HOME:-~/.local/share}/claude-swap`` — one host-global
pool shared with the user's personal cswap, which is the point.

``CLAUDE_SECURESTORAGE_CONFIG_DIR`` is *removed*: when it is defined,
``switcher._read_capture_credentials`` sources the credential from it instead
of from ``CLAUDE_CONFIG_DIR``, so a user who exports it would have
``jailbee claude add`` capture one profile's email against another profile's
token. jailbee asserts exactly one profile, so the override goes.

Run modes
---------
The ``--json`` reads are captured (stdin ``DEVNULL``). ``add`` and ``remove``
are **passthrough**: ``switcher.remove_account`` prompts ``[y/N]`` and
``add_account`` prompts when ``--slot`` names an occupied slot, and neither has
a ``--yes`` flag, so capturing their stdout would strand the prompt and die on
``EOFError``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jailbee.config import Config

CSWAP_BINARY = "cswap"

INSTALL_HINT = (
    "`cswap` (claude-swap) is not installed. It is an optional host "
    "dependency that jailbee drives for account switching:\n"
    "  uv tool install claude-swap\n"
    "Then log in to Claude Code in a container and run `jailbee claude add`."
)

# The `--json` reads fetch each account's quota from Anthropic's usage API,
# refreshing an expired token for parked accounts on the way. That is a
# network round-trip per account, so the budget is generous — a slow answer
# beats a spurious failure.
_JSON_TIMEOUT = 120


class CswapError(Exception):
    """Raised when a `cswap` invocation fails or answers unparseably."""


class CswapMissingError(CswapError):
    """The `cswap` binary is not on PATH.

    A subclass so that every ``except CswapError`` keeps working, while the
    CLI can tell "not installed" (print the install hint, exit 1) from "it
    ran and failed" (report what it said).
    """


class CswapTimeoutError(CswapError):
    """A `cswap` invocation exceeded its timeout."""


@dataclass(frozen=True)
class Account:
    """One account in the cswap pool, as ``cswap list --json`` reports it.

    Carries no credential. ``usage_status`` is cswap's own vocabulary — ``ok``,
    ``token_expired``, ``api_key``, ``keychain_unavailable``,
    ``relogin_required``, ``foreign_credential``, ``no_credentials``,
    ``unavailable`` — passed through rather than re-interpreted, so a status
    cswap adds later renders instead of being swallowed.
    """

    number: int
    email: str
    org_uuid: str
    org_name: str
    alias: str
    active: bool
    disabled: bool
    usage_status: str
    five_hour_pct: float | None
    seven_day_pct: float | None

    @property
    def identity(self) -> tuple[str, str]:
        """The stable key: ``(email, organizationUuid)``.

        Slots move (`cswap move`) and aliases change (`cswap alias`); this
        pair is how cswap itself identifies an account
        (``switcher._find_account_slot``), so it is what jailbee's ledger
        stores.
        """
        return (self.email, self.org_uuid)

    @property
    def label(self) -> str:
        """What to call this account in a message: its alias, else its email."""
        return self.alias or self.email


@dataclass(frozen=True)
class LiveAccount:
    """The live Claude login of one config home, per ``cswap status --json``.

    Three states, and telling them apart is what the "unsaved live login"
    refusal rests on:

    * ``email is None`` — no live login at all (a fresh shared dir).
    * ``email`` set, ``managed`` False — a login cswap does not hold. Switching
      away replaces it; cswap stashes it first, but recovering a stash is
      manual, so jailbee refuses and offers `jailbee claude add`.
    * ``managed`` True — the live login is a pooled account, and ``number``
      says which slot.
    """

    email: str | None
    managed: bool
    number: int | None
    org_uuid: str

    @property
    def identity(self) -> tuple[str, str] | None:
        """``(email, org_uuid)`` when the live login is pooled, else None.

        An unpooled login has an email but no pool identity — there is no row
        it could match, and returning one would let a caller look up a
        holding that cannot exist.
        """
        if self.email is None or not self.managed:
            return None
        return (self.email, self.org_uuid)


@dataclass(frozen=True)
class SwitchResult:
    """What ``cswap switch --json`` reports about the account it landed on.

    ``message`` is cswap's own line, with any warnings appended. ``email`` and
    ``number`` come from the payload's ``to`` block and are None when cswap
    did not report one.

    The landed identity is returned rather than discarded because the ledger's
    central claim — "this repo now holds *that* account" — is otherwise written
    from the identity jailbee resolved *before* the switch. A slot renumbered
    between the listing and the switch (`cswap move`) would make that claim
    silently false, so the policy layer verifies it instead of assuming it.
    """

    message: str
    email: str | None
    number: int | None


def config_home(cfg: Config) -> Path:
    """The Claude config home for this repo: ``<shared_dir>/claude``.

    Hardcoded to match ``init_command._relocate_claude_json`` and
    ``_seed_claude_json``, which build the same path the same way. Derived
    ultimately from ``agent_presets.claude_preset()``'s ``claude`` shared
    subpath.
    """
    assert cfg.shared_dir is not None, "load_config always computes shared_dir"
    return cfg.shared_dir / "claude"


class Cswap:
    """Typed wrapper around the `cswap` CLI, pinned to one config home."""

    def __init__(self, config_home: Path, binary: str = CSWAP_BINARY) -> None:
        self.config_home = config_home
        self.binary = binary

    # ---- internals ------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(self.config_home)
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        return env

    def _run_json(self, args: list[str]) -> dict[str, Any]:
        """Run a `--json` subcommand and return its parsed payload."""
        cmd = [self.binary, *args, "--json"]
        try:
            result = subprocess.run(
                cmd,
                env=self._env(),
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=_JSON_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise CswapMissingError(INSTALL_HINT) from e
        except subprocess.TimeoutExpired as e:
            raise CswapTimeoutError(f"`{' '.join(cmd)}` timed out after {_JSON_TIMEOUT}s") from e

        payload = self._parse(result.stdout)
        if result.returncode != 0:
            raise CswapError(self._failure_detail(cmd, result, payload))
        if payload is None:
            raise CswapError(
                f"`{' '.join(cmd)}` succeeded but did not print JSON: "
                f"{(result.stdout or '').strip()[:200]!r}"
            )
        # A zero exit with an error envelope is not documented, but treating
        # it as success would hand the caller a payload with no accounts key.
        error = payload.get("error")
        if isinstance(error, dict):
            raise CswapError(str(error.get("message") or error))
        return payload

    @staticmethod
    def _parse(stdout: str | None) -> dict[str, Any] | None:
        if not stdout:
            return None
        try:
            parsed = json.loads(stdout)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _failure_detail(
        cmd: list[str],
        result: subprocess.CompletedProcess[str],
        payload: dict[str, Any] | None,
    ) -> str:
        """The message for a non-zero exit.

        stdout is consulted FIRST — the reverse of the usual order — because
        in `--json` mode cswap keeps stdout machine-readable and puts the
        handled-error envelope there, leaving stderr empty.
        """
        if payload is not None:
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout
        base = f"`{' '.join(cmd)}` failed (exit {result.returncode})"
        return f"{base}: {detail}" if detail else base

    def _run_interactive(self, args: list[str]) -> None:
        """Run a subcommand that may prompt, inheriting the terminal."""
        cmd = [self.binary, *args]
        try:
            result = subprocess.run(cmd, env=self._env(), check=False)
        except FileNotFoundError as e:
            raise CswapMissingError(INSTALL_HINT) from e
        if result.returncode != 0:
            # No captured detail to add: cswap already printed its own error
            # (or the user cancelled at the prompt) straight to the terminal.
            raise CswapError(f"`{' '.join(cmd)}` failed (exit {result.returncode})")

    # ---- public API -----------------------------------------------------

    def available(self) -> bool:
        """Whether the `cswap` binary is on PATH."""
        return shutil.which(self.binary) is not None

    def version(self) -> str:
        """`cswap --version`'s output, for `jailbee doctor`."""
        cmd = [self.binary, "--version"]
        try:
            result = subprocess.run(
                cmd,
                env=self._env(),
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
                timeout=30,
                stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError as e:
            raise CswapMissingError(INSTALL_HINT) from e
        except subprocess.TimeoutExpired as e:
            raise CswapTimeoutError("`cswap --version` timed out") from e
        if result.returncode != 0:
            raise CswapError(self._failure_detail(cmd, result, None))
        return (result.stdout or "").strip()

    def list_accounts(self) -> list[Account]:
        """Every pooled account, in slot order.

        ``active`` is meaningful here: cswap derives it from the live config
        under ``CLAUDE_CONFIG_DIR``, so it reports *this repo's* current
        account, not whichever repo switched last.
        """
        payload = self._run_json(["list"])
        rows = payload.get("accounts")
        if not isinstance(rows, list):
            raise CswapError("`cswap list --json` payload has no `accounts` list")
        return [self._account(row) for row in rows if isinstance(row, dict)]

    @staticmethod
    def _account(row: dict[str, Any]) -> Account:
        """Build one :class:`Account` from a `cswap list --json` row.

        ``number`` and ``email`` are mandatory: the pool ledger keys on
        ``(email, organizationUuid)``, so a row silently defaulted to
        ``email=""`` would read back as a legitimate-looking account with an
        empty identity — a garbage ledger row waiting to be written. Every
        other field defaults leniently: ``organizationUuid`` genuinely is
        ``""`` for a personal (non-organization) account, so that stays
        optional.

        The guard is "present *and* usable", not merely "present": a
        ``"email": null`` would pass a key check and then stringify to the
        literal ``"None"``, which is the same garbage identity by another
        route.
        """
        number = row.get("number")
        if not isinstance(number, int) or isinstance(number, bool):
            raise CswapError(
                f"`cswap list --json` row has no usable `number`: {number!r} "
                f"(email={row.get('email')!r})"
            )

        email = row.get("email")
        if not isinstance(email, str) or not email.strip():
            raise CswapError(
                f"`cswap list --json` row has no usable `email`: {email!r} (number={number!r})"
            )

        usage = row.get("usage")
        usage = usage if isinstance(usage, dict) else {}

        def pct(key: str) -> float | None:
            window = usage.get(key)
            if not isinstance(window, dict):
                return None
            value = window.get("pct")
            return float(value) if isinstance(value, (int, float)) else None

        return Account(
            number=number,
            email=email,
            org_uuid=str(row.get("organizationUuid", "") or ""),
            org_name=str(row.get("organizationName", "") or ""),
            alias=str(row.get("alias", "") or ""),
            active=bool(row.get("active", False)),
            disabled=bool(row.get("disabled", False)),
            usage_status=str(row.get("usageStatus", "unavailable")),
            five_hour_pct=pct("fiveHour"),
            seven_day_pct=pct("sevenDay"),
        )

    def status(self) -> LiveAccount:
        """The live Claude login of this config home."""
        payload = self._run_json(["status"])
        active = payload.get("active")
        if not isinstance(active, dict):
            return LiveAccount(email=None, managed=False, number=None, org_uuid="")
        managed = bool(active.get("managed", False))
        number = active.get("number")
        return LiveAccount(
            email=str(active.get("email", "")) or None,
            managed=managed,
            number=int(number) if isinstance(number, int) else None,
            org_uuid=str(active.get("organizationUuid", "") or ""),
        )

    def switch(self, target: str) -> SwitchResult:
        """Switch this config home to ``target`` (a slot, alias or email).

        Always called with an explicit target. A bare ``cswap switch``
        rotates relative to the *stored* ``activeAccountNumber``, which is
        meaningless when several jailbee repos share one store.

        Returns cswap's own message, with any warnings appended — the
        live-session warning in particular is the user's only notice that
        the same grant is now in two places — plus the identity cswap says it
        landed on, so the caller can check it against the account it asked
        for instead of trusting the request.
        """
        payload = self._run_json(["switch", target])
        message = str(payload.get("message", "")) or f"Switched to {target}"
        warnings = payload.get("warnings")
        if isinstance(warnings, list) and warnings:
            message = "\n".join([message, *(str(w) for w in warnings)])
        landed = payload.get("to")
        landed = landed if isinstance(landed, dict) else {}
        email = landed.get("email")
        number = landed.get("number")
        return SwitchResult(
            message=message,
            email=email.strip() if isinstance(email, str) and email.strip() else None,
            number=number if isinstance(number, int) and not isinstance(number, bool) else None,
        )

    def add(self, *, alias: str | None, slot: int | None) -> None:
        """Capture this config home's current login into the pool.

        Interactive: cswap prompts before displacing an occupied ``--slot``.
        """
        args = ["add"]
        if alias is not None:
            args += ["--alias", alias]
        if slot is not None:
            args += ["--slot", str(slot)]
        self._run_interactive(args)

    def remove(self, ref: str) -> None:
        """Remove an account from the pool. Interactive: cswap prompts [y/N]."""
        self._run_interactive(["remove", ref])
