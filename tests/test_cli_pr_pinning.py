"""Repository identity must survive PR lookup, text selection and mutation."""

import json
import os
from subprocess import CompletedProcess

import pytest
from typer.testing import CliRunner

from jailbee import pr_flow, pr_outbox
from jailbee.cli import app
from jailbee.pr_ai import PrText
from jailbee.submodule_pr import SubCandidate, SubPublishResult
from jailbee.sync import FetchResult, PublishResult


def _setup_command(mocker, tmp_path, *, submodule, authored=False):
    cfg = mocker.MagicMock()
    cfg.repo_root = tmp_path
    cfg.container_prefix = "sampleapp"
    cfg.upstream_remote = "origin"
    cfg.claude.enabled = False
    cfg.claude.ai_pr_description = True
    labels = {"user.jailbee.base_branch": "main", "user.jailbee.branch": "feat/foo"}
    if authored:
        labels.update(
            {
                "user.jailbee.pr": "42",
                "user.jailbee.pr_branch": "feat/foo",
                "user.jailbee.pr_author": "1",
            }
        )
    incus = mocker.MagicMock()
    incus.config_get.side_effect = lambda name, key: labels.get(key)
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._resolve_existing", return_value=(incus, "sampleapp-feat-foo"))
    mocker.patch("jailbee.lifecycle.short_name", return_value="feat-foo")
    mocker.patch("jailbee.lifecycle.container_repo_dir", return_value="/home/dev/repo")
    mocker.patch("jailbee.lifecycle._stdin_is_interactive", return_value=False)
    mocker.patch("jailbee.sync.assert_container_publishable", return_value="sampleapp-feat-foo")
    mocker.patch("jailbee.pr.find_pr_for_branch", return_value=None)
    mocker.patch("jailbee.git.commit_subject", return_value="feat: work")
    mocker.patch("jailbee.cli._offer_outbox_comments", return_value=0)
    if submodule:
        root, slug = tmp_path / "lib/a", "acme/lib-a"
        args = ["submodule", "pr", "feat-foo"]
        mocker.patch(
            "jailbee.submodule_pr.detect_candidates",
            return_value=[
                SubCandidate(
                    path="lib/a",
                    commits=2,
                    branch="feat/foo",
                    dirty=False,
                    head_sha="aaa",
                    recorded_sha="aaa",
                    subject="feat: work",
                )
            ],
        )
        mocker.patch("jailbee.submodule_pr.transport_submodule_to_host")
        mocker.patch("jailbee.submodule_pr.resolve_remote", return_value="origin")
        mocker.patch("jailbee.submodule_pr.resolve_base_branch", return_value="main")
        mocker.patch(
            "jailbee.submodule_pr.SubmodulePrState.read",
            return_value=pr_flow.PrRecord(42, "feat/foo", True, False)
            if authored
            else pr_flow.PrRecord(None, None, False, False),
        )
        mocker.patch("jailbee.pr.assert_github_remote")
        mocker.patch(
            "jailbee.submodule_pr.publish_submodule_branch",
            return_value=SubPublishResult(src_ref="r", publish_name="feat/foo", forced=False),
        )
    else:
        root, slug = tmp_path, "acme/widgets"
        args = ["pr", "feat-foo"]
        mocker.patch(
            "jailbee.sync.publish_branch_from_container",
            return_value=PublishResult(
                fetch=FetchResult(
                    branch="feat/foo",
                    old_oid="aaa",
                    new_oid="bbb",
                    base_oid="aaa",
                    commits_added=2,
                ),
                dirty=False,
                publish_name="feat/foo",
                forced=False,
            ),
        )
    mocker.patch("jailbee.git.get_remote_url", return_value=f"https://github.com/{slug}")
    mocker.patch.dict("os.environ", {"GH_REPO": "unrelated/default"})
    return cfg, incus, args, root, slug


@pytest.mark.parametrize("submodule", [False, True], ids=["pr", "submodule-pr"])
@pytest.mark.parametrize("as_name", [False, True], ids=["branch", "as"])
def test_already_exists_without_outbox_hint_uses_scoped_number(
    mocker, tmp_path, submodule, as_name
):
    cfg, incus, args, root, slug = _setup_command(mocker, tmp_path, submodule=submodule)
    files = {
        f"{number}-description.json": json.dumps(
            {
                "version": 1,
                "repo": slug,
                "pr": number,
                "head_sha": None,
                "actions": [
                    {
                        "type": "description",
                        "title": f"Title for {number}",
                        "body": f"Body for {number}",
                    }
                ],
            }
        )
        for number in (42, 99)
    }
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=pr_outbox.Outbox(files=files))
    selected = mocker.spy(pr_outbox, "pending_pr_text")
    plan = mocker.spy(pr_flow, "resolve_pr_text_and_head")
    consumed = mocker.patch("jailbee.pr_outbox.record_consumed")
    commands, edits = [], []

    def run(cmd, **kwargs):
        if cmd[:3] == ["git", "check-ref-format", "--branch"]:
            return CompletedProcess(cmd, 0, "", "")
        assert kwargs["cwd"] == root
        if cmd[0] == "git":
            assert cmd == ["git", "remote", "get-url", "origin"]
            return CompletedProcess(cmd, 0, f"https://github.com/{slug}", "")
        commands.append(cmd)
        repo = cmd[cmd.index("--repo") + 1] if "--repo" in cmd else os.environ["GH_REPO"]
        if cmd[:3] == ["gh", "pr", "create"]:
            return CompletedProcess(cmd, 1, "", "already exists")
        if cmd[:3] == ["gh", "pr", "view"]:
            number = 42 if repo == slug else 99
            return CompletedProcess(
                cmd,
                0,
                json.dumps({"number": number, "url": f"https://github.com/{repo}/pull/{number}"}),
                "",
            )
        assert cmd[:3] == ["gh", "pr", "edit"]
        edits.append((repo, cmd[3], cmd[cmd.index("--title") + 1], cmd[cmd.index("--body") + 1]))
        return CompletedProcess(cmd, 0, "", "")

    mocker.patch("subprocess.run", side_effect=run)
    result = CliRunner().invoke(app, args + (["--as", "feat/foo"] if as_name else []))

    assert result.exit_code == 0, result.output
    assert plan.spy_return.outbox_source is None
    assert edits == [(slug, "42", "Title for 42", "Body for 42")]
    assert [call.kwargs.get("for_pr") for call in selected.call_args_list] == [None, 42]
    assert all(cmd[cmd.index("--repo") + 1] == slug for cmd in commands)
    consumed.assert_called_once_with(
        incus,
        "sampleapp-feat-foo",
        "42-description.json",
        0,
        f"https://github.com/{slug}/pull/42",
        uid=cfg.container_user.uid,
    )
    assert "#42" in result.output and "#99" not in result.output


@pytest.mark.parametrize("submodule", [False, True], ids=["pr", "submodule-pr"])
@pytest.mark.parametrize("no_outbox", [False, True], ids=["outbox", "no-outbox"])
@pytest.mark.parametrize(
    ("flags", "edit_fields", "ready"),
    [
        (["--title", "Typed title"], {"--title": "Typed title"}, None),
        (["--body", "Typed body"], {"--body": "Typed body"}, None),
        (["--description"], {"--title": "Generated title", "--body": "Generated body"}, None),
        (["--ready"], {}, True),
        (["--draft"], {}, False),
        (["--title", "Typed title", "--ready"], {"--title": "Typed title"}, True),
    ],
    ids=["title", "body", "generated", "ready", "draft", "title-and-ready"],
)
def test_update_mutations_keep_lookup_repository(
    mocker, tmp_path, submodule, no_outbox, flags, edit_fields, ready
):
    cfg, _incus, args, root, slug = _setup_command(
        mocker, tmp_path, submodule=submodule, authored=True
    )
    cfg.claude.enabled = True
    mocker.patch("jailbee.pr_outbox.read_outbox", return_value=pr_outbox.Outbox(files={}))
    mocker.patch(
        "jailbee.pr_ai.generate_pr_text",
        return_value=PrText(title="Generated title", body="Generated body", branch="feat/foo"),
    )
    consumed = mocker.patch("jailbee.pr_outbox.record_consumed")
    lookups, mutations = [], []

    def run(cmd, **kwargs):
        assert kwargs["cwd"] == root
        repo = cmd[cmd.index("--repo") + 1] if "--repo" in cmd else os.environ["GH_REPO"]
        if cmd[:3] == ["gh", "pr", "view"]:
            number = 42 if repo == slug else 99
            lookups.append((repo, str(number)))
            return CompletedProcess(
                cmd,
                0,
                json.dumps({"number": number, "url": f"https://github.com/{repo}/pull/{number}"}),
                "",
            )
        assert cmd[:3] in (["gh", "pr", "edit"], ["gh", "pr", "ready"])
        mutations.append((repo, cmd[3], cmd))
        return CompletedProcess(cmd, 0, "", "")

    mocker.patch("subprocess.run", side_effect=run)
    result = CliRunner().invoke(app, args + flags + (["--no-outbox"] if no_outbox else []))

    assert result.exit_code == 0, result.output
    target = ("unrelated/default", "99") if no_outbox else (slug, "42")
    assert lookups == [target]
    assert [(repo, number) for repo, number, _cmd in mutations] == [target] * (
        bool(edit_fields) + (ready is not None)
    )
    if edit_fields:
        edit = next(cmd for _repo, _number, cmd in mutations if cmd[2] == "edit")
        for flag in ("--title", "--body"):
            assert (edit[edit.index(flag) + 1] if flag in edit else None) == edit_fields.get(flag)
    if ready is not None:
        state = next(cmd for _repo, _number, cmd in mutations if cmd[2] == "ready")
        assert ("--undo" in state) is (not ready)
    consumed.assert_not_called()
