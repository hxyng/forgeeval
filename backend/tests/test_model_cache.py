import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from scenarios import N, NullQueue, setup_tickets
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from forgeeval.db.models import EvalRun, ModelCacheEntry, RunItem
from forgeeval.evaluator_configs import parse_config
from forgeeval.evaluators import EvalCase
from forgeeval.evaluators.llm_rubric import build_prompt
from forgeeval.model_cache import CachingModelClient, PgCacheStore, cache_key
from forgeeval.models_client import FakeModels, Generation, TransientError
from forgeeval.worker.processor import process

# ---------- key and policy (no database) ----------


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, tuple[Generation, datetime]] = {}

    def get(self, key: str, max_age_days: int) -> Generation | None:
        row = self.rows.get(key)
        if row is None or row[1] < datetime.now(UTC) - timedelta(days=max_age_days):
            return None
        return row[0]

    def put(self, key: str, kind: str, model: str, gen: Generation, max_age_days: int) -> None:
        self.rows.setdefault(key, (gen, datetime.now(UTC)))

    def touch(self, key: str) -> None: ...


class Counting:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        self.calls += 1
        if self.fail:
            raise TransientError("boom")
        return Generation(output={"text": prompt}, latency_ms=7, cost_usd=Decimal("0.5"))


def client_for(inner: Counting, store: MemoryStore | None = None, **kw: Any) -> CachingModelClient:
    kw.setdefault("enabled", True)
    kw.setdefault("max_age_days", 30)
    return CachingModelClient(inner, store or MemoryStore(), **kw)


def test_key_ignores_param_order_and_changes_with_every_input() -> None:
    base = cache_key("generation", "m", "p", {"a": 1, "b": 2})
    assert base == cache_key("generation", "m", "p", {"b": 2, "a": 1})
    assert len(base) == 64
    others = [
        cache_key("judge", "m", "p", {"a": 1, "b": 2}),
        cache_key("generation", "m2", "p", {"a": 1, "b": 2}),
        cache_key("generation", "m", "p2", {"a": 1, "b": 2}),
        cache_key("generation", "m", "p", {"a": 1, "b": 3}),
        cache_key("generation", "m", "p", {"a": 1}),
    ]
    assert base not in others and len(set(others)) == len(others)


def test_oracle_key_includes_expected_other_models_do_not() -> None:
    assert cache_key("generation", "fake/oracle", "p", {}, "x") != cache_key(
        "generation", "fake/oracle", "p", {}, "y"
    )
    assert cache_key("generation", "fake/echo", "p", {}, "x") == cache_key(
        "generation", "fake/echo", "p", {}, "y"
    )


def test_second_identical_call_is_a_hit_with_original_numbers() -> None:
    inner = Counting()
    c = client_for(inner)
    first = c.generate("m", "p", {})
    second = c.generate("m", "p", {})
    assert inner.calls == 1 and second == first
    assert second.latency_ms == 7 and second.cost_usd == Decimal("0.5")


def test_temperature_bypasses_unless_enabled() -> None:
    inner = Counting()
    c = client_for(inner)
    c.generate("m", "p", {"temperature": 0.7})
    c.generate("m", "p", {"temperature": 0.7})
    assert inner.calls == 2
    c.generate("m", "p", {"temperature": 0})
    c.generate("m", "p", {"temperature": 0})
    assert inner.calls == 3

    sampled = Counting()
    s = client_for(sampled, cache_sampled_outputs=True)
    s.generate("m", "p", {"temperature": 0.7})
    s.generate("m", "p", {"temperature": 0.7})
    assert sampled.calls == 1


def test_failures_are_not_cached() -> None:
    store = MemoryStore()
    failing = Counting(fail=True)
    with pytest.raises(TransientError):
        client_for(failing, store).generate("m", "p", {})
    assert store.rows == {}
    ok = Counting()
    client_for(ok, store).generate("m", "p", {})
    assert ok.calls == 1 and len(store.rows) == 1


def test_disabled_client_and_per_scope_switch_bypass() -> None:
    inner = Counting()
    off = client_for(inner, enabled=False)
    off.generate("m", "p", {})
    off.generate("m", "p", {})
    assert inner.calls == 2

    inner2 = Counting()
    c = client_for(inner2)
    c.scope(use_cache=False).generator.generate("m", "p", {})
    c.scope(use_cache=False).generator.generate("m", "p", {})
    assert inner2.calls == 2 and c.scope().generator.generate("m", "p", {})
    assert inner2.calls == 3  # nothing was written by the bypassed calls


def test_scope_tallies_hits_by_kind() -> None:
    c = client_for(Counting())
    c.scope().generator.generate("m", "p", {})
    c.scope().judge.generate("j", "q", {"temperature": 0})
    scope = c.scope()
    scope.generator.generate("m", "p", {})
    scope.judge.generate("j", "q", {"temperature": 0})
    assert scope.generation_hit and scope.judge_hits == 1 and scope.saved_usd == Decimal("1.0")


def test_cache_read_failure_degrades_to_a_miss() -> None:
    class Broken(MemoryStore):
        def get(self, key: str, max_age_days: int) -> Generation | None:
            raise RuntimeError("db down")

    inner = Counting()
    client_for(inner, Broken()).generate("m", "p", {})
    assert inner.calls == 1


# ---------- Postgres store ----------


@pytest.fixture
def store_factory(database_url: str, migrated_engine: Engine) -> Iterator[sessionmaker[Session]]:
    # Own unpooled engine: the cache store must not change the shared engine's pool state.
    engine = create_engine(database_url, poolclass=NullPool)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as s:
        s.execute(text("DELETE FROM model_call_cache"))
        s.commit()
    yield factory
    engine.dispose()


def test_pg_store_roundtrip_hit_counting_and_expiry(
    store_factory: sessionmaker[Session],
) -> None:
    store = PgCacheStore(store_factory)
    gen = Generation(
        output={"text": "x"},
        latency_ms=12,
        input_tokens=3,
        output_tokens=4,
        cost_usd=Decimal("0.25"),
    )
    assert store.get("k", 30) is None
    store.put("k", "generation", "m", gen, 30)
    store.put("k", "generation", "m", Generation(output="other", latency_ms=1), 30)  # first wins
    assert store.get("k", 30) == gen
    store.touch("k")
    with store_factory() as s:
        row = s.get_one(ModelCacheEntry, "k")
        assert row.hits == 1 and row.last_hit_at is not None
        s.execute(
            update(ModelCacheEntry)
            .where(ModelCacheEntry.key == "k")
            .values(created_at=datetime.now(UTC) - timedelta(days=31))
        )
        s.commit()
    assert store.get("k", 30) is None  # expired entries are ignored
    fresh = Generation(output="new", latency_ms=2)
    store.put("k", "generation", "m", fresh, 30)  # and replaced on the next write
    assert store.get("k", 30) == fresh


def test_pg_store_stores_json_null_output(store_factory: sessionmaker[Session]) -> None:
    store = PgCacheStore(store_factory)
    store.put("n", "generation", "m", Generation(output=None, latency_ms=1), 30)
    hit = store.get("n", 30)
    assert hit is not None and hit.output is None


# ---------- worker integration ----------


class LabelModel:
    """Like scenarios.LabelModel but counts calls; also answers as a judge."""

    def __init__(self) -> None:
        self.generations = 0
        self.judgements = 0

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        if model == "stub/judge":
            self.judgements += 1
            return Generation(
                output={"text": '{"score": 4, "reason": "ok"}'},
                latency_ms=3,
                cost_usd=Decimal("0.002"),
            )
        self.generations += 1
        return Generation(
            output={"label": expected["label"]}, latency_ms=100, cost_usd=Decimal("0.001")
        )


@pytest.fixture
def api(make_client: Callable[..., TestClient]) -> TestClient:
    from forgeeval.api.main import app
    from forgeeval.queue import get_queue

    app.dependency_overrides[get_queue] = lambda: NullQueue()
    c = make_client()
    setup_tickets(c)
    c.post("/v1/evaluators", json={"name": "rub", "kind": "llm_rubric"})
    c.post(
        "/v1/evaluators/rub/versions",
        json={"config": {"rubric": "Is it right?"}, "judge_model": "stub/judge"},
    )
    c.post(
        "/v1/prompts/triage/versions",
        json={"template": "Classify: {q}", "model": "stub/label", "params": {}},
    )
    yield c
    app.dependency_overrides.pop(get_queue, None)


def run_once(
    api: TestClient,
    db: Session,
    caching: CachingModelClient,
    evaluators: list[str],
    **extra: Any,
) -> dict[str, Any]:
    resp = api.post(
        "/v1/runs",
        json={
            "dataset": {"name": "tickets"},
            "prompt": {"name": "triage"},
            "evaluators": [{"name": e} for e in evaluators],
            **extra,
        },
    )
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    for item_id in db.scalars(select(RunItem.id).where(RunItem.run_id == run_id)):
        process(db, item_id, "test", caching)
    db.expire_all()
    out = api.get(f"/v1/runs/{run_id}").json()
    assert out["status"] == "succeeded", out
    return out


def scores(db: Session, run_id: str) -> list[tuple[str, Decimal, bool | None]]:
    rows = db.execute(
        text(
            "SELECT ri.dataset_item_id::text, s.value, s.passed FROM scores s "
            "JOIN run_items ri ON ri.id = s.run_item_id WHERE ri.run_id = :r "
            "ORDER BY 1, s.evaluator_version_id"
        ),
        {"r": run_id},
    ).all()
    return [(a, b, c) for a, b, c in rows]


def make(store_factory: sessionmaker[Session], inner: LabelModel) -> CachingModelClient:
    return CachingModelClient(inner, PgCacheStore(store_factory), enabled=True, max_age_days=30)


def test_identical_rerun_is_all_hits_and_free(
    api: TestClient, db: Session, store_factory: sessionmaker[Session]
) -> None:
    inner = LabelModel()
    caching = make(store_factory, inner)
    a = run_once(api, db, caching, ["label", "speed", "rub"])
    assert (a["cache_hits"], a["use_cache"]) == (0, True)
    assert inner.generations == N and inner.judgements == N
    assert Decimal(a["cost_usd"]) == N * Decimal("0.003")

    b = run_once(api, db, caching, ["label", "speed", "rub"])
    assert b["cache_hits"] == N
    assert (inner.generations, inner.judgements) == (N, N)  # zero new model calls
    assert Decimal(b["cost_usd"]) == 0
    # The answer is judged exactly as before: same latency, cost and judge scores.
    sa, sb = scores(db, a["id"]), scores(db, b["id"])
    assert [x[1:] for x in sa] == [x[1:] for x in sb]
    items_a = db.execute(
        select(RunItem.latency_ms, RunItem.cost_usd).where(RunItem.run_id == a["id"])
    ).all()
    items_b = db.execute(
        select(RunItem.latency_ms, RunItem.cost_usd).where(RunItem.run_id == b["id"])
    ).all()
    assert items_a == items_b
    with store_factory() as s:
        assert s.scalar(text("SELECT count(*) FROM model_call_cache")) == 2 * N
        assert s.scalar(text("SELECT sum(hits) FROM model_call_cache")) == 2 * N


def test_cost_evaluator_scores_hit_like_miss(
    api: TestClient, db: Session, store_factory: sessionmaker[Session]
) -> None:
    api.post("/v1/evaluators", json={"name": "cheap", "kind": "cost"})
    api.post("/v1/evaluators/cheap/versions", json={"config": {"max_usd": 0.0005}})
    caching = make(store_factory, LabelModel())
    a = run_once(api, db, caching, ["cheap"])
    b = run_once(api, db, caching, ["cheap"])
    assert b["cache_hits"] == N
    assert {s[2] for s in scores(db, a["id"])} == {s[2] for s in scores(db, b["id"])} == {False}


def test_use_cache_false_never_reads_or_writes(
    api: TestClient, db: Session, store_factory: sessionmaker[Session]
) -> None:
    inner = LabelModel()
    caching = make(store_factory, inner)
    off = run_once(api, db, caching, ["label", "rub"], use_cache=False)
    assert off["use_cache"] is False and off["cache_hits"] == 0
    assert inner.generations == N and inner.judgements == N
    with store_factory() as s:
        assert s.scalar(text("SELECT count(*) FROM model_call_cache")) == 0

    run_once(api, db, caching, ["label", "rub"])  # populates
    again = run_once(api, db, caching, ["label", "rub"], use_cache=False)
    assert again["cache_hits"] == 0 and inner.generations == 3 * N
    assert Decimal(again["cost_usd"]) == N * Decimal("0.003")
    run = db.get_one(EvalRun, uuid.UUID(again["id"]))
    assert run.use_cache is False


def test_judge_verdicts_are_cached_separately_from_generations(
    api: TestClient, db: Session, store_factory: sessionmaker[Session]
) -> None:
    inner = LabelModel()
    caching = make(store_factory, inner)
    run_once(api, db, caching, ["label"])  # generations cached, no judge yet
    assert inner.generations == N and inner.judgements == 0
    b = run_once(api, db, caching, ["label", "rub"])
    assert b["cache_hits"] == N and inner.generations == N and inner.judgements == N
    # Judge spend is real the first time and the generation was free.
    assert Decimal(b["cost_usd"]) == N * Decimal("0.002")
    c = run_once(api, db, caching, ["label", "rub"])
    assert inner.judgements == N and Decimal(c["cost_usd"]) == 0


def test_uncached_client_still_works_in_worker(api: TestClient, db: Session) -> None:
    out = run_once(api, db, FakeModelsAdapter(), ["label"])  # type: ignore[arg-type]
    assert out["cache_hits"] == 0


class FakeModelsAdapter:
    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        return FakeModels().generate("fake/oracle", prompt, params, expected)


# ---------- judge prompt layout ----------


def test_judge_prompt_shares_a_byte_identical_prefix_across_cases() -> None:
    config = parse_config("llm_rubric", {"rubric": "Is it right?", "scale_max": 5})

    def prompt(i: int) -> str:
        case = EvalCase(
            item_input={"q": f"question {i}"},
            expected_output={"a": i},
            context=None,
            output={"text": f"answer {i}"},
            latency_ms=1,
            cost_usd=None,
            tags=[],
        )
        return build_prompt(config, case)

    p1, p2 = prompt(1), prompt(2)
    marker = "\n\nInput:\n"
    prefix1, rest1 = p1.split(marker, 1)
    prefix2, rest2 = p2.split(marker, 1)
    assert prefix1 == prefix2
    assert "Is it right?" in prefix1 and "Scale:" in prefix1 and "JSON only" in prefix1
    # nothing per-item leaks into the prefix; everything per-item is still present after it
    assert "question" not in prefix1 and "answer" not in prefix1
    assert "question 1" in rest1 and '"a": 1' in rest1 and "answer 1" in rest1
    assert "Expected output:" in rest1 and "Candidate output:" in rest1
    assert p1 != p2
