"""Tests for the Outbox manager controller (no terminal, a fake runner)."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import replace

from jailbee.dashboard import outbox as dob
from jailbee.dashboard.accounts import CliResult
from jailbee.dashboard.jobs import JobResult
from jailbee.dashboard.outbox_flow import OutboxController

C = "alpha-x"


def _action(index: int = 0, **over: object) -> dict[str, object]:
    return {
        "index": index,
        "kind": "comment",
        "repo": "o/r",
        "target": "42",
        "title": None,
        "body": f"body {index}",
        "markdown": True,
        "reply_to": None,
        "state": "pending",
        "receipt": None,
        "delete_scope": [index],
        "delete_block": None,
        "comments": [],
        **over,
    }


def _comment(index: int, text: str | None = None) -> dict[str, object]:
    return {
        "index": index,
        "label": f"c{index}",
        "text": text or f"comment {index}",
        "path": "a.py",
        "line": index + 1,
    }


def _proposal(pid: str = "pr/a.json", **over: object) -> dict[str, object]:
    return {
        "id": pid,
        "kind": pid.split("/")[0],
        "state": "pending",
        "revision": "r1",
        "raw_text": "{}",
        "error": None,
        "edit_block": None,
        "draftable": False,
        "deletable": True,
        "delete_block": None,
        "delete_archive": False,
        "actions": [_action(0), _action(1)],
        **over,
    }


def _listing(*proposals: dict[str, object], name: str = C, **over: object) -> str:
    container = {
        "name": name,
        "available": True,
        "error": None,
        "stores": [],
        "proposals": list(proposals),
        **over,
    }
    return json.dumps({"schema": 1, "containers": [container]})


def _key(pid: str = "pr/a.json", action: int | None = None, comment: int | None = None):
    return (C, pid, action, comment)


class FakeRunner:
    def __init__(
        self,
        *listings: str | CliResult,
        allow: Callable[[Sequence[str]], bool] = lambda argv: True,
    ) -> None:
        self.listings = list(listings)
        self.allow = allow
        self.calls: list[tuple[str, list[str]]] = []
        self.jobs: list[tuple[str, list[str], Callable[[JobResult], None]]] = []
        self.progress: list[Callable[[int, int], None] | None] = []
        self.drop_results: list[CliResult] = []
        self.start_error: Exception | None = None
        self.changes = 0
        self.hold_loads = False
        self.held: list[tuple[str, Callable[[], None]]] = []

    def permitted(self, argv: Sequence[str]) -> bool:
        return self.allow(argv)

    def run_quiet(self, argv: Sequence[str]) -> CliResult:
        self.calls.append(("quiet", list(argv)))
        if argv[1] == "ls":
            item = self.listings.pop(0) if len(self.listings) > 1 else self.listings[0]
            return CliResult(True, "", item) if isinstance(item, str) else item
        return self.drop_results.pop(0) if self.drop_results else CliResult(True, "")

    def run_quiet_later(
        self, key: str, label: str, argv: Sequence[str], on_done: Callable[[CliResult], None]
    ) -> None:
        """Delivered at once, unless `hold_loads`: then `finish_loads` delivers."""
        if self.start_error is not None:
            raise self.start_error
        result = self.run_quiet(argv)
        if self.hold_loads:
            self.held.append((key, lambda: on_done(result)))
        else:
            on_done(result)

    def finish_loads(self) -> None:
        held, self.held = self.held, []
        for _, deliver in held:
            deliver()

    def start(
        self,
        key: str,
        label: str,
        argv: Sequence[str],
        on_done: Callable[[JobResult], None],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> None:
        if self.start_error is not None:
            raise self.start_error
        self.calls.append(("start", list(argv)))
        self.jobs.append((key, list(argv), on_done))
        self.progress.append(on_progress)

    def hand_off(self, argv: Sequence[str]) -> int | None:
        self.calls.append(("hand_off", list(argv)))
        return 0

    def changed(self) -> None:
        self.changes += 1

    def argvs(self, verb: str) -> list[list[str]]:
        return [argv for _, argv in self.calls if argv[1] == verb]

    def finish(self, result: JobResult) -> None:
        _, _, on_done = self.jobs[-1]
        on_done(result)


def _controller(runner: FakeRunner, *, standalone: bool = False) -> OutboxController:
    seen: list[dob.OutboxState] = []
    ctl = OutboxController(
        runner, title="Outbox", container=C, standalone=standalone, on_change=seen.append
    )
    ctl.seen = seen  # type: ignore[attr-defined]  # test-only record of notifications
    return ctl


def _plan(**over: object) -> str:
    plan: dict[str, object] = {
        "schema": 1,
        "proposal": "pr/a.json",
        "kind": "pr",
        "revision": "r1",
        "login": "octo",
        "foreign": False,
        "draftable": True,
        "total": 2,
        "total_draft": 1,
        "warnings": [],
        "refusals": [],
        "notes": [],
        "actions": [
            {"index": 0, "kind": "review", "target": "o/r#42", "will": "publish",
             "will_draft": "draft"},
            {"index": 1, "kind": "comment", "target": "o/r#42", "will": "publish",
             "will_draft": "held_back"},
        ],
        **over,
    }  # fmt: skip
    return json.dumps(plan)


# --- load ------------------------------------------------------------------


def test_load_parses_listing_and_sets_abilities():
    runner = FakeRunner(_listing(_proposal()), allow=lambda argv: argv[1] != "clear")
    ctl = _controller(runner, standalone=True)
    assert ctl.load() is True
    assert runner.calls[0] == ("quiet", dob.outbox_ls_argv(C))
    assert [p.id for p in ctl.state.containers[0].proposals] == ["pr/a.json"]
    ab = ctl.state.abilities
    assert (ab.delete, ab.publish, ab.publish_hand_off, ab.create_pr, ab.clear, ab.scope) == (
        True, True, False, True, False, True,
    )  # fmt: skip
    assert ctl.seen == [ctl.state]  # type: ignore[attr-defined]


def test_load_parses_listing_even_when_ls_fails():
    runner = FakeRunner(CliResult(False, "exit 2", _listing(_proposal())))
    ctl = _controller(runner)
    assert ctl.load() is True
    assert ctl.state.containers[0].proposals[0].id == "pr/a.json"


def test_load_error_keeps_previous_rows():
    runner = FakeRunner(_listing(_proposal()), CliResult(False, "boom", "not json"))
    ctl = _controller(runner)
    ctl.load()
    before = ctl.state.containers
    assert ctl.load() is False
    assert ctl.state.containers == before
    assert ctl.state.status == ("could not list the outbox: boom",)


def test_load_prunes_expanded_and_marks_that_disappeared():
    runner = FakeRunner(
        _listing(_proposal(), _proposal("pr/b.json")), _listing(_proposal("pr/b.json"))
    )
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("expand", _key())
    ctl.intent("mark", _key("pr/b.json"))
    ctl.intent("mark", _key())
    ctl.load()
    assert ctl.state.expanded == frozenset()
    assert ctl.state.marks == frozenset({_key("pr/b.json")})


def test_unavailable_container_has_no_mutation_abilities():
    runner = FakeRunner(_listing(available=False, error="stopped"))
    ctl = _controller(runner)
    ctl.load()
    ab = ctl.state.abilities
    assert not (ab.delete or ab.publish or ab.publish_hand_off or ab.create_pr or ab.clear)
    assert ctl.state.containers[0].error == "stopped"


# --- marks -----------------------------------------------------------------


def test_mark_refuses_blocked_row_with_reason():
    runner = FakeRunner(
        _listing(_proposal(deletable=False, delete_block="publication evidence recorded"))
    )
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("mark", _key())
    assert ctl.state.marks == frozenset()
    assert ctl.state.status == ("pr/a.json: publication evidence recorded",)


def test_mark_toggles_markable_row():
    ctl = _controller(FakeRunner(_listing(_proposal())))
    ctl.load()
    ctl.intent("mark", _key())
    assert ctl.state.marks == frozenset({_key()})
    ctl.intent("mark", _key())
    assert ctl.state.marks == frozenset()


# --- delete ----------------------------------------------------------------


def test_delete_one_confirmation_lists_scope():
    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("delete", _key())
    q = ctl.state.question
    assert q is not None and q.purpose == "delete"
    assert q.title == "Delete from the outbox?"
    assert q.lines == dob.delete_scope_lines(ctl.state, [_key()])
    assert q.default == "no"
    assert q.choices == (("No", "no"), ("Yes, delete", "yes"))
    ctl.intent("answer", None, "no")
    assert ctl.state.question is None
    assert runner.argvs("drop") == []


def _tree() -> dict[str, object]:
    return _proposal(
        revision="r1",
        actions=[
            _action(0, kind="review", comments=[_comment(i) for i in range(4)]),
            _action(1),
            _action(2),
        ],
    )


def test_delete_marked_items_highest_index_first():
    first = _tree()
    # initial load, the check before the first drop, then one listing per drop
    revisions = ["r1", "r1", "r2", "r3", "r4"]
    runner = FakeRunner(*(_listing({**first, "revision": rev}) for rev in revisions))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("expand", _key())
    ctl.intent("expand", _key(action=0))
    for key in (_key(action=0, comment=1), _key(action=0, comment=3), _key(action=2)):
        ctl.intent("mark", key)
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    drops = runner.argvs("drop")
    assert drops == [
        dob.outbox_drop_argv(C, "pr/a.json", "r1", action=2),
        dob.outbox_drop_argv(C, "pr/a.json", "r2", action=0, comment=3),
        dob.outbox_drop_argv(C, "pr/a.json", "r3", action=0, comment=1),
    ]
    # Each drop is pinned to the listing loaded just before it.
    order = [argv[1] for _, argv in runner.calls]
    assert order == ["ls", "ls", "drop", "ls", "drop", "ls", "drop", "ls"]
    assert ctl.state.marks == frozenset()
    assert runner.changes == 1


def test_delete_mismatch_stops_that_proposal():
    a = _proposal("pr/x.json", actions=[_action(0), _action(1), _action(2)])
    b = _proposal("pr/y.json")
    changed = _proposal(
        "pr/x.json", revision="r2", actions=[_action(0, body="someone edited"), _action(1)]
    )
    runner = FakeRunner(
        _listing(a, b),  # initial load
        _listing(a, b),  # fresh check before the first drop
        _listing(changed, b),  # after dropping x action 2: action 0 changed
        _listing(changed, b),  # final reload
    )
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("expand", _key("pr/x.json"))
    ctl.intent("mark", _key("pr/x.json", 0))
    ctl.intent("mark", _key("pr/x.json", 2))
    ctl.intent("mark", _key("pr/y.json"))
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    drops = runner.argvs("drop")
    assert drops == [
        dob.outbox_drop_argv(C, "pr/x.json", "r1", action=2),
        dob.outbox_drop_argv(C, "pr/y.json", "r1"),
    ]
    assert "pr/x.json: changed; refreshed" in ctl.state.status


def test_delete_proposal_changed_before_first_drop_does_nothing_to_it():
    a = _proposal()
    runner = FakeRunner(_listing(a), _listing({**a, "revision": "r9"}))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("delete", _key())
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == []
    assert ctl.state.status == ("pr/a.json: changed; refreshed",)
    assert ctl.state.containers[0].proposals[0].revision == "r9"


def test_delete_failure_reports_message():
    runner = FakeRunner(_listing(_proposal()))
    runner.drop_results = [CliResult(False, "proposal changed; refresh required")]
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("delete", _key())
    ctl.intent("answer", None, "yes")
    assert ctl.state.status == ("pr/a.json: proposal changed; refresh required",)


def test_delete_issue_create_passes_with_dependents():
    issue = _proposal(
        "issue/i.json",
        actions=[
            _action(0, kind="create", title="T", delete_scope=[0, 1]),
            _action(1, kind="comment", target="", delete_scope=[1]),
        ],
    )
    runner = FakeRunner(_listing(issue))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("expand", _key("issue/i.json"))
    ctl.intent("delete", _key("issue/i.json", 0))
    assert ctl.state.question is not None
    assert ctl.state.question.lines == ("issue/i.json action 0 (create) + dependent 1",)
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == [
        dob.outbox_drop_argv(C, "issue/i.json", "r1", action=0, with_dependents=True)
    ]


def test_delete_whole_settled_issue_archives_journal():
    issue = _proposal("issue/i.json", state="applied", delete_archive=True)
    runner = FakeRunner(_listing(issue))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("mark", _key("issue/i.json"))
    ctl.intent("mark", _key("issue/i.json", 1))  # a child of a whole mark is folded away
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == [
        dob.outbox_drop_argv(C, "issue/i.json", "r1", archive_journal=True)
    ]


def test_delete_whole_takes_archive_consent_from_the_listing_shown():
    shown = _proposal("issue/i.json", state="applied", delete_archive=False)
    fresh = _proposal("issue/i.json", state="applied", delete_archive=True)
    runner = FakeRunner(_listing(shown), _listing(fresh))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("mark", _key("issue/i.json"))
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == [dob.outbox_drop_argv(C, "issue/i.json", "r1")]


# --- publish ---------------------------------------------------------------


def _publishing(plan: str, **proposal: object) -> tuple[FakeRunner, OutboxController]:
    runner = FakeRunner(_listing(_proposal(**proposal)))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("publish", _key())
    assert ctl.state.busy == "planning…"
    runner.finish(JobResult(0, "", plan))
    return runner, ctl


def test_publish_runs_plan_then_question():
    runner, ctl = _publishing(
        _plan(foreign=True, warnings=["PR #42 is not bound to container \x1b[31malpha"])
    )
    assert runner.jobs[0][0] == f"outbox-plan:{C}:pr/a.json"
    assert runner.jobs[0][1] == dob.outbox_plan_argv(C, "pr/a.json", "r1")
    q = ctl.state.question
    assert q is not None and q.purpose == "publish" and q.title == "Publish pr/a.json?"
    assert tuple(v for _, v in q.choices) == ("publish", "draft", "cancel")
    assert q.default == "cancel"
    assert q.lines[0] == "action 0 review → o/r#42: publish / draft: draft"
    assert "octo publishes 2 action(s)" in q.lines
    assert q.lines[-1] == "⚠ PR #42 is not bound to container [31malpha"
    assert ctl.state.busy is None


def test_publish_no_draft_choice_when_not_draftable():
    _, ctl = _publishing(_plan(draftable=False))
    assert ctl.state.question is not None
    assert tuple(v for _, v in ctl.state.question.choices) == ("publish", "cancel")


def test_publish_passes_foreign_only_when_plan_says_so():
    for foreign, choice in ((True, "publish"), (False, "draft")):
        runner, ctl = _publishing(_plan(foreign=foreign))
        ctl.intent("answer", None, choice)
        assert ctl.state.busy == "publishing…"
        key, argv, _ = runner.jobs[-1]
        assert key == f"outbox-apply:{C}:pr/a.json"
        assert argv == dob.outbox_apply_argv(
            C, "pr/a.json", "r1", yes=True, draft=choice == "draft", foreign=foreign
        )
        runner.finish(JobResult(0, "", "published 2 actions\n"))
        assert ctl.state.status == ("published 2 actions",)
        assert all("--force" not in argv for _, argv in runner.calls)
        assert runner.changes == 1


def test_publish_busy_shows_progress():
    runner, ctl = _publishing(_plan())
    ctl.intent("answer", None, "publish")
    progress = runner.progress[-1]
    assert progress is not None
    runner.finish(JobResult(0, "", "published"))
    assert ctl.state.busy is None
    ctl.intent("publish", _key())
    runner.finish(JobResult(0, "", _plan()))
    ctl.intent("answer", None, "publish")
    assert ctl.state.busy == "publishing…"
    progress(5, 5)
    assert ctl.state.busy == "publishing…"
    new_progress = runner.progress[-1]
    assert new_progress is not None
    new_progress(2, 5)
    assert ctl.state.busy == "publishing pr/a.json 2/5…"
    assert ctl.seen[-1].busy == ctl.state.busy


def test_publish_without_progress_keeps_plain_busy():
    _, ctl = _publishing(_plan())
    ctl.intent("answer", None, "publish")
    assert ctl.state.busy == "publishing…"


def test_publish_cancel_publishes_nothing():
    runner, ctl = _publishing(_plan())
    ctl.intent("cancel", None)
    assert ctl.state.question is None
    assert ctl.state.status == ("Nothing published.",)
    assert len(runner.jobs) == 1


def test_publish_stale_refusal_points_to_cli_force():
    _, ctl = _publishing(
        _plan(refusals=[{"reason": "the PR head moved", "stale": True}], actions=[])
    )
    assert ctl.state.question is None
    assert ctl.state.status == (
        "the PR head moved",
        "use `jailbee outbox apply --force` from a terminal",
    )


def test_publish_unparseable_plan_shows_failure_line():
    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("publish", _key())
    runner.finish(JobResult(1, "gh: not logged in\n", ""))
    assert ctl.state.status == ("gh: not logged in",)
    assert ctl.state.busy is None


def test_publish_revision_refused_refreshes():
    runner, ctl = _publishing(_plan())
    ctl.intent("answer", None, "publish")
    before = len(runner.argvs("ls"))
    runner.finish(JobResult(2, "proposal changed; refresh required\n"))
    assert "proposal changed; refresh required" in ctl.state.status
    assert len(runner.argvs("ls")) == before + 1


def test_publish_over_ssh_hands_off():
    runner = FakeRunner(
        _listing(_proposal()), allow=lambda argv: not (argv[1] == "apply" and "-y" in argv)
    )
    ctl = _controller(runner)
    ctl.load()
    assert ctl.state.abilities.publish is False
    assert ctl.state.abilities.publish_hand_off is True
    ctl.intent("publish", _key())
    assert runner.jobs == []
    assert runner.calls[-2] == (
        "hand_off", dob.outbox_apply_argv(C, "pr/a.json", "r1", yes=False)
    )  # fmt: skip
    assert runner.calls[-1][1][1] == "ls"
    assert runner.changes == 1


def test_publish_awaiting_pr_points_to_create():
    runner = FakeRunner(_listing(_proposal(state="awaiting-pr")))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("publish", _key())
    assert ctl.state.status == ("pr/a.json awaits its PR: press c to create it",)
    assert runner.jobs == []


# --- create PR -------------------------------------------------------------


def test_create_pr_rechecks_listing():
    awaiting = _proposal(state="awaiting-pr", create_scope={"kind": "repo"})
    runner = FakeRunner(_listing(awaiting), _listing({**awaiting, "revision": "r2"}))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("create-pr", _key())
    assert [c for c in runner.calls if c[0] == "hand_off"] == []
    assert ctl.state.status == ("Proposal changed; refresh the outbox before creating a PR",)


def test_create_pr_hands_off_when_unchanged():
    awaiting = _proposal(state="awaiting-pr", create_scope={"kind": "repo"})
    runner = FakeRunner(_listing(awaiting))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("create-pr", _key())
    assert ("hand_off", ["pr", "push", C]) in runner.calls
    assert runner.calls[-1][1][1] == "ls"
    assert runner.changes == 1


# --- clear -----------------------------------------------------------------


def _snapshot() -> str:
    from jailbee.outbox.clear_snapshot import encode_snapshot
    from jailbee.outbox.delete import ClearItem, ContainerClearPlan, DeleteSelection
    from jailbee.outbox.models import ProposalId

    item = ClearItem(ProposalId.parse("pr/a.json"), "a" * 64, "pending", DeleteSelection(), None)
    return encode_snapshot([ContainerClearPlan(C, True, None, (item,), (), ())])


def test_clear_prepares_then_asks_then_runs_approved():
    from jailbee.outbox.clear_snapshot import decode_snapshot
    from jailbee.outbox.delete import clear_plan_lines

    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("clear", _key())
    key, argv, _ = runner.jobs[-1]
    assert (key, argv) == (f"outbox-clear-prepare:{C}", dob.clear_prepare_argv(C))
    token = _snapshot()
    runner.finish(JobResult(0, "", token + "\n"))
    q = ctl.state.question
    assert q is not None and q.purpose == "clear"
    assert q.title == f"Clear the outbox of {C}?"
    assert q.lines == tuple(clear_plan_lines(decode_snapshot(token)))
    assert q.choices == (("No", "no"), ("Yes, clear 1", "yes"))
    assert q.default == "no"
    ctl.intent("answer", None, "yes")
    key, argv, _ = runner.jobs[-1]
    assert (key, argv) == (f"outbox-clear:{C}", dob.clear_approved_argv(C, token))
    runner.finish(JobResult(0, "", "deleted pr/a.json\nCleared 1 proposal.\n"))
    assert ctl.state.status == ("Cleared 1 proposal.",)
    assert runner.changes == 1


def test_clear_bad_snapshot_reports():
    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("clear", _key())
    runner.finish(JobResult(0, "", "garbage"))
    assert ctl.state.question is None
    assert ctl.state.status == (f"{C}: invalid approved clear snapshot",)
    assert ctl.state.busy is None


# --- busy / start failure --------------------------------------------------


def test_busy_blocks_second_action():
    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("publish", _key())
    for name in ("delete", "publish", "create-pr", "clear"):
        ctl.intent(name, _key())  # type: ignore[arg-type]  # loop over Intent names
        assert ctl.state.status == ("busy: planning…",)
        assert ctl.state.question is None
    assert len(runner.jobs) == 1


def test_start_failure_clears_busy():
    runner = FakeRunner(_listing(_proposal()))
    runner.start_error = OSError("no such file: jailbee")
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("publish", _key())
    assert ctl.state.busy is None
    assert ctl.state.status == ("no such file: jailbee",)


# --- navigation -------------------------------------------------------------


def test_expand_collapse_preview_raw_scope():
    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner, standalone=True)
    ctl.load()
    ctl.intent("expand", _key())
    assert _key() in ctl.state.expanded and ctl.state.cursor == _key()
    ctl.intent("collapse", _key(action=1))
    assert ctl.state.expanded == frozenset() and ctl.state.cursor == _key()
    ctl.intent("toggle", _key())
    assert _key() in ctl.state.expanded
    ctl.intent("preview", None)
    assert ctl.state.preview == "off"
    ctl.intent("preview", None)
    assert ctl.state.preview == "on"
    ctl.intent("raw", None)
    assert ctl.state.raw is True
    ctl.intent("scope", None)
    assert ctl.state.all_repos is True
    assert runner.calls[-1] == ("quiet", dob.outbox_ls_argv(C, all_repos=True))


# --- fix round 1 -------------------------------------------------------------


def test_load_while_delete_question_open_withdraws_it():
    a = _proposal(actions=[_action(0), _action(1)])
    other = _proposal(revision="r2", actions=[_action(0, body="different"), _action(1)])
    runner = FakeRunner(_listing(a), _listing(other))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("delete", _key(action=0))
    assert ctl.state.question is not None
    ctl.load()
    assert ctl.state.question is None
    assert ctl.state.status == ("outbox refreshed; ask again",)
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == []


def test_delete_checks_against_the_listing_the_question_showed():
    # A refresh that keeps the question (a frontend bypassing load) cannot steer
    # the drops: the snapshot taken when the question opened is what is checked.
    a = _proposal(actions=[_action(0), _action(1)])
    other = _proposal(revision="r2", actions=[_action(0, body="different"), _action(1)])
    runner = FakeRunner(_listing(a), _listing(other))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("delete", _key(action=0))
    question = ctl.state.question
    ctl.load()
    ctl.state = replace(ctl.state, question=question)
    ctl._pending_delete = (frozenset({_key(action=0)}), dob.parse_outbox_listing(_listing(a)))
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == []
    assert ctl.state.status == ("pr/a.json: changed; refreshed",)


def test_delete_vanished_proposal_says_changed():
    runner = FakeRunner(_listing(_proposal(), _proposal("pr/b.json")), _listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("delete", _key("pr/b.json"))
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == []
    assert ctl.state.status == ("pr/b.json: changed; refreshed",)


def test_delete_stops_when_a_dependent_appears_between_drops():
    shown = _proposal(
        "issue/i.json",
        actions=[
            _action(0, kind="create", title="T", delete_scope=[0]),
            _action(1, kind="comment", target="9", delete_scope=[1]),
        ],
    )
    grown = _proposal(
        "issue/i.json",
        revision="r2",
        actions=[
            _action(0, kind="create", title="T", delete_scope=[0, 2]),
            _action(1, kind="comment", target="9", delete_scope=[1]),
            _action(2, kind="comment", target="", body="new dependent", delete_scope=[2]),
        ],
    )
    runner = FakeRunner(_listing(shown), _listing(shown), _listing(grown))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("mark", _key("issue/i.json", 0))
    ctl.intent("mark", _key("issue/i.json", 1))
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    drops = runner.argvs("drop")
    assert drops == [dob.outbox_drop_argv(C, "issue/i.json", "r1", action=1)]
    assert all("--with-dependents" not in argv for argv in drops)
    assert ctl.state.status == ("issue/i.json: changed; refreshed",)


def test_delete_with_a_dependent_dropped_first_still_runs():
    shown = _proposal(
        "issue/i.json",
        actions=[
            _action(0, kind="create", title="T", delete_scope=[0, 1]),
            _action(1, kind="comment", target="", delete_scope=[1]),
        ],
    )
    after = _proposal(
        "issue/i.json",
        revision="r2",
        actions=[_action(0, kind="create", title="T", delete_scope=[0])],
    )
    runner = FakeRunner(_listing(shown), _listing(shown), _listing(after))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("mark", _key("issue/i.json", 0))
    ctl.intent("mark", _key("issue/i.json", 1))
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == [
        dob.outbox_drop_argv(C, "issue/i.json", "r1", action=1),
        dob.outbox_drop_argv(C, "issue/i.json", "r2", action=0),
    ]


def test_delete_failure_in_one_proposal_still_deletes_the_other():
    runner = FakeRunner(_listing(_proposal(), _proposal("pr/b.json")))
    runner.drop_results = [CliResult(False, "locked"), CliResult(True, "")]
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("mark", _key())
    ctl.intent("mark", _key("pr/b.json"))
    ctl.intent("delete", None)
    ctl.intent("answer", None, "yes")
    assert runner.argvs("drop") == [
        dob.outbox_drop_argv(C, "pr/a.json", "r1"),
        dob.outbox_drop_argv(C, "pr/b.json", "r1"),
    ]
    assert ctl.state.status == ("pr/a.json: locked",)


def _planned_status(plan: str) -> dob.OutboxState:
    _, ctl = _publishing(plan)
    return ctl.state


def test_publish_rejects_malformed_or_mismatched_plan():
    for plan in (
        _plan(schema=2),
        _plan(proposal="pr/other.json"),
        _plan(revision="r9"),
        _plan(actions=[]),
        json.dumps({"schema": 1, "proposal": "pr/a.json", "revision": "r1"}),
        _plan(total="2"),
    ):
        state = _planned_status(plan)
        assert state.question is None
        assert state.status == ("unexpected plan output",)


def test_publish_foreign_without_foreign_warning_gets_fallback_line():
    _, ctl = _publishing(_plan(foreign=True, warnings=["the PR head has moved"]))
    q = ctl.state.question
    assert q is not None
    assert q.lines[-2:] == (
        "⚠ the PR head has moved",
        "⚠ foreign target: check where this will be published",
    )
    _, ctl = _publishing(_plan(foreign=False, warnings=[]))
    assert ctl.state.question is not None
    assert not any(line.startswith("⚠") for line in ctl.state.question.lines)


def test_start_failure_notifies_once():
    runner = FakeRunner(_listing(_proposal()))
    runner.start_error = ValueError("refused")
    ctl = _controller(runner)
    ctl.load()
    seen = ctl.seen  # type: ignore[attr-defined]
    before = len(seen)
    ctl.intent("publish", _key())
    assert len(seen) == before + 1
    assert seen[-1].busy is None and seen[-1].status == ("refused",)


def test_clear_with_nothing_to_delete_asks_nothing():
    from jailbee.outbox.clear_snapshot import encode_snapshot
    from jailbee.outbox.delete import ClearItem, ContainerClearPlan
    from jailbee.outbox.models import ProposalId

    kept = ClearItem(ProposalId.parse("pr/a.json"), "a" * 64, "partial", None, "published")
    token = encode_snapshot([ContainerClearPlan(C, True, None, (), (kept,), ())])
    runner = FakeRunner(_listing(_proposal()))
    ctl = _controller(runner)
    ctl.load()
    ctl.intent("clear", _key())
    runner.finish(JobResult(0, "", token))
    assert ctl.state.question is None
    assert ctl.state.status[-1] == "Delete 0 proposals in 0 containers."
    assert len(runner.jobs) == 1


def test_scope_ability_follows_the_all_repos_listing_permission():
    refuse_all = FakeRunner(_listing(_proposal()), allow=lambda argv: "--all-repos" not in argv)
    ctl = _controller(refuse_all, standalone=True)
    ctl.load()
    assert ctl.state.abilities.scope is False
    ctl.intent("scope", None)
    assert ctl.state.all_repos is False
    assert all("--all-repos" not in argv for _, argv in refuse_all.calls)
    allowed = _controller(FakeRunner(_listing(_proposal())), standalone=True)
    allowed.load()
    assert allowed.state.abilities.scope is True


def _held(*listings: str | CliResult) -> tuple[FakeRunner, OutboxController, list[dob.OutboxState]]:
    runner = FakeRunner(*listings)
    runner.hold_loads = True
    seen: list[dob.OutboxState] = []
    ctl = OutboxController(runner, title="t", container=C, standalone=False, on_change=seen.append)
    return runner, ctl, seen


def test_load_later_is_busy_until_the_listing_arrives():
    runner, ctl, seen = _held(_listing(_proposal()))
    ctl.load_later()
    assert ctl.state.busy == "reading the outbox…" and ctl.state.containers == ()
    assert seen == [ctl.state]  # on screen at once
    runner.finish_loads()
    assert ctl.state.busy is None
    assert ctl.state.cursor == _key()
    assert ctl.state.abilities.delete is True


def test_load_later_failure_is_a_status_line():
    runner, ctl, _ = _held(CliResult(False, "refused", ""))
    ctl.load_later()
    runner.finish_loads()
    assert ctl.state.busy is None
    assert ctl.state.status == ("could not list the outbox: refused",)


def test_refresh_while_listing_starts_no_second_listing():
    runner, ctl, _ = _held(_listing(_proposal()))
    ctl.load_later()
    ctl.intent("refresh", None)
    assert len(runner.held) == 1 and len(runner.argvs("ls")) == 1
    runner.finish_loads()
    assert ctl.state.busy is None and ctl.state.containers


def test_scope_switched_while_listing_lists_the_new_scope():
    runner = FakeRunner(_listing(_proposal()), allow=lambda argv: True)
    runner.hold_loads = True
    ctl = OutboxController(
        runner, title="t", container=None, standalone=True, on_change=lambda s: None
    )
    ctl.load_later()
    runner.finish_loads()
    assert ctl.state.abilities.scope
    ctl.intent("scope", None)  # lists --all-repos
    ctl.intent("scope", None)  # back, while that is on its way
    runner.finish_loads()  # the --all-repos result is stale: listed again
    assert ctl.state.busy == "reading the outbox…"
    runner.finish_loads()
    ls = runner.argvs("ls")
    assert "--all-repos" in ls[1] and "--all-repos" not in ls[2]
    assert ctl.state.busy is None and not ctl.state.all_repos


def test_load_later_while_another_child_runs_lists_in_place():
    runner, ctl, _ = _held(_listing(_proposal()))
    ctl.state = replace(ctl.state, busy="publishing…")
    ctl.load_later()
    assert runner.held == [] and ctl.state.containers  # read synchronously
    assert ctl.state.busy == "publishing…"


def test_load_later_withdraws_an_open_question():
    runner, ctl, _ = _held(_listing(_proposal()))
    ctl.load_later()
    runner.finish_loads()
    ctl.intent("delete", _key())
    assert ctl.state.question is not None
    ctl.intent("refresh", None)
    assert ctl.state.question is None
    runner.finish_loads()
    assert ctl.state.status[0] == "outbox refreshed; ask again"
