"""Claude Code's account adapter.

**Most methods here are temporary delegations.** `engine.py` reaches the agent
only through this object, but the Claude-specific bodies still live in
`claude_pool.py` — identity, the account note, credential composition, the
member rewrite. Each delegation is one line behind a lazy import, because
`claude_pool` imports this module and the cycle must not close at import time.
Task 4 moves those bodies in here and inverts every delegation; the six members
above `grant_block` are already final.
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from jailbee.accounts.adapters import base

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from contextlib import AbstractContextManager
    from pathlib import Path

    from jailbee.accounts.models import LiveAccount, Member, Slot
    from jailbee.config import Config
    from jailbee.incus import Incus


class ClaudeAdapter:
    name = "claude"
    credential_file = ".credentials.json"
    refresh_token_key = "refreshToken"
    live_switch = True

    def config_home(self, cfg: Config) -> Path:
        """This repo's Claude config home on the host — never shared."""
        assert cfg.shared_dir is not None  # set by load_config
        return cfg.shared_dir / "claude"

    def holder_override(self, cfg: Config) -> Path | None:
        return cfg.claude_credentials_dir

    def grant_block(self, raw: str | None) -> dict[str, Any] | None:
        from jailbee import claude_pool

        return claude_pool._login_block(raw)

    def compose(self, target_raw: str, live_raw: str | None) -> str:
        from jailbee import claude_pool

        return claude_pool.compose_credential(target_raw, claude_pool.shared_fields(live_raw))

    def locks(self, holder: Path) -> AbstractContextManager[None]:
        from jailbee.claude_locks import credential_locks

        return credential_locks(holder)

    def live_account(
        self,
        cfg: Config,
        found: Sequence[Member],
        *,
        prefer: str,
        authoritative: Collection[str],
    ) -> LiveAccount | None:
        from jailbee import claude_pool

        return claude_pool.live_account(cfg, found, prefer=prefer, authoritative=authoritative)

    def record_for(self, slot: Slot, raw: str) -> dict[str, Any] | None:
        from jailbee import claude_pool

        return claude_pool.trusted_record_in(slot, raw)

    def on_park(
        self, cfg: Config, holder: Path, parked: Path, account: LiveAccount | None
    ) -> None:
        """Stamp the account into the parked file and retire the holder's note.

        Both belong to the grant that just left: `_stamp_account_record` keeps
        Claude Code's own `oauthAccount` inside the file (see
        `ACCOUNT_RECORD_KEY`), and the note describes a credential this holder
        no longer has.
        """
        from jailbee import claude_pool

        claude_pool._stamp_account_record(parked, None if account is None else account.record)
        with suppress(OSError):
            claude_pool.account_note_path(holder).unlink(missing_ok=True)

    def on_activate(
        self, holder: Path, record: dict[str, Any] | None, credential_raw: str
    ) -> None:
        from jailbee import claude_pool

        claude_pool.write_account_note(holder, record, credential_raw)

    def on_switch(
        self,
        found: Sequence[Member],
        unreachable: Sequence[str],
        record: dict[str, Any] | None,
        authoritative: Collection[str],
    ) -> tuple[list[str], list[str]]:
        from jailbee import claude_pool

        return claude_pool._rewrite_identities(found, unreachable, record, authoritative)

    def sessions(self, found: Sequence[Member]) -> list[str]:
        from jailbee import claude_pool

        return claude_pool.live_session_prefixes(found)

    def blockers(self, cfg: Config, incus: Incus, containers: Sequence[str]) -> list[str]:
        """Claude never blocks a switch: it re-reads the credential itself."""
        return []

    def wiring(self, cfg: Config, group_dir: Path | None) -> base.Wiring:
        """Empty until Task 6 moves the profile wiring in."""
        return base.Wiring()

    def prepare_config_home(self, cfg: Config, home: Path) -> None:
        """No-op here; `init_command` still owns the onboarding seed."""
        return None


CLAUDE = ClaudeAdapter()
base.register(CLAUDE)
