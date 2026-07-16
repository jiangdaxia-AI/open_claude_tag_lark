"""Tests for Feishu event idempotency in MessageStore."""

import pytest

from ocl.memory.store import MessageStore


@pytest.fixture
async def store(tmp_path):
    s = MessageStore(db_path=tmp_path / "test.db", channel_id="oc_test")
    await s.open()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_is_event_processed_returns_false_for_new_event(store):
    assert await store.is_event_processed("evt_001") is False


@pytest.mark.asyncio
async def test_mark_event_processed_then_is_returns_true(store):
    await store.mark_event_processed("evt_001", "im.message.receive_v1")
    assert await store.is_event_processed("evt_001") is True


@pytest.mark.asyncio
async def test_different_events_tracked_independently(store):
    await store.mark_event_processed("evt_001", "type_a")
    assert await store.is_event_processed("evt_001") is True
    assert await store.is_event_processed("evt_002") is False


@pytest.mark.asyncio
async def test_cleanup_old_events_removes_expired_entries(store):
    # Insert an old event (simulate by backdating received_at)
    await store.mark_event_processed("evt_old", "type_a")
    await store._db.execute(
        "UPDATE processed_events SET received_at = ? WHERE event_id = ?",
        (1.0, "evt_old"),  # unix epoch 1970
    )
    await store._db.commit()
    # And a recent one
    await store.mark_event_processed("evt_new", "type_b")

    deleted = await store.cleanup_old_events(days=7)
    assert deleted == 1
    assert await store.is_event_processed("evt_old") is False
    assert await store.is_event_processed("evt_new") is True
