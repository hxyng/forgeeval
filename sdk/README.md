# forgeeval-sdk

Python client for ForgeEval. Captures LLM traces (with spans), redacts PII locally, and uploads
them in the background in batches. It never blocks or raises into your application.

```bash
pip install "git+https://github.com/hxyng/forgeeval.git#subdirectory=sdk"  # import: forgeeval_sdk
```

```python
from forgeeval_sdk import ForgeEval

client = ForgeEval(api_key="fe_xxxxxxxx_...", base_url="http://localhost:8000")
# or set FORGEEVAL_API_KEY / FORGEEVAL_BASE_URL

with client.trace(model="openai/gpt-5-mini", prompt=("support-answer", 3)) as t:
    t.input = {"messages": [{"role": "user", "content": "Where is my order?"}]}
    with t.span("kb.search", kind="retrieval") as s:
        s.attributes["doc_ids"] = ["kb-12", "kb-40"]
    t.output = {"text": "It ships tomorrow."}
    t.usage(input_tokens=120, output_tokens=18, cost_usd=0.00021)

client.flush()  # optional; also runs at exit and on close()
client.close()
```

- Traces get a client-generated id, so retried uploads are deduplicated by the server.
- If the `with` block raises, the trace is recorded with `status="error"` and your exception
  propagates unchanged.
- Redaction (email, phone, Luhn-valid cards, SSN, `sk-`/`fe_`/`AKIA` secrets) runs before
  queueing; hit counts go to the trace's `redaction` field. Add rules with
  `redactors=[*DEFAULT_REDACTORS, ("employee_id", r"EMP-\d{4}")]`.
- The queue is bounded (`max_queue`); when full the newest trace is dropped and counted in
  `client.stats()["dropped"]`.
- Failures are logged on the `forgeeval_sdk` logger; 5xx/429/network errors are retried with
  exponential backoff and jitter (5 attempts, `Retry-After` honored).
- `client.log_trace(model=..., input=..., output=..., latency_ms=...)` records a finished trace.

## Development

```bash
uv sync --python 3.12
uv run pytest -q
FORGEEVAL_IT_API_KEY=fe_... uv run pytest -m integration   # needs a running stack
```
