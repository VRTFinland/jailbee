"""`GET /api/repos/{prefix}/sessions/{agent}/{session_id}`: one session, rendered.

Through the REST seam with a temporary config home holding a hand-written
transcript; the home resolver is injected, so no host config is read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("markdown")

from fastapi.testclient import TestClient

from jailbee.db import get_engine
from jailbee.remote_ssh.repo_scope import RemoteRepoScope
from jailbee.transcripts.base import UserMessage
from jailbee.transcripts.claude import ClaudeSource
from jailbee.web.app import WebDeps, create_app
from jailbee.web.auth import PinHolder
from jailbee.web.protocol import RenderedUser
from jailbee.web.transcript_render import RenderCache, render_entries
from tests.web_fakes import FakeClock, FakeSourceFactory, pin_sequence

SESSION = "33333333-3333-4333-8333-333333333333"
AGENT_ID = "a0123456789abcdef"


def _record(n: int, kind: str, content: object, **extra: object) -> dict[str, object]:
    message: dict[str, object] = {"role": kind, "content": content}
    if kind == "assistant":
        message["model"] = extra.pop("model", "claude-opus-5-5")
    return {
        "uuid": f"r{n:07d}-0000-4000-8000-000000000000",
        "timestamp": f"2026-11-02T10:00:{n:02d}.000Z",
        "sessionId": SESSION,
        "type": kind,
        "message": message,
        **extra,
    }


ANSWER = """\
## Plan

- first <b>step</b>
- second

| col | other |
| --- | ----- |
| a   | b     |

```python
def hello():
    return "<tag>"
```

<img src=x onerror="alert(1)">

[bad](javascript:alert(1)) [worse](JaVaScRiPt:alert(2)) [data](data:text/html,x)
[good](https://example.com/page) ![pic](javascript:alert(3))
"""

RECORDS = [
    _record(1, "user", "Fix <script>alert(1)</script> the **login**"),
    _record(
        2,
        "assistant",
        [
            {"type": "thinking", "thinking": "Consider <i>auth</i> first"},
            {"type": "text", "text": ANSWER},
        ],
    ),
    _record(
        3,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "toolu_bash",
                "name": "Bash",
                "input": {"command": "echo '<b>hi</b>'", "description": "Say hi"},
            }
        ],
    ),
    _record(
        4,
        "user",
        [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_bash",
                "content": "<script>steal()</script>\nmore output",
            }
        ],
    ),
    _record(5, "user", "<command-name>/clear</command-name>\n<command-args></command-args>"),
    _record(
        6,
        "assistant",
        [
            {
                "type": "tool_use",
                "id": "toolu_agent",
                "name": "Agent",
                "input": {"description": "Review it", "prompt": "Look", "subagent_type": "x"},
            }
        ],
    ),
    _record(
        7,
        "user",
        [{"type": "tool_result", "tool_use_id": "toolu_agent", "content": "Looks fine."}],
        toolUseResult={"agentId": AGENT_ID, "agentType": "general-purpose"},
    ),
    _record(8, "assistant", [{"type": "text", "text": "Done."}], model="claude-sonnet-5"),
]


class Homes:
    def __init__(self, table: dict[str, Sequence[tuple[str, Path]]]) -> None:
        self.table = table
        self.calls: list[str] = []

    def __call__(self, prefix: str) -> Sequence[tuple[str, Path]] | None:
        self.calls.append(prefix)
        return self.table.get(prefix)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    project = tmp_path / "claude-home" / "projects" / "-work-repo"
    project.mkdir(parents=True)
    lines = "".join(json.dumps(r) + "\n" for r in RECORDS)
    (project / f"{SESSION}.jsonl").write_text(lines)
    return tmp_path / "claude-home"


@pytest.fixture
def homes(home: Path) -> Homes:
    return Homes({"alpha": [("claude", home)], "secret": [("claude", home)]})


@pytest.fixture
def client(homes: Homes) -> Iterator[TestClient]:
    clock = FakeClock()
    pin = PinHolder(generate=pin_sequence())
    deps = WebDeps(
        snapshot_source=FakeSourceFactory(),
        engine=get_engine(),
        clock=clock,
        monotonic=clock.monotonic,
        pin=pin,
        repo_scope=RemoteRepoScope(frozenset({"secret"})),
        agent_homes=homes,
    )
    with TestClient(create_app(deps)) as client:
        body = {"method": "pin", "pin": pin.current, "device_name": "boox"}
        token = client.post("/api/auth", json=body).json()["token"]
        client.headers["Authorization"] = f"Bearer {token}"
        yield client


URL = f"/api/repos/alpha/sessions/claude/{SESSION}"


def entries(client: TestClient) -> list[dict[str, object]]:
    res = client.get(URL, params={"meta": "shown"})
    assert res.status_code == 200, res.text
    got: list[dict[str, object]] = res.json()["entries"]
    return got


def of_kind(client: TestClient, kind: str) -> list[dict[str, object]]:
    return [e for e in entries(client) if e["kind"] == kind]


def test_user_prompt_html_is_escaped_and_markdown_rendered(client: TestClient) -> None:
    first = of_kind(client, "user")[0]
    html = str(first["html"])
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<strong>login</strong>" in html
    assert first["meta"] is False


def test_assistant_answer_renders_markdown_structures_with_its_model(client: TestClient) -> None:
    answer = of_kind(client, "assistant")[0]
    html = str(answer["html"])
    assert answer["model"] == "claude-opus-5-5"
    assert "<h2>Plan</h2>" in html
    assert re.search(r"<ul>\s*<li>first", html)
    assert "<table>" in html and "<td>a</td>" in html
    # Fenced code is highlighted with classes, not inline styles.
    assert '<div class="highlight">' in html
    assert 'class="k">def</span>' in html
    assert "style=" not in html


def test_raw_html_in_an_answer_is_escaped(client: TestClient) -> None:
    html = str(of_kind(client, "assistant")[0]["html"])
    assert "<img" not in html and "<b>" not in html
    assert "&lt;img src=x onerror=" in html
    assert "first &lt;b&gt;step&lt;/b&gt;" in html
    assert "&lt;tag&gt;" in html  # inside the code block too


def test_unsafe_link_schemes_lose_their_url(client: TestClient) -> None:
    html = str(of_kind(client, "assistant")[0]["html"]).lower()
    assert "javascript:" not in html
    assert "data:" not in html
    good = re.search(r'<a href="https://example.com/page"[^>]*>good</a>', html)
    assert good and 'target="_blank"' in good.group(0) and "noopener" in good.group(0)
    assert "<a>bad</a>" in html


def test_last_answer_has_its_own_model(client: TestClient) -> None:
    assert [a["model"] for a in of_kind(client, "assistant")] == [
        "claude-opus-5-5",
        "claude-sonnet-5",
    ]


def test_tool_call_has_a_summary_and_escaped_highlighted_input(client: TestClient) -> None:
    call = of_kind(client, "tool_call")[0]
    assert call["name"] == "Bash"
    assert call["call_id"] == "toolu_bash"
    assert "Say hi" in str(call["summary"]) or "echo" in str(call["summary"])
    html = str(call["input_html"])
    assert "<b>" not in html
    assert "&lt;b&gt;hi&lt;/b&gt;" in html
    assert 'class="highlight"' in html and "style=" not in html


def test_tool_result_is_escaped_and_linked_to_its_call(client: TestClient) -> None:
    result = of_kind(client, "tool_result")[0]
    assert result["call_id"] == "toolu_bash"
    html = str(result["html"])
    assert "<script>" not in html
    assert "&lt;script&gt;steal()&lt;/script&gt;" in html
    assert result["truncated"] is False and result["is_error"] is False


def test_thinking_is_rendered_and_escaped(client: TestClient) -> None:
    thought = of_kind(client, "thinking")[0]
    assert "&lt;i&gt;auth&lt;/i&gt;" in str(thought["html"])
    assert thought["model"] == "claude-opus-5-5"


def test_an_agent_wrapper_prompt_is_meta_and_shown_verbatim(client: TestClient) -> None:
    wrapper = of_kind(client, "user")[1]
    assert wrapper["meta"] is True
    html = str(wrapper["html"])
    assert "<command-name>" not in html
    assert "&lt;command-name&gt;/clear&lt;/command-name&gt;" in html


@pytest.mark.parametrize(
    "text",
    [
        "<command-name>/model</command-name> <command-message>model</command-message>",
        "<local-command-caveat>Local only</local-command-caveat>",
        "<local-command-stdout>Model changed</local-command-stdout>",
        "  <system-reminder>Injected\ncontext</system-reminder>",
        "<bash-stdout>/work/repo</bash-stdout>",
        "<bash-stderr>No such file</bash-stderr>",
        "<user-prompt-submit-hook>Hook context</user-prompt-submit-hook>",
        "<local-command-stderr>Command failed</local-command-stderr>",
        "<command-extra>Command context</command-extra>",
        '<teammate-message teammate_id="reviewer">Review finished</teammate-message>',
        "<ide_selection>Selected code</ide_selection>",
        '<terminal name="build">Build output</terminal>',
        '<coordinator-task nonce="abc">Task context</coordinator-task>',
        "<task_notification>Monitor event</task_notification>",
        "<webhook-payload>Event context</webhook-payload>",
    ],
)
def test_agent_preambles_are_meta_in_the_rendered_transcript(
    client: TestClient, home: Path, text: str
) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text(json.dumps(_record(1, "user", text)) + "\n")

    (entry,) = of_kind(client, "user")

    assert entry["meta"] is True
    assert '<pre class="meta">' in str(entry["html"])
    assert "&lt;" in str(entry["html"])


@pytest.mark.parametrize("wrapper", ["task-notification", "bash-input"])
def test_injected_user_records_are_meta_and_cannot_supply_session_titles(
    client: TestClient, home: Path, wrapper: str
) -> None:
    if wrapper == "task-notification":
        corpus = (
            Path(__file__).parent
            / "fixtures/transcripts/claude-corpus/projects/-work-toy"
            / "00000003-0000-4000-8000-000000000003.jsonl"
        )
        record = json.loads(corpus.read_text().splitlines()[6])
    else:
        record = _record(1, "user", "<bash-input>pwd</bash-input>")
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text(
        json.dumps(record) + "\n" + json.dumps(_record(2, "user", "Fix the title")) + "\n"
    )

    response = client.get(URL, params={"meta": "shown"})

    assert response.status_code == 200
    body = response.json()
    assert (body["entries"][0]["meta"], body["title"]) == (True, "Fix the title")


def test_a_compaction_summary_is_meta_and_cannot_supply_the_session_title(
    client: TestClient, home: Path
) -> None:
    corpus = (
        Path(__file__).parent
        / "fixtures/transcripts/claude-corpus/projects/-work-toy"
        / "00000001-0000-4000-8000-000000000001.jsonl"
    )
    (summary,) = [
        record
        for line in corpus.read_text().splitlines()
        if (record := json.loads(line)).get("isCompactSummary") is True
    ]
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text(
        json.dumps(summary) + "\n" + json.dumps(_record(2, "user", "Fix the title")) + "\n"
    )

    body = client.get(URL, params={"meta": "shown"}).json()

    assert body["title"] == "Fix the title"
    summarised, typed = body["entries"]
    assert summarised["meta"] is True
    assert '<pre class="meta">' in summarised["html"]
    assert "This session is being continued" in summarised["html"]
    assert typed["meta"] is False


@pytest.mark.parametrize(
    "notice", ["[Request interrupted by user]", "[Request interrupted by user for tool use]"]
)
def test_an_interruption_notice_is_meta_and_cannot_supply_the_session_title(
    client: TestClient, home: Path, notice: str
) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text(
        json.dumps(_record(1, "user", [{"type": "text", "text": notice}]))
        + "\n"
        + json.dumps(_record(2, "user", "Fix the title"))
        + "\n"
    )

    body = client.get(URL, params={"meta": "shown"}).json()

    assert body["title"] == "Fix the title"
    interrupted, typed = body["entries"]
    assert interrupted["meta"] is True
    assert "Request interrupted by user" in interrupted["html"]
    assert typed["meta"] is False


@pytest.mark.parametrize(
    "context",
    ["Injected context", "Injected <system-reminder>nested</system-reminder> context"],
)
def test_a_mixed_user_record_separates_injected_context_from_the_typed_prompt(
    client: TestClient, home: Path, context: str
) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text(
        json.dumps(
            _record(1, "user", f"<system-reminder>{context}</system-reminder>\nFix **titles**")
        )
        + "\n"
        + json.dumps(_record(2, "user", "A later prompt"))
        + "\n"
    )

    body = client.get(URL, params={"meta": "shown"}).json()

    assert body["title"] == "Fix **titles**"
    injected, typed, later = body["entries"]
    assert injected["meta"] is True
    assert "Injected" in injected["html"] and "Fix" not in injected["html"]
    assert typed["meta"] is False
    assert typed["html"] == "<p>Fix <strong>titles</strong></p>"
    assert len({injected["id"], typed["id"], later["id"]}) == 3


def test_an_unknown_xml_tag_stays_typed_text_in_both_title_and_transcript(
    client: TestClient, home: Path
) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text(json.dumps(_record(1, "user", "<example>broken</example>")) + "\n")

    body = client.get(URL).json()

    assert body["title"] == "<example>broken</example>"
    (entry,) = body["entries"]
    assert entry["meta"] is False
    assert "&lt;example&gt;broken&lt;/example&gt;" in entry["html"]


def _meta_session(client: TestClient, home: Path) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    lines = [
        _record(1, "user", "<task-notification>Job done</task-notification>"),
        _record(
            2,
            "user",
            "<command-name>/model</command-name><command-args>opus</command-args>"
            "<command-message>model</command-message>",
        ),
        _record(3, "user", "<system-reminder>Be brief</system-reminder>\nFix **titles**"),
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in lines))


def _summary(client: TestClient, mode: str | None) -> list[tuple[object, ...]]:
    res = client.get(URL, params={"meta": mode} if mode else None)
    assert res.status_code == 200, res.text
    return [
        (e["meta"], e["label"], e["html"] != "", e["expandable"]) for e in res.json()["entries"]
    ]


def test_agent_context_is_hidden_by_default_but_slash_commands_keep_a_label(
    client: TestClient, home: Path
) -> None:
    _meta_session(client, home)
    rows = _summary(client, None)
    assert rows == [
        (True, "Command: /model opus", False, False),
        (False, None, True, False),
    ]
    assert rows == _summary(client, "hidden")
    assert "Job done" not in client.get(URL).text and "Be brief" not in client.get(URL).text


def test_collapsed_agent_context_sends_labels_without_content(
    client: TestClient, home: Path
) -> None:
    _meta_session(client, home)
    assert _summary(client, "collapsed") == [
        (True, "Task notification", False, True),
        (True, "Command: /model opus", False, True),
        (True, "System reminder", False, True),
        (False, None, True, False),
    ]
    text = client.get(URL, params={"meta": "collapsed"}).text
    assert "Job done" not in text and "Be brief" not in text


def test_shown_agent_context_sends_labels_and_content(client: TestClient, home: Path) -> None:
    _meta_session(client, home)
    assert _summary(client, "shown") == [
        (True, "Task notification", True, False),
        (True, "Command: /model opus", True, False),
        (True, "System reminder", True, False),
        (False, None, True, False),
    ]


def test_an_unknown_agent_context_mode_is_rejected(client: TestClient) -> None:
    assert client.get(URL, params={"meta": "bogus"}).status_code == 422


def test_a_withheld_meta_record_is_fetched_on_expand(client: TestClient, home: Path) -> None:
    _meta_session(client, home)
    first = client.get(URL, params={"meta": "collapsed"}).json()["entries"][0]
    res = client.get(f"{URL}/entries/{first['id']}")
    assert res.status_code == 200
    assert res.json()["id"] == first["id"]
    assert "Job done" in res.json()["html"]
    mixed = client.get(URL, params={"meta": "collapsed"}).json()["entries"][2]
    assert mixed["id"].endswith(":meta")
    assert "Be brief" in client.get(f"{URL}/entries/{mixed['id']}").json()["html"]


def test_entry_content_is_404_for_a_typed_prompt_or_unknown_id(
    client: TestClient, home: Path
) -> None:
    _meta_session(client, home)
    typed = client.get(URL, params={"meta": "shown"}).json()["entries"][-1]
    assert client.get(f"{URL}/entries/{typed['id']}").status_code == 404
    assert client.get(f"{URL}/entries/nope").status_code == 404


def test_a_subagent_is_referenced_as_data(client: TestClient) -> None:
    ref = of_kind(client, "subagent")[0]
    assert ref["agent_id"] == AGENT_ID
    assert ref["call_id"] == "toolu_agent"
    assert ref["agent_type"] == "general-purpose"


def test_entries_keep_transcript_order_and_session_meta(client: TestClient) -> None:
    body = client.get(URL).json()
    assert body["session_id"] == SESSION
    assert body["agent"] == "claude"
    assert body["title"]
    assert body["models"] == ["claude-opus-5-5", "claude-sonnet-5"]
    assert body["last_activity"].startswith("2026-11-02T10:00:08")
    kinds = [e["kind"] for e in body["entries"]]
    assert kinds == [
        "user",
        "thinking",
        "assistant",
        "tool_call",
        "tool_result",
        "user",
        "tool_call",
        "tool_result",
        "subagent",
        "assistant",
    ]
    assert len({e["id"] for e in body["entries"]}) == len(kinds)


@pytest.mark.parametrize(
    "url",
    [
        f"/api/repos/secret/sessions/claude/{SESSION}",  # excluded repo
        f"/api/repos/nope/sessions/claude/{SESSION}",  # unknown repo
        f"/api/repos/alpha/sessions/codex/{SESSION}",  # agent not in the repo
        f"/api/repos/alpha/sessions/base/{SESSION}",  # a module, not a source
        "/api/repos/alpha/sessions/claude/44444444-4444-4444-8444-444444444444",
        "/api/repos/alpha/sessions/claude/-work-repo",
    ],
)
def test_unknown_or_hidden_sessions_give_the_same_404(client: TestClient, url: str) -> None:
    res = client.get(url)
    assert res.status_code == 404
    assert res.json() == {"detail": "Unknown session"}


def test_a_traversal_attempt_is_not_found(client: TestClient) -> None:
    res = client.get("/api/repos/alpha/sessions/claude/..%2F..%2Fetc%2Fpasswd")
    assert res.status_code == 404


def test_an_excluded_repo_is_never_resolved(client: TestClient, homes: Homes) -> None:
    client.get(f"/api/repos/secret/sessions/claude/{SESSION}")
    assert "secret" not in homes.calls


def test_requires_login(client: TestClient) -> None:
    client.headers.pop("Authorization")
    assert client.get(URL).status_code == 401


def test_render_cache_reuses_html_and_stays_bounded() -> None:
    from jailbee.web.transcript_render import RenderCache

    calls: list[str] = []

    def render(text: str) -> str:
        calls.append(text)
        return f"<p>{text}</p>"

    cache = RenderCache(limit=2)
    assert cache.get("a", "one", render) == "<p>one</p>"
    assert cache.get("a", "one", render) == "<p>one</p>"
    assert calls == ["one"]
    cache.get("a", "changed", render)  # same id, new text: rendered again
    cache.get("b", "two", render)
    assert len(cache) == 2
    cache.get("a", "one", render)  # evicted as least recently used
    assert calls == ["one", "changed", "two", "one"]


def test_committed_highlight_stylesheet_is_current() -> None:
    from jailbee.web.highlight import stylesheet

    committed = Path(__file__).parents[1] / "frontend" / "src" / "styles" / "highlight.css"
    assert committed.read_text() == stylesheet(), (
        "regenerate: uv run python -m jailbee.web.highlight > frontend/src/styles/highlight.css"
    )
    css = committed.read_text()
    for theme in ("light", "dark", "eink"):
        assert f':root[data-theme="{theme}"] .highlight' in css


CORPUS = Path(__file__).parent / "fixtures/transcripts/claude-corpus"


def test_no_corpus_user_record_starting_with_a_tag_renders_as_a_typed_prompt() -> None:
    """Guard over every recorded session and subagent: Claude Code's own XML-like
    user records are meta (or split), never typed prompts or session titles.

    The recorder's scripted prompts (corpus.json) never start with ``<``.
    """
    scripted = [
        p
        for s in json.loads((CORPUS / "corpus.json").read_text())["sessions"]
        for p in s["prompts"]
    ]
    assert scripted and not any(p.lstrip().startswith("<") for p in scripted)
    source = ClaudeSource()
    transcripts = sorted((CORPUS / "projects").rglob("*.jsonl"))
    assert any("subagents" in p.parts for p in transcripts)
    tagged = 0
    for path in transcripts:
        for entry in source.read(path).entries:
            if not (isinstance(entry, UserMessage) and re.match(r"\s*<[a-z]", entry.text)):
                continue
            tagged += 1
            first = render_entries([entry], RenderCache())[0]
            assert isinstance(first, RenderedUser) and first.meta, (path.name, entry.text[:80])
    assert tagged
    sessions = source.list_sessions(CORPUS)
    assert sessions
    assert not [s.title for s in sessions if s.title.startswith("<")]


# ---- agent context: review rework (ticket 33) --------------------------------

REMINDER = "<system-reminder>\nWarning: the file is shorter than the offset.\n</system-reminder>"


def _write(home: Path, *records: dict[str, object]) -> None:
    path = home / "projects" / "-work-repo" / f"{SESSION}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _reminder_session(home: Path, output: str = "line one\nline two") -> None:
    _write(
        home,
        _record(
            1,
            "assistant",
            [{"type": "tool_use", "id": "toolu_read", "name": "Read", "input": {"file_path": "x"}}],
        ),
        _record(
            2,
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_read",
                    "content": output + "\n\n" + REMINDER,
                }
            ],
        ),
    )


def _kinds(client: TestClient, mode: str) -> list[dict[str, object]]:
    res = client.get(URL, params={"meta": mode})
    assert res.status_code == 200, res.text
    got: list[dict[str, object]] = res.json()["entries"]
    return got


def test_a_reminder_inside_a_tool_result_is_hidden_by_default(
    client: TestClient, home: Path
) -> None:
    _reminder_session(home)
    res = client.get(URL)
    assert "shorter than the offset" not in res.text
    (result,) = [e for e in res.json()["entries"] if e["kind"] == "tool_result"]
    assert result["html"] == '<pre class="tool-output">line one\nline two</pre>'
    assert [e["kind"] for e in res.json()["entries"]] == ["tool_call", "tool_result"]


def test_a_collapsed_tool_result_reminder_is_a_label_fetched_on_expand(
    client: TestClient, home: Path
) -> None:
    _reminder_session(home)
    entries = _kinds(client, "collapsed")
    result, reminder = entries[1], entries[2]
    assert result["html"] == '<pre class="tool-output">line one\nline two</pre>'
    assert reminder["kind"] == "user"
    assert (reminder["meta"], reminder["label"], reminder["html"], reminder["expandable"]) == (
        True,
        "System reminder",
        "",
        True,
    )
    assert reminder["id"] == f"{result['id']}:meta"
    body = client.get(f"{URL}/entries/{reminder['id']}").json()
    assert "shorter than the offset" in body["html"]
    assert "line one" not in body["html"]


def test_a_shown_tool_result_reminder_is_its_own_meta_entry(client: TestClient, home: Path) -> None:
    _reminder_session(home)
    result, reminder = _kinds(client, "shown")[1:]
    assert "shorter than the offset" not in result["html"]
    assert reminder["label"] == "System reminder"
    assert "shorter than the offset" in reminder["html"]


def test_a_reminder_tag_inside_tool_output_stays_output(client: TestClient, home: Path) -> None:
    """A file that merely mentions the tag is the tool's own output, not context."""
    _write(
        home,
        _record(
            1,
            "user",
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_x",
                    "content": "<system-reminder>quoted</system-reminder>\nthen more file text",
                }
            ],
        ),
    )
    (result,) = _kinds(client, "hidden")
    assert "quoted" in result["html"] and "more file text" in result["html"]


def test_expanding_an_entry_does_not_render_the_whole_transcript(
    client: TestClient, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _meta_session(client, home)
    ids = [e["id"] for e in _kinds(client, "collapsed") if e.get("expandable")]
    import jailbee.web.routes.repo_routes as routes
    import jailbee.web.transcript_render as render

    def boom(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the whole transcript was rendered")

    monkeypatch.setattr(render, "render_entries", boom)
    monkeypatch.setattr(routes, "render_entries", boom)
    monkeypatch.setattr(render, "render_markdown", boom)
    monkeypatch.setattr(render, "highlight_json", boom)
    for entry_id in ids:
        assert client.get(f"{URL}/entries/{entry_id}").status_code == 200


SUBAGENT_URL = f"{URL}/subagents/{AGENT_ID}"


def _subagent_session(home: Path) -> None:
    directory = home / "projects" / "-work-repo" / SESSION / "subagents"
    directory.mkdir(parents=True)
    (directory / f"agent-{AGENT_ID}.jsonl").write_text(
        json.dumps(_record(1, "user", "<task-notification>Sub job</task-notification>"))
        + "\n"
        + json.dumps(_record(2, "assistant", [{"type": "text", "text": "ok"}]))
        + "\n"
    )


def test_a_subagent_meta_record_is_fetched_on_expand(client: TestClient, home: Path) -> None:
    _subagent_session(home)
    res = client.get(SUBAGENT_URL, params={"meta": "collapsed"})
    assert res.status_code == 200, res.text
    first = res.json()["entries"][0]
    assert first["expandable"] is True and "Sub job" not in res.text
    body = client.get(f"{SUBAGENT_URL}/entries/{first['id']}")
    assert body.status_code == 200
    assert "Sub job" in body.json()["html"]
    assert client.get(f"{SUBAGENT_URL}/entries/nope").status_code == 404


@pytest.mark.parametrize("base", [URL, SUBAGENT_URL])
def test_entry_content_requires_login(client: TestClient, home: Path, base: str) -> None:
    _subagent_session(home)
    client.headers.pop("Authorization")
    assert client.get(f"{base}/entries/r0000001-0000-4000-8000-000000000000:0").status_code == 401


@pytest.mark.parametrize("base", [URL, SUBAGENT_URL])
def test_entry_content_of_an_excluded_repo_is_404_and_never_resolved(
    client: TestClient, home: Path, homes: Homes, base: str
) -> None:
    _subagent_session(home)
    _write(home, _record(1, "user", "<task-notification>Job done</task-notification>"))
    entry_id = "r0000001-0000-4000-8000-000000000000:0"
    assert client.get(f"{base}/entries/{entry_id}").status_code == 200
    homes.calls.clear()
    res = client.get(f"{base.replace('/alpha/', '/secret/')}/entries/{entry_id}")
    assert res.status_code == 404
    assert res.json() == {"detail": "Unknown session"}
    assert "secret" not in homes.calls


def _compaction_summary() -> dict[str, object]:
    corpus = (
        Path(__file__).parent
        / "fixtures/transcripts/claude-corpus/projects/-work-toy"
        / "00000001-0000-4000-8000-000000000001.jsonl"
    )
    (summary,) = [
        record
        for line in corpus.read_text().splitlines()
        if (record := json.loads(line)).get("isCompactSummary") is True
    ]
    return summary


def test_a_compaction_summary_follows_the_agent_context_setting(
    client: TestClient, home: Path
) -> None:
    _write(home, _compaction_summary(), _record(2, "user", "Fix the title"))
    hidden = client.get(URL, params={"meta": "hidden"})
    assert "being continued" not in hidden.text
    assert [e["meta"] for e in hidden.json()["entries"]] == [False]

    collapsed = client.get(URL, params={"meta": "collapsed"})
    assert "being continued" not in collapsed.text
    summary = collapsed.json()["entries"][0]
    assert (summary["meta"], summary["label"], summary["expandable"]) == (
        True,
        "Agent context",
        True,
    )
    body = client.get(f"{URL}/entries/{summary['id']}").json()
    assert "This session is being continued" in body["html"]


@pytest.mark.parametrize("mode", ["hidden", "collapsed"])
def test_an_interruption_notice_keeps_its_label_line(
    client: TestClient, home: Path, mode: str
) -> None:
    _write(
        home,
        _record(
            1, "user", [{"type": "text", "text": "[Request interrupted by user for tool use]"}]
        ),
        _record(2, "user", "Go on"),
    )
    notice, typed = _kinds(client, mode)
    assert (notice["meta"], notice["label"], notice["html"], notice["expandable"]) == (
        True,
        "Request interrupted by user for tool use",
        "",
        False,
    )
    assert typed["meta"] is False
