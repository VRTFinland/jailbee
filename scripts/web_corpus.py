"""Record and normalise the web dashboard's corpus of genuine Claude sessions.

`make web-corpus` runs this. It builds a toy repository in a temporary
directory, runs headless Claude Code on it through a fixed list of scripted
prompts (`STEPS`), and writes the normalised transcripts as a fixture Claude
home under `tests/fixtures/transcripts/claude-corpus/`.

Normalisation keeps only what a transcript reader needs and makes the rest
unidentifiable: record types and fields are whitelisted, every UUID and
message/tool/agent id is renumbered in recording order, the toy repository's
path becomes `/work/toy`, and the recording host's home, names and any
credential-shaped string are redacted. `find_leaks` is the final gate: the
recorder refuses to write a corpus it reports anything in.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import posixpath
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

TOY_DIR = "/work/toy"
"""Where the toy repository appears to have been, in every normalised path."""
TOY_PROJECT = "-work-toy"
"""Claude's project directory name for `TOY_DIR`."""
HOME_DIR = "/home/user"
"""The recording user's home, in every normalised path."""

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_API_ID = re.compile(r"\b(msg|toolu|srvtoolu|req)_01[0-9A-Za-z]{6,}")
"""Message, tool-use and request ids as the API mints them."""

REDACTED = "REDACTED"
_CREDENTIAL = re.compile(
    r"sk-ant-[0-9A-Za-z_-]{8,}"
    r"|\bgh[pousr]_[0-9A-Za-z]{20,}"
    r"|\bgithub_pat_[0-9A-Za-z_]{20,}"
    r"|\bglpat-[0-9A-Za-z_-]{20,}"
    r"|\bxox[abposr]-[0-9A-Za-z-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}"
    r"|\beyJ[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}\.[0-9A-Za-z_-]{8,}"
)
"""Credential shapes: Anthropic, GitHub, GitLab, Slack and AWS keys and JWTs."""
_EMAIL = re.compile(r"[0-9A-Za-z._%+-]+@[0-9A-Za-z.-]+\.[A-Za-z]{2,}")
TOY_EMAIL = "toy@example.invalid"
"""The toy repository's commit author."""
EMAILS_ALLOWED = frozenset({"noreply@anthropic.com", TOY_EMAIL})
"""Addresses that identify no one (Claude's commit trailer, the toy author)."""

RECORD_TYPES = frozenset({"user", "assistant", "system", "attachment", "summary"})
"""Records a transcript is made of. The rest (account owner, API request
copies, queue and cost bookkeeping) is the client's own state."""
SYSTEM_SUBTYPES = frozenset(
    {"compact_boundary", "api_error", "informational", "local_command", "turn_duration"}
)
"""`system` records kept; hook summaries name the recording host's commands."""
ATTACHMENT_TYPES = frozenset({"date", "edited_text_file", "queued_command"})
"""Attachments kept. The others carry the session's whole context: system
prompt, instructions files, the account's email and organisation."""
RECORD_FIELDS = frozenset(
    {
        "type",
        "subtype",
        "uuid",
        "parentUuid",
        "logicalParentUuid",
        "isSidechain",
        "sessionId",
        "agentId",
        "timestamp",
        "cwd",
        "gitBranch",
        "version",
        "message",
        "content",
        "level",
        "isMeta",
        "isCompactSummary",
        "isVisibleInTranscriptOnly",
        "compactMetadata",
        "toolUseResult",
        "toolUseID",
        "sourceToolAssistantUUID",
        "attachment",
        "summary",
        "leafUuid",
    }
)
MESSAGE_FIELDS = frozenset({"id", "type", "role", "model", "content", "stop_reason"})
META_FIELDS = frozenset({"agentType", "description", "toolUseId"})
"""A subagent's `.meta.json` keys kept."""


@dataclass(frozen=True)
class Identity:
    """What must not survive normalisation."""

    toy_dir: str
    """The toy repository's real path during the recording."""
    home: str
    """The recording user's home directory."""
    secrets: tuple[str, ...] = ()
    """Literal strings to redact: host, container, user and account names."""
    renames: tuple[tuple[str, str], ...] = ()
    """Other recording paths and what they become, replaced before the rest."""


def claude_project_name(path: str) -> str:
    """The project directory name Claude Code gives a working directory."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


class _Ids:
    """Renumbers ids in first-seen order, so a re-recording diffs cleanly."""

    def __init__(self) -> None:
        self.uuids: dict[str, str] = {}
        self.api: dict[str, str] = {}
        self.agents: dict[str, str] = {}

    def agent(self, raw: str) -> str:
        if raw not in self.agents:
            self.agents[raw] = f"a{len(self.agents) + 1:016x}"
        return self.agents[raw]

    def uuid(self, raw: str) -> str:
        if raw not in self.uuids:
            n = len(self.uuids) + 1
            self.uuids[raw] = f"{n:08x}-0000-4000-8000-{n:012x}"
        return self.uuids[raw]

    def api_id(self, prefix: str, raw: str) -> str:
        if raw not in self.api:
            n = sum(1 for key in self.api if key.startswith(prefix + "_")) + 1
            self.api[raw] = f"{prefix}_{n:06d}"
        return self.api[raw]


class _Normaliser:
    def __init__(self, identity: Identity) -> None:
        self.identity = identity
        self.ids = _Ids()

    def text(self, value: str) -> str:
        toy = self.identity.toy_dir.rstrip("/")
        for raw, new in self.identity.renames:
            value = value.replace(raw, new)
        value = value.replace(toy, TOY_DIR)
        value = value.replace(claude_project_name(toy), TOY_PROJECT)
        value = value.replace(posixpath.dirname(toy), posixpath.dirname(TOY_DIR))
        value = value.replace(self.identity.home.rstrip("/"), HOME_DIR)
        for secret in self.identity.secrets:
            value = value.replace(secret, REDACTED)
        value = _CREDENTIAL.sub(REDACTED, value)
        value = _EMAIL.sub(lambda m: m[0] if m[0] in EMAILS_ALLOWED else REDACTED, value)
        for raw, agent in self.ids.agents.items():
            value = value.replace(raw, agent)
        value = _API_ID.sub(lambda m: self.ids.api_id(m.group(1), m.group(0)), value)
        return _UUID.sub(lambda m: self.ids.uuid(m.group(0)), value)

    def value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {
                key: "signature" if key == "signature" else self.value(item)
                for key, item in value.items()
            }
        return value

    def record(self, record: dict[str, object]) -> dict[str, object] | None:
        """`record` normalised, or None when it is not part of a transcript."""
        kind = record.get("type")
        if kind not in RECORD_TYPES:
            return None
        if kind == "system" and record.get("subtype") not in SYSTEM_SUBTYPES:
            return None
        attachment = record.get("attachment")
        if kind == "attachment" and (
            not isinstance(attachment, dict) or attachment.get("type") not in ATTACHMENT_TYPES
        ):
            return None
        kept = {key: item for key, item in record.items() if key in RECORD_FIELDS}
        message = kept.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            kept["message"] = {k: v for k, v in message.items() if k in MESSAGE_FIELDS}
        result = self.value(kept)
        assert isinstance(result, dict)
        return result


def _records(path: Path) -> list[dict[str, object]]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if isinstance(record, dict):
            records.append(record)
    return records


def _dump(path: Path, records: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )


def _subagents(raw_project: Path, session_id: str) -> list[str]:
    """The raw agent ids of `session_id`'s subagents, in name order."""
    directory = raw_project / session_id / "subagents"
    if not directory.is_dir():
        return []
    return [
        path.name.removeprefix("agent-").removesuffix(".jsonl")
        for path in sorted(directory.glob("agent-*.jsonl"))
    ]


def _normalise_file(normaliser: _Normaliser, source: Path, target: Path) -> None:
    kept = [r for r in (normaliser.record(r) for r in _records(source)) if r is not None]
    _dump(target, kept)


def normalise_home(raw_project: Path, out_home: Path, identity: Identity, order: list[str]) -> None:
    """Normalise the sessions `order` names from Claude's `raw_project` directory.

    Writes them, with their subagents, to `out_home/projects/TOY_PROJECT`.
    Session ids are numbered in `order`, the order they were recorded.
    """
    normaliser = _Normaliser(identity)
    for session_id in order:
        normaliser.ids.uuid(session_id)
    for session_id in order:
        for agent in _subagents(raw_project, session_id):
            normaliser.ids.agent(agent)
    project = out_home / "projects" / TOY_PROJECT
    for session_id in order:
        sid = normaliser.ids.uuid(session_id)
        _normalise_file(normaliser, raw_project / f"{session_id}.jsonl", project / f"{sid}.jsonl")
        for agent in _subagents(raw_project, session_id):
            source = raw_project / session_id / "subagents" / f"agent-{agent}"
            target = project / sid / "subagents" / f"agent-{normaliser.ids.agent(agent)}"
            _normalise_file(normaliser, source.with_suffix(".jsonl"), target.with_suffix(".jsonl"))
            meta_path = source.with_suffix(".meta.json")
            if meta_path.is_file():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                kept = {k: v for k, v in meta.items() if k in META_FIELDS}
                target.with_suffix(".meta.json").write_text(
                    json.dumps(normaliser.value(kept), ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )


_NORMAL_UUID = re.compile(r"([0-9a-f]{8})-0000-4000-8000-0000\1")
_HOST_PATH = re.compile(r"(?<![\w.-])/(?:home|root|tmp|Users|var/folders)(?:/[^\s\"'`)\]]*)?")


def find_leaks(text: str, identity: Identity | None = None) -> list[str]:
    """Everything in `text` that identifies the recording; empty when clean.

    Without `identity` only the shapes are checked: credentials, e-mail
    addresses, host paths and UUIDs `normalise_home` did not mint.
    """
    leaks: list[str] = []
    if identity is not None:
        toy = identity.toy_dir.rstrip("/")
        literals = [
            toy,
            posixpath.dirname(toy),
            claude_project_name(toy),
            identity.home.rstrip("/"),
            *identity.secrets,
            *(raw for raw, _ in identity.renames),
        ]
        leaks += [literal for literal in literals if literal and literal in text]
    leaks += _CREDENTIAL.findall(text)
    leaks += [m for m in _EMAIL.findall(text) if m not in EMAILS_ALLOWED]
    leaks += [m for m in _UUID.findall(text) if not _NORMAL_UUID.fullmatch(m)]
    leaks += [
        m for m in _HOST_PATH.findall(text) if m != HOME_DIR and not m.startswith(HOME_DIR + "/")
    ]
    return leaks


def scan_corpus(root: Path, identity: Identity | None = None) -> dict[str, list[str]]:
    """`find_leaks` over every file under `root`, by path relative to it."""
    return {
        str(path.relative_to(root)): found
        for path in sorted(root.rglob("*"))
        if path.is_file() and (found := find_leaks(path.read_text(encoding="utf-8"), identity))
    }


# --- Shapes -----------------------------------------------------------------

SHAPES = (
    "tool_call",
    "tool_error",
    "long_output",
    "markdown",
    "subagent",
    "thinking",
    "interrupted",
    "compaction",
)
"""Entry shapes the corpus is recorded to contain (#152, story 45)."""

LONG_OUTPUT_CHARS = 4000
"""A tool result longer than this is long output: the reader cuts it."""


def _blocks(record: dict[str, object]) -> list[dict[str, object]]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _result_text(block: dict[str, object]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    return ""


def _is_markdown(text: str) -> bool:
    return all(
        re.search(pattern, text, re.MULTILINE)
        for pattern in (r"^#{1,6} ", r"^\s*[-*] ", r"^\|.*\|$", r"^```")
    )


def detect_shapes(session_file: Path) -> list[str]:
    """The `SHAPES` a normalised session (with its subagents) contains."""
    found: set[str] = set()
    for record in _records(session_file):
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            found.add("compaction")
        for block in _blocks(record):
            kind = block.get("type")
            text = block.get("text")
            if kind == "tool_use":
                found.add("tool_call")
            elif kind == "thinking":
                found.add("thinking")
            elif kind == "tool_result":
                if block.get("is_error") is True:
                    found.add("tool_error")
                if len(_result_text(block)) > LONG_OUTPUT_CHARS:
                    found.add("long_output")
            elif kind == "text" and isinstance(text, str):
                if text.startswith("[Request interrupted by user"):
                    found.add("interrupted")
                if record.get("type") == "assistant" and _is_markdown(text):
                    found.add("markdown")
    subagents = session_file.with_suffix("") / "subagents"
    if subagents.is_dir() and any(subagents.glob("agent-*.jsonl")):
        found.add("subagent")
    return [shape for shape in SHAPES if shape in found]


# --- Recording ----------------------------------------------------------------

CORPUS_DIR = Path(__file__).resolve().parent.parent / "tests/fixtures/transcripts/claude-corpus"
MANIFEST = "corpus.json"
"""Beside `projects/`: what each session was recorded with and contains."""
MODEL = "haiku"
"""The cheapest model; it produces every shape the corpus needs."""
STEP_TIMEOUT = 300
INTERRUPT_GRACE = 3.0
"""Seconds between the interrupted tool call appearing and the SIGINT."""

TOY_FILES = {
    "calc.py": (
        '"""A toy calculator. TOY-MARKER-CALC"""\n\n\n'
        "def add(a, b):\n    return a - b\n\n\n"
        "def mul(a, b):\n    return a * b\n"
    ),
    "test_calc.py": (
        "from calc import add, mul\n\n"
        "assert add(2, 3) == 5, add(2, 3)\n"
        "assert mul(2, 3) == 6\n"
        'print("TOY-TESTS-PASSED")\n'
    ),
    "README.md": (
        "# Toy calculator\n\n"
        "A two-function module used to record Claude Code transcripts.\n\n"
        "The marker word is TOY-MARKER-README.\n"
    ),
}


@dataclass(frozen=True)
class Step:
    prompt: str
    interrupt: bool = False
    """SIGINT the run once its first tool call is in the transcript."""


@dataclass(frozen=True)
class Session:
    name: str
    steps: tuple[Step, ...]
    settings: dict[str, object] = field(default_factory=dict)


SESSIONS = (
    Session(
        "tools",
        (
            Step(
                "CORPUS-TOOLS: Read calc.py, fix the bug in add() with the Edit tool, then run"
                " `python3 test_calc.py` with the Bash tool and report the result in one sentence."
            ),
            Step(
                "CORPUS-FAILING: Use the Read tool on missing.txt (it does not exist), then run"
                " `python3 no_such_script.py` with the Bash tool. Report both errors in one"
                " sentence each; do not try to fix them."
            ),
            Step(
                "CORPUS-LONG-OUTPUT: Run exactly this command with the Bash tool, unchanged and"
                " not piped through tail, head or any other filter, so that I see all of its"
                " output: `python3 -c \"for i in range(1, 401): print(f'line {i}:"
                " TOY-LONG-OUTPUT')\"`. Then tell me its last line."
            ),
            Step("/compact"),
            Step("CORPUS-AFTER-COMPACT: In one sentence, what does add() in calc.py return now?"),
        ),
    ),
    Session(
        "markdown",
        (
            Step(
                "CORPUS-MARKDOWN: Without using any tools, write a short Markdown note about"
                " calc.py with a level-2 heading, a bulleted list, a numbered list, a two-column"
                " table and a fenced python code block."
            ),
        ),
    ),
    Session(
        "subagent",
        (
            Step(
                "CORPUS-SUBAGENT: Use the Agent tool with subagent_type general-purpose and model"
                " haiku to read README.md and report the marker word it contains. Then tell me"
                " the marker in one sentence."
            ),
        ),
    ),
    Session(
        "thinking",
        (
            Step(
                "CORPUS-THINKING: Think it through step by step first, then answer in one line:"
                " is 221 a prime number?"
            ),
        ),
        settings={"alwaysThinkingEnabled": True},
    ),
    Session(
        "interrupted",
        (
            Step(
                "CORPUS-INTERRUPT: Run exactly this with the Bash tool in the foreground and wait"
                " for it: python3 -c 'import time; time.sleep(120)'",
                interrupt=True,
            ),
        ),
    ),
)


def _config_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _host_identity(toy_dir: Path, claude_tmp: Path) -> Identity:
    """The recording host's names, so `find_leaks` can prove they are gone."""
    secrets: set[str] = {socket.gethostname(), getpass.getuser()}
    for key in ("user.name", "user.email"):
        result = subprocess.run(
            ["git", "config", "--global", key], capture_output=True, text=True, check=False
        )
        secrets.add(result.stdout.strip())
    for path, keys in (
        (Path("/etc/jailbee/container.json"), ("name", "short_name", "container_prefix", "branch")),
        (Path.home() / ".claude.json", ()),
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        secrets.update(str(data.get(key) or "") for key in keys)
        account = data.get("oauthAccount")
        if isinstance(account, dict):
            secrets.update(str(value) for value in account.values() if isinstance(value, str))
    # A secret this short would match ordinary words; the user's name is in
    # the home path, which is checked separately.
    kept = sorted((s for s in secrets if len(s) >= 5), key=len, reverse=True)
    # Claude puts its scratch files under `<CLAUDE_CODE_TMPDIR>/claude-<uid>`.
    scratch = str(claude_tmp / f"claude-{os.getuid()}")
    return Identity(
        toy_dir=str(toy_dir),
        home=str(Path.home()),
        secrets=tuple(kept),
        renames=((scratch, posixpath.join(posixpath.dirname(TOY_DIR), "claude-tmp")),),
    )


def _make_toy(root: Path) -> Path:
    toy = root / "toy"
    toy.mkdir()
    for name, text in TOY_FILES.items():
        (toy / name).write_text(text, encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Toy",
        "GIT_AUTHOR_EMAIL": TOY_EMAIL,
        "GIT_COMMITTER_NAME": "Toy",
        "GIT_COMMITTER_EMAIL": TOY_EMAIL,
    }
    for command in (["git", "init", "-q", "-b", "main"], ["git", "add", "."]):
        subprocess.run(command, cwd=toy, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "Toy calculator"], cwd=toy, check=True, env=env)
    return toy


def _no_op_hooks(root: Path) -> Path:
    """A directory with a no-op `jailbee`, first on the recorder's PATH.

    In a JailBee container the managed plugin's inbox hooks would otherwise
    steer the recorded session (a child's Stop hook asks for an inbox waiter).
    """
    shim = root / "bin"
    shim.mkdir()
    (shim / "jailbee").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (shim / "jailbee").chmod(0o755)
    return shim


def _transcript(toy: Path, session_id: str) -> Path:
    return _config_home() / "projects" / claude_project_name(str(toy)) / f"{session_id}.jsonl"


def _run_step(
    step: Step, session: Session, session_id: str, first: bool, toy: Path, env: dict[str, str]
) -> None:
    command = [
        "claude",
        "-p",
        step.prompt,
        "--model",
        MODEL,
        "--permission-mode",
        "bypassPermissions",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--output-format",
        "json",
        # Headless runs record thinking blocks without their text otherwise.
        "--thinking-display",
        "summarized",
        *(["--session-id", session_id] if first else ["--resume", session_id]),
        *(["--settings", json.dumps(session.settings)] if session.settings else []),
    ]
    print(f"  {session.name}: {step.prompt[:60]}", file=sys.stderr)
    process = subprocess.Popen(
        command, cwd=toy, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True
    )
    if step.interrupt:
        deadline = time.monotonic() + STEP_TIMEOUT
        while time.monotonic() < deadline and process.poll() is None:
            path = _transcript(toy, session_id)
            if path.is_file() and '"tool_use"' in path.read_text(encoding="utf-8"):
                time.sleep(INTERRUPT_GRACE)
                process.send_signal(signal.SIGINT)
                break
            time.sleep(0.5)
    try:
        _, stderr = process.communicate(timeout=STEP_TIMEOUT)
    except subprocess.TimeoutExpired:
        process.kill()
        raise SystemExit(f"error: claude timed out on {session.name}: {step.prompt!r}") from None
    if process.returncode != 0 and not step.interrupt:
        raise SystemExit(f"error: claude exited {process.returncode}: {stderr.strip()}")


def _claude_version(env: dict[str, str]) -> str:
    result = subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, check=True, env=env
    )
    return result.stdout.split()[0]


def record(out_dir: Path) -> list[str]:
    """Record every session, normalise it into `out_dir`; the shapes missing."""
    if shutil.which("claude") is None:
        raise SystemExit("error: `claude` not found; recording needs a logged-in Claude Code")
    with tempfile.TemporaryDirectory(prefix="corpus-") as tmp:
        root = Path(tmp).resolve()
        toy = _make_toy(root)
        claude_tmp = root / "tmp"
        claude_tmp.mkdir()
        env = {
            **os.environ,
            "PATH": f"{_no_op_hooks(root)}{os.pathsep}{os.environ['PATH']}",
            "CLAUDE_CODE_TMPDIR": str(claude_tmp),
            # The recording host's CLAUDE.md files and memory stay out of the
            # sessions' context, so the answers cannot quote them.
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        }
        env.pop("CLAUDECODE", None)
        sessions: list[tuple[Session, str]] = []
        raw_project = _config_home() / "projects" / claude_project_name(str(toy))
        try:
            for session in SESSIONS:
                session_id = str(uuid.uuid4())
                sessions.append((session, session_id))
                for index, step in enumerate(session.steps):
                    _run_step(step, session, session_id, index == 0, toy, env)
                if not (raw_project / f"{session_id}.jsonl").is_file():
                    raise SystemExit(f"error: no transcript for {session.name} in {raw_project}")
            return _write_corpus(
                raw_project,
                out_dir,
                _host_identity(toy, claude_tmp),
                sessions,
                _claude_version(env),
            )
        finally:
            # The raw sessions hold the account's context; nothing of them stays.
            shutil.rmtree(raw_project, ignore_errors=True)
            for _, session_id in sessions:
                for kind in ("session-env", "file-history", "tasks", "todos"):
                    shutil.rmtree(_config_home() / kind / session_id, ignore_errors=True)


def _write_corpus(
    raw_project: Path,
    out_dir: Path,
    identity: Identity,
    sessions: list[tuple[Session, str]],
    version: str,
) -> list[str]:
    staging = Path(tempfile.mkdtemp(prefix="corpus-out-"))
    try:
        normalise_home(raw_project, staging, identity, [sid for _, sid in sessions])
        leaks = scan_corpus(staging, identity)
        if leaks:
            for name, found in leaks.items():
                print(f"leak in {name}: {sorted(set(found))[:5]}", file=sys.stderr)
            raise SystemExit("error: normalisation left identifying text; corpus not written")
        project = staging / "projects" / TOY_PROJECT
        entries = []
        covered: set[str] = set()
        for index, (session, _) in enumerate(sessions, start=1):
            sid = f"{index:08x}-0000-4000-8000-{index:012x}"
            shapes = detect_shapes(project / f"{sid}.jsonl")
            covered.update(shapes)
            entries.append(
                {
                    "session_id": sid,
                    "name": session.name,
                    "prompts": [step.prompt for step in session.steps],
                    "shapes": shapes,
                }
            )
        missing = [shape for shape in SHAPES if shape not in covered]
        manifest = {
            "claude_code_version": version,
            "model": MODEL,
            "toy_files": TOY_FILES,
            "sessions": entries,
            "missing_shapes": missing,
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        shutil.copytree(staging, out_dir)
        return missing
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--out", type=Path, default=CORPUS_DIR, help="corpus directory")
    args = parser.parse_args(argv)
    missing = record(args.out)
    print(f"corpus written to {args.out}", file=sys.stderr)
    if missing:
        print(f"shapes not captured: {', '.join(missing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
