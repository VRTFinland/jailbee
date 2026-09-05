"""Tests for the shell-completion callbacks.

Fully mocked: no incus daemon, no git, no filesystem beyond tmp_path. The
recurring assertion is the module's contract — a completer never raises and
returns [] when anything at all goes wrong.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jailbee import completion
from tests.conftest import _raw_container

# The `completion_repo` fixture (fabricated repo config + MagicMock Incus) and
# the `_raw_container` payload helper live in tests/conftest.py — shared with
# tests/test_completion_e2e.py, which drives the same completers through the
# real Typer/Click command tree instead of calling them directly.


def _ctx(**params: Any) -> Any:
    """A stand-in for click's Context: completers only read `.params`."""
    return SimpleNamespace(params=params)


def test_offers_short_names_when_nothing_typed(completion_repo):
    """Short names are what users type and what resolve_container_name accepts."""
    assert completion.complete_container(_ctx(), "") == ["bugfix", "feat-foo"]


def test_filters_by_what_was_typed(completion_repo):
    assert completion.complete_container(_ctx(), "fe") == ["feat-foo"]


def test_offers_full_names_once_the_prefix_is_typed(completion_repo):
    """A user who started typing `myrepo-` must not be left with an empty list."""
    assert completion.complete_container(_ctx(), "myrepo-f") == ["myrepo-feat-foo"]


def test_partial_prefix_offers_both_forms(completion_repo):
    """`my` is a prefix of the container prefix, so full names must appear."""
    assert completion.complete_container(_ctx(), "my") == [
        "myrepo-bugfix",
        "myrepo-feat-foo",
    ]


def test_excludes_other_repos_and_non_gie_containers(completion_repo):
    """`other-thing` and the registry mirror are not this repo's business."""
    offered = completion.complete_container(_ctx(), "")
    assert "other-thing" not in offered
    assert "jailbee-registry-mirror" not in offered


def test_uses_the_fast_bounded_query(completion_repo):
    """A TAB press must not fetch per-instance state, nor hang on a dead daemon."""
    _cfg, incus = completion_repo
    completion.complete_container(_ctx(), "")
    incus.list_containers.assert_called_once_with(
        fast=True,
        timeout=completion.QUERY_TIMEOUT,
    )


def test_returns_empty_when_no_config_can_be_loaded(tmp_path, mocker):
    """No config file *and* `scratch.enabled: false`: nothing to complete.

    "Outside a repo" is no longer the empty case on its own — a directory with
    no config file gets a synthesized one (see
    `test_completes_in_a_scratch_directory`). What is still empty is a loader
    that refuses, which `ConfigNotFoundError` is.
    """
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    assert completion.complete_container(_ctx(), "") == []


def test_completes_in_a_scratch_directory(tmp_path, mocker, monkeypatch):
    """A directory with no config file still completes its containers.

    The file-backed loader raised `ConfigNotFoundError` here, so tab-completion
    silently offered nothing in exactly the directories the scratch feature
    exists for. Asserts the *names*, not merely a non-empty list, and that the
    loader was handed the cwd.
    """
    from tests.conftest import make_config

    repo_root = tmp_path / "tutkimus"
    repo_root.mkdir()
    cfg = make_config(repo_root)
    assert cfg.container_prefix == "tutkimus"
    cfg._synthetic = True

    seen: list[object] = []

    def _fake(root: object) -> Any:
        seen.append(root)
        return cfg

    mocker.patch("jailbee.config.load_repo_config", _fake)
    incus = mocker.MagicMock()
    incus.list_containers.return_value = [
        _raw_container("tutkimus-feat-foo", "tutkimus-base", "tutkimus-net-strict"),
    ]
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    monkeypatch.chdir(repo_root)

    assert completion.complete_container(_ctx(), "") == ["feat-foo"]
    assert [Path(p).resolve() for p in seen] == [repo_root.resolve()]


def test_returns_empty_on_invalid_config(tmp_path, mocker):
    from jailbee.config import ConfigError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigError("bad yaml"),
    )
    assert completion.complete_container(_ctx(), "") == []


def test_returns_empty_on_undecodable_config(tmp_path, mocker):
    """A config file with invalid UTF-8 must not put a traceback on the prompt.

    config.py reads the file with Path.read_text() inside a try that only
    catches yaml.YAMLError (config.py:97), so the UnicodeDecodeError reaches
    the completer.
    """
    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    )
    assert completion.complete_container(_ctx(), "") == []


def test_returns_empty_when_incus_fails(completion_repo):
    """A missing binary, a dead daemon or an expired timeout all land here."""
    from jailbee.incus import IncusError

    _cfg, incus = completion_repo
    incus.list_containers.side_effect = IncusError("`incus list` timed out after 2s")
    assert completion.complete_container(_ctx(), "") == []


def test_returns_empty_on_malformed_json(completion_repo):
    """`json.loads` raising inside the wrapper must not reach the prompt."""
    _cfg, incus = completion_repo
    incus.list_containers.side_effect = ValueError("Expecting value")
    assert completion.complete_container(_ctx(), "") == []


# ---- branches -------------------------------------------------------------


def test_complete_branch_lists_local_branches(completion_repo, mocker):
    from subprocess import CompletedProcess

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(
            args=[],
            returncode=0,
            stdout="main\nfeat/foo\nfeat/bar\n",
            stderr="",
        ),
    )
    assert completion.complete_branch(_ctx(), "feat/") == ["feat/bar", "feat/foo"]


def test_complete_branch_empty_when_git_fails(completion_repo, mocker):
    from subprocess import CompletedProcess

    mocker.patch(
        "jailbee.git.subprocess.run",
        return_value=CompletedProcess(args=[], returncode=128, stdout="", stderr="nope"),
    )
    assert completion.complete_branch(_ctx(), "") == []


def test_complete_branch_empty_when_no_config_can_be_loaded(mocker):
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    assert completion.complete_branch(_ctx(), "") == []


# ---- pool names -------------------------------------------------------


def test_complete_pool_names_filters_by_what_was_typed(completion_repo, mocker):
    """Must narrow by prefix like every sibling completer (`complete_branch`,
    `complete_container`) — offering every pool regardless of what was typed
    would suggest `chrome-profile` for a user who typed `gr`."""
    from jailbee.config import PoolSpec
    from jailbee.pool import Pool

    cfg, _incus = completion_repo
    mocker.patch(
        "jailbee.pool.pools_for",
        return_value=[
            Pool(name="gradle", root=cfg.repo_root, container_path="~/.gradle", spec=PoolSpec()),
            Pool(
                name="chrome-profile",
                root=cfg.repo_root,
                container_path="~/.config/google-chrome",
                spec=PoolSpec(),
            ),
        ],
    )
    assert completion.complete_pool_names(_ctx(), "gr") == ["gradle"]
    assert completion.complete_pool_names(_ctx(), "") == ["gradle", "chrome-profile"]


def test_complete_pool_names_empty_when_no_config_can_be_loaded(mocker):
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    assert completion.complete_pool_names(_ctx(), "") == []


# ---- claude accounts ------------------------------------------------------


def test_complete_claude_account_offers_the_parked_slots_by_prefix(mocker):
    """Full slot names, narrowed by prefix — a name is always an exact match
    for `claude use`, while a bare email is ambiguous once one account has two
    stored logins."""
    from pathlib import Path

    from jailbee.claude_pool import Slot

    mocker.patch(
        "jailbee.claude_pool.parked_slots",
        return_value=[
            Slot("me@corp.com#c0ffee12", Path("/s/a.json"), live=False),
            Slot("other@x.com", Path("/s/b.json"), live=False),
        ],
    )
    assert completion.complete_claude_account(_ctx(), "me") == ["me@corp.com#c0ffee12"]
    assert completion.complete_claude_account(_ctx(), "") == [
        "me@corp.com#c0ffee12",
        "other@x.com",
    ]


def test_complete_claude_account_needs_no_repo_config(mocker):
    """The store is host-wide, so completion must not go through `_load()` —
    a TAB press outside a repo still has accounts to offer, and `list_slots`
    would load every registered repo's config to resolve holder members."""
    load = mocker.patch("jailbee.completion._load")
    mocker.patch("jailbee.claude_pool.parked_slots", return_value=[])
    assert completion.complete_claude_account(_ctx(), "") == []
    load.assert_not_called()


def test_complete_claude_account_survives_an_unreadable_store(mocker):
    """`_never_raises` is the contract for every completer: a TAB press must
    never traceback."""
    mocker.patch("jailbee.claude_pool.parked_slots", side_effect=OSError("boom"))
    assert completion.complete_claude_account(_ctx(), "") == []


# ---- snapshot tags --------------------------------------------------------


def test_complete_snapshot_lists_tags_of_the_typed_container(completion_repo):
    """`gie snapshot restore feat-foo <TAB>` reads the container from ctx.params."""
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = [{"name": "pre-upgrade"}, {"name": "clean"}]

    result = completion.complete_snapshot(_ctx(name="feat-foo"), "")

    assert result == ["clean", "pre-upgrade"]
    incus.snapshot_list.assert_called_once_with(
        "myrepo-feat-foo",
        timeout=completion.QUERY_TIMEOUT,
    )


def test_complete_snapshot_accepts_the_full_container_name(completion_repo):
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = [{"name": "clean"}]

    assert completion.complete_snapshot(_ctx(name="myrepo-feat-foo"), "") == ["clean"]


def test_complete_snapshot_filters_by_what_was_typed(completion_repo):
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = [{"name": "pre-upgrade"}, {"name": "clean"}]

    assert completion.complete_snapshot(_ctx(name="feat-foo"), "pre") == ["pre-upgrade"]


def test_complete_snapshot_empty_without_a_container(completion_repo):
    """No container typed yet: do not guess, and do not query."""
    _cfg, incus = completion_repo
    assert completion.complete_snapshot(_ctx(), "") == []
    incus.snapshot_list.assert_not_called()


def test_complete_snapshot_empty_for_unknown_container(completion_repo):
    _cfg, incus = completion_repo
    assert completion.complete_snapshot(_ctx(name="nope"), "") == []
    incus.snapshot_list.assert_not_called()


def test_complete_snapshot_empty_when_incus_fails(completion_repo):
    from jailbee.incus import IncusError

    _cfg, incus = completion_repo
    incus.snapshot_list.side_effect = IncusError("boom")
    assert completion.complete_snapshot(_ctx(name="feat-foo"), "") == []


def test_complete_snapshot_ignores_malformed_entries(completion_repo):
    """A snapshot dict without a string name is skipped, not crashed on."""
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = [{"name": "clean"}, {}, {"name": None}]

    assert completion.complete_snapshot(_ctx(name="feat-foo"), "") == ["clean"]


# ---- payload shapes json.loads accepts but the completer's own code does not
#
# These pin the escapes a whole-branch review found by running the real code:
# `[s.get("name") for s in snaps]` sits outside complete_snapshot's own
# `except (IncusError, ValueError, OSError)`, so any of these three shapes
# (each one `json.loads` happily produces from a malformed `incus` payload)
# raised straight through to the user's prompt before `_never_raises` existed.


def test_complete_snapshot_empty_for_list_of_str_payload(completion_repo):
    """`snaps` as a bare list of strings: `s.get` on a str raises AttributeError."""
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = ["clean", "pre-upgrade"]

    assert completion.complete_snapshot(_ctx(name="feat-foo"), "") == []


def test_complete_snapshot_empty_for_dict_payload(completion_repo):
    """`snaps` as a dict: iterating it yields keys (str), same AttributeError."""
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = {"clean": {"name": "clean"}}

    assert completion.complete_snapshot(_ctx(name="feat-foo"), "") == []


def test_complete_snapshot_empty_for_non_iterable_payload(completion_repo):
    """`snaps` as a non-iterable: `for s in snaps` raises TypeError."""
    _cfg, incus = completion_repo
    incus.snapshot_list.return_value = 42

    assert completion.complete_snapshot(_ctx(name="feat-foo"), "") == []


def test_complete_container_empty_when_incus_payload_is_missing_name(completion_repo):
    """A raw `incus list` entry without "name" reaches `raw["name"]` in
    lifecycle.list_containers and raises KeyError; _container_names does not
    catch it on its own (only IncusError/ValueError/OSError are caught there).
    """
    _cfg, incus = completion_repo
    incus.list_containers.return_value = [
        {
            "status": "Running",
            "profiles": ["myrepo-base", "myrepo-net-strict"],
            "config": {},
            "state": None,
        }
    ]

    assert completion.complete_container(_ctx(), "") == []


# ---- port handles ----------------------------------------------------------
#
# A whole-branch review found this completer wrote its own untimed
# `[c.name for c in list_containers(...)]` instead of reusing
# `_container_names`, and `list_forwards` had no `timeout` at all — so
# `jailbee port rm <TAB>` against a wedged daemon could hang the shell
# indefinitely, breaking the module's own "never blocks" contract.


def _proxy_device(listen: str, connect: str, bind: str = "instance") -> dict:
    return {"type": "proxy", "bind": bind, "listen": listen, "connect": connect}


def test_complete_port_handle_lists_devices_for_the_typed_container(completion_repo):
    _cfg, incus = completion_repo
    incus.list_containers.return_value = [
        {
            **_raw_container("myrepo-feat-foo", "myrepo-base", "myrepo-net-strict"),
            "devices": {
                "port-cfg-adb": _proxy_device("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
            },
        },
    ]
    assert completion.complete_port_handle(_ctx(name="feat-foo"), "") == ["port-cfg-adb"]


def test_complete_port_handle_filters_by_what_was_typed(completion_repo):
    _cfg, incus = completion_repo
    incus.list_containers.return_value = [
        {
            **_raw_container("myrepo-feat-foo", "myrepo-base", "myrepo-net-strict"),
            "devices": {
                "port-cfg-adb": _proxy_device("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
                "port-th-tcp-8080": _proxy_device(
                    "tcp:127.0.0.1:8080", "tcp:127.0.0.1:8080", bind="host"
                ),
            },
        },
    ]
    assert completion.complete_port_handle(_ctx(name="feat-foo"), "port-c") == ["port-cfg-adb"]


def test_complete_port_handle_without_a_container_unions_every_container(completion_repo):
    """No NAME typed yet on the command line: offer the union of every
    forward across this repo's containers, same as the module docstring
    describes."""
    _cfg, incus = completion_repo
    incus.list_containers.return_value = [
        {
            **_raw_container("myrepo-feat-foo", "myrepo-base", "myrepo-net-strict"),
            "devices": {
                "port-cfg-adb": _proxy_device("tcp:127.0.0.1:5037", "tcp:127.0.0.1:5037"),
            },
        },
        {
            **_raw_container("myrepo-bugfix", "myrepo-base", "myrepo-net-strict"),
            "devices": {
                "port-th-tcp-8080": _proxy_device(
                    "tcp:127.0.0.1:8080", "tcp:127.0.0.1:8080", bind="host"
                ),
            },
        },
    ]
    assert completion.complete_port_handle(_ctx(), "") == ["port-cfg-adb", "port-th-tcp-8080"]


def test_complete_port_handle_uses_the_shared_bounded_name_lookup(completion_repo):
    """Must reuse `_container_names` (fast + timeout), not a bespoke query
    that drops the timeout completely."""
    _cfg, incus = completion_repo
    completion.complete_port_handle(_ctx(), "")
    assert incus.list_containers.call_args_list[0].kwargs == {
        "fast": True,
        "timeout": completion.QUERY_TIMEOUT,
    }


def test_complete_port_handle_bounds_the_forwards_query_too(completion_repo):
    """The forwards lookup itself must also carry a timeout — previously
    `list_forwards` accepted none at all."""
    _cfg, incus = completion_repo
    completion.complete_port_handle(_ctx(), "")
    assert incus.list_containers.call_args_list[-1].kwargs == {
        "timeout": completion.QUERY_TIMEOUT,
    }


def test_complete_port_handle_empty_when_incus_fails(completion_repo):
    from jailbee.incus import IncusError

    _cfg, incus = completion_repo
    incus.list_containers.side_effect = IncusError("`incus list` timed out after 2s")
    assert completion.complete_port_handle(_ctx(), "") == []


# ---- fixed choices --------------------------------------------------------


def test_complete_choices_filters_by_prefix():
    complete = completion.complete_choices("table", "json")
    assert complete("j") == ["json"]


def test_complete_choices_offers_everything_when_nothing_typed():
    complete = completion.complete_choices("shell", "tmux", "none")
    assert complete("") == ["shell", "tmux", "none"]


# A prior version of this module asserted that Typer binds completion
# callback arguments *by name* ("must be named `incomplete`"). That premise is
# false — typer.main.get_param_completion binds by annotation first (see the
# module docstring in completion.py) — so a unit test against the bare
# callback's `inspect.signature` cannot tell "wired right" from "wired
# wrong" anyway: it would pass even if Typer's binding were broken, because it
# never goes through Typer. tests/test_completion_e2e.py replaces it with
# assertions driven through the real `click.shell_completion.ShellComplete`
# machinery, which is what actually binds these callbacks.
