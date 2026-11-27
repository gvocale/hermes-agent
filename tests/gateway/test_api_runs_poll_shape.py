"""GET /v1/runs/{id} keeps the documented poll shape when the canonical admission answers."""
import asyncio
import threading

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from tests.fixtures.storage_holder import raw_holder


@pytest.mark.asyncio
async def test_canonical_run_poll_keeps_object_model_and_created_at(api, owner, monkeypatch):
    from gateway.platforms import api_server_runs

    async def no_execution(*args, **kwargs):
        pass
    monkeypatch.setattr(api_server_runs, '_execute_run', no_execution)
    app = web.Application()
    app.router.add_post('/v1/runs', api._handle_runs)
    app.router.add_get('/v1/runs/{run_id}', api._handle_get_run)
    async with TestClient(TestServer(app)) as client:
        accepted = await client.post('/v1/runs', json={'input': 'hi', 'model': 'hermes-agent'})
        assert accepted.status == 202, await accepted.text()
        run_id = (await accepted.json())['run_id']
        polled = await (await client.get(f'/v1/runs/{run_id}')).json()
    assert polled['admission_id'] and polled['status'] == 'queued'
    assert polled['object'] == 'hermes.run' and polled['model'] == 'hermes-agent', polled
    assert isinstance(polled['created_at'], float)


@pytest.mark.asyncio
async def test_run_poll_leaves_gateway_responsive_while_store_is_locked(api, owner, monkeypatch):
    from gateway.platforms import api_server_runs

    async def no_execution(*args, **kwargs):
        pass

    monkeypatch.setattr(api_server_runs, '_execute_run', no_execution)
    app = web.Application()
    app.router.add_post('/v1/runs', api._handle_runs)
    app.router.add_get('/v1/runs/{run_id}', api._handle_get_run)
    app.router.add_get('/health', api._handle_health)
    async with TestClient(TestServer(app)) as client:
        async with client.post('/v1/runs', json={'input': 'hi', 'model': 'hermes-agent'}) as accepted:
            assert accepted.status == 202, await accepted.text()
            run_id = (await accepted.json())['run_id']

        holder = raw_holder(owner.persistence)
        loop = asyncio.get_running_loop()
        locked, reading = asyncio.Event(), asyncio.Event()
        release = threading.Event()

        def hold_store_lock():
            with holder._lock:
                loop.call_soon_threadsafe(locked.set)
                return release.wait(timeout=5)

        def force_writer_connection():
            loop.call_soon_threadsafe(reading.set)

        monkeypatch.setattr(holder, '_checkout_read_conn', force_writer_connection)
        locker = asyncio.create_task(asyncio.to_thread(hold_store_lock))
        await asyncio.wait_for(locked.wait(), timeout=5)
        poll = asyncio.ensure_future(client.get(f'/v1/runs/{run_id}'))
        try:
            await asyncio.wait_for(reading.wait(), timeout=5)
            async with client.get('/health', timeout=3) as health:
                assert health.status == 200
            assert not poll.done(), 'The run poll should still be waiting for the store lock'
        finally:
            release.set()
            lock_released_by_test = await locker
            async with await poll as status:
                assert status.status == 200
                assert (await status.json())['status'] == 'queued'
        assert lock_released_by_test, 'The gateway loop must respond before the lock times out'
