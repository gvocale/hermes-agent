"""``gateway.delegation_completion_wake``: the failure and exit paths.

What is real here: the HTTP handlers (``/v1/chat/completions`` stream and non-stream), the real
``_run_agent`` wrapper and executor thread, the real ``AIAgent.run_conversation`` loop with real tool
dispatch and ``prepare_iteration``, a real session store that the agent PERSISTS to (nothing about
persistence is patched out) and the real wake self-post over HTTP to the adapter's own server.

What is still faked: the model (``_interruptible_api_call`` is scripted), the tool (a probe that returns
"ok"), and, in the late-turn tests that say so, the wake self-post is a recorder.
"""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from agent.background_notices import AUTOMATED_NOTICE
from gateway.platforms.api_server import _abandon_agent_task
from tests.fixtures.storage_read import transcript_rows
from tests.gateway.fixtures.storage_handles import close_storage_handles  # noqa: F401
from tests.gateway.test_api_server_active_work_drain import _TURN_UNBLOCK_TIMEOUT, _make_admission_app
from tests.gateway.test_delegation_completion_wake import (  # noqa: F401  (fixtures)
    EVT, RESULT_BODY, RESULT_HEADER, RESULT_TEXT, SESSION, _chat, _deliver, _rows, _settle,
    env, home, wake_on, wake_posts,
)


def _scripted_agent(env, model):
    """A real ``AIAgent`` that persists to the real store, with only the model scripted."""
    from gateway.platforms.api_server_store import adapter_persistence
    from run_agent import AIAgent
    from tests.agent.test_background_notices import PROBE_TOOL
    schema = {"type": "function", "function": {
        "name": PROBE_TOOL, "description": "probe", "parameters": {"type": "object", "properties": {}}}}
    with (patch("model_tools.get_tool_definitions", return_value=[schema]),
          patch("model_tools.check_toolset_requirements", return_value={}),
          patch("agent.process_bootstrap.OpenAI")):
        agent = AIAgent(api_key="test-key-1234567890", base_url="https://openrouter.ai/api/v1", quiet_mode=True,
                        skip_context_files=True, skip_memory=True, session_id=SESSION, platform="api_server",
                        session_db=env.adapter._ensure_store(), persistence=adapter_persistence(env.adapter))
    from unittest.mock import MagicMock
    agent.client = MagicMock()
    agent._disable_streaming = True
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._interruptible_api_call = model
    return agent


class Script:
    """Scripted model + the agents the adapter creates for it. ``steps[n]`` is what model call *n* of a
    turn does: ``"tool"``, ``"final"``, ``"raise"``, or a callable run inside the call (agent thread)."""

    def __init__(self, env, steps, *, on_call=None, hold=None, create_delay=None):
        self.env, self.steps, self.on_call, self.hold = env, steps, on_call, hold
        self.create_delay = create_delay
        self.loop = asyncio.get_running_loop()
        self.payloads: list = []
        self.agents: list = []
        self.created = 0
        self.live = 0
        self.max_live = 0
        self._lock = threading.Lock()

    def model(self, api_kwargs):
        from tests.agent.test_background_notices import PROBE_TOOL
        from tests.agent.test_run_agent import _mock_response
        with self._lock:
            self.payloads.append([dict(m) for m in api_kwargs["messages"]])
            n = len(self.payloads)
            self.live += 1
            self.max_live = max(self.max_live, self.live)
        try:
            if self.on_call:
                self.on_call(n)
            if self.hold is not None:
                self.hold(n)
            step = self.steps[min(n - 1, len(self.steps) - 1)]
            if step == "raise":
                raise RuntimeError("model call failed")
            if step == "tool":
                call = SimpleNamespace(id=f"call_{n}", type="function",
                                       function=SimpleNamespace(name=PROBE_TOOL, arguments="{}"))
                return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[call])
            return _mock_response(content="done", finish_reason="stop")
        finally:
            with self._lock:
                self.live -= 1

    def create_agent(self, **kwargs):
        if self.create_delay is not None:
            self.loop.call_soon_threadsafe(self.create_delay[0].set)
            self.create_delay[1].wait(_TURN_UNBLOCK_TIMEOUT)
        with self._lock:
            self.created += 1
        agent = _scripted_agent(self.env, self.model)
        self.agents.append(agent)
        return agent

    def patch(self):
        return patch.object(self.env.adapter, "_create_agent", side_effect=self.create_agent)

    def deliver(self, evt=EVT, text=RESULT_TEXT):
        """A completion that arrives while the model works: the gateway's loop delivers it."""
        return asyncio.run_coroutine_threadsafe(_deliver(self.env, evt, text), self.loop).result(30)

    def seen(self, body=RESULT_BODY) -> int:
        return sum(str(m.get("content")).count(body) for m in self.payloads[-1]) if self.payloads else 0


def _delivery(env) -> list:
    """The delivery rows (the durable fallback) in the real store, apart from the turns' own rows."""
    return [r for r in _rows(env) if r.get("display_kind") == "async_delegation_complete"]


def _places(script, env, wake_posts, body=RESULT_BODY) -> dict:
    """Every place one result can reach the model or the client; exactly one must hold it."""
    return {"mid_turn": script.seen(body),
            "new_turn": sum(body in post["text"] for post in wake_posts),
            "delivery_row": sum(body in str(row.get("content")) for row in _delivery(env))}


async def _wait(predicate, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _clean(env):
    adapter = env.adapter
    assert adapter._session_turns_in_flight == {}
    assert adapter._turn_agents == {} and adapter._late_notices == {}
    assert not adapter._late_notice_tasks


# -- (1) the wake self-post is sent at most once -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_wake_turn_longer_than_the_timeout_is_sent_once_over_real_http(env, wake_on, monkeypatch):
    """The reviewer's case: real self-post to the adapter's own server, a short client timeout, a wake turn
    that outlives it. The first turn keeps running (the server does not stop it), so a second post
    would be a second turn. Result must be: one turn, no delivery row."""
    monkeypatch.setattr("gateway.wake.WAKE_TURN_TIMEOUT_SECONDS", 0.4)
    monkeypatch.setattr("gateway.wake._RETRY_DELAYS_SECONDS", (0.05, 0.05, 0.05))
    script = Script(env, ["final"], hold=lambda n: time.sleep(1.2))
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        env.adapter._host, env.adapter._port, env.adapter._api_key = "127.0.0.1", client.port, "sk-test"
        with script.patch():
            assert await _deliver(env) is True
            await _wait(lambda: script.created >= 1 and script.live == 0 and env.adapter._session_turns_in_flight == {}, 15)
            await asyncio.sleep(0.5)  # a (wrong) second post would have started by now
    assert script.created == 1, f"{script.created} wake turns for one result"
    assert script.max_live == 1
    wake_rows = [r for r in _rows(env) if r.get("role") == "user" and RESULT_BODY in str(r.get("content"))]
    assert len(wake_rows) == 1  # the one wake turn, as a persisted user row
    assert _delivery(env) == []  # no delivery row


@pytest.mark.asyncio
async def test_real_wake_turn_runs_through_the_real_handler_and_is_persisted(env, wake_on):
    script = Script(env, ["tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        env.adapter._host, env.adapter._port, env.adapter._api_key = "127.0.0.1", client.port, "sk-test"
        with script.patch():
            assert await _deliver(env) is True
    rows = _rows(env)
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    user_rows = [r for r in rows if r["role"] == "user"]
    assert len(user_rows) == 1
    text = user_rows[0]["content"]
    assert text.startswith(RESULT_HEADER) and AUTOMATED_NOTICE in text and RESULT_BODY in text
    assert script.created == 1 and len(script.payloads) == 2
    assert _delivery(env) == []


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
    assert len(posts) == 1 and len(_delivery(env)) == 1
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
    assert _delivery(env) == []


# -- (2) a cancelled or abandoned turn keeps the session busy until its worker has really finished --------


@pytest.mark.asyncio
async def test_disconnect_after_a_hand_over_still_delivers_the_result_once(env, wake_on, wake_posts):
    """The reviewer's case: a turn running, a completion handed to it, then ``_abandon_agent_task`` (what an
    SSE disconnect does). Before: the notice landed in ``_late_notices`` after the count had dropped and
    nothing ever picked it up (0 turns, 0 rows)."""
    release = threading.Event()
    started = asyncio.Event()
    script = Script(env, ["final"], on_call=lambda n: script.deliver(),
                    hold=lambda n: (script.loop.call_soon_threadsafe(started.set), release.wait(_TURN_UNBLOCK_TIMEOUT)))
    agent_ref: list = [None]
    with script.patch():
        task = asyncio.ensure_future(env.adapter._run_agent(
            user_message="hi", conversation_history=[], session_id=SESSION, agent_ref=agent_ref))
        await asyncio.wait({task, asyncio.ensure_future(started.wait())}, timeout=_TURN_UNBLOCK_TIMEOUT,
                           return_when=asyncio.FIRST_COMPLETED)
        assert not task.done(), repr(task.exception() if not task.cancelled() else "cancelled")
        assert wake_posts == [] and _delivery(env) == []  # handed to the running turn
        await _abandon_agent_task(agent_ref, task, "SSE client disconnected")
        # the handler is gone, the worker thread is not: the session must still count as busy
        assert env.adapter._session_turns_in_flight == {SESSION: 1}
        from gateway.platforms.api_server_runs import session_is_idle
        assert session_is_idle(env.adapter, SESSION) is False
        release.set()
        assert await _wait(lambda: env.adapter._session_turns_in_flight == {}, 15)
        await _settle(env)
    places = _places(script, env, wake_posts)
    assert sum(places.values()) == 1, places  # delivered once, somewhere: never 0, never 2
    _clean(env)


@pytest.mark.asyncio
async def test_a_completion_while_an_abandoned_turn_is_still_creating_its_agent_is_not_woken_beside_it(
        env, wake_on, wake_posts):
    """Defect 3: the client disconnects while the worker is still in ``_create_agent``. No agent is known,
    so nothing is interrupted and the worker goes on to run the whole turn. The session must stay busy."""
    entered, release = asyncio.Event(), threading.Event()
    script = Script(env, ["final"], create_delay=(entered, release))
    with script.patch():
        task = asyncio.ensure_future(env.adapter._run_agent(user_message="hi", conversation_history=[], session_id=SESSION))
        await asyncio.wait_for(entered.wait(), _TURN_UNBLOCK_TIMEOUT)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _deliver(env) is True
        assert wake_posts == [], "a wake turn was started beside the abandoned turn"
        assert len(_delivery(env)) == 1  # kept as the delivery row
        release.set()
        assert await _wait(lambda: env.adapter._session_turns_in_flight == {} and script.created == 1, 15)
    assert env.adapter._session_turns_in_flight == {}


@pytest.mark.asyncio
async def test_a_turn_cancelled_before_its_thread_starts_does_not_leave_the_session_busy(env, wake_on, wake_posts):
    """The other end of the same rule: if the worker never ran, nothing may keep the session busy forever."""
    from gateway.platforms import api_server_runs as runs
    lease = runs._TurnLease(env.adapter, asyncio.get_running_loop(), SESSION)
    assert env.adapter._session_turns_in_flight == {SESSION: 1}
    ran = []
    wrapped = lease.wrap(lambda: ran.append(1))
    lease.cancelled_before_start()  # the awaiting side was cancelled before the thread started
    assert env.adapter._session_turns_in_flight == {}
    with pytest.raises(asyncio.CancelledError):
        wrapped()  # a worker that starts late does not run the turn
    assert ran == [] and env.adapter._session_turns_in_flight == {}


# -- every exit path hands back or reads each notice (exactly one place) -----------------------------------


async def _run_turn(env, script, *, stream=False):
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        with script.patch():
            response = await asyncio.wait_for(_chat(client, stream=stream), _TURN_UNBLOCK_TIMEOUT)
            await response.read()
            await _settle(env)
    return response.status


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["normal-end", "exception", "stop", "iteration-limit"])
async def test_every_exit_path_delivers_the_result_exactly_once(env, wake_on, wake_posts, path):
    def stop_it(n):
        if n == 1:
            script.agents[-1].interrupt("stop")  # what /stop does to the running agent

    steps = {"normal-end": ["final"], "exception": ["raise"], "stop": ["tool", "final"],
             "iteration-limit": ["tool", "tool", "tool"]}[path]
    script = Script(env, steps, on_call=lambda n: n == 1 and (script.deliver() is True) and (stop_it(n) if path == "stop" else None))
    if path == "iteration-limit":
        orig = script.create_agent

        def limited(**kw):
            agent = orig(**kw)
            agent.max_iterations = 1
            agent.iteration_budget.max_total = 1
            return agent
        script.create_agent = limited
    await _run_turn(env, script)
    places = _places(script, env, wake_posts)
    assert sum(places.values()) == 1, (path, places)
    _clean(env)


# -- (6) the late new-turn task is counted by the restart drain and writes its row when cancelled ----------


@pytest.mark.asyncio
async def test_a_pending_late_turn_is_counted_as_active_work_and_a_cancel_writes_the_row(env, wake_on, monkeypatch):
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def hang(adapter, *, text, session_id, **extra):
        entered.set()
        await gate.wait()

    monkeypatch.setattr("gateway.wake._self_post_chat_completion", hang)
    script = Script(env, ["final"], on_call=lambda n: script.deliver())
    assert env.adapter.active_agent_work_count() == 0
    await _run_turn_no_settle(env, script)
    await asyncio.wait_for(entered.wait(), 10)
    assert env.adapter.active_agent_work_count() == 1, "the restart drain would not wait for the late turn"
    assert env.runner._active_api_run_count() >= 1
    for task in list(env.adapter._late_notice_tasks):
        task.cancel()
    await _settle_quiet(env)
    assert len(_delivery(env)) == 1  # cancelled: the result is kept as the delivery row
    assert env.adapter.active_agent_work_count() == 0


async def _run_turn_no_settle(env, script):
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        with script.patch():
            response = await asyncio.wait_for(_chat(client), _TURN_UNBLOCK_TIMEOUT)
            await response.read()


async def _settle_quiet(env):
    for _ in range(200):
        if not env.adapter._late_notice_tasks:
            return
        await asyncio.gather(*list(env.adapter._late_notice_tasks), return_exceptions=True)
        await asyncio.sleep(0)


# -- (4) a notice row is never merged with a real user message: live, from the store, after compaction -----


@pytest.mark.asyncio
async def test_a_user_message_after_a_turn_that_ended_on_a_notice_stays_a_separate_row(env, wake_on, wake_posts):
    """The reviewer's case: a turn ended on the notice row (stopped right after it was appended), then the
    person writes. Before: the person's words were merged INTO the notice row, under "NOT USER INPUT".

    This one drives the whole route (handler, store read-back, real loop, real persistence) and checks the
    stored rows and what the provider is sent; the live-list weld itself is pinned by
    ``test_real_loop_next_turn_keeps_the_notice_row_and_the_users_row_apart_in_the_live_list``.

    The stored state is seeded: in this harness the real loop always writes a row after the notice (an
    answer or a failed-turn marker) when it ends, so the stop that leaves the notice newest is set up in
    the store, in the shape the real loop wrote it in the next test (display_kind internal_notification,
    text from ``with_automated_notice``). Everything after that is real: the handler reads the history back
    from the store, the real loop runs the next turn and persists it."""
    from agent.background_notices import with_automated_notice
    from gateway.run_notifications import _mark_internal_notification
    from tests.fixtures.storage_seed import seed_messages
    notice = _mark_internal_notification(with_automated_notice(RESULT_TEXT))
    seed_messages(env.db, SESSION, {"role": "user", "content": "start"},
                  {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                   "function": {"name": "background_notice_probe", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "c1", "content": "ok"},
                  {"role": "user", "content": notice, "display_kind": "internal_notification"})
    wanted = "yes go ahead and delete the old branch"
    second = Script(env, ["final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        with second.patch():
            response = await asyncio.wait_for(client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": wanted}]},
                headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Id": SESSION}), _TURN_UNBLOCK_TIMEOUT)
            assert response.status == 200
            await response.read()
    # What is sent to the provider is a per-call copy: providers need user/assistant alternation, so two
    # adjacent user rows are joined THERE (the stored and live rows are not touched). The person's words
    # stay after the notice's footer, which says the notice was not written by a person.
    sent = [m for m in second.payloads[0] if m["role"] == "user"][-1]["content"]
    assert sent.index(AUTOMATED_NOTICE) < sent.index("not written by a human user") < sent.index(wanted)
    # reloaded from the store afterwards: two rows, the notice row keeps its machine type, the person's does not
    rows = [r for r in _rows(env) if r["role"] == "user"]
    assert not any(AUTOMATED_NOTICE in str(r["content"]) and wanted in str(r["content"]) for r in rows)
    assert [r.get("display_kind") for r in rows if AUTOMATED_NOTICE in str(r["content"])] == ["internal_notification"]
    from agent.conversation_compression_scaffolding import is_user_role_scaffolding
    from agent.conversation_compression import _is_real_user_message
    person = [r for r in rows if wanted in str(r["content"])]
    assert not is_user_role_scaffolding(person[0]) and _is_real_user_message(person[0])
    assert is_user_role_scaffolding([r for r in rows if AUTOMATED_NOTICE in str(r["content"])][0])
    assert len(person) == 1 and person[0].get("display_kind") in (None, "")


@pytest.mark.asyncio
async def test_the_real_loop_persists_a_handed_over_notice_as_its_own_typed_row(env, wake_on, wake_posts):
    first = Script(env, ["tool", "final"], on_call=lambda n: n == 1 and first.deliver())
    await _run_turn(env, first)
    notice_rows = [r for r in _rows(env) if r["role"] == "user" and AUTOMATED_NOTICE in str(r["content"])]
    assert len(notice_rows) == 1 and notice_rows[0].get("display_kind") == "internal_notification"
    assert [r["role"] for r in _rows(env)] == ["user", "assistant", "tool", "user", "assistant"]
    # the next turn on the same session reads it back from the store and keeps it apart from the person's words
    second = Script(env, ["final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        with second.patch():
            response = await asyncio.wait_for(client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": "thanks"}]},
                headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Id": SESSION}), _TURN_UNBLOCK_TIMEOUT)
            await response.read()
    assert sum(AUTOMATED_NOTICE in str(m["content"]) for m in second.payloads[0]) == 1
    assert [r["role"] for r in _rows(env)][-2:] == ["user", "assistant"]
    thanks = [r for r in _rows(env) if r["role"] == "user" and "thanks" in str(r["content"])]
    assert len(thanks) == 1 and AUTOMATED_NOTICE not in str(thanks[0]["content"])
