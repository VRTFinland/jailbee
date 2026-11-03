"""Standard-library command entry point for the inbox port."""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from jailbee.inbox.errors import InboxUsage

PROG = "jailbee inbox"
USAGE = "Usage: jailbee inbox wait [--timeout DUR|none] [--wake-on LIST] [--quiet-after DUR]\n       jailbee inbox ls\n       jailbee inbox read <id> [--keep]\n       jailbee inbox runners [--json]\n       jailbee inbox children [--json]\n       jailbee inbox send <type> [options]\n\n  wait   Print the path of the oldest unread message (exit 0), waiting up to\n         DUR (seconds, or <n>s|m|h; default 30m) for one to arrive; exit 1\n         on timeout. --timeout 0 checks once; --timeout none waits until a message\n         arrives or the process is interrupted (use it for a background waiter).\n         After an arrival, wait briefly for the burst to settle; then use ls to\n         handle every unread message.\n         --wake-on LIST filters comma-separated message kinds/status states\n         (note,question,done,blocked,idle,queued,started,failed,refused). A child's\n         first message after each start always wakes. Nothing is marked read.\n         --quiet-after DUR prints `quiet-child CHILD` (exit 0) when a running\n         child is silent that long; needs fresh broker children.json (<=15m).\n         Only one wait may run; a second exits 2. Concurrent send --wait is\n         safe: wait skips its reserved statuses until that sender exits.\n  ls     One line per unread message, oldest first:\n         <id> <type> <from or ref> <kind or state> <first 60 chars of text>\n  read   Pretty-print message <id> and move it to inbox/read/ (--keep leaves\n         it in place); exit 1 if no unread message has that id.\n\n  runners  List the default runner and the runners a spawn or restart may name\n         (--agent/--profile/--model of send). --json prints one array.\n\n  children [--json]  Each child: name, running, latest agent session (from the\n         broker's children.json).\n\n  send   Write a manifest for the orchestrator's host into outbox/ and print\n         its id. --wait [--timeout DUR|none] waits for its status (default 30m):\n         exit 0 on done/queued/started, 1 on refused/failed, 3 on timeout.\n         Text (body or prompt) comes from --text T, --file PATH or -\n         (stdin); at most 65536 bytes, not empty.\n           message  [--to C] [--kind note|question|done|blocked]\n                    [--reply-to ID] + text\n           spawn    [--branch B] [--new [--base B] | --ref REF | --pr N]\n                    [--mount M]... [--loose] [--egress H]... [--note T]\n                    [--on-done destroy|keep] [--autostart-profile NAME] [--services LIST] [runner] + text\n                    --branch is required except for --pr; the broker derives\n                    the branch from the PR and ignores any supplied branch.\n           refresh  --child C --services LIST [--wait]  Refresh services in a child (prepare + restart); elevated prepares stop and resume its agent.\n           restart  --child C [--on-done destroy|keep] [--autostart-profile NAME] [--services LIST]\n                    [--agent-only] [--resume ID|last] [runner | --default-runner] + text\n                    --resume continues a recorded agent session (see children).\n                    --agent-only relaunches an idle RUNNING child's agent only;\n                    no boot/on_start, no extra slot, cannot use --autostart-profile or --services.\n                    --services selects comma-separated services; an empty list starts none.\n           update   --child C [--ref REF | --base BRANCH] [--merge]\n                    --merge merges the update into the child's branch after a\n                    conflict check; a conflict refuses it.\n           import | stop   --child C\n           destroy  --child C [--force]\n           rights   [--child C] [--loose] [--mount M]... [--egress H]... --for DUR\n                    [--revoke-loose] [--revoke-mount M]... [--revoke-egress H]...\n                    `send rights` changes a child's rights at runtime. A grant needs --for DUR (s|m|h); revokes need no duration. A\n                    child may only revoke its own rights; an orchestrator names\n                    --child. Refused while autostart runs in the child.\n         runner = --agent claude|claude-jb [--profile P (claude-jb only)]\n                  [--model fable|opus|sonnet|haiku]; see `runners`.\n                  --default-runner (restart only) clears the child's runner.\n\nEnvironment: JAILBEE_ORCHESTRATION_DIR (default ~/.jailbee/orchestration),\nJAILBEE_LITELLM_CONFIG (default /etc/jailbee/litellm.json), JAILBEE_DEFAULT_AGENT,\nJAILBEE_INBOX_POLL (poll seconds without inotifywait; default 5),\nJAILBEE_INBOX_SETTLE (quiet seconds after an arrival; default 1, 0 disables).\nJAILBEE_INBOX_INOTIFY=0 forces polling.\n"

@dataclass
class Settings:
    base: Path
    inbox: Path
    outbox: Path
    poll: float
    settle: float


def load_settings(environ: Mapping[str, str]) -> Settings:
    base = Path(environ.get("JAILBEE_ORCHESTRATION_DIR") or (environ.get("HOME", "") + "/.jailbee/orchestration"))
    settle = environ.get("JAILBEE_INBOX_SETTLE") or "1"
    poll = environ.get("JAILBEE_INBOX_POLL") or "5"
    pattern = r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)"
    if not re.fullmatch(pattern, settle):
        raise InboxUsage(f"JAILBEE_INBOX_SETTLE must be a non-negative number of seconds, got '{settle}'")
    if not re.fullmatch(pattern, poll) or not re.search(r"[1-9]", poll):
        raise InboxUsage(f"JAILBEE_INBOX_POLL must be a positive number of seconds, got '{poll}'")
    return Settings(base, base / "inbox", base / "outbox", float(poll), float(settle))


def parse_duration(text: str) -> int:
    match = re.fullmatch(r"([0-9]+)([smh]?)", text)
    if not match:
        raise InboxUsage(f"invalid duration '{text}' (use seconds or <n>s|m|h)")
    return int(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def parse_timeout(text: str) -> int | None:
    return None if text == "none" else parse_duration(text)


def main(argv: list[str]) -> int:
    try:
        load_settings(os.environ)
        if not argv or argv[0] not in {"wait", "ls", "read", "runners", "children", "send", "help", "-h", "--help"}:
            print(USAGE, end="", file=sys.stderr)
            return 2
        if argv[0] in {"help", "-h", "--help"}:
            print(USAGE, end="")
            return 0
        raise InboxUsage("not implemented")
    except InboxUsage as exc:
        print(f"{PROG}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
