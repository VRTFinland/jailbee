import asyncio

from jailbee.dashboard.notifications import NotificationQueue
from jailbee.dashboard.questions import AnswerRegistry, QuestionRouter
from jailbee.prompt_channel import Prompt


def test_first_answer_wins_and_disconnect_cancels() -> None:
    async def exercise() -> None:
        registry = AnswerRegistry()
        answer = asyncio.get_running_loop().create_future()
        registry.register(1, answer)
        assert registry.resolve(1, "first")
        assert not registry.resolve(1, "second")
        assert await answer == "first"
        registry.finish(1)
        registry.disconnect(2)
        assert registry.disconnected(2)
        registry.finish(2)
        assert not registry.disconnected(2)

    asyncio.run(exercise())


def test_router_defers_after_interaction_and_records_question() -> None:
    queue = NotificationQueue()
    entry = queue.start("jailbee start", "one")
    router = QuestionRouter(queue)
    router.dispatched([entry])
    router.interacted()
    prompt = Prompt(1, "text", "Name?")
    assert not router.arrived(entry, prompt, on_top=True, overlay="none", suspended=False)
    assert queue.get(entry).pending_question == prompt
    router.answered(entry)
    assert router.arrived(entry, prompt, on_top=True, overlay="none", suspended=False)
