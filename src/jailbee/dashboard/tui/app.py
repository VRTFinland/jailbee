"""The native terminal dashboard around one DashboardSession.

Keys, timer, hand-off, title and mouse; the frame and its widgets draw what
the session holds.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Any

from textual import events
from textual.app import App, ComposeResult
from textual.geometry import Size
from textual.message import Message

from jailbee.config.models_remote import RemotePolicy
from jailbee.dashboard import runner as drunner
from jailbee.dashboard.commands import Transport
from jailbee.dashboard.hit import Hit
from jailbee.dashboard.runner import ActionOutcome, ActionRun
from jailbee.dashboard.tui import scrollbar
from jailbee.dashboard.tui.frame import DashboardView
from jailbee.dashboard.tui.keys import parse_key
from jailbee.dashboard.tui.layout import FRAME_INSET_COLS
from jailbee.dashboard.tui.native import (
    AccountsBox,
    CommandBox,
    EgressBox,
    MenuBox,
    OverlayBox,
    PickerBox,
    PromptBox,
    SettingsBox,
    TextBox,
)
from jailbee.dashboard.tui.outbox_box import OutboxBox
from jailbee.dashboard.tui.overlay import overlay_key
from jailbee.dashboard.tui.prompt_modals import LATER, OutputScreen, modal_for
from jailbee.dashboard.tui.session import (
    DOUBLE_CLICK_KINDS,
    OVERLAY_GLOBAL_TOKENS,
    DashboardSession,
    Outcome,
    Startup,
    open_dashboard,
)
from jailbee.dashboard.tui.terminal import terminal_title_scope, title_sequence
from jailbee.dashboard.tui.widgets import DashboardFrame, DetailsPanel, FleetTable
from jailbee.prompt_channel import Prompt
from jailbee.remote_ssh.repo_scope import RemoteRepoScope

if TYPE_CHECKING:
    from textual.driver import Driver

    from jailbee.incus import Incus

TICK_SECONDS = 0.25


def _can_suspend(driver: Driver | None) -> bool:
    """Whether ``App.suspend`` can hand the terminal over (not headless, not web)."""
    return driver is not None and driver.can_suspend


def _set_mouse_reporting(driver: Driver | None, on: bool) -> None:
    """Switch the terminal's mouse reporting, and keep it switched across hand-offs.

    Textual 8.2.8 has no public toggle. Its driver writes the reporting modes
    from the private ``_mouse`` flag at start and again on every resume after
    ``suspend()``, so the flag is flipped too: otherwise `jb tmux` and back
    would silently turn the mouse on again.
    """
    if driver is None:
        return
    if on:
        driver._mouse = True
        enable = getattr(driver, "_enable_mouse_support", None)
        if enable is not None:
            enable()
    else:
        disable = getattr(driver, "_disable_mouse_support", None)
        if disable is not None:
            disable()
        driver._mouse = False


logger = logging.getLogger(__name__)


class _OnAppThread:
    """A :class:`~jailbee.dashboard.runner.RunProgress` whose calls land on the app thread.

    Output never blocks the runner's pump: chunks pile up under a lock and at
    most one flush is posted, which hands the app thread their coalesced text
    (a blocking hand-off would stall the child once its pipe fills while the
    app thread is busy, e.g. in a tmux hand-off). A question blocks, after
    draining the output before it; :meth:`drain` settles what is still pending
    before the batch finishes.
    """

    def __init__(
        self,
        inner: drunner.RunProgress,
        on_app_thread: Callable[[Callable[[], None]], None],
        post: Callable[[Callable[[], None]], None],
    ) -> None:
        self._inner = inner
        self._on_app_thread = on_app_thread
        self._post = post
        self._lock = threading.Lock()
        self._pending: list[tuple[int, str]] = []
        self._scheduled = False

    def entry_id(self, index: int) -> int:
        return self._inner.entry_id(index)

    def output(self, index: int, chunk: str) -> None:
        with self._lock:
            self._pending.append((index, chunk))
            if self._scheduled:
                return
            self._scheduled = True
        self._post(self.drain)

    def drain(self) -> None:
        """Hand the pending output to the inner progress, one call per run; app thread only."""
        with self._lock:
            pending, self._pending = self._pending, []
            self._scheduled = False
        merged: dict[int, str] = {}
        for index, chunk in pending:
            merged[index] = merged.get(index, "") + chunk
        for index, text in merged.items():
            self._inner.output(index, text)

    def question(self, index: int, asking: bool) -> None:
        def ask() -> None:
            self.drain()
            self._inner.question(index, asking)

        self._on_app_thread(ask)


class DashboardApp(App[int], inherit_bindings=False):
    """The dashboard's Textual frontend: one frame, keys, timer, hand-off, title.

    ``inherit_bindings=False`` drops Textual's own ``ctrl+c``/``ctrl+q``/
    ``ctrl+p`` bindings: every key is routed by `on_event`, so Ctrl-C can
    cancel a prompt instead of quitting.
    """

    CSS = """
    Screen { background: ansi_default; layers: base float; }
    #frame { layer: base; background: ansi_default; color: ansi_default; }
    """
    ENABLE_COMMAND_PALETTE = False
    # Textual's selection binds double-click (select all) and a copy key that
    # would compete with the dashboard's own; the terminal's Shift-drag stays.
    ALLOW_SELECT = False

    def __init__(
        self,
        startup: Startup,
        *,
        incus: Incus,
        cwd_root: Path | None,
        remote: bool = False,
        transport: Transport = "none",
        ssh_policy: RemotePolicy | None = None,
        scope: RemoteRepoScope | None = None,
        tick_seconds: float | None = TICK_SECONDS,
        auto_optimize: bool = True,
        remote_client: str | None = None,
    ) -> None:
        super().__init__(ansi_color=True)
        scrollbar.install()
        self.session = DashboardSession(
            startup,
            incus=incus,
            cwd_root=cwd_root,
            terminal=self,
            remote=remote,
            transport=transport,
            ssh_policy=ssh_policy,
            scope=scope,
            auto_optimize=auto_optimize,
        )
        self.session.url_opener = self.open_url
        # Who is at the other end of a remote session (the SSH key's fingerprint);
        # written into the audit record of every action this dashboard runs.
        self.remote_client = remote_client
        self._tick_seconds = tick_seconds
        self._painted: tuple[DashboardView, Size] | None = None
        self._last_title: str | None = None
        self.hover: Hit | None = None
        self.quit_requested = False
        self.mouse_on = startup.mouse
        self._last_click: Hit | None = None
        # Test seams: the runner (swap in a fake) and whether actions run on a
        # worker thread (False runs them inline, for runners that never ask).
        self.action_runner: Callable[..., drunner.RunResult] = drunner.run_action
        self.action_spawn: drunner.Spawner | None = None
        self.threaded_actions = True
        self._ui_thread = threading.get_ident()
        self._pending_prompts: set[asyncio.Future[Any]] = set()
        self._prompts_closed = False
        self._entry_answers: dict[int, asyncio.Future[Any]] = {}
        self._disconnected_entries: set[int] = set()
        self._shown_questions: set[int] = set()
        self._output_screen: OutputScreen | None = None  # the one a live entry follows
        self._critical_screen: OutputScreen | None = None

    # --- Terminal protocol -------------------------------------------------

    @property
    def table_width(self) -> int:
        # Private flag (like the driver's in ``_set_mouse_reporting``): the public
        # ``App.is_mounted`` needs a widget, and ``frame`` raises before compose ends.
        if self._is_mounted:
            width = self.frame.table.content_width
            if width > 0:
                return width
        return max(0, self.size.width - FRAME_INSET_COLS)

    def hand_off(self, fn: Callable[[], int]) -> int:
        """Run ``fn`` on the real terminal with the dashboard suspended."""
        client = self.session.client
        attention_before = self.session.queue.attention_items()
        self.session.begin_hand_off()
        # Nothing of this dashboard is on screen while `fn` runs: let the
        # shared service stop gathering on its behalf.
        client.set_active(False)
        suspended: AbstractContextManager[None] = (
            self.suspend() if _can_suspend(self._driver) else nullcontext()
        )
        try:
            with suspended:
                return fn()
        finally:
            # The snapshot is as old as the command was long.
            client.set_active(True)
            client.refresh()
            self.session.end_hand_off(attention_before)
            # `fn` may have set its own OSC 2 title: rewrite ours on the next frame.
            self._last_title = None
            self._painted = None

    # --- action runner -----------------------------------------------------

    @property
    def modal_open(self) -> bool:
        """Whether a question or an output screen is on top of the dashboard."""
        return self._is_mounted and len(self.screen_stack) > 1

    def run_actions(
        self,
        runs: Sequence[ActionRun],
        done: Callable[[ActionOutcome], None],
        progress: drunner.RunProgress | None = None,
    ) -> None:
        """Run the actions on a worker thread; ``done`` and ``progress`` run on the app thread."""
        session = self.session
        extra: dict[str, Any] = {"spawn": self.action_spawn} if self.action_spawn else {}

        relay = (
            None
            if progress is None
            else _OnAppThread(progress, self._on_app_thread, self._post_to_app_thread)
        )

        def work() -> None:
            try:
                outcome = drunner.run_actions(
                    runs,
                    run=self.action_runner,
                    transport=session.transport,
                    policy=session.ssh_policy,
                    on_prompt=self._ask_from_worker,
                    on_entry_prompt=lambda entry_id, prompt: self._ask_from_worker(
                        prompt, entry_id
                    ),
                    on_entry_disconnect=self._disconnect_entry,
                    progress=relay,
                    client=self.remote_client,
                    **extra,
                )
            except Exception as exc:
                # A crashed worker must still settle its entries, or they stay running forever.
                logger.debug("action worker crashed", exc_info=True)
                outcome = ActionOutcome([drunner.RunResult(error=str(exc))], str(exc), [])

            def settle() -> None:
                if relay is not None:
                    relay.drain()  # no chunk is lost before the entries finish
                done(outcome)

            self._on_app_thread(settle)

        if self.threaded_actions:
            self.run_worker(work, thread=True, exit_on_error=False)
        else:
            work()

    def show_output(
        self,
        title: str,
        text: str,
        *,
        failed: bool,
        live: bool = False,
        background: bool = False,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        if not background:
            self.session.user_interacted()
        screen = OutputScreen(title, text, failed=failed, background=background)
        if live:  # only a live screen is ever rewritten; a plain one stacked on top leaves it be
            self._output_screen = screen
        if background:  # identify the critical modal, not whichever output last streamed
            self._critical_screen = screen
        self.push_screen(screen, None if on_close is None else lambda _result: on_close())

    def update_output(self, text: str, failed: bool = False) -> None:
        screen = self._output_screen
        if screen is not None and screen in self.screen_stack:
            screen.set_text(text)
            if failed:
                screen.add_class("failed")

    def _on_app_thread(self, fn: Callable[[], None]) -> None:
        if threading.get_ident() == self._ui_thread:
            fn()
            return
        try:
            self.call_from_thread(fn)
        except (Exception, asyncio.CancelledError):
            # the dashboard is gone: nobody is left to tell
            logger.debug("app-thread call dropped", exc_info=True)

    def _post_to_app_thread(self, fn: Callable[[], None]) -> None:
        """Run ``fn`` on the app thread without waiting for it (inline on the app thread)."""
        if threading.get_ident() == self._ui_thread:
            fn()
            return
        loop = self._loop
        if loop is None:
            logger.debug("app-thread post dropped: no loop")
            return
        try:
            loop.call_soon_threadsafe(fn)
        except (Exception, asyncio.CancelledError):
            logger.debug("app-thread post dropped", exc_info=True)

    def _disconnect_entry(self, entry_id: int) -> None:
        def cancel() -> None:
            self._disconnected_entries.add(entry_id)
            answer = self._entry_answers.get(entry_id)
            if answer is not None:
                self._resolve_answer(answer, None)

        self._on_app_thread(cancel)

    def _ask_from_worker(self, prompt: Prompt, entry_id: int | None = None) -> Any:
        """Block the worker until the UI answers, possibly later from the queue."""
        if self._prompts_closed or threading.get_ident() == self._ui_thread:
            return None
        try:
            return self.call_from_thread(self._ask, prompt, entry_id, self.session.handed_off)
        except (Exception, asyncio.CancelledError):
            return None  # the dashboard is closing: cancel the question

    async def _ask(
        self, prompt: Prompt, entry_id: int | None = None, handed_off: bool = False
    ) -> Any:
        if self._prompts_closed or entry_id in self._disconnected_entries:
            if entry_id is not None:
                self._disconnected_entries.discard(entry_id)
            return None
        answer: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending_prompts.add(answer)
        entry = None if entry_id is None else self.session.queue.get(entry_id)
        if entry is None:
            self.push_screen(modal_for(prompt), lambda value: self._resolve_answer(answer, value))
        else:
            self._entry_answers[entry.id] = answer
            if self.session.question_arrived(
                entry.id,
                prompt,
                on_top=not handed_off
                and (
                    self.screen is self._critical_screen
                    if entry.foreground
                    else not self.modal_open
                ),
                queue_key="Q",
            ):
                self.show_question(entry.id)
            if handed_off and not self.session.handed_off and self.session.overlay is None:
                self.session.open_queue()
                self.refresh_frame()
        try:
            return await answer
        finally:
            self._pending_prompts.discard(answer)
            if entry is not None:
                self._entry_answers.pop(entry.id, None)
                self._disconnected_entries.discard(entry.id)

    @staticmethod
    def _resolve_answer(answer: asyncio.Future[Any], value: Any) -> None:
        if not answer.done():
            answer.set_result(value)

    def show_question(self, entry_id: int) -> None:
        """Answer an entry without unblocking any other command waiting in the queue."""
        entry = self.session.queue.get(entry_id)
        answer = self._entry_answers.get(entry_id)
        if entry is None or entry.pending_question is None or answer is None:
            return
        if entry_id in self._shown_questions or answer.done():
            return
        self._shown_questions.add(entry_id)

        def resolve(value: Any) -> None:
            self._shown_questions.discard(entry_id)
            if value is not LATER:
                if not answer.done():
                    self.session.question_answered(entry_id)
                self._resolve_answer(answer, value)

        modal = modal_for(entry.pending_question, entry=entry)

        def close_resolved(_future: asyncio.Future[Any]) -> None:
            if entry_id in self._shown_questions and self.screen is modal:
                modal.dismiss(None)

        answer.add_done_callback(close_resolved)
        self.push_screen(modal, resolve)

    def _cancel_pending_prompts(self) -> None:
        """Answer every open question with a cancel, so no child waits on a dead UI."""
        self._prompts_closed = True
        for answer in list(self._pending_prompts):
            if not answer.done():
                answer.set_result(None)

    def exit(self, result: int | None = None, return_code: int = 0, message: Any = None) -> None:
        self._cancel_pending_prompts()
        super().exit(result, return_code, message)

    # --- Textual -----------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield DashboardFrame(
            id="frame",
            mouse_enabled=lambda: self.mouse_on,
            candidates=self.session.command_candidates,
        )

    @cached_property
    def frame(self) -> DashboardFrame:
        return self.query_one(DashboardFrame)

    def on_mount(self) -> None:
        self.refresh_frame()
        if self._tick_seconds is not None:
            self.set_interval(self._tick_seconds, self._timer_tick)

    def on_resize(self, _event: events.Resize) -> None:
        self._painted = None
        self.refresh_frame()
        # Clamp again after child geometry (including scrollbar allowance) settles.
        self.call_after_refresh(self.refresh_frame)

    async def on_event(self, event: events.Event) -> None:
        # Keys and pastes are routed here, one at a time, never through the focus chain:
        # Textual would pick a key's widget on arrival, before the keys ahead of it in
        # the same read had opened or closed anything.
        if (
            isinstance(event, (events.Key, events.Paste, events.MouseDown))
            and not event.is_forwarded
        ):
            self.session.user_interacted()
        if self.modal_open and isinstance(event, (events.Key, events.Paste)):
            # A question or output screen has the keyboard: Textual's own routing.
            await super().on_event(event)
        elif isinstance(event, events.Key) and not event.is_forwarded:
            self.app_focus = True  # what Textual's own handler does for a key
            await self._route_key(event)
        elif isinstance(event, events.Paste) and not event.is_forwarded:
            await self._route_paste(event.text)
        else:
            await super().on_event(event)

    async def _route_key(self, event: events.Key) -> None:
        """One key, completely: the slot shows what it did before the next key is read."""
        await self.frame.settle_native()
        box = self.frame.native_box
        if box is None:
            token = parse_key(event.key)
            if token:
                self._after(self.session.handle_key(token))
            elif event.character and self.session.open_menu_for_letter(event.character):
                # A menu letter in the table is Enter plus that letter.
                self._after(None)
                await self.frame.settle_native()
                if self.frame.native_box is not None:
                    outcome = await self.frame.native_box.handle_key(event)
                    if isinstance(outcome, Message):
                        self._apply(outcome)
        else:
            outcome = await box.handle_key(event)
            if isinstance(outcome, Message):
                self._apply(outcome)
            elif not outcome:
                # A key the box leaves to the dashboard: q, h, S, Ctrl-C.
                token = parse_key(event.key)
                if token in OVERLAY_GLOBAL_TOKENS:
                    self._after(self.session.overlay_global_key(token))
        await self.frame.settle_native()

    async def _route_paste(self, text: str) -> None:
        await self.frame.settle_native()
        box = self.frame.native_box
        if isinstance(box, TextBox):
            box.paste(text)

    def _apply(self, message: Message) -> None:
        """A box's outcome for a routed key, through the handler a posted one would reach."""
        getattr(self, message.handler_name)(message)

    def _native_current(self, key: tuple[object, ...] | None) -> bool:
        """Whether a box's message is about the overlay still open (a tick may have closed it)."""
        return key is not None and key == overlay_key(self.session.overlay)

    def _after_native(self) -> None:
        # The box may show something the view does not carry (a level, a re-synced
        # checkbox): repaint even when the view compares equal.
        self._painted = None
        self.refresh_frame()

    def on_overlay_box_cancelled(self, message: OverlayBox.Cancelled) -> None:
        if self._native_current(message.key):
            self.session.overlay_cancel()
        self._after_native()

    def on_picker_box_chosen(self, message: PickerBox.Chosen) -> None:
        if self._native_current(message.key):
            self.session.picker_chosen(message.entry)
        self._after_native()

    def on_menu_box_chosen(self, message: MenuBox.Chosen) -> None:
        if self._native_current(message.key):
            self._after(self.session.menu_chosen(message.verb, message.group, message.index))
        self._after_native()

    def on_egress_box_add(self, message: EgressBox.Add) -> None:
        if self._native_current(message.key):
            self.session.egress_add(message.index)
        self._after_native()

    def on_egress_box_remove(self, message: EgressBox.Remove) -> None:
        if self._native_current(message.key):
            self.session.egress_remove(message.row)
        self._after_native()

    def on_accounts_box_chosen(self, message: AccountsBox.Chosen) -> None:
        if self._native_current(message.key):
            self.session.account_chosen(message.row, message.index)
        self._after_native()

    def on_accounts_box_new_group(self, message: AccountsBox.NewGroup) -> None:
        if self._native_current(message.key):
            self.session.account_new_group(message.index)
        self._after_native()

    def on_outbox_box_intent(self, message: OutboxBox.Intent) -> None:
        if self._native_current(message.key):
            self.session.outbox_intent(message.name, message.row, message.value)
        self._after_native()

    def on_settings_box_toggled(self, message: SettingsBox.Toggled) -> None:
        if self._native_current(message.key):
            self.session.setting_toggled(message.tab, message.row_key)
        self._after_native()  # also re-syncs a refused checkbox

    def on_settings_box_moved(self, message: SettingsBox.Moved) -> None:
        if self._native_current(message.key):
            self.session.setting_moved(message.row_key, message.step)
        self._after_native()

    def on_command_box_submitted(self, message: CommandBox.Submitted) -> None:
        if self._native_current(message.key):
            self.session.command_submitted(message.text)
        self._after_native()

    def on_overlay_box_changed(self, _message: OverlayBox.Changed) -> None:
        self._after_native()

    def on_prompt_box_submitted(self, message: PromptBox.Submitted) -> None:
        if self._native_current(message.key):
            error = self.session.prompt_submitted(message.text)
            box = self.frame.native_box
            if error is not None and isinstance(box, PromptBox) and box.key == message.key:
                box.show_error(error)
        self._after_native()

    # --- frame -------------------------------------------------------------

    def _timer_tick(self) -> None:
        """The periodic refresh: the only caller that steps the queue spinner."""
        self.refresh_frame(spin=True)

    def refresh_frame(self, *, spin: bool = False) -> None:
        """Refresh the session and repaint if anything visible changed."""
        self.session.tick(spin=spin)
        title = self.session.title()
        if title != self._last_title:
            # Only on change: an OSC 2 write on every frame makes some
            # terminals redraw their title bar continuously.
            self._write_terminal(title_sequence(title))
            self._last_title = title
        view = self.session.view(self.hover)
        painted = (view, self.size)
        if painted == self._painted:
            return
        if (
            self._painted is not None
            and (replace(view, queue_frame=self._painted[0].queue_frame), self.size)
            == self._painted
        ):
            self.frame.show_title(view)
        else:
            self.frame.show(view)
        self.session.details_furthest = self.frame.details_furthest
        self.session.details_shown = self.frame.details_shown
        self.session.details_room = self.frame.details_room
        self._painted = painted

    def _after(self, outcome: Outcome) -> None:
        if outcome == "quit":
            count = sum(entry.pending_question is not None for entry in self.session.queue)
            if count:
                noun = "command is" if count == 1 else "commands are"
                prompt = Prompt(
                    0,
                    "confirm",
                    f"{count} {noun} waiting for an answer. Quitting aborts them. Quit?",
                )
                self.push_screen(modal_for(prompt), self._confirm_quit)
            else:
                self._confirm_quit(True)
            return
        if outcome == "toggle-mouse":
            self.toggle_mouse()
            return
        self.refresh_frame()

    def _confirm_quit(self, confirmed: bool | None) -> None:
        if confirmed:
            self.quit_requested = True
            self.exit(0)

    def toggle_mouse(self) -> None:
        """`M`: mouse reporting on or off for this session."""
        self.mouse_on = not self.mouse_on
        _set_mouse_reporting(self._driver, self.mouse_on)
        self.hover = None
        self.session.set_notice(
            "mouse on" if self.mouse_on else "mouse off — terminal text selection active"
        )
        self.refresh_frame()

    def on_click(self, event: events.Click) -> None:
        self.session.user_interacted()
        if not self.mouse_on or self.modal_open:
            return
        box = self.frame.native_box
        if box is not None and event.widget is not None and box in event.widget.ancestors_with_self:
            return  # the box acted on it itself
        hit = Hit.of(event.style.meta)
        if event.chain > 1:
            # The first click of the pair already acted; only a row-like target
            # that the first click hit too, clicked twice, means Enter there.
            # A right-click never pairs: its first click already opened the menu.
            if (
                event.button == 1
                and hit is not None
                and hit == self._last_click
                and hit.kind in DOUBLE_CLICK_KINDS
            ):
                self._last_click = None  # a third click of the chain acts on nothing
                self.session.click(hit, double=True)
                self.refresh_frame()
            return
        self._last_click = hit
        self.session.click(hit, right=event.button == 3, toggle=event.ctrl)
        self.refresh_frame()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if not self.mouse_on or self.modal_open:
            return
        self._set_hover(Hit.of(event.style.meta))

    def _set_hover(self, hit: Hit | None) -> None:
        if hit == self.hover:
            return
        self.hover = hit
        self.refresh_frame()

    def on_fleet_table_wheel_scrolled(self, _message: FleetTable.WheelScrolled) -> None:
        """Re-resolve hover from what is now under the pointer (no MouseMove follows a wheel)."""
        if self.mouse_on:
            self.call_after_refresh(self._rehover)

    def _rehover(self) -> None:
        if self.mouse_on:
            x, y = self.mouse_position
            self._set_hover(Hit.of(self.screen.get_style_at(x, y).meta))

    def on_details_panel_scrolled(self, message: DetailsPanel.Scrolled) -> None:
        if self.mouse_on:
            self.session.scroll_details(message.step)
            self.refresh_frame()

    def on_details_panel_resized(self, message: DetailsPanel.Resized) -> None:
        if self.mouse_on:
            self.session.set_details_rows(message.rows)
            self.refresh_frame()

    def on_fleet_table_geometry_changed(self, _message: FleetTable.GeometryChanged) -> None:
        self.refresh_frame()

    def on_fleet_table_column_scroll(self, message: FleetTable.ColumnScroll) -> None:
        if self.mouse_on:
            self.session.wheel_columns(message.step)
            self.refresh_frame()

    def _write_terminal(self, sequence: str) -> None:
        """Write a raw control sequence in order with Textual's own output."""
        driver = self._driver
        if driver is not None:
            driver.write(sequence)
            driver.flush()


def run(
    incus: Incus,
    cwd_root: Path | None,
    *,
    remote: bool = False,
    transport: Transport = "none",
    ssh_policy: RemotePolicy | None = None,
    scope: RemoteRepoScope | None = None,
    remote_client: str | None = None,
) -> int:
    """Open the terminal dashboard; return its exit code."""
    startup = open_dashboard(cwd_root, scope=scope)
    if isinstance(startup, int):
        return startup
    try:
        app = DashboardApp(
            startup,
            incus=incus,
            cwd_root=cwd_root,
            remote=remote,
            transport=transport,
            ssh_policy=ssh_policy,
            scope=scope,
            remote_client=remote_client,
        )
        # Pushed before Textual takes the screen and popped after it gives it
        # back, so the terminal's own title is saved and restored intact.
        with terminal_title_scope(sys.stdout):
            rc = app.run(mouse=startup.mouse)
        # An unhandled handler exception ends Textual with return code 1 and no value.
        rc = app.return_code or rc or 0
    except KeyboardInterrupt:
        rc = 0
    finally:
        startup.client.close()
    return rc
