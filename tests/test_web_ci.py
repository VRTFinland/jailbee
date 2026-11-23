"""The web CI contract, read as GitHub Actions' public workflow format."""

import fnmatch
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/web.yml"


def workflow():
    # BaseLoader keeps the Actions key `on` a string, not YAML 1.1's boolean.
    return yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_web_ci_runs_for_web_changes_only(event):
    triggers = workflow()["on"]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    assert set(triggers["push"]["branches"]) == {"main", "dev"}
    paths = triggers[event]["paths"]
    for changed in (
        "frontend/src/App.vue",
        "src/jailbee/web/app.py",
        "src/jailbee/transcripts/claude.py",
        "tests_e2e/test_tracer.py",
        "tests/web_fakes.py",
        "tests/fixtures/transcripts/claude/projects/toy/session.jsonl",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        ".github/workflows/web.yml",
    ):
        assert any(fnmatch.fnmatchcase(changed, pattern) for pattern in paths), changed
    for changed in ("README.md", "website/index.html", "src/jailbee/pool.py"):
        assert not any(fnmatch.fnmatchcase(changed, pattern) for pattern in paths), changed


def test_web_ci_runs_make_targets_and_preserves_failure_evidence():
    definition = workflow()
    assert definition["permissions"] == {"contents": "read"}
    job = definition["jobs"]["test"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = job["steps"]
    actions = {step["uses"].split("@")[0]: step for step in steps if "uses" in step}
    assert "actions/checkout" in actions
    assert actions["actions/setup-node"]["with"]["node-version"] == "24"
    assert actions["astral-sh/setup-uv"]["with"]["python-version"] == "3.13"
    commands = [step["run"] for step in steps if "run" in step]
    assert commands.index("make web-test") < commands.index("make web-e2e")
    assert "uv sync --locked --group e2e" in commands
    assert not any("playwright install" in command for command in commands)
    artifact = actions["actions/upload-artifact"]
    assert artifact["if"] == "failure()"
    assert artifact["with"]["path"] == "_build/e2e-results/"
