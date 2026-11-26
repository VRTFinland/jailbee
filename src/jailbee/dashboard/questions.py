"""Frontend-independent pending question coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal

from jailbee.dashboard.notifications import NotificationQueue, should_show_question

if TYPE_CHECKING:
    from jailbee.prompt_channel import Prompt


class QuestionRouter:
    """Track attention and decide whether arriving questions may be shown."""

    def __init__(self, queue: NotificationQueue) -> None:
        self.queue = queue
        self._interaction = 0
        self._dispatch: dict[int, int] = {}
        self._answer: dict[int, int] = {}

    def dispatched(self, ids: Iterable[int]) -> None:
        self._dispatch.update(dict.fromkeys(ids, self._interaction))

    def finished(self, entry_id: int) -> None:
        self._dispatch.pop(entry_id, None)
        self._answer.pop(entry_id, None)

    def interacted(self) -> None:
        self._interaction += 1

    def answered(self, entry_id: int) -> None:
        if self.queue.get(entry_id) is not None:
            self._answer[entry_id] = self._interaction

    def arrived(
        self,
        entry_id: int,
        prompt: Prompt,
        *,
        on_top: bool,
        overlay: Literal["none", "queue", "other"],
        suspended: bool,
    ) -> bool:
        entry = self.queue.get(entry_id)
        if entry is None:
            return False
        self.queue.set_question(entry_id, prompt)
        return should_show_question(
            critical=entry.critical,
            foreground=entry.foreground,
            on_top=on_top,
            overlay=overlay,
            dispatch_interaction=self._dispatch.get(entry_id),
            answer_interaction=self._answer.get(entry_id),
            interaction=self._interaction,
            suspended=suspended,
        )


class AnswerRegistry:
    """Track answers and arbitrate the first answer to each question."""

    def __init__(self) -> None:
        self._answers: dict[int, asyncio.Future[Any]] = {}
        self._disconnected: set[int] = set()
        self._shown: set[int] = set()

    def register(self, entry_id: int, answer: asyncio.Future[Any]) -> None:
        self._answers[entry_id] = answer

    def get(self, entry_id: int) -> asyncio.Future[Any] | None:
        return self._answers.get(entry_id)

    @staticmethod
    def resolve_future(answer: asyncio.Future[Any], value: Any) -> bool:
        if answer.done():
            return False
        answer.set_result(value)
        return True

    def resolve(self, entry_id: int, value: Any) -> bool:
        answer = self.get(entry_id)
        return answer is not None and self.resolve_future(answer, value)

    def disconnect(self, entry_id: int) -> None:
        self._disconnected.add(entry_id)
        self.resolve(entry_id, None)

    def disconnected(self, entry_id: int) -> bool:
        return entry_id in self._disconnected

    def finish(self, entry_id: int) -> None:
        self._answers.pop(entry_id, None)
        self._disconnected.discard(entry_id)

    def show(self, entry_id: int) -> bool:
        answer = self.get(entry_id)
        if answer is None or answer.done() or entry_id in self._shown:
            return False
        self._shown.add(entry_id)
        return True

    def shown(self, entry_id: int) -> bool:
        return entry_id in self._shown

    def hide(self, entry_id: int) -> None:
        self._shown.discard(entry_id)
