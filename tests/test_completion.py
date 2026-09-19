"""Tests for the shell-completion callbacks.

Fully mocked: no incus daemon, no git, no filesystem beyond tmp_path. The
recurring assertion is the module's contract — a completer never raises and
returns [] when anything at all goes wrong.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jailbee import completion
from tests.conftest import _raw_container

# The `completion_repo` fixture (fabricated repo config + MagicMock Incus) and
# the `_raw_container` payload helper live in tests/conftest.py — shared with
# tests/test_completion_e2e.py, which drives the same completers through the
# real Typer/Click command tree instead of calling them directly.


def _ctx(**params: Any) -> Any:
    """A stand-in for click's Context: completers only read `.params`."""
    return SimpleNamespace(params=params)


def _adapter(name: str) -> SimpleNamespace:
    """A stand-in for an adapter: the completers only read `.name`."""
    return SimpleNamespace(name=name)


@pytest.fixture
def group_store(tmp_path, monkeypatch):
    """A private XDG data home, so a group test starts with no directories.

    The session-wide `isolated_xdg_data_home` fixture is shared by every test,
    so a group test that created directories would leak them into the next one
    and make "the store is absent" unobservable.
    """
    root = tmp_path / "xdg-data"
    root.mkdir()
    monkeypatch.setenv("XDG_DATA_HOME", str(root))
    return root


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


# ---- app names --------------------------------------------------------


def test_complete_app_name_filters_by_what_was_typed(completion_repo, mocker):
    """Must narrow by prefix like every sibling completer — offering every
    app regardless of what was typed would suggest `idea` for a user who
    typed `fi`."""
    from jailbee.apps import AppSpec

    mocker.patch(
        "jailbee.apps.resolve_apps",
        return_value=[
            AppSpec(name="figma", command=["/opt/f/f"]),
            AppSpec(name="idea", command=["/opt/idea/bin/idea"]),
        ],
    )
    assert completion.complete_app_name(_ctx(), "fi") == ["figma"]
    assert completion.complete_app_name(_ctx(), "") == ["figma", "idea"]


def test_complete_app_name_empty_when_no_config_can_be_loaded(mocker):
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    assert completion.complete_app_name(_ctx(), "") == []


# ---- accounts -------------------------------------------------------------


def test_complete_account_offers_the_parked_slots_by_prefix(completion_repo, mocker):
    """Full slot names, narrowed by prefix — a name is always an exact match
    for `account use`, while a bare email is ambiguous once one account has two
    stored logins."""
    from jailbee.accounts.models import Slot

    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )
    mocker.patch(
        "jailbee.accounts.engine.parked_slots",
        return_value=[
            Slot("me@corp.com#c0ffee12", Path("/s/a.json"), live=False),
            Slot("other@x.com", Path("/s/b.json"), live=False),
        ],
    )
    assert completion.complete_account(_ctx(), "me") == ["me@corp.com#c0ffee12"]
    assert completion.complete_account(_ctx(), "") == [
        "me@corp.com#c0ffee12",
        "other@x.com",
    ]


def test_complete_account_combines_and_deduplicates_every_pooled_adapter(completion_repo, mocker):
    """No `--agent`: the union of every enabled agent's parked store.

    One email can legitimately be parked in two agents' stores, so the union is
    a set, not a concatenation.
    """
    from jailbee.accounts.models import Slot

    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude"), _adapter("codex")],
    )
    stores = {
        "claude": [Slot("me@corp.com", Path("/s/a.json"), live=False)],
        "codex": [
            Slot("me@corp.com", Path("/s/b.json"), live=False),
            Slot("other@x.com", Path("/s/c.json"), live=False),
        ],
    }
    mocker.patch(
        "jailbee.accounts.engine.parked_slots",
        side_effect=lambda adapter: stores[adapter.name],
    )
    assert completion.complete_account(_ctx(), "") == ["me@corp.com", "other@x.com"]


def test_complete_account_narrows_to_the_typed_agent(completion_repo, mocker):
    """`ctx.params["agent"]` is what the parsed `--agent` value becomes."""
    from jailbee.accounts.models import Slot

    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude"), _adapter("codex")],
    )
    parked = mocker.patch(
        "jailbee.accounts.engine.parked_slots",
        return_value=[Slot("only@codex.com", Path("/s/c.json"), live=False)],
    )
    assert completion.complete_account(_ctx(agent="codex"), "") == ["only@codex.com"]
    assert parked.call_count == 1
    assert parked.call_args.args[0].name == "codex"


def test_complete_account_resolves_an_explicit_agent_without_a_repo(mocker):
    """Outside a repo the parked store is still host-wide and worth offering.

    `_load()` cannot discover enabled adapters there, so an explicit `--agent`
    is resolved directly instead — the one case where the store outlives the
    config that names its agent.
    """
    from jailbee.accounts.models import Slot
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    mocker.patch(
        "jailbee.accounts.adapters.base.get_adapter",
        return_value=_adapter("claude"),
    )
    mocker.patch(
        "jailbee.accounts.engine.parked_slots",
        return_value=[Slot("me@corp.com", Path("/s/a.json"), live=False)],
    )
    assert completion.complete_account(_ctx(agent="claude"), "") == ["me@corp.com"]


def test_complete_account_empty_without_a_repo_and_no_agent(mocker):
    """Without `--agent` there is no way to know which agents exist."""
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    parked = mocker.patch("jailbee.accounts.engine.parked_slots", return_value=[])
    assert completion.complete_account(_ctx(), "") == []
    parked.assert_not_called()


def test_complete_account_empty_when_the_config_cannot_be_loaded(mocker):
    from jailbee.config import ConfigError

    mocker.patch("jailbee.config.load_repo_config", side_effect=ConfigError("bad yaml"))
    assert completion.complete_account(_ctx(), "") == []


def test_complete_account_survives_an_unreadable_store(completion_repo, mocker):
    """`_completion_guard` is the contract for every completer: a TAB press must
    never traceback."""
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )
    mocker.patch("jailbee.accounts.engine.parked_slots", side_effect=OSError("boom"))
    assert completion.complete_account(_ctx(), "") == []


def test_complete_account_prints_nothing_when_the_store_fails(completion_repo, mocker, capsys):
    """A store failure must return [] *and* leave stdout/stderr untouched."""
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )

    def noisy(*_args: Any, **_kwargs: Any) -> Any:
        print("store advisory")
        raise OSError("boom")

    mocker.patch("jailbee.accounts.engine.parked_slots", side_effect=noisy)
    capsys.readouterr()
    assert completion.complete_account(_ctx(), "") == []
    assert capsys.readouterr() == ("", "")


# ---- agent names ----------------------------------------------------------


def test_complete_account_agent_offers_every_enabled_agent(completion_repo, mocker):
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude"), _adapter("codex")],
    )
    assert completion.complete_account_agent(_ctx(), "") == ["claude", "codex"]
    assert completion.complete_account_agent(_ctx(), "co") == ["codex"]


def test_complete_account_agent_empty_when_no_config_can_be_loaded(mocker):
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    assert completion.complete_account_agent(_ctx(), "") == []


def test_complete_account_agent_survives_an_adapter_failure(completion_repo, mocker):
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        side_effect=RuntimeError("boom"),
    )
    assert completion.complete_account_agent(_ctx(), "") == []


# ---- credential groups ----------------------------------------------------


def _group_dirs(*names: str, agent: str = "claude") -> None:
    from jailbee.accounts.groups import group_dir

    for name in names:
        group_dir(agent, name).mkdir(parents=True, exist_ok=True)


def test_complete_credential_group_unions_every_adapter_plus_none(
    completion_repo, group_store, mocker
):
    """One group name is one directory per agent; the union is what a user sees."""
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude"), _adapter("codex")],
    )
    _group_dirs("work", "personal", agent="claude")
    _group_dirs("work", "team", agent="codex")

    assert completion.complete_credential_group(_ctx(), "") == [
        "none",
        "personal",
        "team",
        "work",
    ]


def test_complete_credential_group_ignores_the_store_directory(
    completion_repo, group_store, mocker
):
    """`_parked` is not a group — the leading underscore keeps it out, the same
    property `engine.store_dir` relies on for its own name."""
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )
    _group_dirs("work")
    _group_dirs("_parked")

    assert completion.complete_credential_group(_ctx(), "") == ["none", "work"]


def test_complete_credential_group_narrows_to_the_typed_agent(completion_repo, group_store, mocker):
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude"), _adapter("codex")],
    )
    _group_dirs("claude-only", agent="claude")
    _group_dirs("codex-only", agent="codex")

    assert completion.complete_credential_group(_ctx(agent="codex"), "") == [
        "codex-only",
        "none",
    ]


def test_complete_credential_group_filters_by_prefix(completion_repo, group_store, mocker):
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )
    _group_dirs("work", "personal")

    assert completion.complete_credential_group(_ctx(), "pe") == ["personal"]


def test_complete_credential_group_offers_none_when_the_store_is_absent(
    completion_repo, group_store, mocker
):
    """A store that does not exist yet is an empty pool, not a failure."""
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )
    assert completion.complete_credential_group(_ctx(), "") == ["none"]


def test_complete_credential_group_empty_when_no_adapter_is_enabled(completion_repo, mocker):
    """No enabled adapter means no directory to enumerate — not even `none`."""
    mocker.patch("jailbee.accounts.adapters.base.pooled_adapters", return_value=[])
    assert completion.complete_credential_group(_ctx(), "") == []


def test_complete_credential_group_empty_when_no_config_can_be_loaded(mocker):
    from jailbee.config import ConfigNotFoundError

    mocker.patch(
        "jailbee.config.load_repo_config",
        side_effect=ConfigNotFoundError("no config"),
    )
    assert completion.complete_credential_group(_ctx(), "") == []


def test_complete_credential_group_empty_when_the_store_is_unreadable(
    completion_repo, tmp_path, mocker
):
    """A store that cannot be read is a failure, not an empty pool: fail closed
    rather than offer `none` for a host whose groups could not be enumerated."""
    mocker.patch(
        "jailbee.accounts.adapters.base.pooled_adapters",
        return_value=[_adapter("claude")],
    )
    not_a_dir = tmp_path / "claude-credentials"
    not_a_dir.write_text("not a directory")
    mocker.patch("jailbee.accounts.groups.group_dir", return_value=not_a_dir / "x")

    assert completion.complete_credential_group(_ctx(), "") == []


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
# raised straight through to the user's prompt before `_completion_guard` existed.


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


# ---- the silence contract -------------------------------------------------


def test_the_guard_swallows_everything_a_completer_prints(capsys):
    """Stdout is the completion protocol; stderr lands on the prompt line.

    Completers call into config loading, `lifecycle` and `pool`, all of which
    are free to print advisories (`tui.warn_plain` for the pre-1.0 `.gie/`
    directory, `tui.hint` for a legacy `chrome:` block). In a normal command
    that is the point; in a completion process it corrupts the channel. The
    guard is the one place that can make every completer quiet at once, so it
    does, and tests/test_completion_e2e.py checks the same contract end to end.
    """

    @completion._completion_guard
    def noisy(incomplete: str) -> list[str]:
        import sys

        print("on stdout")
        print("on stderr", file=sys.stderr)
        return ["value"]

    capsys.readouterr()
    assert noisy("") == ["value"]
    assert capsys.readouterr() == ("", "")


def test_the_guard_swallows_output_printed_before_a_raise(capsys):
    """The two duties compose: a completer that prints *and then* blows up
    still yields `[]` and a clean channel, rather than leaking the half-written
    advisory it managed to emit first."""

    @completion._completion_guard
    def noisy_and_broken(incomplete: str) -> list[str]:
        print("on stdout")
        raise RuntimeError("boom")

    capsys.readouterr()
    assert noisy_and_broken("") == []
    assert capsys.readouterr() == ("", "")


# A prior version of this module asserted that Typer binds completion
# callback arguments *by name* ("must be named `incomplete`"). That premise is
# false — typer.main.get_param_completion binds by annotation first (see the
# module docstring in completion.py) — so a unit test against the bare
# callback's `inspect.signature` cannot tell "wired right" from "wired
# wrong" anyway: it would pass even if Typer's binding were broken, because it
# never goes through Typer. tests/test_completion_e2e.py replaces it with
# assertions driven through the real `click.shell_completion.ShellComplete`
# machinery, which is what actually binds these callbacks.
