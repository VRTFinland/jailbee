"""Behaviour checks for the `/etc/profile.d/jailbee-claude.sh` snippet that
`install.sh` bakes into the golden image.

The snippet exists because two halves of the Claude install disagree about
lifetime: `~/.local/share/claude/versions` is a bind mount shared by every
container of a repo (`agent_presets.claude_preset`), while
`~/.local/bin/claude` is a per-container symlink pinned to one exact version
by `ensure-claude.sh` — which only runs at `jailbee new`. Claude's own
updater prunes old releases from the shared store, so `claude update` in one
container can delete the version another container is pinned to, leaving a
dangling launcher that never heals. The snippet repoints it at login.

These tests *run* the exact heredoc bytes `install.sh` writes against a fake
`$HOME` under `tmp_path`: a substring assertion would pass just as happily on
a snippet that picks the wrong version, clobbers a healthy pin, or prints to
stdout. Pure POSIX shell, no Incus and no network — nothing to mock.
"""

import importlib.resources
import os
import subprocess
from pathlib import Path

import pytest

_MARKER = "cat > /etc/profile.d/jailbee-claude.sh <<'EOF'\n"


def _install_sh() -> str:
    return importlib.resources.files("jailbee.provision").joinpath("install.sh").read_text()


@pytest.fixture
def snippet() -> str:
    """The snippet's body, as written into the golden image."""
    text = _install_sh()
    assert _MARKER in text, "install.sh must write /etc/profile.d/jailbee-claude.sh"
    start = text.index(_MARKER) + len(_MARKER)
    end = text.index("\nEOF\n", start)
    return text[start:end]


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".local" / "bin").mkdir(parents=True)
    return h


def _versions(home: Path, *names: str, executable: bool = True) -> Path:
    """Populate the shared version store with `names`."""
    store = home / ".local" / "share" / "claude" / "versions"
    store.mkdir(parents=True, exist_ok=True)
    for name in names:
        binary = store / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755 if executable else 0o644)
    return store


def _run(snippet: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Source the snippet the way a login shell would."""
    return subprocess.run(
        ["bash", "-c", snippet],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )


def _launcher(home: Path) -> Path:
    return home / ".local" / "bin" / "claude"


def test_heals_dangling_launcher(snippet: str, home: Path) -> None:
    """The reported bug: another container's `claude update` pruned the
    version this container is pinned to. 2.1.9 vs 2.1.10 also pins the
    version sort — a plain lexicographic `sort` would pick 2.1.9.
    """
    store = _versions(home, "2.1.9", "2.1.10")
    pruned = store / "2.1.8"
    _launcher(home).symlink_to(pruned)
    assert not _launcher(home).exists()  # dangling

    _run(snippet, home)

    assert _launcher(home).resolve() == store / "2.1.10"


def test_creates_launcher_when_absent(snippet: str, home: Path) -> None:
    """A fresh container's $HOME has no launcher even when the shared store
    is full — the same case `ensure-claude.sh` relinks at `jailbee new`.
    """
    store = _versions(home, "2.1.10")

    _run(snippet, home)

    assert _launcher(home).resolve() == store / "2.1.10"


def test_leaves_healthy_pin_alone(snippet: str, home: Path) -> None:
    """A working launcher must survive untouched: with
    `claude.auto_update: false` the pin to an older release is deliberate,
    and the snippet is a repair, not an updater.
    """
    store = _versions(home, "2.1.9", "2.1.10")
    _launcher(home).symlink_to(store / "2.1.9")

    _run(snippet, home)

    assert _launcher(home).resolve() == store / "2.1.9"


def test_skips_non_executable_candidate(snippet: str, home: Path) -> None:
    """The newest name in the store isn't necessarily a usable binary — an
    interrupted download leaves a non-executable file behind. Fall through
    to the newest release that actually runs rather than linking a stub.
    """
    store = _versions(home, "2.1.9")
    _versions(home, "2.1.10", executable=False)
    _launcher(home).symlink_to(store / "2.1.8")

    _run(snippet, home)

    assert _launcher(home).resolve() == store / "2.1.9"


def test_noop_when_store_empty(snippet: str, home: Path) -> None:
    """`claude.enabled: false`, or a root login (`sudo -i`, HOME=/root):
    no store, so no launcher — and no error out of a login shell.
    """
    _run(snippet, home)

    assert not _launcher(home).exists()
    assert not _launcher(home).is_symlink()


def test_snippet_is_silent(snippet: str, home: Path) -> None:
    """Nothing may reach stdout/stderr. Every in-container `claude`
    invocation goes through `bash -lc`, and `pr_ai.ask_claude_for_pr_text`
    parses that shell's stdout as JSON — a chatty profile.d snippet would
    corrupt it for every `jailbee pr`.
    """
    _versions(home, "2.1.10")
    _launcher(home).symlink_to(home / ".local" / "share" / "claude" / "versions" / "2.1.8")

    proc = _run(snippet, home)

    assert proc.stdout == ""
    assert proc.stderr == ""


def test_snippet_leaks_no_variables(snippet: str, home: Path) -> None:
    """The snippet is *sourced* into the user's shell, so its helper
    variables must not survive it.
    """
    _versions(home, "2.1.10")

    proc = subprocess.run(
        ["bash", "-c", f"{snippet}\nset | grep -c '^_jb' || true"],
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        check=True,
    )

    assert proc.stdout.strip() == "0", "snippet must unset its own helper variables"


def test_snippet_is_world_readable(snippet: str) -> None:
    """profile.d snippets are sourced by the unprivileged dev user; a
    root-only mode 0600 file would silently never load.
    """
    install_sh = _install_sh()
    after = install_sh.split(_MARKER, 1)[1]
    assert "chmod 0644 /etc/profile.d/jailbee-claude.sh" in after
