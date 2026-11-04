import json

import pytest
from typer.testing import CliRunner

from jailbee.container_cli import container_app
from jailbee.container_outbox.store import Store


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('JAILBEE_ROLE', raising=False)
    return tmp_path


def invoke(*args):
    return CliRunner().invoke(container_app, ['outbox', *args])


def test_empty_list_and_child_guard(home, monkeypatch):
    result = invoke('ls', '--json')
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {'issue': [], 'pr': [], 'drafts': []}
    monkeypatch.setenv('JAILBEE_ROLE', 'child')
    for command in ('drop', 'submit', 'discard'):
        result = invoke(command)
        assert result.exit_code == 1
        assert 'child container' in result.output
    assert not (home / '.jailbee').exists()


def test_ambiguous_draft(home):
    store = Store(home)
    for kind in ('issue', 'pr'):
        store.draft_save(kind, 'same', {'version': 1, 'actions': []}, {})
    result = invoke('submit', 'same')
    assert result.exit_code == 2
    assert '--kind' in result.output


def test_issue_batch_cli_roundtrip_and_drop(home):
    from jailbee.issue_manifest import parse_manifest
    runner = CliRunner()
    def run(*args):
        result = runner.invoke(container_app, list(args))
        assert result.exit_code == 0, result.output
        return result
    run('issue', 'create', '--title', 'Bug', '--body', 'x' * 5000, '--ref', 'bug', '--batch', 'batch')
    run('issue', 'comment', '@bug', '--body', 'comment', '--batch', 'batch')
    listing = json.loads(invoke('ls', '--json').stdout)
    assert listing['drafts'] == [{'kind': 'issue', 'name': 'batch', 'actions': 2}]
    run('outbox', 'submit', 'batch')
    directory = Store(home).outbox('issue')
    manifest = next(directory.glob('*.json'))
    bodies = {p.name: p.read_text() for p in directory.glob('*.md')}
    parse_manifest(manifest.name, manifest.read_text(), bodies)
    assert len(json.loads(manifest.read_text())['actions']) == 2
    result = invoke('show', 'issue/' + manifest.name)
    assert json.loads(result.stdout)['version'] == 1
    assert invoke('drop', 'issue/' + manifest.name).exit_code == 0
    assert not list(directory.glob('*.md'))
    assert not manifest.exists()


def test_pr_batch_and_review_protection(home, monkeypatch):
    from jailbee.container_outbox import gh
    from jailbee.pr_outbox import parse_manifest
    monkeypatch.setattr(gh, 'pr_view', lambda n: gh.PrView(n or 12, 'old', 'acme/repo'))
    runner = CliRunner()
    for args in [('pr', 'comment', '12', '--body', 'hello', '--batch', 'b'),
                 ('pr', 'review', 'add', '12', '--file', 'a.py', '--line', '1', '--body', 'note')]:
        result = runner.invoke(container_app, list(args))
        assert result.exit_code == 0, result.output
    refused = invoke('submit', 'review-12')
    assert refused.exit_code == 2
    assert 'jailbee pr review submit' in refused.output
    assert Store(home).draft_load('pr', 'review-12') is not None
    assert invoke('submit', 'b', '--kind', 'pr').exit_code == 0
    path = next(Store(home).outbox('pr').glob('*.json'))
    parse_manifest(path.name, path.read_text(), {})
    assert invoke('discard', 'review-12', '--kind', 'pr').exit_code == 0


def test_child_reads_and_refuses_mutation(home, monkeypatch):
    runner = CliRunner()
    result = runner.invoke(container_app, ['issue', 'create', '--title', 'Bug', '--body', 'body'])
    assert result.exit_code == 0, result.output
    store = Store(home)
    name = store.list()[0].name
    monkeypatch.setenv('JAILBEE_ROLE', 'child')
    assert invoke('ls', '--json').exit_code == 0
    assert invoke('show', 'issue/' + name).exit_code == 0
    for args in [('drop', 'issue/' + name), ('submit', 'x'), ('discard', 'x')]:
        result = invoke(*args)
        assert result.exit_code == 1
        assert 'jailbee inbox send message --text' in result.output
    assert (store.outbox('issue') / name).exists()


def test_real_container_entry(home, monkeypatch, capsys):
    import sys
    from jailbee import container_main
    monkeypatch.setattr(container_main.container_deps, 'check', lambda: None)
    monkeypatch.setattr(container_main, 'PACKAGE_DIR', home)
    monkeypatch.setattr(sys, 'argv', ['jailbee', 'outbox', 'ls', '--json'])
    with pytest.raises(SystemExit) as exc:
        container_main.main()
    assert exc.value.code == 0
    assert json.loads(capsys.readouterr().out)['drafts'] == []
