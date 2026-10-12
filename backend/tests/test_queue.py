import time
import uuid

from forgeeval.queue import RedisStreamQueue


def test_enqueue_receive_ack(redis_queue: RedisStreamQueue) -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    redis_queue.enqueue(ids)
    assert redis_queue.depth() == 3

    msgs = redis_queue.receive("w1", max_messages=10, wait_seconds=0.1)
    assert [m.run_item_id for m in msgs] == ids
    assert all(m.deliveries == 1 for m in msgs)
    assert redis_queue.depth() == 3  # read but not acked

    for m in msgs:
        redis_queue.ack(m)
    assert redis_queue.depth() == 0
    assert redis_queue.receive("w1", max_messages=10, wait_seconds=0.05) == []


def test_unacked_message_is_redelivered_after_visibility_timeout(
    redis_queue: RedisStreamQueue,
) -> None:
    item = uuid.uuid4()
    redis_queue.enqueue([item])
    first = redis_queue.receive("crashed-worker", max_messages=1, wait_seconds=0.1)
    assert [m.run_item_id for m in first] == [item]

    assert redis_queue.receive("w2", max_messages=1, wait_seconds=0.05) == []
    time.sleep(0.3)  # visibility timeout is 0.2s in the fixture
    again = redis_queue.receive("w2", max_messages=1, wait_seconds=0.05)
    assert [m.run_item_id for m in again] == [item]
    assert again[0].deliveries == 2


def test_max_messages_is_respected(redis_queue: RedisStreamQueue) -> None:
    redis_queue.enqueue([uuid.uuid4() for _ in range(5)])
    assert len(redis_queue.receive("w", max_messages=2, wait_seconds=0.1)) == 2
    assert len(redis_queue.receive("w", max_messages=10, wait_seconds=0.1)) == 3


def test_group_creation_is_idempotent(redis_queue: RedisStreamQueue) -> None:
    import redis

    again = RedisStreamQueue(
        redis.Redis.from_url("redis://localhost:6379/15"), stream=redis_queue.stream
    )
    again.enqueue([uuid.uuid4()])
    assert redis_queue.depth() == 1
