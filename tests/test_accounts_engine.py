"""Engine tests driven by a fake adapter.

The fake is the point: every behaviour asserted here has to hold for an agent
that is not Claude, which is what makes the engine generic rather than Claude
with the names changed.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any

import pytest

from jailbee.accounts import models
from jailbee.accounts.adapters import base


class FakeAdapter:
    """A minimal agent: one credential file, identity inside it, no locks."""

    name = "fake"
    credential_file = "cred.json"
    refresh_token_key = "refresh"
    live_switch = True

    def __init__(self, home: Path, holder: Path | None = None) -> None:
        self._home = home
        self._holder = holder

    def config_home(self, cfg: Any) -> Path:
        return self._home

    def holder_override(self, cfg: Any) -> Path | None:
        return self._holder

    def grant_block(self, raw: str | None) -> dict[str, Any] | None:
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        block = data.get("login")
        return block if isinstance(block, dict) else None

    def compose(self, target_raw: str, live_raw: str | None) -> str:
        return target_raw

    def locks(self, holder: Path) -> Any:
        return nullcontext()

    def live_account(
        self, cfg: Any, found: Any, *, prefer: str, authoritative: Any
    ) -> models.LiveAccount | None:
        raw = (self._holder or self._home) / self.credential_file
        if not raw.exists():
            return None
        block = self.grant_block(raw.read_text(encoding="utf-8"))
        if block is None or not isinstance(block.get("email"), str):
            return None
        record = {"emailAddress": block["email"]}
        return models.LiveAccount(
            identity=models.Identity(email=block["email"]), record=record
        )

    def record_for(self, slot: models.Slot, raw: str) -> dict[str, Any] | None:
        return None

    def on_park(self, cfg: Any, holder: Path, parked: Path, account: Any) -> None:
        return None

    def on_switch(
        self, found: Any, unreachable: Any, record: Any, authoritative: Any
    ) -> tuple[list[str], list[str]]:
        return [], list(unreachable)

    def sessions(self, found: Any) -> list[str]:
        return []

    def blockers(self, cfg: Any, incus: Any, containers: Any) -> list[str]:
        return []

    def wiring(self, cfg: Any, group_dir: Path | None) -> base.Wiring:
        return base.Wiring(devices={}, env={})

    def prepare_config_home(self, cfg: Any, home: Path) -> None:
        return None


def test_the_fake_adapter_satisfies_the_protocol(tmp_path: Path) -> None:
    adapter: base.AccountAdapter = FakeAdapter(tmp_path)
    assert isinstance(adapter, base.AccountAdapter)
    assert adapter.name == "fake"


def test_registry_returns_a_registered_adapter(tmp_path: Path) -> None:
    adapter = FakeAdapter(tmp_path)
    base.register(adapter)
    try:
        assert base.get_adapter("fake") is adapter
    finally:
        base.ADAPTERS.pop("fake", None)


def test_registry_raises_for_an_unknown_agent() -> None:
    with pytest.raises(KeyError):
        base.get_adapter("nosuchagent")
