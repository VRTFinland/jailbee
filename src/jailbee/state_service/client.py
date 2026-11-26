"""A dashboard's connection to the state service.

`StateClient` connects — spawning the server if none answers — and keeps the
latest snapshot from a reader thread. It never gathers anything itself: when
the service is unreachable the dashboard says so and keeps its last snapshot,
rather than silently falling back to polling incus on its own.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from typing import IO, TYPE_CHECKING, Any

from jailbee import __version__
from jailbee.state_service import StaleDashboard, StateServiceError, StateServiceUnavailable
from jailbee.state_service.paths import ensure_runtime_dir, log_path, socket_path, spawn_lock_path
from jailbee.state_service.protocol import (
    MAX_WIRE_LINE_BYTES,
    PROTOCOL,
    ActionDismissed,
    ActionEntry,
    ActionFinish,
    ActionOutput,
    ActionQuestion,
    ActionQuestionResolved,
    ActionRejected,
    ActionSyncEnd,
    ActionSyncStart,
    Active,
    ActivityQuery,
    AnswerActionQuestion,
    Drain,
    DismissAction,
    GatherError,
    Hello,
    Message,
    ProtocolError,
    Refresh,
    RunAction,
    ServiceActivity,
    Shutdown,
    Snapshot,
    decode,
    encode,
)
from jailbee.upgrade import parse_version

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

ACTIVITY_PROTOCOL = 50
ACTION_HOST_PROTOCOL = 45
CONNECT_TIMEOUT_SECONDS = 5.0
BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 10.0)
SETTLE_SECONDS = 8.0
"""How long the service gathers fast after `StateClient.refresh`."""
DISCONNECTED = "state service disconnected — reconnecting"


def spawn_server() -> None:
    """Start `jailbee _state-service` detached, its output appended to the log."""
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as logf:
        subprocess.Popen(
            [sys.executable, "-m", "jailbee", "_state-service"],
            stdin=subprocess.DEVNULL,
            stdout=logf,
            stderr=logf,
            cwd="/",  # do not pin the first dashboard's directory for the server's lifetime
            start_new_session=True,
        )


@contextlib.contextmanager
def _flock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _try_connect(path: Path) -> socket.socket | None:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError):
        sock.close()
        return None
    return sock


class StateClient:
    """One dashboard's live view of the state service.

    ``on_update`` is called from the reader thread after every snapshot or
    status change — the Qt frontend turns it into a signal; the TUI polls
    `latest`/`status` each frame and passes none.
    """

    def __init__(
        self,
        cwd_root: Path | None,
        *,
        on_update: Callable[[], None] | None = None,
        dashboard_id: str | None = None,
        excluded_prefixes: Sequence[str] = (),
        spawn: Callable[[], None] = spawn_server,
        version: str = __version__,
        connect_timeout: float = CONNECT_TIMEOUT_SECONDS,
        backoff: Sequence[float] = BACKOFF_SECONDS,
    ) -> None:
        self._dashboard_id = dashboard_id
        self._cwd_root = cwd_root
        self._excluded_prefixes = list(excluded_prefixes)
        self._on_update = on_update
        self._spawn = spawn
        self._version = version
        self._connect_timeout = connect_timeout
        self._backoff = backoff
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._latest: Snapshot | None = None
        self._action_entries: dict[int, ActionEntry] = {}
        self._action_questions: dict[int, ActionQuestion] = {}
        self._replay_questions: dict[int, ActionQuestion] | None = None
        self._question_resolutions: deque[ActionQuestionResolved] = deque()
        self._action_rejections: dict[int, str] = {}
        self._replay_entries: dict[int, ActionEntry] | None = None
        self._connection_generation = 0
        self._action_synced = False
        self._status: str | None = None
        self._active = True
        self._closed = threading.Event()
        self._first = threading.Event()
        self._replacement_changed = threading.Event()
        self._replacement: ServiceActivity | None = None
        self._replacement_pending = False
        self._replacement_answer: bool | None = None
        self._replacement_waiting = False
        self._ever_connected = False
        self._thread = threading.Thread(target=self._run, name="jailbee-state-client", daemon=True)

    @property
    def dashboard_id(self) -> str | None:
        """Identity advertised to the state service for hosted actions."""
        return self._dashboard_id

    @property
    def excluded_prefixes(self) -> tuple[str, ...]:
        """Repository prefixes excluded from this dashboard's view."""
        return tuple(self._excluded_prefixes)

    # ---- the dashboard's side ------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def wait_first_snapshot(self, timeout: float) -> Snapshot:
        """The first snapshot, or `StateServiceUnavailable` after ``timeout``
        (at once when this dashboard turned out to be stale)."""
        deadline = time.monotonic() + timeout
        while not self._first.is_set():
            with self._lock:
                pending = self._replacement_pending
                activity = self._replacement
                waiting = self._replacement_waiting
            if pending:
                answer = self._ask_replacement(activity)
                with self._lock:
                    self._replacement_answer = answer
                    self._replacement_pending = False
                self._replacement_changed.set()
                deadline = time.monotonic() + timeout
                continue
            if waiting:
                deadline = time.monotonic() + timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = self.status() or "no snapshot from the state service"
                raise StateServiceUnavailable(f"{reason} (log: {log_path()})")
            self._first.wait(min(remaining, 0.05))
        snapshot = self.latest()
        if snapshot is None:  # `_first` was set by a stale stop, not a snapshot
            raise StateServiceUnavailable(self.status() or "the state client stopped")
        return snapshot

    def latest(self) -> Snapshot | None:
        with self._lock:
            return self._latest

    def status(self) -> str | None:
        """None while healthy; otherwise what is wrong, ready to show."""
        with self._lock:
            return self._status

    def refresh(self, settle: float = SETTLE_SECONDS) -> None:
        """Gather now, then keep gathering fast for ``settle`` seconds."""
        self._send(Refresh(settle))

    def set_active(self, value: bool) -> None:
        with self._lock:
            self._active = value
        self._send(Active(value))

    def run_action(self, request: RunAction) -> None:
        """Send an action request; raise when disconnected instead of dropping it."""
        with self._lock:
            if self._sock is None:
                raise StateServiceUnavailable("state service disconnected")
            self._sock.sendall(encode(request))

    def action_connection(self) -> int | None:
        """Generation of the connected socket after its action replay completes."""
        with self._lock:
            return (
                self._connection_generation
                if self._sock is not None and self._action_synced
                else None
            )

    def action_rejection(self, entry_id: int) -> str | None:
        with self._lock:
            return self._action_rejections.get(entry_id)

    def action_entries(self) -> dict[int, ActionEntry]:
        with self._lock:
            return self._action_entries.copy()

    def action_questions(self) -> dict[int, ActionQuestion]:
        with self._lock:
            return self._action_questions.copy()

    def drain_question_resolutions(self) -> list[ActionQuestionResolved]:
        with self._lock:
            resolutions = list(self._question_resolutions)
            self._question_resolutions.clear()
            return resolutions

    def answer_action_question(self, entry_id: int, question_id: int, value: Any) -> None:
        with self._lock:
            if self._sock is None:
                raise StateServiceUnavailable("state service disconnected")
            self._sock.sendall(encode(AnswerActionQuestion(entry_id, question_id, value)))

    def dismiss_action(self, entry_id: int) -> None:
        """Dismiss a finished shared entry for every dashboard."""
        self._send(DismissAction(entry_id))

    def close(self) -> None:
        self._closed.set()
        self._replacement_changed.set()
        self._first.set()
        with self._lock:
            if self._sock is not None:
                with contextlib.suppress(OSError):
                    self._sock.shutdown(socket.SHUT_RDWR)
        if self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # ---- the reader thread ---------------------------------------------------

    def _send(self, message: Message) -> None:
        """Best effort: a message to a dead connection is dropped, and the
        reader thread's reconnect re-sends what matters (the active flag)."""
        with self._lock:
            sock = self._sock
            if sock is None:
                return
            with contextlib.suppress(OSError):
                sock.sendall(encode(message))

    def _set_status(self, text: str | None) -> None:
        with self._lock:
            changed = self._status != text
            self._status = text
        if changed:
            self._notify()

    def _notify(self) -> None:
        """Call ``on_update``; a failing callback must not kill the reader."""
        if self._on_update is None:
            return
        try:
            self._on_update()
        except Exception:
            log.exception("state client on_update callback failed")

    def _run(self) -> None:
        attempt = 0
        while not self._closed.is_set():
            try:
                sock, reader = self._connect()
            except StaleDashboard as exc:
                # Retrying would respawn a server after each idle exit, and
                # the newer one is what everyone else is already using.
                self._set_status(str(exc))
                self._first.set()  # wakes `wait_first_snapshot`
                return
            except (OSError, ProtocolError, StateServiceError) as exc:
                log.debug("state service connect failed", exc_info=True)
                self._set_status(f"state service unavailable: {exc}")
                self._closed.wait(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1
                continue
            with self._lock:
                if self._closed.is_set():
                    # close() ran while we were connecting: it saw no socket
                    # to shut down, so this connection is ours to drop.
                    reader.close()
                    sock.close()
                    return
                self._sock = sock
                self._connection_generation += 1
                self._action_synced = False
                # Replayed under the lock, so a concurrent `set_active` lands
                # wholly before this send (and is what we replay) or after it.
                if not self._active:
                    with contextlib.suppress(OSError):
                        sock.sendall(encode(Active(False)))
            with self._lock:
                self._replay_entries = None
            got_snapshot = False
            try:
                while line := reader.readline(MAX_WIRE_LINE_BYTES + 1):
                    if len(line) > MAX_WIRE_LINE_BYTES or not line.endswith(b"\n"):
                        raise ProtocolError("state service message exceeds wire line limit")
                    if self._dispatch(decode(line)):
                        got_snapshot = True
            except (OSError, ProtocolError):
                log.debug("state service connection lost", exc_info=True)
            finally:
                with self._lock:
                    self._sock = None
                    self._action_synced = False
                reader.close()
                sock.close()
            if self._closed.is_set():
                return
            self._set_status(DISCONNECTED)
            if got_snapshot:
                attempt = 0
            else:
                # The server answered but never delivered: back off rather
                # than hammer a server that drops us straight away.
                self._closed.wait(self._backoff[min(attempt, len(self._backoff) - 1)])
                attempt += 1

    def _dispatch(self, message: Message) -> bool:
        """Apply ``message``; True when it was a snapshot."""
        if isinstance(message, ActionSyncStart):
            with self._lock:
                self._replay_entries = {}
                self._replay_questions = {}
            return False
        if isinstance(message, ActionSyncEnd):
            with self._lock:
                if self._replay_entries is not None:
                    self._action_entries = self._replay_entries
                    self._replay_entries = None
                    self._action_synced = True
                if self._replay_questions is not None:
                    self._action_questions = self._replay_questions
                    self._replay_questions = None
            self._notify()
            return False
        if isinstance(message, (ActionQuestion, ActionQuestionResolved)):
            with self._lock:
                questions = (
                    self._replay_questions
                    if self._replay_questions is not None
                    else self._action_questions
                )
                if isinstance(message, ActionQuestion):
                    questions[message.entry_id] = message
                elif (
                    current := questions.get(message.entry_id)
                ) is not None and current.question_id == message.question_id:
                    del questions[message.entry_id]
                    self._question_resolutions.append(message)
            self._notify()
            return False
        if isinstance(message, ActionDismissed):
            with self._lock:
                self._action_entries.pop(message.entry_id, None)
            self._notify()
            return False
        if isinstance(message, (ActionEntry, ActionOutput, ActionFinish, ActionRejected)):
            from jailbee.dashboard.runner import OUTPUT_DROPPED_MARKER, OUTPUT_KEEP_CHARS

            with self._lock:
                entries = (
                    self._replay_entries
                    if self._replay_entries is not None
                    else self._action_entries
                )
                if isinstance(message, ActionRejected):
                    self._action_rejections.pop(message.entry_id, None)
                    self._action_rejections[message.entry_id] = message.reason
                    if len(self._action_rejections) > 256:
                        del self._action_rejections[next(iter(self._action_rejections))]
                elif isinstance(message, ActionEntry):
                    entries[message.entry_id] = message
                elif not isinstance(message, ActionRejected) and message.entry_id in entries:
                    entry = entries[message.entry_id]
                    if isinstance(message, ActionOutput):
                        output = entry.output + message.chunk
                        if len(output) > OUTPUT_KEEP_CHARS:
                            output = OUTPUT_DROPPED_MARKER + output[-OUTPUT_KEEP_CHARS:]
                        entries[message.entry_id] = replace(entry, output=output)
                    else:
                        entries[message.entry_id] = replace(
                            entry,
                            finished=True,
                            status=message.status,
                            refused=message.refused,
                            error=message.error,
                            cancelled=message.cancelled,
                            declined=message.declined,
                        )
            self._notify()
            return False
        if isinstance(message, Snapshot):
            with self._lock:
                self._latest = message
                self._status = None
            self._ever_connected = True
            self._first.set()
            self._notify()
            return True
        if isinstance(message, GatherError):
            self._set_status(f"refresh failed: {message.message}")
        return False

    def _connect(self) -> tuple[socket.socket, IO[bytes]]:
        ensure_runtime_dir()
        path = socket_path()
        sock = _try_connect(path)
        if sock is None:
            with _flock(spawn_lock_path()):
                sock = _try_connect(path) or self._spawn_and_connect(path)
        return self._handshake(sock, path)

    def _spawn_and_connect(self, path: Path) -> socket.socket:
        self._spawn()
        deadline = time.monotonic() + self._connect_timeout
        while time.monotonic() < deadline:
            sock = _try_connect(path)
            if sock is not None:
                return sock
            time.sleep(0.05)
        raise StateServiceError(f"the state service did not start (log: {log_path()})")

    def _handshake(self, sock: socket.socket, path: Path) -> tuple[socket.socket, IO[bytes]]:
        sock.settimeout(self._connect_timeout)
        reader = sock.makefile("rb")
        try:
            cwd = str(self._cwd_root) if self._cwd_root is not None else None
            sock.sendall(
                encode(
                    Hello(PROTOCOL, self._version, cwd, self._dashboard_id, self._excluded_prefixes)
                )
            )
            line = reader.readline(MAX_WIRE_LINE_BYTES + 1)
            if len(line) > MAX_WIRE_LINE_BYTES or not line.endswith(b"\n"):
                raise ProtocolError("invalid or oversized state service hello")
            reply = decode(line)
            if not isinstance(reply, Hello):
                raise ProtocolError(f"expected hello, got {type(reply).__name__}")
            if (reply.protocol, reply.version) != (PROTOCOL, self._version):
                # The newer jailbee wins: a stale dashboard must not shut
                # down a newer server (its respawn runs the new code, so the
                # two would flap forever). A higher protocol integer is newer
                # whatever the version strings say; at equal protocol the
                # version decides. Otherwise (older server, unparseable
                # version) the server is replaced by our own.
                theirs, ours = parse_version(reply.version), parse_version(self._version)
                newer = reply.protocol > PROTOCOL or (
                    reply.protocol == PROTOCOL and theirs and ours and theirs > ours
                )
                if newer:
                    raise StaleDashboard(
                        f"jailbee was upgraded to {reply.version}; restart this dashboard"
                    )
                if reply.protocol < ACTION_HOST_PROTOCOL:
                    sock.sendall(encode(Shutdown()))
                    self._await_gone(path)
                else:
                    activity = None
                    if reply.protocol >= ACTIVITY_PROTOCOL:
                        sock.sendall(encode(ActivityQuery()))
                        response = decode(reader.readline(MAX_WIRE_LINE_BYTES + 1))
                        if not isinstance(response, ServiceActivity):
                            raise ProtocolError("expected service activity")
                        activity = response
                    if activity is None or activity.running_actions or activity.pending_questions:
                        replace = self._request_replacement(activity)
                    else:
                        replace = False
                    if replace:
                        sock.sendall(encode(Shutdown()))
                        self._await_gone(path)
                        with self._lock:
                            self._replacement_waiting = False
                    elif activity is None:
                        self._wait_for_legacy_exit(sock)
                    else:
                        sock.sendall(encode(Drain()))
                        response = decode(reader.readline(MAX_WIRE_LINE_BYTES + 1))
                        if not isinstance(response, ServiceActivity) or not response.draining:
                            raise ProtocolError("expected drain acknowledgement")
                        self._wait_for_legacy_exit(sock)
                raise StateServiceError(f"replaced a state service from jailbee {reply.version}")
        except BaseException:
            reader.close()
            sock.close()
            raise
        sock.settimeout(None)
        return sock, reader

    def _ask_replacement(self, activity: ServiceActivity | None) -> bool:
        from jailbee import prompting

        if not prompting.is_interactive():
            return False
        detail = (
            "an unknown number of actions"
            if activity is None
            else f"{activity.running_actions} actions and {activity.pending_questions} pending questions"
        )
        return prompting.choose_one(
            "state service replacement",
            [
                prompting.Option(False, "Wait for the old service", "wait"),
                prompting.Option(True, "Replace it and interrupt its actions", "replace"),
            ],
            question=f"An older state service has {detail}. What should happen?",
            default=False,
        )

    def _request_replacement(self, activity: ServiceActivity | None) -> bool:
        from jailbee import prompting

        ask = not self._ever_connected and prompting.is_interactive()
        with self._lock:
            self._replacement_waiting = True
            self._replacement = activity
            self._replacement_answer = None
            self._replacement_pending = ask
        self._set_status("waiting for older state service")
        if not ask:
            return False
        while not self._closed.is_set():
            self._replacement_changed.wait(0.1)
            self._replacement_changed.clear()
            with self._lock:
                answer = self._replacement_answer
            if answer is not None:
                return answer
        raise StateServiceError("state client closed")

    def _wait_for_legacy_exit(self, sock: socket.socket) -> None:
        with self._lock:
            self._replacement_waiting = True
        self._set_status("waiting for older state service to finish")
        sock.settimeout(0.2)
        try:
            while not self._closed.is_set():
                try:
                    if not sock.recv(4096):
                        break
                except TimeoutError:
                    continue
        finally:
            with self._lock:
                self._replacement_waiting = False
        if self._closed.is_set():
            raise StateServiceError("state client closed")

    def _await_gone(self, path: Path) -> None:
        deadline = time.monotonic() + self._connect_timeout
        while path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
