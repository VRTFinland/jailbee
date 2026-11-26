"""The terminal dashboard sends background actions to its state client."""

from jailbee.dashboard.model import RepoGroup
from tests.dashboard_fixtures import ci
from tests.dashboard_pilot import drive, keys


def test_menu_action_is_sent_to_the_state_service(mocker, tmp_path):
    group = RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])
    mocker.patch("subprocess.run", return_value=mocker.Mock(returncode=0))
    seen = []

    def inspect(app):
        seen.extend(app.session.client.action_requests)
        assert any(entry.finished for entry in app.session.queue)
        assert "output of jailbee" in next(iter(app.session.queue)).output

    drive(mocker, ["j", "!", *keys("start alpha-a"), "enter", inspect], [group])
    assert len(seen) == 1
    assert seen[0].argv == ["start", "alpha-a"]
    assert seen[0].target == "alpha-a"
    assert seen[0].repo_root == str(tmp_path)


def test_other_dashboard_critical_action_is_scoped_and_has_no_modal(mocker, tmp_path):
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope
    from jailbee.state_service.protocol import ActionEntry
    from tests.dashboard_pilot import FakeStateClient

    groups = [RepoGroup("alpha", str(tmp_path), None, [ci("alpha-a", "alpha")])]
    client = FakeStateClient(groups)
    client._action_entries = {
        51: ActionEntry(51, ["merge", "alpha-a"], str(tmp_path), "alpha-a", True, "other-dashboard", "none", str(tmp_path), "alpha"),
        52: ActionEntry(52, ["ls"], str(tmp_path), "private-a", False, "other-dashboard", "none", str(tmp_path), "private"),
    }

    def inspect(app):
        app.session.tick()
        assert {entry.id for entry in app.session.queue} == {51}
        assert not app.modal_open
        assert app.session.queue.blocking_entry() is None

    drive(mocker, [inspect], groups, client=client, scope=RemoteRepoScope(frozenset({"private"})))
