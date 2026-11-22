"""Serve the web dashboard over fixture data, for the headless smoke recipe.

Not part of the test suite. It builds the real app (`create_app`) with a fake
state snapshot (one repo, one container) and the transcript fixtures as that
repo's Claude home, so a browser sees a populated front page and session list
without Incus. Run it with `XDG_STATE_HOME` and `XDG_CONFIG_HOME` pointing at a
throwaway directory: the login database lives there.

    uv run --extra web python scripts/web_smoke_server.py --port 8919

See docs/manual-testing.md ("Web dashboard headless smoke test").
"""

from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> None:
    import uvicorn

    from jailbee.db import get_engine
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.web import assets
    from jailbee.web.app import create_app
    from jailbee.web.auth import PinHolder
    from jailbee.web.context import WebDeps
    from tests.web_fakes import FakeSourceFactory, container, repo_group, snapshot_of

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8919)
    parser.add_argument(
        "--pin", default="123456", help="first login PIN (6 digits, not a palindrome)"
    )
    parser.add_argument("--no-auth", action="store_true", help="serve without a login")
    args = parser.parse_args()

    home = ROOT / "tests/fixtures/transcripts/claude"
    # A PIN is spent by every attempt and the next must differ from the last, so
    # a fixed one cannot work: alternate between the given PIN and its reverse.
    pins = itertools.cycle([args.pin, args.pin[::-1]])
    group = repo_group("work", container("work-main", "work"))
    factory = FakeSourceFactory(initial=snapshot_of(group))
    deps = WebDeps(
        snapshot_source=factory,
        engine=get_engine(),
        pin=PinHolder(generate=lambda: next(pins)),
        auth_enabled=not args.no_auth,
        static_dir=assets.default_static_dir(),
        repo_scope=RemoteRepoScope(frozenset()),
        agent_homes=lambda prefix: [("claude", home)] if prefix == "work" else None,
    )
    uvicorn.run(create_app(deps), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
