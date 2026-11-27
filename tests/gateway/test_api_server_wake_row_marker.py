"""A wake turn on a stateless (api_server) session is saved as a runtime notice (#152765).

Clients read ``GET /api/sessions/{id}/messages`` and must tell a wake from a person typing the
same text without guessing from the text. The wake path names the producer
(``gateway.wake.wake_origin``); the row is typed ``internal_notification`` (the kind push adapters
already persist for the same wake) with ``display_metadata.origin`` and ids. A client cannot type
its own turn: the HTTP self-post carries a one-time in-process token, never a body field.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.wake import (WAKE_TOKEN_HEADER, _pending_wake_origins, deliver_wake, self_post_turn_kwargs,
                          valid_wake_origin, wake_origin)

KANBAN = {"origin": "kanban_wake", "task_id": "t_1", "board": "main"}


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self, port):
        self._host, self._port, self._api_key, self._model_name = "127.0.0.1", port, "k", "hermes"


async def _serve(handler):
    from aiohttp import web
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, site._server.sockets[0].getsockname()[1]


def _self_post(origin):
    """Wake through a stand-in listener; return what the listener could resolve per session."""
    from aiohttp import web
    seen = {}

    async def handler(request):
        seen["header"] = request.headers.get(WAKE_TOKEN_HEADER)
        seen["own"] = self_post_turn_kwargs(request.headers, "sid")
        seen["foreign"] = self_post_turn_kwargs(request.headers, "other-sid")
        seen["body"] = await request.json()
        return web.json_response({"choices": []})

    async def run():
        runner, port = await _serve(handler)
        try:
            await deliver_wake(ApiServerLikeAdapter(port), text="wake", session_id="sid", origin=origin)
        finally:
            await runner.cleanup()

    asyncio.run(run())
    return seen


def test_wake_origin_carries_short_string_ids_only():
    assert wake_origin("kanban_wake", task_id="t_1", board=None, events="") == {
        "origin": "kanban_wake", "task_id": "t_1"}
    assert wake_origin("process_notification", pid=42)["pid"] == "42"
    assert len(wake_origin("x", note="n" * 500)["note"]) == 200
    assert valid_wake_origin(KANBAN)
    for bad in (None, {}, {"origin": ""}, {"task_id": "t"}, {"origin": "x", "n": 1}, {"origin": "x", "n": "n" * 201}):
        assert not valid_wake_origin(bad)


def test_self_post_token_resolves_only_for_its_session_while_in_flight():
    seen = _self_post(KANBAN)
    assert seen["header"]
    assert seen["own"] == {"wake_origin": KANBAN}
    assert seen["foreign"] == {}  # bound to the raw session id it was registered for
    assert seen["body"]["messages"] == [{"role": "user", "content": "wake"}]  # model-facing text unchanged
    assert _pending_wake_origins == {}  # gone once the post returns
    assert self_post_turn_kwargs({WAKE_TOKEN_HEADER: seen["header"]}, "sid") == {}  # not replayable
    assert self_post_turn_kwargs({WAKE_TOKEN_HEADER: "made-up"}, "sid") == {}
    assert self_post_turn_kwargs({}, "sid") == {}


def test_wake_without_origin_sends_no_token():
    seen = _self_post(None)
    assert seen["header"] is None and seen["own"] == {}


def test_without_session_authority_only_the_wake_row_is_typed():
    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    agent = MagicMock()
    agent.run_conversation.return_value = {"final_response": "ok"}
    agent.session_prompt_tokens = agent.session_completion_tokens = agent.session_total_tokens = 0

    async def run(**extra):
        with patch.object(adapter, "_create_agent", return_value=agent):
            await adapter._run_agent(user_message="same text", conversation_history=[], session_id="sid", **extra)
        return agent.run_conversation.call_args.kwargs

    typed = asyncio.run(run())
    assert "persist_user_display_kind" not in typed and "persist_user_display_metadata" not in typed
    woken = asyncio.run(run(wake_origin=KANBAN))
    assert woken["user_message"] == "same text"
    assert woken["persist_user_display_kind"] == "internal_notification"
    assert woken["persist_user_display_metadata"] == KANBAN


def test_session_messages_return_display_metadata():
    row = {"id": 1, "role": "user", "content": "x", "display_kind": "internal_notification",
           "display_metadata": KANBAN}
    assert APIServerAdapter._message_response(row)["display_metadata"] == KANBAN


def test_api_wake_rows_end_to_end(tmp_path):
    """Real API server, session authority and SQLite store with a loopback model."""
    repo = Path(__file__).resolve().parents[2]
    home, state = tmp_path / "home", tmp_path / "state"
    home.mkdir()
    state.mkdir()
    env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), USERPROFILE=str(home), HERMES_HOME=str(state), PYTHONPATH=str(repo))
    result = subprocess.run([sys.executable, str(Path(__file__).parent / "fixtures" / "api_wake_row_peer.py")],
                            cwd=tmp_path, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=100, check=False)
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    receipt = json.loads((state / "receipt.json").read_text())
    rows = {row["content"]: row for row in receipt["rows"]}
    wake = next(text for text in rows if text.startswith("[kanban]") and text.endswith("notification."))

    def kind(text):
        return rows[text]["display_kind"]

    def origin(text):
        return {k: v for k, v in (rows[text]["display_metadata"] or {}).items()
                if k in ("origin", "task_id", "board", "process_session_id", "event")}

    assert kind("TYPED_FIRST") is None
    # The HTTP self-post wake, and the wake text typed by a person: only the wake is typed.
    wakes = [row for row in receipt["rows"] if row["content"] == wake]
    assert [row["display_kind"] for row in wakes] == ["internal_notification", None]
    assert {k: wakes[0]["display_metadata"][k] for k in KANBAN} == KANBAN
    assert "origin" not in (wakes[1]["display_metadata"] or {})
    assert kind(wake + " forged") is None and "origin" not in (rows[wake + " forged"]["display_metadata"] or {})
    assert kind(wake + " in-process") == "internal_notification"
    assert origin(wake + " in-process") == {"origin": "process_notification", "process_session_id": "proc_1",
                                            "event": "completion"}
    # The model saw exactly the text each turn sent: the marker is display-only.
    assert receipt["model_saw"][-len(receipt["sent"]):] == receipt["sent"]


@pytest.mark.parametrize("origin", [{"origin": "x", "n": 1}, {"task_id": "t"}])
def test_admission_refuses_a_malformed_wake_origin(origin):
    from gateway.session_api_turn import admit_api_steps
    from persistence.core.errors import AuthorityRefusal
    adapter = MagicMock()
    with patch("gateway.session_authorities.require_active_authority") as authority, \
            patch("gateway.session_api_turn.authority_persistence") as persistence:
        authority.return_value.logical_owner.side_effect = lambda sid: sid
        persistence.return_value.sessions.get.return_value = None  # a new api_server session
        steps = admit_api_steps(adapter, session_id="sid", user_message="hi", conversation_history=[],
                                wake_origin=origin)
        with pytest.raises(AuthorityRefusal) as refused:
            next(steps)
    assert refused.value.reason == "invalid_params"
