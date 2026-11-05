"""Persistent and current service launch settings."""

import json
from pathlib import Path

import pytest

from jailbee.services.launch_store import Launch, LaunchStore, resolve_launch


def test_store_roundtrip_and_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "launch.json"
    store = LaunchStore(path)
    persisted = Launch(("--persisted",), {"BASE": "one"})
    current = Launch(("--current",), {"LIVE": "two"})

    store.set_persisted("api", persisted)
    store.set_current("api", current, args_persisted=False)

    assert store.persisted("api") == persisted
    assert store.current("api") == (current, False)
    assert json.loads(path.read_text()) == {
        "api": {
            "persisted": {"args": ["--persisted"], "env": {"BASE": "one"}},
            "current": {
                "args": ["--current"],
                "env": {"LIVE": "two"},
                "persisted_args": False,
            },
        }
    }
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_store_is_empty(tmp_path: Path) -> None:
    path = tmp_path / "launch.json"
    path.write_text("not json")
    store = LaunchStore(path)

    assert store.persisted("api") == Launch()
    assert store.current("api") is None


@pytest.mark.parametrize(
    "current",
    [
        {"args": "broken", "env": {}, "persisted_args": False},
        {"args": [], "env": {"A": 1}, "persisted_args": False},
        {"args": [], "env": {}, "persisted_args": "false"},
        {"args": [], "env": {}},
    ],
)
def test_structurally_corrupt_current_is_absent(tmp_path: Path, current: dict[str, object]) -> None:
    path = tmp_path / "launch.json"
    path.write_text(json.dumps({"api": {"current": current}}))

    assert LaunchStore(path).current("api") is None


def test_clear_persisted_preserves_current_and_other_services(tmp_path: Path) -> None:
    store = LaunchStore(tmp_path / "launch.json")
    store.set_persisted("api", Launch(("saved",), {"A": "1"}))
    store.set_current("api", Launch(("live",), {"B": "2"}), False)
    store.set_persisted("worker", Launch(("worker",), {}))

    store.clear_persisted("api")

    assert store.persisted("api") == Launch()
    assert store.current("api") == (Launch(("live",), {"B": "2"}), False)
    assert store.persisted("worker") == Launch(("worker",), {})


def test_clear_current_preserves_persisted_and_other_services(tmp_path: Path) -> None:
    store = LaunchStore(tmp_path / "launch.json")
    store.set_persisted("api", Launch(("saved",), {"A": "1"}))
    store.set_current("api", Launch(("live",), {"B": "2"}), False)
    store.set_current("worker", Launch(("worker",), {}), True)

    store.clear_current("api")

    assert store.current("api") is None
    assert store.persisted("api") == Launch(("saved",), {"A": "1"})
    assert store.current("worker") == (Launch(("worker",), {}), True)


@pytest.mark.parametrize(
    (
        "persisted",
        "args",
        "env",
        "persist",
        "reset",
        "want_persisted",
        "want_effective",
        "want_flag",
    ),
    [
        (
            Launch(("old",), {"A": "1"}),
            None,
            {},
            False,
            False,
            Launch(("old",), {"A": "1"}),
            Launch(("old",), {"A": "1"}),
            True,
        ),
        (
            Launch(("old",), {}),
            ("new",),
            {},
            False,
            False,
            Launch(("old",), {}),
            Launch(("new",), {}),
            False,
        ),
        (
            Launch((), {"A": "1", "B": "2"}),
            None,
            {"B": "3", "C": "4"},
            False,
            False,
            Launch((), {"A": "1", "B": "2"}),
            Launch((), {"A": "1", "B": "3", "C": "4"}),
            True,
        ),
        (
            Launch(("old",), {"A": "1"}),
            ("new",),
            {"B": "2"},
            True,
            False,
            Launch(("new",), {"A": "1", "B": "2"}),
            Launch(("new",), {"A": "1", "B": "2"}),
            True,
        ),
        (
            Launch(("old",), {"A": "1"}),
            ("new",),
            {},
            True,
            True,
            Launch(("new",), {}),
            Launch(("new",), {}),
            True,
        ),
        (Launch(("old",), {"A": "1"}), None, {}, False, True, Launch(), Launch(), False),
        (
            Launch(("old",), {"A": "1"}),
            None,
            {"B": "2"},
            False,
            False,
            Launch(("old",), {"A": "1"}),
            Launch(("old",), {"A": "1", "B": "2"}),
            True,
        ),
    ],
)
def test_resolve_launch_table(
    persisted: Launch,
    args: tuple[str, ...] | None,
    env: dict[str, str],
    persist: bool,
    reset: bool,
    want_persisted: Launch,
    want_effective: Launch,
    want_flag: bool,
) -> None:
    assert resolve_launch(persisted, args=args, env=env, persist=persist, reset=reset) == (
        want_persisted,
        want_effective,
        want_flag,
    )
