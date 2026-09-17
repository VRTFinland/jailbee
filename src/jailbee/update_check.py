"""Tell the user when a newer jailbee is on PyPI.

The check never sits on a command's path. A command reads the last answer
out of the state database (`UpdateCheckState`, one row) and prints at most
one hint from it; if that answer is older than `CACHE_TTL` it starts a
*detached* probe — this module, run as `python -m jailbee.update_check` —
which does the network call and writes the row for whoever runs next. So the
first command after an upgrade of the outside world is never slower, and
`jailbee shell`, which hands the terminal to a container and does not return,
is no special case.

The probe is a separate process rather than a thread on purpose: `cli.py`
imports lazily inside command functions, and an interpreter whose package
tree is being replaced underneath it (which is exactly what an upgrade does)
must not still be holding jailbee's own import machinery.

What goes over the wire is one unauthenticated GET of
`https://pypi.org/pypi/jailbee/json`, with a `jailbee/<version>` user agent
and nothing identifying the user or the repo. `update_check: false` in
`~/.config/jailbee/global.yaml`, or `JAILBEE_NO_UPDATE_CHECK=1`, stops it —
in this module *and* in the probe, which re-reads the setting itself.

An editable install (`uv tool install -e .`) is deliberately never advised:
it runs from a checkout, and no `pip`/`uv` command would upgrade it to
anything the developer wants.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import timedelta
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.request import Request, urlopen

from jailbee import __version__
from jailbee.db import get_engine
from jailbee.upgrade import parse_version

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.db.models import UpdateCheckState

PYPI_JSON_URL = "https://pypi.org/pypi/jailbee/json"
"""PyPI's JSON API for this package. `info.version` is the latest release."""

FETCH_TIMEOUT = 5.0
"""Seconds. Only the detached probe waits on this, never a user's command."""

CACHE_TTL = timedelta(hours=24)
"""How long a fetched answer is reused before a fresh probe is started."""

HINT_INTERVAL = timedelta(hours=24)
"""How often the *same* release may be advertised. A newer one ignores it."""

ENV_DISABLE = "JAILBEE_NO_UPDATE_CHECK"
"""Off switch for scripts and CI, which cannot edit the user's config file."""

Manager = Literal["uv", "pipx", "pip", "editable", "unknown"]


@dataclass(frozen=True)
class Install:
    """How this jailbee was installed, and what would upgrade it.

    `upgrade_command` is `None` when no command should be offered — an
    editable checkout, or an install whose package metadata cannot be read.
    A `None` here silences the hint entirely: advice naming the wrong
    command is worse than no advice, because acting on it can replace a
    working install.
    """

    manager: Manager
    upgrade_command: str | None


def classify_install(*, editable: bool, location: Path) -> Install:
    """Map an installed location to the manager that owns it.

    Path-based because that is what the managers agree on: `uv tool` installs
    under `<data>/uv/tools/<name>/`, pipx under `<data>/pipx/venvs/<name>/`.
    Anything else is some venv or system install, where plain pip is the only
    answer that is true everywhere.
    """
    if editable:
        return Install("editable", None)
    parts = location.parts
    if "uv" in parts and "tools" in parts:
        return Install("uv", "uv tool upgrade jailbee")
    if "pipx" in parts:
        return Install("pipx", "pipx upgrade jailbee")
    return Install("pip", "pip install -U jailbee")


def detect_install() -> Install:
    """Classify the running install from its package metadata.

    `direct_url.json` (PEP 610) is what marks an editable install; every
    installer this project documents writes it. Missing metadata means the
    package was not installed by any of them — there is nothing to advise,
    so the result carries no command.
    """
    try:
        dist = metadata.distribution("jailbee")
    except metadata.PackageNotFoundError:
        return Install("unknown", None)
    editable = False
    raw = dist.read_text("direct_url.json")
    if raw:
        try:
            editable = bool(json.loads(raw).get("dir_info", {}).get("editable"))
        except (json.JSONDecodeError, AttributeError):
            editable = False
    location = dist.locate_file("")
    return classify_install(editable=editable, location=Path(str(location)))


def newer_version(current: str, latest: str | None) -> str | None:
    """Return `latest` when it is a strictly newer release than `current`.

    Both sides go through `upgrade.parse_version`, which accepts only
    `X.Y.Z`. That is the whole prerelease policy: PyPI serving `1.5.0rc1` as
    `info.version`, or a local `0.0.0+unknown`, produces silence rather than
    a comparison nobody can act on.
    """
    if latest is None:
        return None
    here = parse_version(current)
    there = parse_version(latest)
    if here is None or there is None:
        return None
    return latest if there > here else None


def hint_lines(current: str, latest: str, install: Install) -> list[str]:
    """Render the advice for `tui.hint`, or nothing when there is no command."""
    if install.upgrade_command is None:
        return []
    return [
        f"jailbee {latest} is available (you are running {current}).",
        f"    Upgrade with: {install.upgrade_command}",
    ]


def check_enabled(*, configured: bool, env: Mapping[str, str]) -> bool:
    """Whether the check may run at all. The environment only ever says no."""
    raw = env.get(ENV_DISABLE, "").strip().lower()
    if raw and raw not in {"0", "false", "no"}:
        return False
    return configured


def _row(session: Session) -> UpdateCheckState | None:
    """The single cache row, or `None` before anything has written it.

    The model import is local, like `upgrade._bootstrap_row`'s: it keeps
    `db.models` (and the SQLModel metadata it registers) off this module's
    import graph, so the pure helpers above stay importable without a DB.
    """
    from jailbee.db.models import UpdateCheckState

    return session.get(UpdateCheckState, 1)


def needs_probe(session: Session, *, now: datetime, ttl: timedelta = CACHE_TTL) -> bool:
    """True when the cached answer is missing or older than `ttl`."""
    row = _row(session)
    if row is None or row.checked_at is None:
        return True
    return now - row.checked_at >= ttl


def record_check(session: Session, latest: str | None, *, now: datetime) -> None:
    """Store the probe's result: what it found, and that it ran.

    A failed fetch passes `latest=None` and stamps only `checked_at`. Both
    halves matter: the stamp is what stops an offline host from spawning a
    probe on every command, and keeping the previous `latest_version` is what
    lets the hint survive a week without a network.
    """
    from jailbee.db.models import UpdateCheckState

    row = _row(session)
    if row is None:
        row = UpdateCheckState(id=1)
    row.checked_at = now
    if latest is not None:
        row.latest_version = latest
    session.add(row)
    session.commit()


def consume_hint(
    session: Session,
    current: str,
    *,
    now: datetime,
    install: Install | None = None,
    interval: timedelta = HINT_INTERVAL,
) -> list[str]:
    """The read path: the lines to print, and the record that they were.

    Returns `[]` unless a strictly newer release is cached, this install can
    be advised at all, and the same release was not already advertised within
    `interval`. A *different* release ignores the interval — the point of the
    rate limit is to stop `jailbee ls` repeating itself, not to sit on news.
    """
    row = _row(session)
    if row is None:
        return []
    latest = newer_version(current, row.latest_version)
    if latest is None:
        return []
    if (
        row.hint_shown_version == latest
        and row.hint_shown_at is not None
        and now - row.hint_shown_at < interval
    ):
        return []
    lines = hint_lines(current, latest, install if install is not None else detect_install())
    if not lines:
        return []
    row.hint_shown_at = now
    row.hint_shown_version = latest
    session.add(row)
    session.commit()
    return lines


def probe_argv() -> list[str]:
    """The detached probe's command line: this interpreter, this module."""
    return [sys.executable, "-m", "jailbee.update_check"]


def spawn_probe() -> None:
    """Start the probe and forget it.

    `start_new_session` detaches it from the caller's process group, so it
    survives the command that started it — and a Ctrl-C aimed at that command
    does not land here. Every stream goes to `/dev/null`: nothing it could
    print belongs in the output of whatever the user actually ran.
    """
    try:
        subprocess.Popen(  # fixed argv, no shell
            probe_argv(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return  # a probe that cannot start is not worth a word to the user


def maybe_probe(
    session: Session,
    *,
    now: datetime,
    enabled: bool,
    spawn: Callable[[], None] = spawn_probe,
) -> None:
    """Start a probe if the check is on and the cached answer has expired."""
    if not enabled:
        return
    if needs_probe(session, now=now):
        spawn()


def fetch_latest(url: str = PYPI_JSON_URL, *, timeout: float = FETCH_TIMEOUT) -> str | None:
    """Ask PyPI for the latest released version. `None` on any failure.

    Deliberately total: DNS down, a proxy returning HTML, PyPI changing the
    payload — the probe's only job is to leave the cache no worse than it
    found it, so every failure mode collapses into "nothing new to store".
    """
    request = Request(url, headers={"User-Agent": f"jailbee/{__version__}"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except Exception:
        return None
    version = payload.get("info", {}).get("version") if isinstance(payload, dict) else None
    return version if isinstance(version, str) else None


def _configured_enabled() -> bool:
    """Read `update_check` from `global.yaml`, defaulting to off on trouble.

    Only the probe calls this — a foreground command already holds a loaded
    `GlobalConfig`. An unreadable or invalid file yields `False`: the file is
    where the user says no, and a parse error must not be the one path that
    reaches the network anyway.
    """
    from jailbee.global_config import default_global_config_path, load_global_config

    try:
        gcfg, _ = load_global_config(default_global_config_path())
    except Exception:
        return False
    return gcfg.update_check


def run_probe(*, now: datetime | None = None, url: str = PYPI_JSON_URL) -> None:
    """The probe's body: check the opt-out, fetch, record.

    The opt-out is re-read here because this runs in its own process: the
    command that spawned it decided only that the cache was stale, and a
    `global.yaml` edited in between must be obeyed by the process that would
    do the talking.
    """
    from datetime import UTC
    from datetime import datetime as datetime_

    from sqlmodel import Session

    if not check_enabled(configured=_configured_enabled(), env=os.environ):
        return
    latest = fetch_latest(url)
    with Session(get_engine()) as session:
        record_check(session, latest, now=now if now is not None else datetime_.now(UTC))


def main() -> int:
    """`python -m jailbee.update_check`. Never fails loudly: nobody is reading."""
    try:
        run_probe()
    except Exception:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
