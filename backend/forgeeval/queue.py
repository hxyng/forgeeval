"""Job queue for evaluation work. One message = one ``run_items.id``.

Delivery is at-least-once in every backend, so correctness lives in Postgres, not the queue:
workers claim an item with a conditional UPDATE and a duplicate delivery simply loses that race.
The queue is therefore only a wake-up signal, which is what makes swapping Redis Streams (local)
for SQS (cloud) a configuration change instead of a redesign.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import redis
from opentelemetry.trace import SpanKind

from forgeeval.config import get_settings
from forgeeval.telemetry import inject_headers, tracer

if TYPE_CHECKING:
    from mypy_boto3_sqs import SQSClient


@dataclass(frozen=True)
class Message:
    run_item_id: uuid.UUID
    receipt: str
    deliveries: int
    headers: dict[str, str] = field(default_factory=dict)  # W3C trace context


class JobQueue(Protocol):
    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None: ...

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]: ...

    def ack(self, message: Message) -> None: ...

    def depth(self) -> int: ...


class RedisStreamQueue:
    """Redis Streams with a consumer group.

    ``receive`` first reclaims messages another consumer read but never acked within
    ``visibility_timeout`` (a crashed worker), mirroring SQS visibility semantics, then reads new
    ones. ``ack`` removes the message from the pending list and the stream.
    """

    GROUP = "workers"

    def __init__(
        self,
        client: redis.Redis,
        stream: str = "forgeeval:eval-jobs",
        visibility_timeout: float = 120.0,
    ) -> None:
        self._r = client
        self.stream = stream
        self._visibility_ms = int(visibility_timeout * 1000)
        try:
            self._r.xgroup_create(stream, self.GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None:
        with tracer.start_as_current_span(
            f"{self.stream} publish",
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "redis_streams",
                "messaging.operation.type": "publish",
                "messaging.destination.name": self.stream,
                "messaging.batch.message_count": len(run_item_ids),
            },
        ):
            headers: dict[Any, Any] = inject_headers()  # consumers continue this trace
            pipe = self._r.pipeline(transaction=False)
            for item_id in run_item_ids:
                pipe.xadd(self.stream, {"run_item_id": str(item_id), **headers})
            pipe.execute()

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        out: list[Message] = []
        _next, claimed, _deleted = self._r.xautoclaim(
            self.stream,
            self.GROUP,
            consumer,
            self._visibility_ms,
            start_id="0-0",
            count=max_messages,
        )
        if claimed:
            counts = self._delivery_counts([mid for mid, _ in claimed])
            out += [self._message(mid, fields, counts.get(mid, 1)) for mid, fields in claimed]
        remaining = max_messages - len(out)
        if remaining > 0:
            block = None if out else max(1, int(wait_seconds * 1000))
            resp = cast(
                list[tuple[bytes, list[tuple[bytes, dict[bytes, bytes]]]]] | None,
                self._r.xreadgroup(
                    self.GROUP, consumer, {self.stream: ">"}, count=remaining, block=block
                ),
            )
            for _stream, entries in resp or []:
                out += [self._message(mid, fields, 1) for mid, fields in entries]
        return out

    def ack(self, message: Message) -> None:
        pipe = self._r.pipeline(transaction=False)
        pipe.xack(self.stream, self.GROUP, message.receipt)
        pipe.xdel(self.stream, message.receipt)
        pipe.execute()

    def depth(self) -> int:
        """Messages not yet acked: never read plus read-but-pending."""
        info = self._r.xinfo_groups(self.stream)
        group = next(g for g in info if g["name"] in (self.GROUP, self.GROUP.encode()))
        lag = group.get("lag") or 0
        return int(lag) + int(group["pending"])

    def _delivery_counts(self, ids: list[bytes]) -> dict[bytes, int]:
        if not ids:
            return {}
        rows: list[dict[str, Any]] = self._r.xpending_range(
            self.stream, self.GROUP, min=ids[0], max=ids[-1], count=len(ids) * 2
        )
        return {r["message_id"]: int(r["times_delivered"]) for r in rows}

    @staticmethod
    def _message(mid: bytes, fields: dict[bytes, bytes], deliveries: int) -> Message:
        decoded = {k.decode(): v.decode() for k, v in fields.items()}
        run_item_id = uuid.UUID(decoded.pop("run_item_id"))
        return Message(run_item_id, mid.decode(), deliveries, headers=decoded)


class SqsQueue:
    """Amazon SQS standard queue (ElasticMQ locally).

    Semantics match RedisStreamQueue: a received message is hidden for the queue's visibility
    timeout and reappears if not deleted (``ack``); SQS's redrive policy moves a message to the
    dead-letter queue after ``maxReceiveCount`` receives (set in Terraform). Trace context rides
    in message attributes. ``receive`` long-polls, so an idle worker costs one request per
    ``wait_seconds`` rather than spinning.
    """

    MAX_BATCH = 10  # SQS limit for SendMessageBatch / ReceiveMessage

    def __init__(self, client: "SQSClient", queue_url: str) -> None:
        self._sqs = client
        self.queue_url = queue_url

    def enqueue(self, run_item_ids: Sequence[uuid.UUID]) -> None:
        with tracer.start_as_current_span(
            "eval-jobs publish",
            kind=SpanKind.PRODUCER,
            attributes={
                "messaging.system": "aws_sqs",
                "messaging.operation.type": "publish",
                "messaging.destination.name": self.queue_url.rsplit("/", 1)[-1],
                "messaging.batch.message_count": len(run_item_ids),
            },
        ):
            attributes: dict[str, Any] = {
                k: {"DataType": "String", "StringValue": v} for k, v in inject_headers().items()
            }
            ids = list(run_item_ids)
            for start in range(0, len(ids), self.MAX_BATCH):
                chunk = ids[start : start + self.MAX_BATCH]
                resp = self._sqs.send_message_batch(
                    QueueUrl=self.queue_url,
                    Entries=[
                        {
                            "Id": str(i),
                            "MessageBody": str(item_id),
                            "MessageAttributes": attributes,
                        }
                        for i, item_id in enumerate(chunk)
                    ],
                )
                if resp.get("Failed"):
                    raise RuntimeError(
                        f"SQS rejected {len(resp['Failed'])} messages: {resp['Failed']}"
                    )

    def receive(self, consumer: str, max_messages: int, wait_seconds: float) -> list[Message]:
        resp = self._sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max(1, min(max_messages, self.MAX_BATCH)),
            WaitTimeSeconds=max(0, min(int(wait_seconds), 20)),
            MessageAttributeNames=["All"],
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        out: list[Message] = []
        for m in resp.get("Messages", []):
            headers = {
                k: v["StringValue"]
                for k, v in m.get("MessageAttributes", {}).items()
                if "StringValue" in v
            }
            out.append(
                Message(
                    run_item_id=uuid.UUID(m["Body"]),
                    receipt=m["ReceiptHandle"],
                    deliveries=int(m.get("Attributes", {}).get("ApproximateReceiveCount", "1")),
                    headers=headers,
                )
            )
        return out

    def ack(self, message: Message) -> None:
        self._sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=message.receipt)

    def depth(self) -> int:
        attrs = self._sqs.get_queue_attributes(
            QueueUrl=self.queue_url,
            AttributeNames=[
                "ApproximateNumberOfMessages",
                "ApproximateNumberOfMessagesNotVisible",
            ],
        )["Attributes"]
        return int(attrs.get("ApproximateNumberOfMessages", 0)) + int(
            attrs.get("ApproximateNumberOfMessagesNotVisible", 0)
        )


_queue: JobQueue | None = None


def build_queue() -> JobQueue:
    settings = get_settings()
    if settings.queue_backend == "sqs":
        if not settings.sqs_queue_url:
            raise RuntimeError("QUEUE_BACKEND=sqs requires SQS_QUEUE_URL")
        from forgeeval.aws import client

        return SqsQueue(client("sqs"), settings.sqs_queue_url)
    return RedisStreamQueue(redis.Redis.from_url(settings.redis_url), stream=settings.queue_stream)


def get_queue() -> JobQueue:
    """Process-wide queue from settings (QUEUE_BACKEND=redis|sqs). FastAPI dependency."""
    global _queue
    if _queue is None:
        _queue = build_queue()
    return _queue
