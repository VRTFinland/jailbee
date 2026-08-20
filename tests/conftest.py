"""Shared pytest fixtures."""

from __future__ import annotations

import os

# Qt widget tests run headless in CI; select the offscreen platform plugin
# unless the environment already chose one. Harmless for non-Qt tests.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from jailbee.config import Config, resolve_agents_raw
from jailbee.db import _ensure_schema


def make_config(
    repo_root: Path,
    *,
    default_branch: str = "main",
    upstream_remote: str = "origin",
    shared_dir: Path | None = None,
    **overrides: Any,
) -> Config:
    """Build a fully-defaulted Config with computed fields wired up.

    Use in tests instead of constructing Config directly so that
    repo_root/default_branch/upstream_remote/container_prefix are always set.
    Extra keyword args are forwarded to ``Config.model_validate`` so callers
    can pass e.g. ``gpg={"enabled": False}`` or ``host_mounts=[...]``.

    Overrides are routed through ``resolve_agents_raw`` first — the same
    normalisation ``load_config`` applies to YAML — so a legacy
    ``claude={...}`` override and a preset-backed ``agents={...}`` override
    both resolve exactly as they would from a real config file.
    """
    cfg = Config.model_validate(resolve_agents_raw(overrides)) if overrides else Config()
    object.__setattr__(cfg, "repo_root", repo_root)
    object.__setattr__(cfg, "default_branch", default_branch)
    object.__setattr__(cfg, "upstream_remote", upstream_remote)
    if not cfg.container_prefix:
        object.__setattr__(cfg, "container_prefix", repo_root.name)
    if shared_dir is not None:
        object.__setattr__(cfg, "shared_dir", shared_dir)
    elif cfg.shared_dir is None:
        object.__setattr__(
            cfg,
            "shared_dir",
            Path.home() / ".local" / "share" / "jailbee" / "shared" / cfg.container_prefix,
        )
    return cfg


# Module-level alias for tests that prefer `from tests.conftest import make_cfg`
# over the pytest fixture form.
make_cfg = make_config


@pytest.fixture(name="make_cfg")
def make_cfg_fixture():
    """Pytest fixture wrapper for make_config."""
    return make_config


def with_agent(cfg: Config, name: str, **fields: Any) -> Config:
    """Return a copy of `cfg` with `agents[name]` updated by `fields`.

    Use instead of `cfg.model_copy(update={"claude": ...})`: `Config.claude` is
    a property, so that form is silently ignored rather than failing.

    Builds the model directly and does **not** run `resolve_agents_raw`, so
    presets are not applied here — pass `command=` explicitly when the agent
    is not already present in `cfg`. Presets resolve on the load path only.
    """
    from jailbee.config import AgentConfig, ClaudeAgentConfig

    model = ClaudeAgentConfig if name == "claude" else AgentConfig
    current = cfg.agents.get(name)
    base = current.model_dump() if current is not None else {}
    if name == "claude":
        base.setdefault("command", "claude")
    merged = model.model_validate({**base, **fields})
    return cfg.model_copy(update={"agents": {**cfg.agents, name: merged}})


def _raw_container(name: str, *profiles: str) -> dict[str, Any]:
    """One entry as `incus list --format json --fast` returns it (state: null).

    Shared between tests/test_completion.py (calls the completers directly)
    and tests/test_completion_e2e.py (drives them through the real Typer/Click
    command tree), so it lives here rather than in either test module.
    """
    return {
        "name": name,
        "status": "Running",
        "profiles": list(profiles),
        "config": {},
        "state": None,
    }


@pytest.fixture
def completion_repo(tmp_path, mocker):
    """Point `completion._load()` at a fabricated repo and a MagicMock Incus.

    `_load` imports load_config/find_repo_config/Incus lazily at call time, so
    patching them on their defining modules is enough. Shared by
    tests/test_completion.py and tests/test_completion_e2e.py.
    """
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    cfg = make_config(repo_root)
    assert cfg.container_prefix == "myrepo"

    mocker.patch(
        "jailbee.paths.find_repo_config",
        return_value=repo_root / ".jailbee" / "config.yaml",
    )
    mocker.patch("jailbee.config.load_config", return_value=cfg)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw_container("myrepo-feat-foo", "myrepo-base", "myrepo-net-strict"),
        _raw_container("myrepo-bugfix", "myrepo-base", "myrepo-net-strict"),
        _raw_container("other-thing", "other-base"),
        _raw_container("jailbee-registry-mirror", "jailbee-registry-mirror-profile"),
    ]
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    return cfg, incus


@pytest.fixture(scope="session", autouse=True)
def _isolate_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point ``HOME`` at a tmp dir for the whole test session.

    Sibling of ``_isolate_global_config`` / ``_isolate_state_dir``,
    covering what jailbee derives from ``Path.home()`` rather than from an
    XDG variable: the systemd units dir (``~/.config/systemd/user``), the
    default ``shared_dir`` under ``~/.local/share/jailbee/shared/``, and the
    gpg/ssh mount sources. Without it a plain ``uv run pytest`` litters
    the developer's real home with a ``shared/<pytest-tmp-name>/`` tree
    per test.

    Session-scoped, so residue still accumulates *within* one run the way
    it always has — only the escape into the real home is closed. A test
    that writes enough home-dir state to disturb others (or that wants to
    assert on an empty home) takes the function-scoped ``private_home``
    fixture instead.

    Note this does not isolate the host *session*: `systemctl --user`,
    `docker` and `incus` calls that escape mocking still reach the real
    daemons. Those belong in the individual test's mocks.
    """
    home = tmp_path_factory.mktemp("home", numbered=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("HOME", str(home))
        yield home


@pytest.fixture
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give this one test an empty ``HOME`` of its own.

    Overrides the session-wide ``_isolate_home`` for the duration of the
    test (``monkeypatch`` is function-scoped, so the session value comes
    back afterwards). Returns the directory, so tests can assert on the
    paths they expect to be created under it.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path_factory, monkeypatch):
    """Redirect ``default_global_config_path`` to an empty tmp dir.

    Without this, every ``load_config()`` call layers the developer's
    real ``~/.config/gie/global.yaml`` on top of the test fixture,
    leaking host_mounts / github tokens / apparmor settings into
    assertions that expect a clean default. Tests that exercise the
    global+repo layering set their own ``XDG_CONFIG_HOME`` later, which
    supersedes this fixture (monkeypatch.setenv keeps the most recent
    value).
    """
    iso = tmp_path_factory.mktemp("xdg-isolation", numbered=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(iso))


@pytest.fixture(autouse=True)
def _isolate_state_dir(tmp_path_factory, monkeypatch):
    """Redirect XDG_STATE_HOME to a tmp dir so tests never touch ~/.local/state/jailbee/.

    _resolve_attachable always calls get_engine() (via
    list_containers(..., with_background=True) and wait_for_background_ready),
    which creates the DB at XDG_STATE_HOME/jailbee/state.sqlite. Per-test
    monkeypatch.setenv("XDG_STATE_HOME", ...) calls supersede this default
    because monkeypatch.setenv keeps the most recent value.
    """
    iso = tmp_path_factory.mktemp("xdg-state-isolation", numbered=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(iso))


@pytest.fixture(autouse=True)
def _disable_cli_color(monkeypatch):
    """Force Rich/Typer help output to be plain (no ANSI) in tests.

    CI runners (e.g. GitHub Actions) export ``FORCE_COLOR``, which makes
    Typer's Rich help formatter emit ANSI style codes. Those codes split
    styled tokens apart — ``--pr 1234`` renders as ``-`` + ``-pr`` +
    `` 1234`` with escape sequences interleaved — which breaks the
    substring assertions in the CLI help tests. Locally the same tests
    pass because ``CliRunner`` captures a plain, non-tty buffer with no
    colour. Neutralise the colour env so ``invoke(...).output`` is plain
    text wherever the suite runs. ``TERM=dumb`` also disables colour even
    if ``FORCE_COLOR`` leaks back in (it wins over ``FORCE_COLOR`` in
    Rich's detection).
    """
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR_FORCE", raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")


@pytest.fixture(autouse=True)
def _mock_runtime_mounts(request, mocker):
    """Auto-mock runtime_mounts.attach/detach in all tests except its own.

    `attach_runtime_devices` polls `incus exec` for ~15 s when given a
    bare MagicMock — the polling loop never sees the dev UID. That kills
    the unit suite's runtime. Tests that exercise lifecycle / cli
    integration don't care about the polling mechanics; tests that *do*
    live in tests/test_runtime_mounts.py and skip this autouse via the
    `unmock_runtime_mounts` marker on the file.
    """
    if request.node.get_closest_marker("unmock_runtime_mounts"):
        return
    if request.module.__name__.endswith(".test_runtime_mounts"):
        return
    mocker.patch(
        "jailbee.runtime_mounts.attach_runtime_devices",
        return_value=True,
    )
    mocker.patch(
        "jailbee.runtime_mounts.detach_runtime_devices",
    )


@pytest.fixture(autouse=True)
def _neutralize_kitty_autodetect(request, mocker):
    """Force the kitty terminfo autodetect to find nothing by default.

    Without this, the host-side `_kitty_terminfo_candidates` paths get
    probed for real, and `effective_host_mounts()` returns a different
    mount list on developer machines with kitty installed vs. CI runners
    without it. Tests that exercise kitty autodetect explicitly re-patch
    `_kitty_terminfo_candidates` to inject their own paths and override
    this default.
    """
    # Exempt the one test whose subject IS this function (it asserts on
    # the real candidate list). Tests that exercise kitty autodetect
    # against fabricated paths re-patch this symbol themselves, which
    # supersedes our default.
    if request.node.name == "test_kitty_terminfo_candidates_includes_known_paths":
        return
    mocker.patch(
        "jailbee.config._kitty_terminfo_candidates",
        return_value=[Path("/nonexistent/kitty-terminfo-sentinel")],
    )


@pytest.fixture
def db_engine() -> Engine:
    """In-memory SQLite engine with the gie schema applied. One per test."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    _ensure_schema(engine)
    return engine


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """A session scoped to a single test against the in-memory DB."""
    with Session(db_engine) as session:
        yield session


@pytest.fixture
def frozen_now() -> datetime:
    """A stable timestamp for TTL math in tests."""
    return datetime(2026, 5, 19, 17, 25, 0, tzinfo=UTC)
