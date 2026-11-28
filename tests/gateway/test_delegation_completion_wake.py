"""``gateway.delegation_completion_wake``: a finished async delegation may start a turn on an IDLE
``api_server`` session; every other case keeps the durable delivery row.

These tests use the harness gateway, which has NO session authority (the executor route). Every gateway
that is actually started has one: the behaviour on that route is proven in
``test_delegation_wake_authority.py`` (real authority, real HTTP, loopback model). Busy sessions here are
made by the production handlers parked inside a real ``_run_agent``; the delivery rows are read back from a
real session store; the wake self-post is a recorder.
"""
import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import Platform, PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.run import GatewayRunner
from gateway.run_notifications import INTERNAL_NOTIFICATION_FOOTER
from session_rules.turns import AUTOMATED_NOTICE
from tests.fixtures.storage_read import transcript_rows
from tests.fixtures.storage_seed import seed_session
from tests.gateway.fixtures.delegation_secret_shapes import SHAPES
from tests.gateway.fixtures.storage_handles import close_storage_handles, storage_handle  # noqa: F401
from tests.gateway.test_api_server_active_work_drain import (
    _TURN_UNBLOCK_TIMEOUT, _make_admission_app, _parked_agent,
)

SESSION = "20261001_090000_wake01"
RESULT_HEADER = "[ASYNC DELEGATION COMPLETE \u2014 d1]"
RESULT_BODY = "the helper finished"
EVT = {"type": "async_delegation", "delegation_id": "d1", "status": "completed", "summary": RESULT_BODY}


def _text_of(evt):
    """What the watcher formats from a finished helper's event. The wake rebuilds this text from the event."""
    from tools.process_registry_notifications import format_process_notification
    return format_process_notification(evt)


RESULT_TEXT = _text_of(EVT)


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


def _live_and_replayed(summary):
    """The finished helper's live event (secrets partly masked, as the producer leaves it for the parent's own
    copy) and the copy the ledger keeps and a restart replays (fully masked)."""
    from tools.async_delegation import completion_event
    from tools.delegation_redaction import redact_completion_event
    record = {"delegation_id": "d1", "session_key": "k", "goal": "g", "dispatched_at": 1.0, "completed_at": 2.0}
    live, _result, _silent = completion_event(record, {"summary": summary, "duration_seconds": 1.0}, "completed")
    return live, redact_completion_event(live)


@pytest.mark.asyncio
@pytest.mark.parametrize("name, text, secret", SHAPES)
async def test_the_wake_text_of_a_live_event_is_the_text_its_replay_gives_and_never_less_masked(
        env, wake_on, wake_posts, name, text, secret):
    """After a hard kill the restart replays the ledger's fully masked copy and looks for the wake by its exact
    text. The live event is only partly masked (``Bearer abcdef...2345``), so a wake built from it never matched.
    Before the fix 5 of these 16 shapes gave different texts (the Bearer, token and api_key= forms)."""
    from gateway.run_notifications import _mark_internal_notification
    from session_rules.turns import with_automated_notice
    from tools.process_registry_notifications import format_process_notification
    live, replayed = _live_and_replayed("RESULT " + text)
    live_text, replay_text = format_process_notification(live), format_process_notification(replayed)
    assert await env.runner._self_post_api_server(env.adapter, live_text, SESSION, live) is True
    assert len(wake_posts) == 1
    posted = wake_posts[0]["text"]
    assert posted == _mark_internal_notification(with_automated_notice(replay_text))  # byte-identical to the replay
    assert secret not in posted or secret in live_text  # never less masked than the text it replaces


@pytest.mark.asyncio
async def test_failure_notice_never_starts_a_turn_it_stays_a_diagnostic_row(env, wake_on, wake_posts):
    """D2: the early per-task notice is a diagnostic; the batch's final result is the one that wakes (the rule of
    ``delegation_wakes_parent``). Before the fix it was woken as its own turn, giving two turns per batch."""
    notice = {**EVT, "task_failure_notice": True, "results": [{"task_index": 0, "status": "failed"}]}
    assert await _deliver(env, notice) is True
    assert wake_posts == []
    rows = _rows(env)
    assert len(rows) == 1 and rows[0]["display_kind"] == "async_delegation_complete"


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
    second = await _deliver(env, {**EVT, "delegation_id": "d2"}, text=_text_of({**EVT, "delegation_id": "d2"}))
    gate.set()
    assert await first is True and second is True
    assert len(posts) == 1
    assert len(_rows(env)) == 1  # the second completion kept its row


# -- busy sessions: the routes the api_server runs a turn on (executor route) ------------------------------






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
