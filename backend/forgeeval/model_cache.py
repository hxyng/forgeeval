"""Memoise model calls so re-evaluating unchanged work costs nothing.

``CachingModelClient`` wraps any ``ModelClient``. A call is identified by what determines its
answer: ``{kind, model, params, prompt}`` (plus ``expected`` for ``fake/oracle``, whose output
is the expected value). The same wrapper serves generations and LLM-judge calls. A judge prompt
already contains the rubric, input, expected and candidate output, so prompt + judge model +
params fully identify a judge call.

Kind is chosen per call site, not per client: ``CachingModelClient.scope()`` returns a
``CacheScope`` with two thin views (``generator`` and ``judge``) that share one tally. The
processor uses the generator view for the model under test and passes the judge view to
evaluators, so evaluators keep calling the plain ``ModelClient`` protocol and the scope can
report which calls were hits and how much spend they avoided.

Only successful calls are stored. Calls with ``temperature > 0`` are a draw, not a fixed answer,
so they bypass the cache unless ``cache_sampled_outputs`` is on. Entries older than
``max_age_days`` are ignored because providers change models behind the same name. Cache
failures never fail an item: they degrade to a miss.
"""

import hashlib
import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from forgeeval.db.models import ModelCacheEntry
from forgeeval.models_client import Generation, ModelClient
from forgeeval.telemetry import model_cache_lookups, record

log = logging.getLogger("forgeeval.model_cache")

Kind = Literal["generation", "judge"]
ORACLE = "fake/oracle"


def cache_key(
    kind: str, model: str, prompt: str, params: dict[str, Any], expected: Any = None
) -> str:
    material: dict[str, Any] = {"kind": kind, "model": model, "params": params, "prompt": prompt}
    if model == ORACLE:
        material["expected"] = expected  # fake/oracle's output depends on it
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class CacheStore(Protocol):
    def get(self, key: str, max_age_days: int) -> Generation | None: ...

    def put(self, key: str, kind: str, model: str, gen: Generation, max_age_days: int) -> None: ...

    def touch(self, key: str) -> None: ...


class PgCacheStore:
    """Postgres-backed store. Each operation uses its own short transaction."""

    def __init__(self, session_factory: Callable[[], AbstractContextManager[Session]]) -> None:
        self._session = session_factory

    def get(self, key: str, max_age_days: int) -> Generation | None:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        with self._session() as db:
            row = db.execute(
                select(ModelCacheEntry).where(
                    ModelCacheEntry.key == key, ModelCacheEntry.created_at > cutoff
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return Generation(
                output=row.output,
                latency_ms=row.latency_ms,
                input_tokens=row.input_tokens,
                output_tokens=row.output_tokens,
                cost_usd=row.cost_usd,
            )

    def put(self, key: str, kind: str, model: str, gen: Generation, max_age_days: int) -> None:
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        with self._session() as db:
            # An expired row would otherwise block the insert below forever.
            db.execute(
                delete(ModelCacheEntry).where(
                    ModelCacheEntry.key == key, ModelCacheEntry.created_at <= cutoff
                )
            )
            # Two workers that missed the same key both call the model; the first write wins.
            db.execute(
                insert(ModelCacheEntry)
                .values(
                    key=key,
                    kind=kind,
                    model=model,
                    output=gen.output,
                    latency_ms=gen.latency_ms,
                    input_tokens=gen.input_tokens,
                    output_tokens=gen.output_tokens,
                    cost_usd=gen.cost_usd,
                )
                .on_conflict_do_nothing(index_elements=["key"])
            )
            db.commit()

    def touch(self, key: str) -> None:
        with self._session() as db:
            db.execute(
                update(ModelCacheEntry)
                .where(ModelCacheEntry.key == key)
                .values(hits=ModelCacheEntry.hits + 1, last_hit_at=func.now())
            )
            db.commit()


@dataclass
class CacheScope:
    """Tally of cache activity for one run item (one thread, one ``process`` call)."""

    generation_hit: bool = False
    judge_hits: int = 0
    saved_usd: Decimal = Decimal(0)  # cost of hits: original spend that was not incurred
    generator: ModelClient = field(init=False)
    judge: ModelClient = field(init=False)


class _View:
    def __init__(
        self, owner: "CachingModelClient", scope: CacheScope, kind: Kind, use_cache: bool
    ) -> None:
        self._owner, self._scope, self._kind, self._use_cache = owner, scope, kind, use_cache

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        gen, hit = self._owner.call(
            self._kind, model, prompt, params, expected, use_cache=self._use_cache
        )
        if hit:
            if self._kind == "generation":
                self._scope.generation_hit = True
            else:
                self._scope.judge_hits += 1
            self._scope.saved_usd += gen.cost_usd or Decimal(0)
        return gen


class CachingModelClient:
    def __init__(
        self,
        inner: ModelClient,
        store: CacheStore,
        *,
        enabled: bool,
        max_age_days: int,
        cache_sampled_outputs: bool = False,
    ) -> None:
        self._inner = inner
        self._store = store
        self._enabled = enabled
        self._max_age_days = max_age_days
        self._sampled = cache_sampled_outputs

    def scope(self, *, use_cache: bool = True) -> CacheScope:
        s = CacheScope()
        s.generator = _View(self, s, "generation", use_cache)
        s.judge = _View(self, s, "judge", use_cache)
        return s

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        """Plain ``ModelClient`` use: a cached generation."""
        return self.call("generation", model, prompt, params, expected)[0]

    def call(
        self,
        kind: Kind,
        model: str,
        prompt: str,
        params: dict[str, Any],
        expected: Any = None,
        *,
        use_cache: bool = True,
    ) -> tuple[Generation, bool]:
        if not (self._enabled and use_cache) or (
            float(params.get("temperature", 0) or 0) > 0 and not self._sampled
        ):
            record(model_cache_lookups.add, 1, {"kind": kind, "outcome": "bypass"})
            return self._inner.generate(model, prompt, params, expected), False
        key = cache_key(kind, model, prompt, params, expected)
        cached = self._lookup(key)
        if cached is not None:
            record(model_cache_lookups.add, 1, {"kind": kind, "outcome": "hit"})
            self._best_effort(self._store.touch, key)
            return cached, True
        record(model_cache_lookups.add, 1, {"kind": kind, "outcome": "miss"})
        gen = self._inner.generate(model, prompt, params, expected)  # exceptions pass through
        self._best_effort(self._store.put, key, kind, model, gen, self._max_age_days)
        return gen, False

    def _lookup(self, key: str) -> Generation | None:
        try:
            return self._store.get(key, self._max_age_days)
        except Exception:
            log.warning("model cache read failed; treating as a miss", exc_info=True)
            return None

    @staticmethod
    def _best_effort(fn: Callable[..., None], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:
            log.warning("model cache write failed", exc_info=True)
