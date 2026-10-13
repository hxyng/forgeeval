"""Seed a ForgeEval project for the triage CI-gate demo. Safe to run repeatedly.

    FORGEEVAL_API_KEY=fe_... FORGEEVAL_BASE_URL=http://localhost:8000 python examples/triage/seed.py

Creates the `triage` dataset version, the `label-match` and `latency` evaluators, the `accuracy`
and `p95-latency` gates, and a baseline run of the `triage` prompt on `fake/oracle`. Prints the
baseline run id on the last line. Needs only the standard library and httpx.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).parent
DATASET = "triage"
PROMPT = "triage"
TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


def call(
    client: httpx.Client, method: str, path: str, body: object = None, *, tolerate: tuple[int, ...] = ()
) -> httpx.Response:
    resp = client.request(method, path, json=body)
    if resp.status_code >= 400 and resp.status_code not in tolerate:
        sys.exit(f"{method} {path} failed: HTTP {resp.status_code}: {resp.text[:500]}")
    return resp


def wait_for_api(client: httpx.Client, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if client.get("/healthz").status_code == 200:
                return
        except httpx.TransportError:
            pass
        if time.monotonic() > deadline:
            sys.exit("API did not become healthy in time")
        time.sleep(2)


def main() -> None:
    api_key = os.environ.get("FORGEEVAL_API_KEY")
    if not api_key:
        sys.exit("set FORGEEVAL_API_KEY")
    base_url = os.environ.get("FORGEEVAL_BASE_URL", "http://localhost:8000")
    with httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=30.0
    ) as c:
        wait_for_api(c)

        items = [
            json.loads(line)
            for line in (HERE / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        call(c, "POST", "/v1/datasets", {"name": DATASET, "description": "Support tickets"}, tolerate=(409,))
        call(c, "POST", f"/v1/datasets/{DATASET}/versions", {"items": items})
        print(f"dataset {DATASET}: {len(items)} items")

        evaluators = {
            "label-match": ("exact_match", {"kind": "exact_match", "field": "label"}),
            "latency": ("latency", {"kind": "latency", "max_ms": 2000}),
        }
        for name, (kind, config) in evaluators.items():
            call(c, "POST", "/v1/evaluators", {"name": name, "kind": kind}, tolerate=(409,))
            call(c, "POST", f"/v1/evaluators/{name}/versions", {"config": config})
        print("evaluators: label-match, latency")

        gates = [
            ("accuracy", "label-match", "mean", "max_drop_vs_baseline", "0.05"),
            ("p95-latency", "latency", "p95", "min_absolute", "1500"),
        ]
        for name, evaluator, aggregate, comparator, threshold in gates:
            body = {
                "name": name,
                "evaluator": {"name": evaluator},
                "aggregate": aggregate,
                "comparator": comparator,
                "threshold": threshold,
            }
            call(c, "POST", "/v1/gates", body, tolerate=(409,))
        print("gates: accuracy, p95-latency")

        template = (HERE / "prompt.txt").read_text(encoding="utf-8")
        prompt = call(
            c,
            "POST",
            f"/v1/prompts/{PROMPT}/versions",
            {"template": template, "model": "fake/oracle", "params": {}},
        ).json()
        run = call(
            c,
            "POST",
            "/v1/runs",
            {
                "dataset": {"name": DATASET},
                "prompt": {"name": PROMPT, "version": prompt["version"]},
                "evaluators": [{"name": "label-match"}, {"name": "latency"}],
                "trigger": "manual",
            },
        ).json()
        deadline = time.monotonic() + 300
        while True:
            status = call(c, "GET", f"/v1/runs/{run['id']}").json()["status"]
            if status in TERMINAL:
                break
            if time.monotonic() > deadline:
                sys.exit(f"baseline run {run['id']} did not finish in time")
            time.sleep(2)
        if status not in {"succeeded", "partial"}:
            sys.exit(f"baseline run {run['id']} ended {status}")
        print(f"baseline run {run['id']}: {status}")


if __name__ == "__main__":
    main()
