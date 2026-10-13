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

## CI regression gate (`forgeeval gate`)

Installing the SDK also installs the `forgeeval` command. `forgeeval gate` registers a prompt
version, starts an evaluation run (`trigger: ci`), waits for it, checks the project's regression
gates and exits non-zero when one fails.

```bash
export FORGEEVAL_API_KEY=fe_...            # never pass it on the command line in CI logs
export FORGEEVAL_BASE_URL=https://forgeeval.example.com
forgeeval gate --prompt triage --template-file prompt.txt --model openai/gpt-5-mini   --param temperature=0 --dataset triage --evaluator label-match --evaluator latency
```

`--dataset` and `--evaluator` accept `NAME` or `NAME@VERSION`; `--param key=value` is parsed as
JSON when possible. Other options: `--baseline-run`, `--max-failure-ratio`, `--timeout` (900 s),
`--poll-interval` (3 s), `--summary-file`, `--fail-on-inconclusive`, `--git-sha`, `--repo`, `--pr`.
Inside GitHub Actions the SHA, repository, PR number (from `GITHUB_REF`) and summary file
(`GITHUB_STEP_SUMMARY`) are picked up automatically.

| Exit code | Meaning |
| --- | --- |
| 0 | all gates passed (inconclusive gates only warn) |
| 1 | at least one gate failed |
| 2 | inconclusive, only with `--fail-on-inconclusive` |
| 3 | usage, API or network error, or timeout (details on stderr) |
