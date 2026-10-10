"""Tests for `pr_links`: the marker-block upsert and PR family linking."""

from __future__ import annotations

import json

import pytest

from jailbee.pr_links import upsert_marker_block

S, E = "<!-- jailbee:m -->", "<!-- /jailbee:m -->"


def test_appends_block_after_a_blank_line():
    assert upsert_marker_block("Hello.\n", "m", "x") == f"Hello.\n\n{S}\nx\n{E}"


def test_empty_body_becomes_the_block():
    assert upsert_marker_block("", "m", "x") == f"{S}\nx\n{E}"


def test_replaces_in_place_keeping_text_on_both_sides():
    body = f"Intro\n\n{S}\nold\n{E}\n\nOutro"
    assert upsert_marker_block(body, "m", "new") == f"Intro\n\n{S}\nnew\n{E}\n\nOutro"


def test_unchanged_returns_none():
    body = f"Intro\n\n{S}\nx\n{E}"
    assert upsert_marker_block(body, "m", "x") is None


def test_stray_unclosed_start_is_kept_and_a_block_appended():
    body = f"Intro {S} mentioned in prose\nMore text"
    out = upsert_marker_block(body, "m", "x")
    assert out == f"{body}\n\n{S}\nx\n{E}"
    # Second run pairs the closing marker with its nearest start, so the
    # prose before it survives.
    assert upsert_marker_block(out, "m", "y") == f"{body}\n\n{S}\ny\n{E}"


def test_other_markers_are_ignored():
    body = "<!-- jailbee:other -->\nz\n<!-- /jailbee:other -->"
    assert upsert_marker_block(body, "m", "x") == f"{body}\n\n{S}\nx\n{E}"


def test_replacement_preserves_prose_and_unmatched_closing_marker():
    body = f"{S}\nold\n{E}\n\nKeep this prose\n{E}"
    updated = f"{S}\nnew\n{E}\n\nKeep this prose\n{E}"

    assert upsert_marker_block(body, "m", "new") == updated
    assert upsert_marker_block(updated, "m", "new") is None


def _labels(mocker, tmp_path, labels, sub_map=None):
    cfg = mocker.MagicMock()
    cfg.repo_root = tmp_path
    cfg.upstream_remote = "origin"
    incus = mocker.MagicMock()
    values = dict(labels)
    if sub_map is not None:
        values["user.jailbee.sub_pr"] = json.dumps(sub_map)
    incus.config_get.side_effect = lambda name, key: values.get(key)
    mocker.patch(
        "jailbee.pr_outbox.scope_slug",
        side_effect=lambda scope: "acme/lib-a" if scope.subpath == "lib/a" else "acme/app",
    )
    mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
    body = mocker.patch("jailbee.pr.pr_body", return_value="Body.")
    edit = mocker.patch("jailbee.pr.edit_pr")
    return cfg, incus, body, edit


SUB = {"lib/a": {"pr": 7, "branch": "feat/a", "author": True, "adopted": False}}


def test_no_submodule_prs_makes_no_gh_call(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(mocker, tmp_path, {})
    link_pr_family(cfg, incus, "c", "s")
    body.assert_not_called()
    edit.assert_not_called()


def test_authored_superproject_gets_block_and_submodule_gets_part_of(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(
        mocker,
        tmp_path,
        {
            "user.jailbee.pr": "34",
            "user.jailbee.pr_author": "1",
            "user.jailbee.pr_branch": "feat/x",
        },
        SUB,
    )
    link_pr_family(cfg, incus, "c", "s")
    assert body.call_args_list[0].args == (tmp_path, 34)
    assert body.call_args_list[0].kwargs == {"repo": "acme/app"}
    assert edit.call_args_list[0].kwargs == {
        "repo": "acme/app",
        "body": "Body.\n\n<!-- jailbee:submodule-prs -->\n"
        "**Submodule PRs** (merge these first):\n- acme/lib-a#7 — `lib/a`\n"
        "<!-- /jailbee:submodule-prs -->",
    }
    assert edit.call_args_list[1].args == (tmp_path / "lib/a", 7)
    assert edit.call_args_list[1].kwargs == {
        "repo": "acme/lib-a",
        "body": "Body.\n\n<!-- jailbee:superproject-pr -->\nPart of acme/app#34\n"
        "<!-- /jailbee:superproject-pr -->",
    }


def test_foreign_superproject_is_not_edited(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(mocker, tmp_path, {"user.jailbee.pr": "34"}, SUB)
    info = mocker.patch("jailbee.pr_links.info")
    link_pr_family(cfg, incus, "c", "s")
    assert [c.args[1] for c in body.call_args_list] == [7]
    assert [c.args[1] for c in edit.call_args_list] == [7]
    info.assert_called_once_with("Submodule PRs for PR #34: acme/lib-a#7")


def test_foreign_submodule_body_is_never_read_or_edited(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(
        mocker,
        tmp_path,
        {"user.jailbee.pr": "34", "user.jailbee.pr_author": "1"},
        {"lib/a": {"pr": 7, "author": False, "adopted": True}},
    )
    link_pr_family(cfg, incus, "c", "s")
    assert [c.args[1] for c in body.call_args_list] == [34]
    assert [c.args[1] for c in edit.call_args_list] == [34]


def test_unchanged_body_is_not_rewritten(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(
        mocker, tmp_path, {"user.jailbee.pr": "34", "user.jailbee.pr_author": "1"}, SUB
    )
    current = {
        34: "B\n\n<!-- jailbee:submodule-prs -->\n**Submodule PRs** (merge these first):\n"
        "- acme/lib-a#7 — `lib/a`\n<!-- /jailbee:submodule-prs -->",
        7: "B\n\n<!-- jailbee:superproject-pr -->\nPart of acme/app#34\n"
        "<!-- /jailbee:superproject-pr -->",
    }
    body.side_effect = lambda root, n, repo=None: current[n]
    link_pr_family(cfg, incus, "c", "s")
    assert body.call_count == 2
    edit.assert_not_called()


@pytest.mark.parametrize("failure_at", ["body", "edit"])
def test_gh_failure_is_a_warning_and_other_prs_are_attempted(mocker, tmp_path, failure_at):
    from jailbee.pr import PrError
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(
        mocker, tmp_path, {"user.jailbee.pr": "34", "user.jailbee.pr_author": "1"}, SUB
    )
    failing = body if failure_at == "body" else edit
    failing.side_effect = PrError("boom")
    warn = mocker.patch("jailbee.pr_links.warn")
    link_pr_family(cfg, incus, "c", "s")
    assert [c.args[0] for c in warn.call_args_list] == [
        "Could not update the links in acme/app#34: boom",
        "Could not update the links in acme/lib-a#7: boom",
    ]


@pytest.mark.parametrize("failure_at", ["body", "edit"])
def test_gh_permission_error_warns_and_remaining_link_is_updated(mocker, tmp_path, failure_at):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(
        mocker, tmp_path, {"user.jailbee.pr": "34", "user.jailbee.pr_author": "1"}, SUB
    )
    failing = body if failure_at == "body" else edit
    failing.side_effect = [PermissionError("denied"), "Body."]
    warn = mocker.patch("jailbee.pr_links.warn")
    link_pr_family(cfg, incus, "c", "s")
    warn.assert_called_once_with("Could not update the links in acme/app#34: denied")
    assert body.call_args_list[-1].args == (tmp_path / "lib/a", 7)
    assert edit.call_args_list[-1].args == (tmp_path / "lib/a", 7)
    assert edit.call_args_list[-1].kwargs == {
        "repo": "acme/lib-a",
        "body": "Body.\n\n<!-- jailbee:superproject-pr -->\nPart of acme/app#34\n"
        "<!-- /jailbee:superproject-pr -->",
    }


def test_stacked_record_takes_precedence_over_main(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, _, edit = _labels(
        mocker,
        tmp_path,
        {
            "user.jailbee.pr": "34",
            "user.jailbee.pr_author": "1",
            "user.jailbee.stacked_pr": "56",
            "user.jailbee.stacked_pr_author": "1",
        },
        SUB,
    )
    link_pr_family(cfg, incus, "c", "s")
    assert [c.args[1] for c in edit.call_args_list] == [56, 7]
    assert "Part of acme/app#56" in edit.call_args_list[1].kwargs["body"]


@pytest.mark.parametrize("super_labels", [{}, {"user.jailbee.pr": "34"}])
def test_missing_superproject_number_or_slug_prevents_backlinks(mocker, tmp_path, super_labels):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(mocker, tmp_path, super_labels, SUB)
    if super_labels:
        mocker.patch(
            "jailbee.pr_outbox.scope_slug",
            side_effect=lambda scope: "acme/lib-a" if scope.subpath else None,
        )
    link_pr_family(cfg, incus, "c", "s")
    body.assert_not_called()
    edit.assert_not_called()


def test_entries_are_sorted_and_unresolvable_or_numberless_entries_skipped(mocker, tmp_path):
    from jailbee.pr_links import link_pr_family

    cfg, incus, _, edit = _labels(
        mocker,
        tmp_path,
        {"user.jailbee.pr": "34", "user.jailbee.pr_author": "1"},
        {
            "lib/z": {"pr": 9},
            "lib/a": SUB["lib/a"],
            "lib/missing": {"pr": 8},
            "lib/empty": {"branch": "feat/empty"},
        },
    )
    mocker.patch(
        "jailbee.pr_outbox.scope_slug",
        side_effect=lambda scope: {
            None: "acme/app",
            "lib/a": "acme/lib-a",
            "lib/z": "acme/lib-z",
        }.get(scope.subpath),
    )
    link_pr_family(cfg, incus, "c", "s")
    text = edit.call_args_list[0].kwargs["body"]
    assert "- acme/lib-a#7 — `lib/a`\n- acme/lib-z#9 — `lib/z`" in text
    assert "lib/missing" not in text
    assert "lib/empty" not in text


@pytest.mark.parametrize("failure_at", ["stacked", "main", "paths", "submodule"])
def test_incus_read_failure_warns_and_returns(mocker, tmp_path, failure_at):
    from jailbee.incus import IncusError
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(mocker, tmp_path, {}, SUB)
    if failure_at in {"stacked", "main"}:
        original = incus.config_get.side_effect
        key = "user.jailbee.stacked_pr" if failure_at == "stacked" else "user.jailbee.pr"

        def read(name, label):
            if label == key:
                raise IncusError("boom")
            return original(name, label)

        incus.config_get.side_effect = read
    else:
        target = "recorded_paths" if failure_at == "paths" else "SubmodulePrState.read"
        mocker.patch(f"jailbee.submodule_pr.{target}", side_effect=IncusError("boom"))
    warn = mocker.patch("jailbee.pr_links.warn")
    link_pr_family(cfg, incus, "c", "s")
    warn.assert_called_once()
    body.assert_not_called()
    edit.assert_not_called()


@pytest.mark.parametrize("key", ["user.jailbee.pr", "user.jailbee.stacked_pr"])
def test_malformed_superproject_label_warns_and_returns(mocker, tmp_path, key):
    from jailbee.pr_links import link_pr_family

    cfg, incus, body, edit = _labels(mocker, tmp_path, {key: "not-a-number"}, SUB)
    warn = mocker.patch("jailbee.pr_links.warn")
    link_pr_family(cfg, incus, "c", "s")
    warn.assert_called_once()
    body.assert_not_called()
    edit.assert_not_called()
