"""``gateway.delegation_completion_wake``: a finished async delegation may start a turn on an IDLE
``api_server`` session; every other case keeps the durable delivery row.

Busy sessions here are made by the production handlers (a real ``/v1/chat/completions`` request,
streaming and not, and a real ``/v1/runs`` run) parked inside a real ``_run_agent``; the delivery
rows are read back from a real session store. Nothing in the idle check is hand-built.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from agent.background_notices import AUTOMATED_NOTICE
from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.run_notifications import INTERNAL_NOTIFICATION_FOOTER
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle  # noqa: F401
from tests.gateway.test_api_server_active_work_drain import (
    _TURN_UNBLOCK_TIMEOUT, _make_admission_app, _parked_agent,
)

SESSION = "20261001_090000_wake01"
RESULT_HEADER = "[ASYNC DELEGATION d1 COMPLETE]"
RESULT_BODY = "the helper finished"
RESULT_TEXT = f"{RESULT_HEADER}\n{RESULT_BODY}"
EVT = {"type": "async_delegation", "delegation_id": "d1"}


def _write_config(home, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    return tmp_path


@pytest.fixture
def wake_on(home):
    _write_config(home, "gateway:\n  delegation_completion_wake: true\n")


@pytest.fixture
def wake_posts(monkeypatch):
    """Record the wake self-posts ``deliver_wake`` makes instead of sending them over HTTP."""
    posts = []

    async def record(adapter, *, text, session_id, **extra):
        posts.append({"text": text, "session_id": session_id, **extra})

    monkeypatch.setattr("gateway.wake._self_post_chat_completion", record)
    return posts


@pytest.fixture
def env(home, api_store, monkeypatch):
    """A real adapter on a real session store holding the api_server session, plus a gateway runner."""
    db = storage_handle(home)
    session_id = seed_session(db, SESSION, "api_server")
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-test"}))
    adapter._home_stores[str(home)] = db
    api_store(db)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.API_SERVER: adapter}
    runner.config = SimpleNamespace(multiplex_profiles=False, profile_routes=())
    runner._primary_profile_name = "default"
    # A live adapter has its runner (``gateway/run.py`` sets it), and then reads its store from the
    # authority the runner reserved for the home; here that authority is the seeded store.
    adapter.gateway_runner = runner
    monkeypatch.setattr("gateway.session_authorities.authority_for_home",
                        lambda _runner, _home: SimpleNamespace(persistence=db))
    return SimpleNamespace(db=db, adapter=adapter, runner=runner, session_id=session_id, home=home)


async def _deliver(env, evt=EVT, text=RESULT_TEXT, sid=SESSION):
    return await env.runner._self_post_api_server(env.adapter, text, sid, evt)


def _rows(env):
    return transcript_rows(env.db, env.session_id)


def _chat(client, *, stream=False):
    return asyncio.ensure_future(client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}], "stream": stream},
        headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Id": SESSION}))


async def _park_turn(env):
    """Patch in an agent whose turn parks inside ``_run_agent`` -> (started event, release event, patcher)."""
    loop = asyncio.get_running_loop()
    started, release = asyncio.Event(), threading.Event()
    agent = _parked_agent(loop, started, release)
    patcher = patch.object(env.adapter, "_create_agent", return_value=agent)
    patcher.start()
    return started, release, patcher


# -- the setting ----------------------------------------------------------------------------------


@pytest.mark.parametrize("body, expected", [
    ("", False),
    ("gateway:\n  delegation_completion_wake: true\n", True),
    ("gateway:\n  delegation_completion_wake: false\n", False),
    ("gateway:\n  delegation_completion_wake: 'false'\n", False),
    ("gateway:\n  delegation_completion_wake: 'yes'\n", True),
])
def test_setting_reads_the_gateway_config(home, body, expected):
    _write_config(home, body)
    assert GatewayRunner._delegation_wake_enabled() is expected


@pytest.mark.asyncio
async def test_default_off_persists_the_row_and_never_wakes(env, home, wake_posts):
    assert await _deliver(env) is True
    assert wake_posts == []
    assert len(_rows(env)) == 1


# -- idle session: woken once, marked as internal, no duplicate row -----------------------------------


@pytest.mark.asyncio
async def test_idle_session_is_woken_with_internal_marker_and_no_row(env, wake_on, wake_posts):
    assert await _deliver(env) is True
    assert len(wake_posts) == 1
    post = wake_posts[0]
    assert post["session_id"] == SESSION
    assert post["text"].startswith(RESULT_HEADER)  # start-anchored consumers still match
    assert RESULT_BODY in post["text"]
    assert AUTOMATED_NOTICE in post["text"]  # and the model is told it is not user input
    assert post["text"].endswith(INTERNAL_NOTIFICATION_FOOTER)
    assert "notification_category" not in post  # a normal result, not a diagnostic
    assert _rows(env) == []  # no delivery row next to the wake turn
    assert env.adapter._session_turns_in_flight == {}  # the reservation is released


@pytest.mark.asyncio
async def test_failure_notice_is_presented_as_a_diagnostic(env, wake_on, wake_posts):
    notice = {**EVT, "task_failure_notice": True, "results": [{"task_index": 0, "status": "failed"}]}
    assert await _deliver(env, notice) is True
    assert wake_posts[0]["notification_category"] == "diagnostic"
    assert wake_posts[0]["text"].endswith(INTERNAL_NOTIFICATION_FOOTER)


@pytest.mark.asyncio
async def test_a_failed_wake_keeps_the_delivery_row(env, wake_on, monkeypatch):
    async def refuse(adapter, **kwargs):
        raise RuntimeError("wake self-post failed: HTTP 503")

    monkeypatch.setattr("gateway.wake._self_post_chat_completion", refuse)
    assert await _deliver(env) is True
    assert len(_rows(env)) == 1
    assert env.adapter._session_turns_in_flight == {}


@pytest.mark.asyncio
async def test_two_completions_for_one_idle_session_wake_it_once(env, wake_on, monkeypatch):
    posts, gate = [], asyncio.Event()

    async def slow_wake(adapter, *, text, session_id, **extra):
        posts.append(text)
        await gate.wait()

    monkeypatch.setattr("gateway.wake._self_post_chat_completion", slow_wake)
    first = asyncio.ensure_future(_deliver(env, {**EVT, "delegation_id": "d1"}))
    for _ in range(100):
        if posts:
            break
        await asyncio.sleep(0.01)
    second = await _deliver(env, {**EVT, "delegation_id": "d2"}, text="[ASYNC DELEGATION d2 COMPLETE]")
    gate.set()
    assert await first is True and second is True
    assert len(posts) == 1
    assert len(_rows(env)) == 1  # the second completion kept its row


# -- busy sessions: every route the api_server runs a turn on ------------------------------------------


async def _settle(env):
    """Wait for the new turn a finished turn starts for results it never read."""
    for _ in range(200):
        if not env.adapter._late_notice_tasks:
            return
        await asyncio.gather(*list(env.adapter._late_notice_tasks))
        await asyncio.sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True], ids=["chat-completions", "chat-completions-stream"])
async def test_chat_completion_in_flight_hands_over_and_a_turn_that_reads_nothing_gets_one_new_turn(
        env, wake_on, wake_posts, stream):
    app = _make_admission_app(env.adapter)
    started, release, patcher = await _park_turn(env)  # a model-less agent that never reads a notice
    try:
        async with TestClient(TestServer(app)) as client:
            request = _chat(client, stream=stream)
            await asyncio.wait_for(started.wait(), _TURN_UNBLOCK_TIMEOUT)
            assert await _deliver(env) is True
            assert wake_posts == [] and _rows(env) == []  # handed to the running turn: no row, no wake yet
            release.set()
            response = await asyncio.wait_for(request, _TURN_UNBLOCK_TIMEOUT)
            assert response.status == 200
            await response.read()
            await _settle(env)
            # the turn ended without reading it: exactly one new turn, still no row
            assert len(wake_posts) == 1 and _rows(env) == []
            assert wake_posts[0]["text"].startswith(RESULT_HEADER)
            assert AUTOMATED_NOTICE in wake_posts[0]["text"]
            assert wake_posts[0]["text"].endswith(INTERNAL_NOTIFICATION_FOOTER)
            # once everything is over the same session is idle again and is woken directly
            assert await _deliver(env, {**EVT, "delegation_id": "d2"}, text="[ASYNC DELEGATION d2 COMPLETE]") is True
            assert len(wake_posts) == 2
    finally:
        release.set()
        patcher.stop()
    assert env.adapter._session_turns_in_flight == {}
    assert env.adapter._turn_agents == {} and env.adapter._late_notices == {}


@pytest.mark.asyncio
async def test_runs_api_turn_in_flight_keeps_the_row(env, wake_on, wake_posts):
    """``/v1/runs`` runs its own agent lifecycle, outside ``_run_agent``: the session is provably busy but
    no agent can be handed anything, so the result is saved as a delivery row (not woken, not lost)."""
    app = _make_admission_app(env.adapter)
    started, release, patcher = await _park_turn(env)
    try:
        async with TestClient(TestServer(app)) as client:
            response = await client.post(
                "/v1/runs", json={"input": "hi", "session_id": SESSION},
                headers={"Authorization": "Bearer sk-test"})
            assert response.status == 202
            await asyncio.wait_for(started.wait(), _TURN_UNBLOCK_TIMEOUT)
            assert await _deliver(env) is True
            assert wake_posts == []
            assert len(_rows(env)) == 1
            release.set()
    finally:
        release.set()
        patcher.stop()


@pytest.mark.asyncio
async def test_session_chat_turn_in_flight_is_handed_the_result(env, wake_on, wake_posts):
    app = _make_admission_app(env.adapter)
    started, release, patcher = await _park_turn(env)
    try:
        async with TestClient(TestServer(app)) as client:
            request = asyncio.ensure_future(client.post(
                f"/api/sessions/{SESSION}/chat", json={"message": "hi"},
                headers={"Authorization": "Bearer sk-test"}))
            await asyncio.wait_for(started.wait(), _TURN_UNBLOCK_TIMEOUT)
            assert await _deliver(env) is True
            assert wake_posts == [] and _rows(env) == []
            release.set()
            await asyncio.wait_for(request, _TURN_UNBLOCK_TIMEOUT)
            await _settle(env)
            assert len(wake_posts) == 1 and _rows(env) == []
    finally:
        release.set()
        patcher.stop()


@pytest.mark.asyncio
async def test_a_turn_whose_agent_cannot_be_reached_keeps_the_row(env, wake_on, wake_posts, monkeypatch):
    """A turn on a path that registers no agent here (another process, a managed worker) is busy but
    cannot be handed anything: no hand-over, no wake, the delivery row."""
    monkeypatch.setattr("gateway.platforms.api_server_runs.register_turn_agent", lambda *a: False)
    app = _make_admission_app(env.adapter)
    started, release, patcher = await _park_turn(env)
    try:
        async with TestClient(TestServer(app)) as client:
            request = _chat(client)
            await asyncio.wait_for(started.wait(), _TURN_UNBLOCK_TIMEOUT)
            assert await _deliver(env) is True
            assert wake_posts == []
            assert len(_rows(env)) == 1
            release.set()
            await asyncio.wait_for(request, _TURN_UNBLOCK_TIMEOUT)
            await _settle(env)
            assert wake_posts == [] and len(_rows(env)) == 1
    finally:
        release.set()
        patcher.stop()


@pytest.mark.asyncio
async def test_a_turn_on_another_session_does_not_block_the_wake(env, wake_on, wake_posts):
    app = _make_admission_app(env.adapter)
    started, release, patcher = await _park_turn(env)
    try:
        async with TestClient(TestServer(app)) as client:
            request = asyncio.ensure_future(client.post(
                "/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]},
                headers={"Authorization": "Bearer sk-test", "X-Hermes-Session-Id": "some-other-session"}))
            await asyncio.wait_for(started.wait(), _TURN_UNBLOCK_TIMEOUT)
            assert await _deliver(env) is True
            assert len(wake_posts) == 1
            release.set()
            await asyncio.wait_for(request, _TURN_UNBLOCK_TIMEOUT)
    finally:
        release.set()
        patcher.stop()


@pytest.mark.asyncio
async def test_pending_clarify_prompt_keeps_the_row(env, wake_on, wake_posts):
    from tools import clarify_gateway
    clarify_gateway.register("clarify-1", SESSION, "Which one?", ["a", "b"])
    try:
        assert await _deliver(env) is True
    finally:
        clarify_gateway.clear_session(SESSION)
    assert wake_posts == []
    assert len(_rows(env)) == 1


@pytest.mark.asyncio
async def test_a_request_admitted_but_not_yet_running_keeps_the_row(env, wake_on, wake_posts):
    env.adapter._pending_agent_requests = 1  # what _admit_api_agent_request holds before _run_agent starts
    assert await _deliver(env) is True
    assert wake_posts == []
    assert len(_rows(env)) == 1


@pytest.mark.asyncio
async def test_adapter_without_the_turn_table_keeps_the_row(env, wake_on, wake_posts):
    del env.adapter._session_turns_in_flight  # an adapter that cannot prove idleness never wakes
    assert await _deliver(env) is True
    assert wake_posts == []
    assert len(_rows(env)) == 1


# -- served profile ownership -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unproven_served_profile_keeps_the_row(env, wake_on, wake_posts, monkeypatch):
    def refuse(self, evt, raw_sid):
        raise LookupError("session is not in served profile 'builder's own store")

    monkeypatch.setattr(GatewayRunner, "_served_api_server_wake_profile", refuse)
    assert await _deliver(env) is True
    assert wake_posts == []
    assert len(_rows(env)) == 1


@pytest.mark.asyncio
async def test_served_profile_session_is_woken_in_process_never_in_the_default_store(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "builder").mkdir(parents=True)
    (root / "config.yaml").write_text(
        "gateway:\n  multiplex_profiles: true\n  delegation_completion_wake: true\n", encoding="utf-8")
    (root / "profiles" / "builder" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr("gateway.run._hermes_home", root)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: root)
    from persistence_assembly import open_persistence, profile_scope
    with open_persistence(profile_scope(home=root / "profiles" / "builder")) as store:
        seed_session(store, SESSION, "webui", profile_name="builder")

    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "sk-test"}))
    turns = []

    async def in_process(*, session_id, text, profile, notification_category="result"):
        from hermes_constants import get_hermes_home
        turns.append({"session_id": session_id, "profile": profile, "text": text,
                      "home": str(get_hermes_home()), "category": notification_category})

    monkeypatch.setattr(adapter, "run_internal_session_turn", in_process)
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.adapters = {Platform.API_SERVER: adapter}
    runner._profile_adapters = {"builder": {}}
    runner._profile_failed_platforms = {}
    runner._primary_profile_name = "default"
    runner.config = SimpleNamespace(multiplex_profiles=True, profile_routes=())
    assert await runner._self_post_api_server(adapter, RESULT_TEXT, SESSION, EVT) is True
    assert len(turns) == 1
    assert turns[0]["profile"] == "builder"
    assert turns[0]["home"] == str(root / "profiles" / "builder")
    assert turns[0]["text"].endswith(INTERNAL_NOTIFICATION_FOOTER)
    assert adapter._session_turns_in_flight == {}


# -- a real agent loop: the notice reaches the model at its step boundary ----------------------------------
#
# Real handler (``/v1/chat/completions``), real ``_run_agent``, real ``AIAgent.run_conversation`` with its real
# tool dispatch and ``prepare_iteration``. ONLY THE MODEL IS SCRIPTED (``_interruptible_api_call``): each
# call records what it was shown, may deliver a completion while it "works", and answers with a tool call
# or a final answer.


class _Turn:
    """One scripted turn. ``on_call(n)`` runs inside model call *n* (in the agent's thread)."""

    def __init__(self, env, on_call, answers):
        from tests.agent.test_background_notices import loop_agent
        self.env, self.on_call, self.answers = env, on_call, answers
        self.payloads: list = []
        self.agent = loop_agent()
        self.agent._interruptible_api_call = self._model
        self.loop = asyncio.get_running_loop()

    def _model(self, api_kwargs):
        from tests.agent.test_run_agent import _mock_response
        from tests.agent.test_background_notices import PROBE_TOOL
        self.payloads.append([dict(m) for m in api_kwargs["messages"]])
        n = len(self.payloads)
        if self.on_call:
            self.on_call(n)
        if self.answers[n - 1] == "tool":
            call = SimpleNamespace(id=f"call_{n}", type="function",
                                   function=SimpleNamespace(name=PROBE_TOOL, arguments="{}"))
            return _mock_response(content=None, finish_reason="tool_calls", tool_calls=[call])
        return _mock_response(content="done", finish_reason="stop")

    def deliver_from_the_turn(self, evt=EVT, text=RESULT_TEXT):
        """A completion that arrives while the model works: the gateway's loop delivers it."""
        return asyncio.run_coroutine_threadsafe(_deliver(self.env, evt, text), self.loop).result(30)

    def seen(self, body=RESULT_BODY) -> int:
        """How many times the model was shown *body* in its LAST request (history is re-sent each call)."""
        return sum(str(m.get("content")).count(body) for m in self.payloads[-1])

    async def run(self, client):
        patches = [patch.object(self.env.adapter, "_create_agent", return_value=self.agent),
                   patch.object(self.agent, "_flush_messages_to_session_db"),
                   patch.object(self.agent, "_persist_session"),
                   patch.object(self.agent, "_save_trajectory"),
                   patch.object(self.agent, "_cleanup_task_resources")]
        for each in patches:
            each.start()
        try:
            response = await asyncio.wait_for(_chat(client), _TURN_UNBLOCK_TIMEOUT)
            assert response.status == 200, await response.text()
            await response.read()
            await _settle(self.env)
        finally:
            for each in patches:
                each.stop()


def _deliveries(turn, env, wake_posts, body=RESULT_BODY) -> dict:
    """Every place one result can be delivered to, so a test can assert exactly one of them got it."""
    return {"mid_turn": turn.seen(body) if turn.payloads else 0,
            "new_turn": sum(body in post["text"] for post in wake_posts),
            "delivery_row": sum(body in str(row.get("content")) for row in _rows(env))}


@pytest.mark.asyncio
async def test_busy_turn_with_a_following_tool_step_gets_the_notice_in_the_same_turn(env, wake_on, wake_posts):
    turn = _Turn(env, lambda n: n == 1 and turn.deliver_from_the_turn() is True, ["tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        await turn.run(client)
    assert len(turn.payloads) == 2
    assert RESULT_BODY not in str(turn.payloads[0])
    notice = turn.payloads[1][-1]  # the row right after the tool result
    assert [m["role"] for m in turn.payloads[1][-3:]] == ["assistant", "tool", "user"]
    assert notice["content"].startswith(RESULT_HEADER) and RESULT_BODY in notice["content"]
    assert AUTOMATED_NOTICE in notice["content"]
    assert notice["content"].endswith(INTERNAL_NOTIFICATION_FOOTER)
    assert "OUT-OF-BAND USER MESSAGE" not in str(turn.payloads)  # not the user-steer marker
    # exactly once: read mid-turn, no new turn, no delivery row
    assert _deliveries(turn, env, wake_posts) == {"mid_turn": 1, "new_turn": 0, "delivery_row": 0}
    assert env.adapter._turn_agents == {} and env.adapter._late_notices == {}


@pytest.mark.asyncio
async def test_busy_turn_on_its_final_answer_gets_exactly_one_new_turn_after_it_ends(env, wake_on, wake_posts):
    turn = _Turn(env, lambda n: n == 2 and turn.deliver_from_the_turn() is True, ["tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        await turn.run(client)
    assert len(turn.payloads) == 2  # no third model call: the turn never read it
    assert _deliveries(turn, env, wake_posts) == {"mid_turn": 0, "new_turn": 1, "delivery_row": 0}
    text = wake_posts[0]["text"]
    assert text.startswith(RESULT_HEADER) and AUTOMATED_NOTICE in text and text.endswith(INTERNAL_NOTIFICATION_FOOTER)
    assert env.adapter._session_turns_in_flight == {} and env.adapter._late_notices == {}


@pytest.mark.asyncio
async def test_two_completions_during_one_turn_are_each_delivered_once(env, wake_on, wake_posts):
    def arrive(n):
        if n == 1:
            assert turn.deliver_from_the_turn({**EVT, "delegation_id": "d1"}, "[ASYNC DELEGATION d1 COMPLETE]\nfirst done")
            assert turn.deliver_from_the_turn({**EVT, "delegation_id": "d2"}, "[ASYNC DELEGATION d2 COMPLETE]\nsecond done")
        if n == 3:  # a third one, too late for this turn
            assert turn.deliver_from_the_turn({**EVT, "delegation_id": "d3"}, "[ASYNC DELEGATION d3 COMPLETE]\nthird done")

    turn = _Turn(env, arrive, ["tool", "tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        await turn.run(client)
    assert _deliveries(turn, env, wake_posts, "first done") == {"mid_turn": 1, "new_turn": 0, "delivery_row": 0}
    assert _deliveries(turn, env, wake_posts, "second done") == {"mid_turn": 1, "new_turn": 0, "delivery_row": 0}
    assert _deliveries(turn, env, wake_posts, "third done") == {"mid_turn": 0, "new_turn": 1, "delivery_row": 0}
    assert len(wake_posts) == 1  # one new turn, only for the late one


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", ["clarify", "approval"])
async def test_completion_during_a_prompt_for_a_person_starts_nothing_and_waits_as_a_row(
        env, wake_on, wake_posts, prompt):
    """A clarify question or a dangerous-command approval is pending: no hand-over the model could read
    as the answer, no new turn. The result is kept as the delivery row; the turn never sees it."""
    from tools import approval, clarify_gateway

    def ask():
        if prompt == "clarify":
            clarify_gateway.register("clarify-1", SESSION, "Which one?", ["a", "b"])
        else:
            approval._gateway_queues[SESSION] = [object()]  # what a blocked approval leaves queued

    def answer():
        clarify_gateway.clear_session(SESSION)
        approval._gateway_queues.pop(SESSION, None)

    def arrive(n):
        if n == 1:
            ask()
            try:
                turn.deliver_from_the_turn()
            finally:
                answer()  # the person answered; the turn goes on

    turn = _Turn(env, arrive, ["tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        await turn.run(client)
    assert _deliveries(turn, env, wake_posts) == {"mid_turn": 0, "new_turn": 0, "delivery_row": 1}
    assert "AUTOMATED NOTICE" not in str(turn.payloads)


@pytest.mark.asyncio
async def test_with_the_setting_off_a_running_turn_is_untouched_and_the_row_is_kept(env, home, wake_posts):
    turn = _Turn(env, lambda n: n == 1 and turn.deliver_from_the_turn() is True, ["tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        await turn.run(client)
    assert _deliveries(turn, env, wake_posts) == {"mid_turn": 0, "new_turn": 0, "delivery_row": 1}
    assert "AUTOMATED NOTICE" not in str(turn.payloads)
    assert not hasattr(turn.agent, "_background_notices")  # the agent was never handed a buffer


@pytest.mark.asyncio
async def test_a_late_result_whose_new_turn_cannot_start_is_kept_as_the_delivery_row(env, wake_on, monkeypatch):
    async def refuse(adapter, **kwargs):
        raise RuntimeError("wake self-post failed: HTTP 503")

    monkeypatch.setattr("gateway.wake._self_post_chat_completion", refuse)
    turn = _Turn(env, lambda n: n == 2 and turn.deliver_from_the_turn() is True, ["tool", "final"])
    async with TestClient(TestServer(_make_admission_app(env.adapter))) as client:
        await turn.run(client)
    assert _deliveries(turn, env, []) == {"mid_turn": 0, "new_turn": 0, "delivery_row": 1}
    assert env.adapter._late_notices == {} and env.adapter._session_turns_in_flight == {}
