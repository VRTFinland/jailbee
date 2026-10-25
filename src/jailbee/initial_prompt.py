"""Initial prompt hand-off: a prompt file in the container plus a label.

The host writes the prompt to a file inside the container (text on stdin,
never on a command line) and marks the container ``pending``.  The primary
agent's autostart step then renames the file on its first launch and passes
its contents to the agent; a restart finds no file and launches plain.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jailbee.agents import primary_agent
from jailbee.config import CONTAINER_USERNAME
from jailbee.incus import IncusError

if TYPE_CHECKING:
    from jailbee.config import Config
    from jailbee.incus import Incus

INITIAL_PROMPT_LABEL = "user.jailbee.initial_prompt"
PENDING = "pending"
CONSUMED = "consumed"

_DIR = f"/home/{CONTAINER_USERNAME}/.jailbee"
PROMPT_PATH = f"{_DIR}/initial-prompt.md"
CONSUMED_PATH = f"{_DIR}/initial-prompt.consumed.md"
PROMPT_ENV = "JAILBEE_INITIAL_PROMPT_FILE"
MAX_PROMPT_BYTES = 65536


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


def prompt_problem(cfg: Config) -> str | None:
    """Why an initial prompt cannot be delivered for `cfg`, or None."""
    primary = primary_agent(cfg)
    if primary.name is None:
        return primary.problem or "no agent is enabled to receive an initial prompt"
    agent = cfg.agents[primary.name]
    if not agent.prompt_args:
        return f"agent `{primary.name}` has no prompt_args, so it cannot take an initial prompt"
    return None


def prompted_run(command: str, prompt_args: str) -> str:
    """Step `run` string: consume the prompt file once, else launch plain."""
    return (
        f"if [ -f {PROMPT_PATH} ]; then mv -f {PROMPT_PATH} {CONSUMED_PATH} && "
        f"exec {command} {prompt_args}; fi; exec {command}"
    )


def write_prompt(incus: Incus, name: str, text: str, *, uid: int, gid: int) -> None:
    """Write the prompt file (mode 0600, container user) and mark it pending."""
    incus.exec_with_input(
        name,
        [
            "bash",
            "-c",
            'umask 077; mkdir -p "$(dirname "$1")" && cat > "$1.tmp" && mv -f "$1.tmp" "$1"',
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
