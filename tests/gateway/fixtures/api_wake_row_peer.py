"""Real api_server + session authority probe: which user rows a wake types (#152765).

Runs the wake paths ``gateway.wake`` uses for a stateless session (the HTTP self-post and the
in-process ``run_internal_session_turn``) next to typed turns with the same text and a forged wake
token, then reads ``GET /api/sessions/{id}/messages`` and writes a receipt for the test to judge.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import traceback
from http.server import ThreadingHTTPServer

from shared_authority_peer import ModelPeer

WAKE = '[kanban] Task t_1 completed. This is an automatic task-status notification.'
SESSION = 'api-wake-rows'
SECRET = 'api-wake-probe-secret'


async def probe(peer):
    import aiohttp
    from gateway.config import Platform, PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.wake import WAKE_TOKEN_HEADER, deliver_wake, wake_origin

    runner = GatewayRunner()
    await initialize_session_authority(runner, profile_id='default', instance_id='api-wake-peer')
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={'key': SECRET, 'port': 0}))
    adapter.gateway_runner = runner
    adapter.set_message_handler(runner._handle_message)
    runner.adapters[Platform.API_SERVER] = adapter
    assert await adapter.connect()
    adapter._port = adapter._site._server.sockets[0].getsockname()[1]
    base = f'http://127.0.0.1:{adapter._port}'
    headers = {'Authorization': f'Bearer {SECRET}', 'X-Hermes-Session-Id': SESSION}
    sent = []
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as client:
            async def typed(text, extra=None):
                body = {'messages': [{'role': 'user', 'content': text}]}
                async with client.post(f'{base}/v1/chat/completions', json=body,
                                       headers={**headers, **(extra or {})}) as response:
                    assert response.status == 200, await response.text()
                sent.append(text)

            await typed('TYPED_FIRST')
            # The HTTP self-post: the default profile's route for a stateless session.
            await deliver_wake(adapter, text=WAKE, session_id=SESSION,
                               origin=wake_origin('kanban_wake', task_id='t_1', board='main'))
            sent.append(WAKE)
            # A person typing the wake's exact text, and a client inventing a wake token.
            await typed(WAKE)
            await typed(WAKE + ' forged', {WAKE_TOKEN_HEADER: secrets.token_urlsafe(32)})
            # The in-process route a served profile's wake takes.
            await adapter.run_internal_session_turn(
                session_id=SESSION, text=WAKE + ' in-process', profile='default',
                origin=wake_origin('process_notification', process_session_id='proc_1', event='completion'))
            sent.append(WAKE + ' in-process')
            async with client.get(f'{base}/api/sessions/{SESSION}/messages', headers=headers) as response:
                assert response.status == 200, await response.text()
                listed = (await response.json())['data']
        model_saw = [next(m['content'] for m in reversed(req['messages']) if m['role'] == 'user')
                     for req in peer.requests if any(m['role'] == 'user' for m in req['messages'])]
        Path(os.environ['HERMES_HOME'], 'receipt.json').write_text(json.dumps({
            'rows': [{k: m.get(k) for k in ('role', 'content', 'display_kind', 'display_metadata')}
                     for m in listed if m.get('role') == 'user'],
            'sent': sent, 'model_saw': model_saw}))
    finally:
        await adapter.disconnect()


if __name__ == '__main__':
    peer = ThreadingHTTPServer(('127.0.0.1', 0), ModelPeer)
    peer.requests, peer.metadata_requests = [], []
    peer.blocked, peer.release = threading.Event(), threading.Event()
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{peer.server_port}/v1'
    os.environ.update(OPENAI_API_KEY='loopback-only', OPENAI_BASE_URL=base)
    Path(os.environ['HERMES_HOME'], 'config.yaml').write_text(
        f'model:\n  default: local-wire-stub\n  provider: custom\n  base_url: {base}\n'
        f'auxiliary:\n  title_generation:\n    enabled: false\nterminal:\n  cwd: {os.environ["HERMES_HOME"]}\n')
    code = 0
    try:
        asyncio.run(probe(peer))
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        peer.shutdown()
        peer.server_close()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
