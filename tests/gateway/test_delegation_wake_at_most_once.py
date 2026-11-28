"""The delegation wake self-post is sent AT MOST ONCE (``gateway.wake._self_post_chat_completion(at_most_once=True)``).

A stand-in aiohttp server counts the posts. The server keeps a turn running when its client gives up
waiting, so a second post after a timeout or a broken connection would be a second turn for one result.
The real-handler timeout case (real session authority, real turn) is in ``test_delegation_wake_authority.py``;
the last two tests here use a recorder for the self-post itself.
"""
import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.gateway.test_delegation_completion_wake import (  # noqa: F401  (fixtures)
    SESSION, _deliver, _rows, env, home, wake_on, wake_posts,
)


class _Server:
    """A tiny aiohttp server standing in for the adapter's own endpoint, to count posts."""

    def __init__(self, behaviour):
        self.behaviour, self.posts = behaviour, 0

    async def handler(self, request):
        self.posts += 1
        return await self.behaviour(self, request)


async def _serve(behaviour):
    state = _Server(behaviour)
    app = web.Application()
    app.router.add_post("/v1/chat/completions", state.handler)
    client = TestClient(TestServer(app))
    await client.start_server()
    adapter = SimpleNamespace(_host="127.0.0.1", _port=client.port, _api_key="sk-test", _model_name="m")
    return state, client, adapter


async def _slow(state, request):
    await asyncio.sleep(1.0)
    return web.json_response({})


@pytest.mark.asyncio
async def test_default_self_post_keeps_its_retries_for_the_other_wakes(monkeypatch):
    """The watch and kanban wakes call the same function without ``at_most_once`` and keep today's
    behaviour (so this PR does not change them). Recorded here as a fact, not endorsed: they share the
    re-send-after-timeout fault and the PR text says so."""
    from gateway import wake
    monkeypatch.setattr(wake, "WAKE_TURN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(wake, "_RETRY_DELAYS_SECONDS", (0.01,))
    state, client, adapter = await _serve(_slow)
    try:
        with pytest.raises(RuntimeError, match="gave up"):
            await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION)
        assert state.posts == 2
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_at_most_once_never_posts_again_after_a_timeout(monkeypatch):
    from gateway import wake
    monkeypatch.setattr(wake, "WAKE_TURN_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(wake, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))
    state, client, adapter = await _serve(_slow)
    try:
        with pytest.raises(wake.WakeOutcomeUnknown) as err:
            await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION, at_most_once=True)
        assert err.value.timed_out is True
        assert state.posts == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_at_most_once_never_posts_again_after_the_connection_breaks(monkeypatch):
    from gateway import wake
    monkeypatch.setattr(wake, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))

    async def drop(state, request):
        request.transport.close()
        await asyncio.sleep(0.05)
        return web.json_response({})

    state, client, adapter = await _serve(drop)
    try:
        with pytest.raises(wake.WakeOutcomeUnknown) as err:
            await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION, at_most_once=True)
        assert err.value.timed_out is False
        assert state.posts == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_at_most_once_still_retries_a_refusal_that_started_nothing(monkeypatch):
    from gateway import wake
    monkeypatch.setattr(wake, "_RETRY_DELAYS_SECONDS", (0.01, 0.01, 0.01))

    async def busy_then_ok(state, request):
        return web.Response(status=429) if state.posts == 1 else web.json_response({})

    state, client, adapter = await _serve(busy_then_ok)
    try:
        await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION, at_most_once=True)
        assert state.posts == 2  # HTTP 429 is refused before any turn starts: safe to try again
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_broken_connection_after_the_post_keeps_the_delivery_row(env, wake_on, monkeypatch):
    """Outcome unknown and the connection (not just the wait) failed: the turn may have died with it, so
    the row is kept (a possible duplicate, never a loss). No second post."""
    from gateway import wake
    posts = []

    async def broken(adapter, *, text, session_id, **extra):
        posts.append(text)
        raise wake.WakeOutcomeUnknown("connection reset", timed_out=False)

    monkeypatch.setattr(wake, "_self_post_chat_completion", broken)
    assert await _deliver(env) is True
    assert len(posts) == 1 and len(_rows(env)) == 1
    assert env.adapter._session_turns_in_flight == {}


@pytest.mark.asyncio
async def test_a_timed_out_wake_writes_no_row_and_sends_no_second_turn(env, wake_on, monkeypatch):
    from gateway import wake
    posts = []

    async def late(adapter, *, text, session_id, **extra):
        posts.append(extra)
        raise wake.WakeOutcomeUnknown("timed out", timed_out=True)

    monkeypatch.setattr(wake, "_self_post_chat_completion", late)
    assert await _deliver(env) is True
    assert len(posts) == 1 and posts[0].get("at_most_once") is True
    assert [r for r in _rows(env) if r.get('display_kind') == 'async_delegation_complete'] == []


# -- the answers a wake turn can get back --------------------------------------------------------------------


_QUEUED = {"error": {"message": "Hermes is restarting. Your input is saved and will run once when it is back; "
                                "do not resend it.", "type": "server_error", "code": "admission_queued"}}


@pytest.mark.asyncio
async def test_a_503_admission_queued_is_a_delivered_wake():
    """The session authority saved the turn and will run it once: delivered, not a failure, and not retried."""
    from gateway import wake

    async def queued(state, request):
        return web.json_response(_QUEUED, status=503, headers={"x-should-retry": "false"})

    state, client, adapter = await _serve(queued)
    try:
        await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION, at_most_once=True)
        assert state.posts == 1
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", [
    web.json_response({"error": {"code": "runtime_draining", "message": "retry"}}, status=503),
    web.json_response({"error": {"code": "storage_unavailable"}}, status=503),
    web.Response(status=503, text="not json"),
], ids=["draining", "storage", "plain"])
async def test_any_other_503_is_still_a_failed_wake(answer):
    from gateway import wake

    async def refuse(state, request):
        return answer

    state, client, adapter = await _serve(refuse)
    try:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION, at_most_once=True)
        assert state.posts == 1
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_the_other_wakes_still_treat_a_503_admission_queued_as_a_failure():
    """Watch and kanban wakes (no ``at_most_once``) keep today's behaviour: only the delegation wake changed."""
    from gateway import wake

    async def queued(state, request):
        return web.json_response(_QUEUED, status=503)

    state, client, adapter = await _serve(queued)
    try:
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION)
    finally:
        await client.close()


_FAILED = {"id": "c", "object": "chat.completion", "choices": [{"index": 0, "finish_reason": "error",
           "message": {"role": "assistant", "content": "the model did not answer"}}],
           "hermes": {"completed": False, "partial": False, "failed": True, "error": "HTTP 500"}}


@pytest.mark.asyncio
async def test_a_wake_turn_whose_model_call_failed_is_not_a_delivered_wake():
    from gateway import wake

    async def failed(state, request):
        return web.json_response(_FAILED)

    state, client, adapter = await _serve(failed)
    try:
        with pytest.raises(wake.WakeTurnFailed):
            await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION, at_most_once=True)
        assert state.posts == 1  # and never sent again
        await wake._self_post_chat_completion(adapter, text="x", session_id=SESSION)  # other wakes: unchanged
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_a_wake_turn_whose_model_call_failed_keeps_the_delivery_row(env, wake_on, monkeypatch):
    from gateway import wake

    async def failed(adapter, *, text, session_id, **extra):
        raise wake.WakeTurnFailed("the model call failed")

    monkeypatch.setattr(wake, "_self_post_chat_completion", failed)
    assert await _deliver(env) is True
    assert len(_rows(env)) == 1  # the result stays available to the client as a delivery row


@pytest.mark.asyncio
async def test_a_wake_turn_whose_text_never_reached_the_transcript_keeps_the_delivery_row(env, wake_on, monkeypatch):
    """D3: the row is the one copy only when the wake text is not already in the transcript."""
    from gateway import wake

    async def failed(adapter, *, text, session_id, **extra):
        raise wake.WakeTurnFailed("the model call failed")

    monkeypatch.setattr(wake, "_self_post_chat_completion", failed)
    assert await _deliver(env) is True
    assert len([r for r in _rows(env) if r.get("display_kind") == "async_delegation_complete"]) == 1


@pytest.mark.asyncio
async def test_a_wake_turn_whose_own_row_is_in_the_transcript_adds_no_delivery_row(env, wake_on, monkeypatch):
    from gateway import wake
    from persistence.conversation.messages import NewMessage
    from session_rules.turns import with_automated_notice
    from tests.gateway.test_delegation_completion_wake import RESULT_TEXT

    async def failed(adapter, *, text, session_id, **extra):
        env.db.messages.append(env.session_id, NewMessage(role="user", content=text))  # what the wake turn stores first
        for i in range(40):  # a long tool chain after it: the wake row is far from the end of the transcript
            env.db.messages.append(env.session_id, NewMessage(role="assistant", content=f"step {i}"))
        raise wake.WakeTurnFailed("the model call failed")

    monkeypatch.setattr(wake, "_self_post_chat_completion", failed)
    assert await _deliver(env) is True
    assert not [r for r in _rows(env) if r.get("display_kind") == "async_delegation_complete"]
    assert any(with_automated_notice(RESULT_TEXT) in str(r["content"]) for r in _rows(env))


@pytest.mark.asyncio
async def test_the_busy_wait_bound_logs_once_and_keeps_deferring(env, monkeypatch, caplog):
    """The wait of a delivery refused by a busy session is never turned into a refusal that spends attempts (and
    ends in a drop): every refusal stays deferred (``WakeNotAccepted``, attempt refunded); at the bound one line
    is logged. The early notice of a batch counts on its own, and a delivered result clears its count."""
    import gateway.run_notifications as notifications
    from gateway import wake
    from persistence.core.errors import TurnLeaseLost

    async def refused(*args, **kwargs):
        raise TurnLeaseLost("the session has an active turn lease")

    monkeypatch.setattr(wake, "persist_delegation_delivery", refused)
    monkeypatch.setattr(notifications, "_BUSY_WAIT_MAX_REFUSALS", 3)
    caplog.set_level("INFO", logger="gateway.run")
    evt, notice = {"delegation_id": "d-bound"}, {"delegation_id": "d-bound", "task_failure_notice": True}
    call = lambda e: env.runner._persist_delegation_row(env.adapter, "text", SESSION, e, raise_not_accepted=True)
    for _ in range(6):
        with pytest.raises(wake.WakeNotAccepted):
            await call(evt)
    lines = [r.getMessage() for r in caplog.records if r.levelname == "WARNING" and "still waiting" in r.getMessage()]
    assert len(lines) == 1 and "d-bound" in lines[0]
    with pytest.raises(wake.WakeNotAccepted):
        await call(notice)  # its own count: one refusal, below the bound
    assert set(env.runner._busy_waits) == {"d-bound", "d-bound:notice"}
    env.runner._forget_busy_wait(evt, SESSION)
    env.runner._forget_busy_wait(notice, SESSION)
    assert env.runner._busy_waits == {}
