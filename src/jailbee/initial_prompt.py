"""Initial prompt hand-off: a prompt file in the container plus a label.

The host writes the prompt to a file inside the container (text on stdin,
never on a command line) and marks the container ``pending``.  The primary
agent's autostart step then renames the file on its first launch and passes
its contents to the agent; a restart finds no file and launches plain.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from jailbee.agents import primary_agent
from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus
    from jailbee.orchestration.runner import Runner

INITIAL_PROMPT_LABEL = "user.jailbee.initial_prompt"
PENDING = "pending"
CONSUMED = "consumed"

_DIR = f"/home/{CONTAINER_USERNAME}/.jailbee"
PROMPT_PATH = f"{_DIR}/initial-prompt.md"
CONSUMED_PATH = f"{_DIR}/initial-prompt.consumed.md"
PROMPT_ENV = "JAILBEE_INITIAL_PROMPT_FILE"
MAX_PROMPT_BYTES = 65536
AGENT_PID_PATH = f"{_DIR}/prompted-agent.json"
AGENT_EXIT_PATH = f"{_DIR}/prompted-agent.exit"


class PromptError(ValueError):
    """The initial prompt text is unusable."""


def validate_prompt_text(text: str) -> str:
    """Return `text` unchanged, or raise `PromptError` if blank or too large."""
    if not text.strip():
        raise PromptError("the initial prompt is empty")
    size = len(text.encode("utf-8"))
    if size > MAX_PROMPT_BYTES:
        raise PromptError(f"the initial prompt is {size} bytes; the limit is {MAX_PROMPT_BYTES}")
    return text


def prompt_problem(cfg: Config, runner: Runner | None = None) -> str | None:
    """Why an initial prompt cannot be delivered for `cfg`, or None.

    Under a `runner` the prompted agent is `claude`, not the primary.
    """
    if runner is not None:
        claude = cfg.agents.get("claude")
        if claude is None or not claude.enabled:
            return "agent `claude` is not enabled, so it cannot take an initial prompt"
        if not claude.prompt_args:
            return "agent `claude` has no prompt_args, so it cannot take an initial prompt"
        return None
    primary = primary_agent(cfg)
    if primary.name is None:
        return primary.problem or "no agent is enabled to receive an initial prompt"
    agent = cfg.agents[primary.name]
    if not agent.prompt_args:
        return f"agent `{primary.name}` has no prompt_args, so it cannot take an initial prompt"
    return None


def prompted_run(
    command: str, prompt_args: str, *, report_exit: bool = False, agent: str = ""
) -> str:
    """Step `run` string: consume the prompt file once, else launch plain.

    With ``report_exit`` a foreground subshell records its process identity
    before exec, and the parent records every exit status. The broker owns
    reporting, including loss of this wrapper, and suppresses intentional stops.
    """
    if not report_exit:
        return (
            f"if [ -f {PROMPT_PATH} ]; then mv -f {PROMPT_PATH} {CONSUMED_PATH} && "
            f"exec {command} {prompt_args}; fi; exec {command}"
        )
    record = (
        "import json, pathlib, sys; "
        "p = pathlib.Path('/proc') / sys.argv[1]; "
        "d = dict(pid=int(sys.argv[1]), "
        "start=(p / 'stat').read_text().rsplit(')', 1)[1].split()[19], "
        "boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip(), "
        "agent=sys.argv[3]); "
        "f = pathlib.Path(sys.argv[2]); "
        "t = f.with_suffix('.tmp'); t.write_text(json.dumps(d)); t.replace(f)"
    )
    return (
        f"if [ -f {PROMPT_PATH} ]; then mv -f {PROMPT_PATH} {CONSUMED_PATH} && "
        f"{{ umask 077; ( python3 -c {shlex.quote(record)} "
        f'"$BASHPID" {shlex.quote(AGENT_PID_PATH)} {shlex.quote(agent)}; '
        f"exec {command} {prompt_args} ); rc=$?; "
        f'printf "%s\\n" "$rc" > {shlex.quote(AGENT_EXIT_PATH)}.tmp; '
        f"mv -f {shlex.quote(AGENT_EXIT_PATH)}.tmp {shlex.quote(AGENT_EXIT_PATH)}; "
        f'exit "$rc"; }}; fi; exec {command}'
    )


def write_prompt(incus: Incus, name: str, text: str, *, uid: int, gid: int) -> None:
    """Write the prompt file (mode 0600, container user) and mark it pending."""
    incus.exec_with_input(
        name,
        [
            "bash",
            "-c",
            f'umask 077; mkdir -p "$(dirname "$1")" && '
            f"rm -f {shlex.quote(AGENT_PID_PATH)} {shlex.quote(AGENT_EXIT_PATH)} && "
            'cat > "$1.tmp" && mv -f "$1.tmp" "$1"',
            "bash",
            PROMPT_PATH,
        ],
        text,
        uid=uid,
        gid=gid,
        timeout=30,
    )
    incus.config_set(name, INITIAL_PROMPT_LABEL, PENDING)


def prompt_pending(incus: Incus, name: str, *, uid: int) -> bool:
    """True when a prompt is still waiting; flips the label once it is gone."""
    if incus.config_get(name, INITIAL_PROMPT_LABEL) != PENDING:
        return False
    try:
        out = incus.exec(
            name,
            ["bash", "-c", '[ -f "$1" ] && echo yes || echo no', "bash", PROMPT_PATH],
            uid=uid,
            timeout=15,
        )
    except IncusError:
        return False
    if out.strip() == "yes":
        return True
    incus.config_set(name, INITIAL_PROMPT_LABEL, CONSUMED)
    return False
