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

    def account_at(
        self, holder: Path, found: Any, *, prefer: str, authoritative: Any
    ) -> models.LiveAccount | None:
        raw = holder / self.credential_file
        if not raw.exists():
            return None
        block = self.grant_block(raw.read_text(encoding="utf-8"))
        if block is None or not isinstance(block.get("email"), str):
            return None
        record = {"emailAddress": block["email"]}
        return models.LiveAccount(identity=models.Identity(email=block["email"]), record=record)

    def record_for(self, slot: models.Slot, raw: str) -> dict[str, Any] | None:
        return None

    def on_park(self, cfg: Any, holder: Path, parked: Path, account: Any) -> None:
        return None

    def on_activate(self, holder: Path, record: Any, credential_raw: str) -> None:
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


def _write(path: Path, email: str, refresh: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"login": {"email": email, "refresh": refresh}}), encoding="utf-8")


@pytest.fixture
def fake_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An XDG data home the engine's store lands in."""
    monkeypatch.setattr("jailbee.paths.xdg_data_home", lambda: tmp_path / "xdg")
    return tmp_path


def test_park_moves_the_live_credential_into_the_store(fake_env: Path, mocker) -> None:
    from jailbee.accounts import engine

    home = fake_env / "home"
    adapter = FakeAdapter(home)
    _write(home / adapter.credential_file, "me@example.com", "r1")
    cfg = mocker.MagicMock(container_prefix="repo")
    gcfg = mocker.MagicMock()
    mocker.patch.object(engine, "members", return_value=([], []))

    change = engine.park(adapter, cfg, gcfg, authoritative={"repo"})

    assert change.parked_as == "me@example.com"
    assert not (home / adapter.credential_file).exists()
    assert (engine.store_dir(adapter) / "me@example.com.json").exists(), (
        "the login must be in the store, not deleted"
    )


def test_switch_never_leaves_one_grant_in_two_files(fake_env: Path, mocker) -> None:
    from jailbee.accounts import engine

    home = fake_env / "home"
    adapter = FakeAdapter(home)
    _write(home / adapter.credential_file, "live@example.com", "r-live")
    store = engine.store_dir(adapter)
    _write(store / "parked@example.com.json", "parked@example.com", "r-parked")
    cfg = mocker.MagicMock(container_prefix="repo")
    gcfg = mocker.MagicMock()
    mocker.patch.object(engine, "members", return_value=([], []))

    change = engine.switch(adapter, cfg, gcfg, "parked@example.com", authoritative={"repo"})

    assert change.activated == "parked@example.com"
    assert change.parked_as == "live@example.com"
    live = json.loads((home / adapter.credential_file).read_text(encoding="utf-8"))
    assert live["login"]["email"] == "parked@example.com"
    assert not (store / "parked@example.com.json").exists()
    assert (store / "live@example.com.json").exists()
    assert not list(store.glob("*.activating")), "no staging file may survive"


def test_switch_refuses_to_activate_the_live_slot(fake_env: Path, mocker) -> None:
    from jailbee.accounts import engine

    home = fake_env / "home"
    adapter = FakeAdapter(home)
    _write(home / adapter.credential_file, "me@example.com", "r1")
    cfg = mocker.MagicMock(container_prefix="repo")
    gcfg = mocker.MagicMock()
    mocker.patch.object(engine, "members", return_value=([], []))

    with pytest.raises(models.PoolError, match="already the live account"):
        engine.switch(adapter, cfg, gcfg, "me@example.com", authoritative={"repo"})


def test_the_store_is_named_after_the_agent(fake_env: Path) -> None:
    from jailbee.accounts import engine

    adapter = FakeAdapter(fake_env / "home")
    assert engine.store_dir(adapter).parent.name == "fake-credentials"
    assert engine.store_dir(adapter).name == "_parked"
