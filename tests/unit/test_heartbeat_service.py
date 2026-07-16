"""Tests for HeartbeatService.run_once behavior."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ocl.ambient.heartbeat import HeartbeatService


@pytest.fixture
def service():
    gateway = MagicMock()
    gateway.tenant_id = "T1"
    gateway.send_message = AsyncMock()
    scheduler = MagicMock()
    lock_holder = {}

    def get_lock(tenant, channel):
        return lock_holder.setdefault((tenant, channel), asyncio.Lock())

    svc = HeartbeatService(gateway=gateway, scheduler=scheduler, get_session_lock=get_lock)
    svc.gateway = gateway  # expose for test assertions
    return svc


async def test_run_once_no_messages_does_not_send(service, monkeypatch):
    async def fake_get_store(t, c):
        store = MagicMock()
        store.get_recent_messages = AsyncMock(return_value=[])
        return store
    monkeypatch.setattr("ocl.ambient.heartbeat.get_store", fake_get_store)
    await service.run_once("C1", guidance="focus", max_recent=30)
    service.gateway.send_message.assert_not_awaited()


async def test_run_once_silent_does_not_send(service, monkeypatch):
    async def fake_get_store(t, c):
        store = MagicMock()
        store.get_recent_messages = AsyncMock(return_value=[
            MagicMock(display_name="alice", content="hi"),
        ])
        return store
    monkeypatch.setattr("ocl.ambient.heartbeat.get_store", fake_get_store)

    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="SILENT"))]
    monkeypatch.setattr("ocl.ambient.heartbeat.acompletion", AsyncMock(return_value=resp))

    await service.run_once("C1", guidance="x", max_recent=30)
    service.gateway.send_message.assert_not_awaited()


async def test_run_once_posts_when_not_silent(service, monkeypatch):
    async def fake_get_store(t, c):
        store = MagicMock()
        store.get_recent_messages = AsyncMock(return_value=[
            MagicMock(display_name="alice", content="stale question"),
        ])
        return store
    monkeypatch.setattr("ocl.ambient.heartbeat.get_store", fake_get_store)

    resp = MagicMock()
    resp.choices = [MagicMock(message=MagicMock(content="Following up on the stale question"))]
    monkeypatch.setattr("ocl.ambient.heartbeat.acompletion", AsyncMock(return_value=resp))

    await service.run_once("C1", guidance="focus", max_recent=30)
    service.gateway.send_message.assert_awaited_once()
    args = service.gateway.send_message.await_args
    assert args.kwargs["chat_id"] == "C1"
    assert "stale question" in args.kwargs["text"]


async def test_run_once_acquires_session_lock(service, monkeypatch):
    async def fake_get_store(t, c):
        store = MagicMock()
        store.get_recent_messages = AsyncMock(return_value=[])
        return store
    monkeypatch.setattr("ocl.ambient.heartbeat.get_store", fake_get_store)

    acquired = {"yes": False}

    class TrackingLock(asyncio.Lock):
        async def acquire(self):
            acquired["yes"] = True
            await super().acquire()

    import ocl.gateway.router  # noqa
    # swap the get_session_lock closure to return a tracking lock
    service._get_session_lock = lambda t, c: TrackingLock()
    await service.run_once("C1", guidance="", max_recent=5)
    assert acquired["yes"] is True


async def test_run_once_passes_channel_id_to_acompletion(service, monkeypatch):
    async def fake_get_store(t, c):
        store = MagicMock()
        store.get_recent_messages = AsyncMock(return_value=[
            MagicMock(display_name="alice", content="hi"),
        ])
        return store
    monkeypatch.setattr("ocl.ambient.heartbeat.get_store", fake_get_store)
    mock_acompletion = AsyncMock(
        return_value=MagicMock(choices=[MagicMock(message=MagicMock(content="SILENT"))])
    )
    monkeypatch.setattr("ocl.ambient.heartbeat.acompletion", mock_acompletion)
    await service.run_once("C1", guidance="g", max_recent=10)
    assert mock_acompletion.await_args.kwargs.get("channel_id") == "C1"
