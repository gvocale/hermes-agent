"""Tests for Slack Block Kit interactive clarify buttons.

Mirrors test_slack_approval_buttons.py (harness) and
test_telegram_clarify_buttons.py (semantics) for the ``send_clarify`` override
and the indexed ``hermes_clarify_choice_<idx>`` /
``hermes_clarify_other`` action dispatch.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Ensure the repo root is importable
# ---------------------------------------------------------------------------
_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)


# ---------------------------------------------------------------------------
# Minimal Slack SDK mock so SlackAdapter can be imported (mirrors
# test_slack_approval_buttons.py)
# ---------------------------------------------------------------------------
def _ensure_slack_mock():
    if "slack_bolt" in sys.modules:
        return
    slack_bolt = MagicMock()
    slack_bolt.async_app.AsyncApp = MagicMock
    sys.modules["slack_bolt"] = slack_bolt
    sys.modules["slack_bolt.async_app"] = slack_bolt.async_app
    handler_mod = MagicMock()
    handler_mod.AsyncSocketModeHandler = MagicMock
    sys.modules["slack_bolt.adapter"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode"] = MagicMock()
    sys.modules["slack_bolt.adapter.socket_mode.async_handler"] = handler_mod
    sdk_mod = MagicMock()
    sdk_mod.web = MagicMock()
    sdk_mod.web.async_client = MagicMock()
    sdk_mod.web.async_client.AsyncWebClient = MagicMock
    sys.modules["slack_sdk"] = sdk_mod
    sys.modules["slack_sdk.web"] = sdk_mod.web
    sys.modules["slack_sdk.web.async_client"] = sdk_mod.web.async_client


_ensure_slack_mock()

from plugins.platforms.slack.adapter import SlackAdapter
from gateway.config import PlatformConfig


def _make_adapter():
    config = PlatformConfig(enabled=True, token="xoxb-test-token")
    adapter = SlackAdapter(config)
    adapter._app = MagicMock()
    adapter._bot_user_id = "U_BOT"
    adapter._team_clients = {"T1": AsyncMock()}
    adapter._team_bot_user_ids = {"T1": "U_BOT"}
    adapter._channel_team = {"C1": "T1"}
    return adapter


class _AuthRunner:
    def __init__(self, auth_fn=None):
        self._auth_fn = auth_fn or (lambda _source: True)

    async def handle(self, event):
        return None

    def _is_user_authorized(self, source):
        return self._auth_fn(source)


def _attach_auth_runner(adapter, auth_fn=None):
    adapter.set_message_handler(_AuthRunner(auth_fn=auth_fn).handle)


def _clear_clarify_state():
    from tools import clarify_gateway as cm
    with cm._lock:
        cm._entries.clear()
        cm._session_index.clear()
        cm._notify_cbs.clear()


# ===========================================================================
# send_clarify — Block Kit render (a)
# ===========================================================================

class TestSlackSendClarify:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_multi_choice_renders_buttons_and_other(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1234.5678"})

        result = await adapter.send_clarify(
            chat_id="C1",
            question="Which environment?",
            choices=["staging", "production"],
            clarify_id="cid1",
            session_key="sk1",
        )

        assert result.success is True
        assert result.message_id == "1234.5678"
        # ts recorded for the double-click guard
        assert adapter._clarify_resolved.get("1234.5678") is False

        kwargs = mock_client.chat_postMessage.call_args[1]
        blocks = kwargs["blocks"]
        assert blocks[0]["type"] == "section"
        assert "Which environment?" in blocks[0]["text"]["text"]
        assert blocks[1]["type"] == "actions"
        elements = blocks[1]["elements"]
        # 2 choices + Other
        assert len(elements) == 3
        assert elements[0]["action_id"] == "hermes_clarify_choice_0"
        assert elements[0]["value"] == "cid1|0"
        assert elements[1]["action_id"] == "hermes_clarify_choice_1"
        assert elements[1]["value"] == "cid1|1"
        assert elements[0]["text"]["text"] == "staging"
        # Final button is the free-text "Other"
        assert elements[2]["action_id"] == "hermes_clarify_other"
        assert elements[2]["value"] == "cid1|other"
        for block in blocks:
            if block["type"] == "actions":
                action_ids = [element["action_id"] for element in block["elements"]]
                assert len(action_ids) == len(set(action_ids))


    @pytest.mark.asyncio
    async def test_long_choices_are_explained_above_short_reference_buttons(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1.0"})
        choices = [
            "Switch the operator advisor to gpt-5.6-luna, retry all nine decisions, and verify Slack delivery (Recommended)",
            "Keep gpt-5.6-sol and raise the timeout to fifteen minutes before retrying all nine decisions",
        ]

        await adapter.send_clarify(
            chat_id="C1",
            question="How should I finish recovery?",
            choices=choices,
            clarify_id="cid-long",
            session_key="sk-long",
        )

        blocks = mock_client.chat_postMessage.call_args[1]["blocks"]
        explanations = "\n".join(
            block["text"]["text"] for block in blocks if block["type"] == "section")
        assert "How should I finish recovery?" in explanations
        assert "*Option 1*" in explanations
        assert choices[0] in explanations
        assert choices[1] in explanations
        labels = [element["text"]["text"] for element in blocks[-1]["elements"]]
        assert labels == ["Option 1", "Option 2", "✏️ Other…"]

    @pytest.mark.asyncio
    async def test_each_long_choice_keeps_its_own_section_budget(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1.0"})
        choices = ["A" * 2000, "B" * 2000]

        await adapter.send_clarify(
            chat_id="C1",
            question="Choose one",
            choices=choices,
            clarify_id="cid-large",
            session_key="sk-large",
        )

        blocks = mock_client.chat_postMessage.call_args[1]["blocks"]
        section_texts = [block["text"]["text"] for block in blocks if block["type"] == "section"]
        assert choices[0] in section_texts[1]
        assert choices[1] in section_texts[2]
        assert [element["text"]["text"] for element in blocks[-1]["elements"]] == [
            "Option 1", "Option 2", "✏️ Other…"]

    @pytest.mark.asyncio
    async def test_recommended_suffix_does_not_make_a_short_choice_long(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1.0"})

        await adapter.send_clarify(
            chat_id="C1",
            question="Choose one",
            choices=["Use Luna for retries (Recommended)", "Keep Sol"],
            clarify_id="cid-recommended",
            session_key="sk-recommended",
        )

        blocks = mock_client.chat_postMessage.call_args[1]["blocks"]
        labels = [element["text"]["text"] for element in blocks[1]["elements"]]
        assert labels == ["Use Luna for retries (Recommended)", "Keep Sol", "✏️ Other…"]

    @pytest.mark.asyncio
    async def test_mrkdwn_escapes_question(self):
        adapter = _make_adapter()
        mock_client = adapter._team_clients["T1"]
        mock_client.chat_postMessage = AsyncMock(return_value={"ts": "1.1"})

        await adapter.send_clarify(
            chat_id="C1",
            question="Use <A> & <B>?",
            choices=["yes"],
            clarify_id="cid2",
            session_key="sk2",
        )
        section_text = mock_client.chat_postMessage.call_args[1]["blocks"][0]["text"]["text"]
        assert "<A>" not in section_text
        assert "&lt;A&gt;" in section_text
        assert "&amp;" in section_text


# ===========================================================================
# _handle_clarify_action — choice click resolves (b)
# ===========================================================================

class TestSlackClarifyChoiceAction:
    def setup_method(self):
        _clear_clarify_state()


    @pytest.mark.asyncio
    async def test_unauthorized_click_ignored(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        _attach_auth_runner(adapter, auth_fn=lambda _s: False)
        cm.register("cidAuth", "sk-auth", "Pick", ["a", "b"])
        adapter._clarify_resolved["2.2"] = False

        ack = AsyncMock()
        body = {
            "message": {"ts": "2.2", "blocks": []},
            "channel": {"id": "C1"},
            "user": {"name": "mallory", "id": "U_BAD"},
        }
        action = {"action_id": "hermes_clarify_choice", "value": "cidAuth|0"}

        await adapter._handle_clarify_action(ack, body, action)

        with cm._lock:
            entry = cm._entries.get("cidAuth")
        assert entry is not None
        assert not entry.event.is_set()


# ===========================================================================
# _handle_clarify_action — "Other" → text-capture → typed reply (c)
# ===========================================================================

class TestSlackClarifyOtherFlow:
    def setup_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    async def test_other_flips_to_text_mode_then_typed_reply_resolves(self):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        cm.register("cidO", "sk-other", "Pick", ["x", "y"])
        adapter._clarify_resolved["4.4"] = False

        mock_client = adapter._team_clients["T1"]
        mock_client.chat_update = AsyncMock()

        ack = AsyncMock()
        body = {
            "message": {"ts": "4.4", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "❓ Pick"}},
                {"type": "actions", "elements": []},
            ]},
            "channel": {"id": "C1"},
            "user": {"name": "norbert", "id": "U_N"},
        }
        action = {"action_id": "hermes_clarify_other", "value": "cidO|other"}

        await adapter._handle_clarify_action(ack, body, action)

        # Entry flipped to text-capture; NOT yet resolved.
        pending = cm.get_pending_for_session("sk-other")
        assert pending is not None and pending.clarify_id == "cidO"
        assert pending.awaiting_text is True
        with cm._lock:
            entry = cm._entries.get("cidO")
        assert not entry.event.is_set()
        assert "awaiting" in mock_client.chat_update.call_args[1]["text"].lower()

        # Now the gateway text-intercept (platform-agnostic) resolves from the
        # user's next typed message. We exercise that leveraged path directly.
        assert cm.resolve_text_response_for_session("sk-other", "my custom answer") is True
        with cm._lock:
            entry = cm._entries.get("cidO")
        assert entry.response == "my custom answer"
        assert entry.event.is_set()


# Synthetic data, real adapter methods, fake Slack transport. Historical cards
# without recipient metadata retain literal prose through every lifecycle update.
class TestSlackClarifyMentionContract:
    def setup_method(self):
        _clear_clarify_state()

    def teardown_method(self):
        _clear_clarify_state()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["choice", "other", "expired"])
    async def test_question_encoding_is_introduced_on_send_not_action(self, outcome):
        from tools import clarify_gateway as cm

        adapter = _make_adapter()
        _attach_auth_runner(adapter)
        client = adapter._team_clients["T1"]
        client.chat_postMessage = AsyncMock(return_value={"ts": "1234.567890"})
        mention = "<@U123ABCDE45>"
        question = f"{mention} Apply the correction?"
        choices = ["Apply correction", "Keep baseline"]
        cm.register("mention-probe", "mention-session", question, choices)
        try:
            result = await adapter.send_clarify(
                "C1", question, choices, "mention-probe", "mention-session")
            assert result.success
            sent = client.chat_postMessage.call_args.kwargs
            original = sent["blocks"][0]["text"]["text"]
            escaped = mention.replace("<", "&lt;").replace(">", "&gt;")
            assert escaped in original and mention not in original
            assert escaped in sent["text"]
            assert mention in adapter.format_message(question)

            if outcome == "expired":
                _clear_clarify_state()
            token = "other" if outcome == "other" else "0"
            action_id = "hermes_clarify_other" if outcome == "other" else "hermes_clarify_choice_0"
            await adapter._handle_clarify_action(AsyncMock(), {
                "message": {"ts": result.message_id, "blocks": sent["blocks"]},
                "channel": {"id": "C1"},
                "user": {"name": "synthetic-user", "id": "U123ABCDE45"},
            }, {"action_id": action_id, "value": f"mention-probe|{token}"})
            updated = client.chat_update.call_args.kwargs
            assert updated["blocks"][0]["text"]["text"] == original
            assert "&amp;lt;" not in updated["blocks"][0]["text"]["text"]
            if outcome == "choice":
                assert cm._entries["mention-probe"].response == choices[0]
                # The permalink reader joins decision fallback + question block;
                # it does not introduce the entities seen in answered prompts.
                readable = adapter._render_message_text(updated)
                assert readable.startswith(f"✅ synthetic-user: {choices[0]}")
                assert escaped in readable and mention not in readable

            # A pre-encoded input adds another encoding layer at initial SEND.
            await adapter.send_clarify(
                "C1", question.replace(mention, escaped), choices,
                "encoded-probe", "encoded-session")
            encoded = client.chat_postMessage.call_args.kwargs["blocks"][0]["text"]["text"]
            assert "&amp;lt;@U123ABCDE45&amp;gt;" in encoded
        finally:
            _clear_clarify_state()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("surface", ["fallback", "section"])
    async def test_intended_recipient_is_an_active_mention(self, surface):
        adapter = _make_adapter()
        client = adapter._team_clients["T1"]
        client.chat_postMessage = AsyncMock(return_value={"ts": "1234.567890"})
        mention = "<@U123ABCDE45>"
        result = await adapter.send_clarify(
            "C1", f"{mention} Apply the correction?", ["Apply", "Keep"],
            "recipient-probe", "recipient-session", metadata={
                "slack_team_id": "T1",
                "clarify_recipient": {"platform": "slack", "scope_id": "T1",
                                      "user_id": "U123ABCDE45", "chat_id": "C1"},
            })
        assert result.success
        sent = client.chat_postMessage.call_args.kwargs
        text = sent["text"] if surface == "fallback" else sent["blocks"][0]["text"]["text"]
        assert mention in text


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["choice", "other", "expired"])
@pytest.mark.parametrize("identity", ["trusted", "missing", "foreign", "malformed", "unknown", "conflicting"])
async def test_clarify_recipient_is_the_only_active_syntax(outcome, identity):
    import re
    from tools import clarify_gateway as cm

    _clear_clarify_state()
    adapter = _make_adapter()
    _attach_auth_runner(adapter)
    client = adapter._team_clients["T1"]
    # Exercise the legacy primary fallback too, without any network client.
    adapter._app.client = client
    client.chat_postMessage.return_value = {"ts": "1234.567890"}
    recipient = {"platform": "slack", "scope_id": "T1", "user_id": "U123ABCDE45", "chat_id": "C1"}
    metadata = {"clarify_recipient": recipient, "slack_team_id": "T1"}
    recipient.update({
        "foreign": {"scope_id": "T2"},
        "malformed": {"user_id": "U123ABCDE45> <!here"},
    }.get(identity, {}))
    if identity == "missing":
        metadata.pop("clarify_recipient")
    if identity == "unknown":
        adapter._channel_team.clear()
    if identity == "conflicting":
        metadata["slack_team_id"] = "T2"
    prose = ('<@U123ABCDE45> <@UOTHER123|label> <!channel> <!here> <!everyone> '
             '<!subteam^S123ABCD> `<@U123ABCDE45>` "> <@U123ABCDE45>" '
             '<https://example.com|link> <A> & &lt;@U123ABCDE45&gt; '
             '&amp;lt;@U123ABCDE45&amp;gt;')
    # Force escaped entities across section/fallback boundaries, with Unicode.
    question = prose + '🧪&' * 2000
    choices = [prose + '🧪&' * 1000, "Keep"]
    cm.register("security-card", "security-session", question, choices)
    try:
        result = await adapter.send_clarify("C1", question, choices, "security-card",
                                            "security-session", metadata=metadata)
        assert result.success
        sent = client.chat_postMessage.call_args.kwargs
        sections = [b["text"]["text"] for b in sent["blocks"] if b["type"] == "section"]
        expected = ["<@U123ABCDE45>"] if identity == "trusted" else []
        for text in (sent["text"], *sections):
            assert len(text) <= 3000
            assert not re.search(r"&(?!amp;|lt;|gt;)", text), "truncated escape entity"
        for text in (sent["text"], sections[0]):
            assert re.findall(r"<[^>]*>", text) == expected
            assert "&lt;@U123ABCDE45&gt;" in text  # no prose-token stripping
        assert all("<" not in text for text in sections[1:])
        original = sections[0]
        if outcome == "expired":
            cm.clear_session("security-session")
        action = {"action_id": "hermes_clarify_other" if outcome == "other" else "hermes_clarify_choice_0",
                  "value": "security-card|other" if outcome == "other" else "security-card|0"}
        body = {"message": {"ts": result.message_id, "blocks": sent["blocks"]},
                "channel": {"id": "C1"}, "user": {"id": "U123ABCDE45", "name": "<!here> & user"}}
        await adapter._handle_clarify_action(AsyncMock(), body, action)
        updated = client.chat_update.call_args.kwargs
        assert updated["blocks"][0]["text"]["text"] == original
        for text in (updated["text"], updated["blocks"][1]["elements"][0]["text"]):
            assert "<" not in text  # neither display name nor canonical answer can notify
            assert len(text) <= 3000
            assert not re.search(r"&(?!amp;|lt;|gt;)", text)
        if outcome == "choice":
            assert cm._entries["security-card"].response == choices[0]
        # Duplicate interaction never re-edits or notifies.
        count = client.chat_update.await_count
        await adapter._handle_clarify_action(AsyncMock(), body, action)
        assert client.chat_update.await_count == count
    finally:
        _clear_clarify_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("chat_id,thread_id", [("C1", None), ("C1", "12.34"), ("D1", None)])
@pytest.mark.parametrize("route", ["origin", "home-chat", "other-adapter", "missing-scope", "foreign-scope"])
async def test_real_clarify_callback_binds_turn_recipient(native, chat_id, thread_id, route, monkeypatch):
    import asyncio
    import concurrent.futures
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.turn_context import TurnContext
    from gateway.run_turn_runner import TurnRunner
    from gateway.session import SessionSource
    from tools import clarify_gateway as cm

    _clear_clarify_state()
    adapter = _make_adapter()
    client = adapter._team_clients["T1"]
    client.chat_postMessage.return_value = {"ts": "1234.567890"}
    adapter._channel_team[chat_id] = "T1"
    source = SessionSource(platform=Platform.SLACK, chat_id=chat_id,
                           scope_id="T1", user_id="U123ABCDE45", thread_id=thread_id)
    runner = object.__new__(GatewayRunner)
    monkeypatch.setattr(runner, "_adapter_for_source", lambda _source: adapter)
    _, _, metadata = runner._run_agent_progress_threading(source, None, native)
    destination_adapter, destination_chat = adapter, chat_id
    if route == "home-chat":
        destination_chat = "C2"
        adapter._channel_team[destination_chat] = "T1"
    elif route == "other-adapter":
        destination_adapter = _make_adapter()
        destination_adapter._channel_team[chat_id] = "T1"
        client = destination_adapter._team_clients["T1"]
        client.chat_postMessage.return_value = {"ts": "1234.567890"}
    source.scope_id = {"missing-scope": None, "foreign-scope": "T2"}.get(route, source.scope_id)
    # A cached progress recipient must never survive a routed clarification.
    if route != "origin":
        metadata = dict(metadata or {}, clarify_recipient={
            "platform": "slack", "scope_id": "T1", "user_id": "U123ABCDE45", "chat_id": destination_chat})
    consumer = MagicMock() if native else None
    if consumer:
        consumer._use_native_streaming = True
        boundary = concurrent.futures.Future()
        boundary.set_result(True)
        consumer.close_for_approval_prompt.return_value = boundary
    ctx = TurnContext(source=source, _status_adapter=destination_adapter, _status_chat_id=destination_chat,
                          _status_thread_metadata=metadata, session_key="caller-session",
                          _loop_for_step=asyncio.get_running_loop(),
                          stream_consumer_holder=[consumer])
    turn = TurnRunner(runner, ctx)
    original_metadata = dict(metadata) if metadata else None

    # Only replace the human wait; real callback registration, scheduling, adapter and
    # payload rendering run, and the wait resolves the exact canonical choice.
    def answer(clarify_id, timeout):
        assert cm.resolve_gateway_clarify(clarify_id, "Keep")
        return cm._entries[clarify_id].response

    monkeypatch.setattr(cm, "wait_for_response", answer)
    try:
        result = await asyncio.to_thread(turn._clarify_callback_sync,
                                         "<@UOTHER123> <!here> Choose?", ["Apply", "Keep"])
        assert result == "Keep"
        sent = client.chat_postMessage.call_args.kwargs
        for text in (sent["text"], sent["blocks"][0]["text"]["text"]):
            assert text.count("<@U123ABCDE45>") == (1 if route == "origin" else 0)
            assert "<@UOTHER123>" not in text and "<!here>" not in text
        assert sent.get("thread_ts") == thread_id
        assert metadata == original_metadata  # no shared progress-state mutation
    finally:
        _clear_clarify_state()


# ===========================================================================
# Base text-fallback unchanged for platforms without an override (e)
# ===========================================================================

class TestBaseAdapterClarifyFallbackUnchanged:
    @pytest.mark.asyncio
    async def test_base_numbered_text_fallback(self):
        from gateway.platforms.base import BasePlatformAdapter, SendResult

        class _Stub(BasePlatformAdapter):
            name = "stub"

            def __init__(self):
                self.sent: list = []

            async def connect(self, *, is_reconnect: bool = False): pass
            async def disconnect(self): pass
            async def send(self, chat_id, content, **kw):
                self.sent.append(content)
                return SendResult(success=True, message_id="1")
            async def edit(self, *a, **k): return SendResult(success=False)
            async def get_history(self, *a, **k): return []
            async def get_chat_info(self, *a, **k): return {}

        adapter = _Stub()
        result = await adapter.send_clarify(
            chat_id="c", question="Pick a fruit",
            choices=["apple", "banana"], clarify_id="x", session_key="s",
        )
        assert result.success is True
        text = adapter.sent[0]
        assert "Pick a fruit" in text
        assert "1." in text and "apple" in text
        assert "2." in text and "banana" in text
