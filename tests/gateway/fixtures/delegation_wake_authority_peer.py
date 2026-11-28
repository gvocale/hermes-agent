"""Delegation-completion wake on the PRODUCTION route: real session authority, real HTTP, loopback model.

Run as ``python delegation_wake_authority_peer.py SCENARIO`` with ``HERMES_HOME`` set to an empty directory
(``tests/gateway/test_delegation_wake_authority.py`` does that). What is real: a ``GatewayRunner`` whose
session authority was built by ``initialize_session_authority`` (what every gateway start does), an
``APIServerAdapter`` on a real port, every turn is a real ``/v1/chat/completions`` request into its own
handler (the wake too: the production self-post), the real durable delegation ledger, the real watcher loop
(``_async_delegation_watcher``), the real agent loop and the real session store. What is not: the model (a
loopback HTTP peer that answers by marker words in the user text) and the helper that "finished" (the
completion event is pushed the way ``tools.async_delegation`` pushes it, after a real ledger dispatch).

Timing hooks (the same code runs, only the waits are shorter): the watcher interval, and
``_refusal_retry_at`` (4 s doubling to 60 s in production) is replaced by a fixed short backoff.
"""
import asyncio
import json
import os
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

API_KEY = "wake-probe-key-0123456789"
CONFIG_ON = "gateway:\n  delegation_completion_wake: true\n"


class Peer(BaseHTTPRequestHandler):
    """Loopback model. Marker words in the last user message steer it: HOLD_<n> blocks until gate <n> is
    set; FAIL400 answers HTTP 400; a wake text ([ASYNC DELEGATION) answers HTTP 400 when ``fail_wake``."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"data": [{"id": "local-wire-stub", "object": "model"}]}).encode())

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "messages" not in request:
            self.server.metadata_requests.append(request)
            self._json(200, {})
            return
        last = next((m.get("content", "") for m in reversed(request["messages"]) if m.get("role") == "user"), "")
        last = last if isinstance(last, str) else json.dumps(last)
        started = time.time()
        self.server.requests.append({"last_user": last, "started": started, "n_messages": len(request["messages"])})
        for word in last.replace("\n", " ").split():
            if word.startswith("HOLD_"):
                self.server.gate(word).wait(120)
        if "FAIL400" in last or ("[ASYNC DELEGATION" in last and self.server.fail_wake):
            self._json(400, {"error": {"message": "loopback provider refuses this request", "type": "invalid_request_error"}})
            return
        reply = "WOKEN_ACK" if "[ASYNC DELEGATION" in last else "ACK"
        base = {"id": "chatcmpl-local", "model": "local-wire-stub", "created": 1}
        if "ASK_PERSON" in last and request["messages"][-1].get("role") != "tool":
            # The model asks the person a question through the real clarify tool.
            call = {"index": 0, "id": "call_clarify", "type": "function",
                    "function": {"name": "clarify", "arguments": json.dumps({"question": "Which one?"})}}
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "tool_calls": [call]}, None), ({}, "tool_calls")]:
                chunk = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        if request.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [({"role": "assistant", "content": reply}, None), ({}, "stop")]:
                chunk = {**base, "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            return
        self._json(200, {**base, "object": "chat.completion",
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})

    def _json(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Peer)
        self.requests, self.metadata_requests, self.fail_wake = [], [], False
        self._gates, self._lock = {}, threading.Lock()
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def gate(self, name):
        with self._lock:
            return self._gates.setdefault(name, threading.Event())


class Rig:
    def __init__(self, peer, *, wake: bool, backoff=None, interval=0.1, wake_timeout=None):
        self.peer, self.wake, self.backoff, self.interval, self.wake_timeout = peer, wake, backoff, interval, wake_timeout
        self.t0 = time.monotonic()
        self.posts = []

    async def start(self):
        import aiohttp
        from gateway.config import Platform, PlatformConfig
        from gateway.platforms.api_server import APIServerAdapter
        from gateway.run import GatewayRunner
        from gateway.session_authority import initialize_session_authority
        import gateway.run_notifications as notifications
        import gateway.wake as wake_mod

        base = f"http://127.0.0.1:{self.peer.server_port}/v1"
        home = Path(os.environ["HERMES_HOME"])
        (home / "config.yaml").write_text(
            f"model:\n  default: local-wire-stub\n  provider: custom\n  base_url: {base}\n"
            f"auxiliary:\n  title_generation:\n    enabled: false\nterminal:\n  cwd: {home}\n"
            f"agent:\n  max_turns: 4\n" + (CONFIG_ON if self.wake else ""))
        os.environ.update(OPENAI_API_KEY="loopback-only", OPENAI_BASE_URL=base)
        if self.backoff is not None:
            delay = self.backoff
            notifications._refusal_retry_at = lambda step: time.monotonic() + delay
        if self.wake_timeout is not None:
            wake_mod.WAKE_TURN_TIMEOUT_SECONDS = self.wake_timeout
        real_post = wake_mod._self_post_chat_completion

        async def counted_post(adapter, **kwargs):
            self.posts.append({"session_id": kwargs.get("session_id"), "at_most_once": kwargs.get("at_most_once", False),
                               "t": round(time.monotonic() - self.t0, 2)})
            return await real_post(adapter, **kwargs)
        wake_mod._self_post_chat_completion = counted_post

        import tools.async_delegation as ad
        self.claims = 0
        for name in ("claim_completion_delivery", "claim_event_delivery"):
            def counted(*args, _real=getattr(ad, name), **kwargs):
                self.claims += 1
                return _real(*args, **kwargs)
            setattr(ad, name, counted)

        self.runner = GatewayRunner()
        await initialize_session_authority(self.runner, profile_id="default", instance_id="wake-probe")
        self.adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": API_KEY, "port": 0}))
        self.adapter.gateway_runner = self.runner
        self.adapter.set_message_handler(self.runner._handle_message)
        self.runner.adapters[Platform.API_SERVER] = self.adapter
        assert await self.adapter.connect()
        self.port = self.adapter._site._server.sockets[0].getsockname()[1]
        self.adapter._port = self.port  # the self-post reads the configured port; 0 would fall back to 8642 (a live gateway)
        self.url = f"http://127.0.0.1:{self.port}/v1/chat/completions"
        self.runner._running = True
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        self.watcher = asyncio.create_task(self.runner._async_delegation_watcher(interval=self.interval))
        return self

    async def stop(self):
        self.runner._running = False
        self.watcher.cancel()
        await asyncio.gather(self.watcher, return_exceptions=True)
        await self.http.close()
        await self.adapter.disconnect()

    # -- clients -----------------------------------------------------------------------------------------
    def turn(self, session_id, text):
        """A real client turn: returns a task resolving to (status, body text)."""
        async def go():
            async with self.http.post(self.url, json={"messages": [{"role": "user", "content": text}]},
                                      headers={"Authorization": f"Bearer {API_KEY}",
                                               "X-Hermes-Session-Id": session_id}) as resp:
                return resp.status, await resp.text()
        return asyncio.create_task(go())

    async def make_session(self, session_id):
        status, body = await self.turn(session_id, "hello")
        assert status == 200, body

    async def wait_for(self, predicate, timeout=30, what="condition"):
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out waiting for {what}")
            await asyncio.sleep(0.05)

    async def wait_model_calls(self, count, timeout=30):
        await self.wait_for(lambda: len(self.peer.requests) >= count, timeout, f"{count} model calls")

    # -- helper completion -----------------------------------------------------------------------------
    def complete(self, session_id, delegation_id, summary="the helper finished"):
        from tools.async_delegation import _persist_dispatch, _push_completion_event
        record = {"delegation_id": delegation_id, "session_key": "", "origin_session_id": session_id,
                  "parent_session_id": session_id, "goal": "probe goal", "dispatched_at": time.time() - 5,
                  "completed_at": time.time()}
        _persist_dispatch(record)
        _push_completion_event(record, {"summary": summary, "duration_seconds": 5.0}, "completed")

    def ledger(self, delegation_id):
        from tools.async_delegation import get_durable_delegation
        return get_durable_delegation(delegation_id)

    def rows(self, session_id):
        from persistence.conversation.messages import TranscriptRead
        store = self.runner.session_authority.persistence
        return [row.as_wire_row() for row in store.messages.read_transcript(session_id, TranscriptRead())]


def shape(rows):
    """Rows as the page would read them: role, display kind, finish reason and the first 90 characters."""
    out = []
    for row in rows:
        content = row.get("content")
        out.append({"role": row.get("role"), "display_kind": row.get("display_kind"),
                    "finish_reason": row.get("finish_reason"), "text": (content if isinstance(content, str) else json.dumps(content))[:90]})
    return out


def delivery_rows(rows):
    return [r for r in rows if r.get("display_kind") == "async_delegation_complete"]


def wake_rows(rows):
    return [r for r in rows if r.get("role") == "user" and "[ASYNC DELEGATION" in str(r.get("content"))
            and r.get("display_kind") != "async_delegation_complete"]


async def settle(rig, session_id, seconds=1.5):
    """Let the watcher run a few passes."""
    await asyncio.sleep(seconds)


# =============================== scenarios ================================================================
async def s_idle(rig, out):
    """Idle parent: one wake turn with the notice wording, sent once, no delivery row."""
    await rig.make_session("sess-idle")
    rig.complete("sess-idle", "deleg_idle")
    await rig.wait_for(lambda: (rig.ledger("deleg_idle") or {}).get("delivery_state") == "delivered", what="ledger delivered")
    await asyncio.sleep(1)
    rows = rig.rows("sess-idle")
    out.update(posts=rig.posts, rows=shape(rows), wake_text=(wake_rows(rows) or [{}])[0].get("content", ""),
               delivery_rows=len(delivery_rows(rows)), ledger=rig.ledger("deleg_idle"),
               model_calls=[r["last_user"][:60] for r in rig.peer.requests])


async def s_busy(rig, out, *, hold_seconds):
    """Parent busy (a real turn held in its model call) while the helper finishes: not woken on top; the
    result arrives after the turn ends, as a wake (setting on) or a delivery row (setting off)."""
    await rig.make_session("sess-busy")
    n = len(rig.peer.requests)
    busy = rig.turn("sess-busy", "work HOLD_busy")
    await rig.wait_model_calls(n + 1)
    started = time.monotonic()
    rig.complete("sess-busy", "deleg_busy")
    await asyncio.sleep(hold_seconds)
    mid = {"posts_during_turn": len(rig.posts), "model_calls_during_turn": len(rig.peer.requests) - n - 1,
           "ledger_during_turn": rig.ledger("deleg_busy"), "rows_during_turn": shape(rig.rows("sess-busy"))[-2:]}
    rig.peer.gate("HOLD_busy").set()
    status, _ = await busy
    ended = time.monotonic()
    await rig.wait_for(lambda: (rig.ledger("deleg_busy") or {}).get("delivery_state") in ("delivered", "dropped"), 60, "ledger final")
    await asyncio.sleep(1)
    rows = rig.rows("sess-busy")
    out.update(claims=rig.claims, mid=mid, turn_status=status, busy_seconds=round(ended - started, 1), posts=rig.posts, rows=shape(rows),
               wakes=len(wake_rows(rows)), delivery_rows=len(delivery_rows(rows)), ledger=rig.ledger("deleg_busy"),
               seconds_after_turn=round(time.monotonic() - ended, 1))


async def s_other_session(rig, out):
    """A turn held on another session does not block the wake of an idle one (fix 1)."""
    await rig.make_session("sess-a")
    await rig.make_session("sess-b")
    n = len(rig.peer.requests)
    busy = rig.turn("sess-b", "work HOLD_other")
    await rig.wait_model_calls(n + 1)
    rig.complete("sess-a", "deleg_other")
    await rig.wait_for(lambda: wake_rows(rig.rows("sess-a")), 20, "wake on the idle session")
    rows = rig.rows("sess-a")
    out.update(posts=rig.posts, wakes=len(wake_rows(rows)), delivery_rows=len(delivery_rows(rows)),
               b_turn_still_running=not busy.done(), pending=rig.adapter._pending_agent_requests,
               authority_turns=getattr(rig.adapter, "_authority_turns_in_flight", None))
    rig.peer.gate("HOLD_other").set()
    await busy


async def s_queued(rig, out):
    """The authority answers the wake with HTTP 503 ``admission_queued`` ("saved, will run once"): that is a
    delivered wake, so no delivery row is written next to it (fix 2). FAKED: the idle proof (a turn the
    adapter cannot see is running, which is what a disconnected SSE client leaves behind) and the trigger
    (the owner retiring, simulated by its last step: failing the queued waiters with ``runtime_draining``,
    as ``_retire_profile_authority`` does). The 503 itself is the real handler's answer over real HTTP."""
    import gateway.platforms.api_server_runs as runs
    from persistence.core.errors import AuthorityRefusal
    await rig.make_session("sess-q")
    n = len(rig.peer.requests)
    held = rig.turn("sess-q", "work HOLD_q")
    await rig.wait_model_calls(n + 1)
    authority = rig.runner.session_authority
    before = set(authority.waiters)
    runs.session_is_idle = lambda *a, **k: True
    task = asyncio.create_task(rig.runner._wake_idle_session_for_delegation(
        rig.adapter, "[ASYNC DELEGATION COMPLETE \u2014 dq]\nbody", "sess-q", {"type": "async_delegation", "delegation_id": "dq"}))
    await rig.wait_for(lambda: set(authority.waiters) - before, 20, "the wake admitted behind the held turn")
    for key in set(authority.waiters) - before:
        authority.waiters[key].set_exception(AuthorityRefusal("runtime_draining"))
    out["wake_returned"] = await asyncio.wait_for(task, 30)
    rig.peer.gate("HOLD_q").set()
    await held
    await asyncio.sleep(1)
    rows = rig.rows("sess-q")
    out.update(posts=rig.posts, delivery_rows=len(delivery_rows(rows)), rows=shape(rows))


async def s_prompt(rig, out):
    """A person is being asked something on the session (a pending clarify question, a pending approval):
    no wake turn; the result is kept as a delivery row. FAKED: the prompt is registered straight in the
    registries a real prompt uses (``tools.clarify_gateway``, ``tools.approval``): on an api_server session
    the real clarify tool answers at once, and a real approval needs a dangerous command to be run."""
    from types import SimpleNamespace
    from tools import approval, clarify_gateway
    for sid, kind in (("sess-pc", "clarify"), ("sess-pa", "approval")):
        await rig.make_session(sid)
        n_calls = len(rig.peer.requests)
        if kind == "clarify":
            clarify_gateway.register("c-probe", sid, "Which one?", None)
        else:
            approval._gateway_queues[sid] = [SimpleNamespace(data={"request_id": "a-probe"})]
        rig.complete(sid, f"deleg_{kind}")
        await rig.wait_for(lambda: (rig.ledger(f"deleg_{kind}") or {}).get("delivery_state") in ("delivered", "dropped"),
                           30, "ledger final")
        rows = rig.rows(sid)
        out[kind] = {"posts": len(rig.posts), "model_calls_after_hello": len(rig.peer.requests) - n_calls,
                     "wakes": len(wake_rows(rows)), "delivery_rows": len(delivery_rows(rows)),
                     "ledger": rig.ledger(f"deleg_{kind}")}
        clarify_gateway.clear_session(sid)
        approval._gateway_queues.pop(sid, None)


async def s_wake_fails(rig, out):
    """The wake turn's model call fails: the result stays available as a delivery row."""
    rig.peer.fail_wake = True
    await rig.make_session("sess-f")
    rig.complete("sess-f", "deleg_fail")
    await rig.wait_for(lambda: (rig.ledger("deleg_fail") or {}).get("delivery_state") in ("delivered", "dropped"), 60, "ledger final")
    await asyncio.sleep(1)
    rows = rig.rows("sess-f")
    out.update(posts=rig.posts, rows=shape(rows), wakes=len(wake_rows(rows)), delivery_rows=len(delivery_rows(rows)),
               ledger=rig.ledger("deleg_fail"), model_calls=[r["last_user"][:50] for r in rig.peer.requests])


async def s_timeout(rig, out):
    """The wake turn outlasts the self-post timeout: the turn is not sent a second time, and no row is
    written next to it. The wake's model call is held (HOLD_wake is in the helper's summary)."""
    await rig.make_session("sess-t")
    rig.complete("sess-t", "deleg_t", summary="HOLD_wake the helper finished")
    await rig.wait_for(lambda: rig.posts, 20, "the wake self-post")
    await asyncio.sleep(float(os.environ.get("WAKE_TIMEOUT", "1")) + 2.5)  # past the timeout, plus the watcher passes
    out["calls_after_timeout"] = [r["last_user"][:40] for r in rig.peer.requests if "[ASYNC DELEGATION" in r["last_user"]]
    out["posts_after_timeout"] = len(rig.posts)
    rig.peer.gate("HOLD_wake").set()
    await rig.wait_for(lambda: wake_rows(rig.rows("sess-t")), 20, "the one wake turn to finish")
    await asyncio.sleep(1.5)
    rows = rig.rows("sess-t")
    out.update(posts=rig.posts, wakes=len(wake_rows(rows)), delivery_rows=len(delivery_rows(rows)),
               ledger=rig.ledger("deleg_t"), wake_model_calls=len([r for r in rig.peer.requests if "[ASYNC DELEGATION" in r["last_user"]]))


async def s_failbody(rig, out):
    """What the HTTP answer of a turn whose model call fails looks like."""
    await rig.make_session("sess-fb")
    status, body = await rig.turn("sess-fb", "please FAIL400")
    out.update(status=status, body=json.loads(body), rows=shape(rig.rows("sess-fb")))


SCENARIOS = {"timeout": (s_timeout, {}), "failbody": (s_failbody, {}), "idle": (s_idle, {}), "other_session": (s_other_session, {}), "queued": (s_queued, {}),
             "prompt": (s_prompt, {}), "wake_fails": (s_wake_fails, {})}


async def main(name):
    peer = Server()
    wake = os.environ.get("WAKE", "1") == "1"
    backoff = float(os.environ["BACKOFF"]) if os.environ.get("BACKOFF") else None
    kwargs = {}
    fn = SCENARIOS[name][0] if name in SCENARIOS else None
    if name.startswith("busy"):
        hold = float(os.environ.get("HOLD", "3"))
        fn = lambda rig, out: s_busy(rig, out, hold_seconds=hold)
    rig = await Rig(peer, wake=wake, backoff=backoff, wake_timeout=float(os.environ["WAKE_TIMEOUT"]) if os.environ.get("WAKE_TIMEOUT") else None).start()
    out = {"scenario": name, "wake_setting": wake}
    try:
        await fn(rig, out)
    finally:
        for gate in list(peer._gates.values()):
            gate.set()
        await rig.stop()
    Path(os.environ["HERMES_HOME"], "receipt.json").write_text(json.dumps(out, indent=1, default=str))
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    code = 0
    if os.environ.get("PROBE_LOG"):
        import logging
        logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(name)s %(message)s")
    try:
        asyncio.run(asyncio.wait_for(main(sys.argv[1]), 150))
    except BaseException:
        traceback.print_exc()
        code = 1
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
