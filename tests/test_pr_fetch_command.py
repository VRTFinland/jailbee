from __future__ import annotations

import pytest
import typer

from jailbee import pr, pr_flow
from tests.conftest import make_cfg


class _Incus:
    def __init__(self, labels: dict[str, str]) -> None:
        self.labels = labels

    def config_get(self, full: str, key: str) -> str:
        return self.labels.get(full, "") if key == "user.jailbee.pr" else ""


def _result(prev: str | None, new: str) -> pr.FetchResult:
    return pr.FetchResult(updated=prev != new, prev_sha=prev, new_sha=new, ref="r")


def test_fetching_one_container_refreshes_its_pr_head(tmp_path, mocker, capsys):
    cfg = make_cfg(tmp_path)
    refresh = mocker.patch("jailbee.pr.refresh_pr_head", return_value=_result("a" * 40, "b" * 40))

    pr_flow.fetch_container_pr_head(cfg, _Incus({"c-1": "7"}), "c-1", short="1")

    refresh.assert_called_once_with(cfg.repo_root, 7, remote=cfg.upstream_remote)
    captured = capsys.readouterr()
    assert "PR #7 head moved" in captured.err + captured.out


def test_a_container_without_a_pr_is_a_clear_error(tmp_path, mocker, capsys):
    cfg = make_cfg(tmp_path)
    refresh = mocker.patch("jailbee.pr.refresh_pr_head")

    with pytest.raises(typer.Exit) as exc:
        pr_flow.fetch_container_pr_head(cfg, _Incus({}), "c-1", short="1")

    assert exc.value.exit_code == 1
    refresh.assert_not_called()
    out = capsys.readouterr()
    assert "has no associated PR" in out.err + out.out


def test_a_failed_fetch_exits_1(tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    mocker.patch("jailbee.pr.refresh_pr_head", side_effect=pr.PrFetchError("denied"))

    with pytest.raises(typer.Exit) as exc:
        pr_flow.fetch_container_pr_head(cfg, _Incus({"c-1": "7"}), "c-1", short="1")

    assert exc.value.exit_code == 1


def _list(mocker, cfg, names):
    from types import SimpleNamespace

    mocker.patch(
        "jailbee.lifecycle.list_containers",
        return_value=[SimpleNamespace(name=n) for n in names],
    )
    mocker.patch("jailbee.lifecycle.short_name", side_effect=lambda _cfg, n: n)


def test_all_fetches_each_bound_pr_once_and_skips_unbound(tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    _list(mocker, cfg, ["a", "b", "c", "d"])
    refresh = mocker.patch("jailbee.pr.refresh_pr_head", return_value=_result("a" * 40, "a" * 40))

    pr_flow.fetch_all_pr_heads(cfg, _Incus({"a": "7", "b": "7", "c": "9"}))

    assert [call.args[1] for call in refresh.call_args_list] == [7, 9]


def test_all_keeps_going_after_a_failure_and_exits_1(tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    _list(mocker, cfg, ["a", "b"])
    refresh = mocker.patch(
        "jailbee.pr.refresh_pr_head",
        side_effect=[pr.PrFetchError("denied"), _result(None, "b" * 40)],
    )

    with pytest.raises(typer.Exit) as exc:
        pr_flow.fetch_all_pr_heads(cfg, _Incus({"a": "7", "b": "9"}))

    assert exc.value.exit_code == 1
    assert refresh.call_count == 2


def test_all_with_nothing_bound_is_not_an_error(tmp_path, mocker):
    cfg = make_cfg(tmp_path)
    _list(mocker, cfg, ["a"])
    mocker.patch("jailbee.pr.refresh_pr_head")

    pr_flow.fetch_all_pr_heads(cfg, _Incus({}))
