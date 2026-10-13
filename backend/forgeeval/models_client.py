"""Calls the model under test (and LLM judges). Errors are classified so the worker can decide
whether to retry: TransientError (timeouts, 429, 5xx) or PermanentError (bad request, bad config).

``fake/*`` models are deterministic and need no network. They exist for tests and for failure
drills: set prompt params ``fail_rate`` (0-1) or ``permanent_fail: true`` to inject faults.
"""

import json
import random
import re
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

import httpx

from forgeeval.config import get_settings


class TransientError(Exception):
    """Worth retrying: the same request may succeed later."""


class PermanentError(Exception):
    """Retrying cannot help: the request or configuration is wrong."""


@dataclass(frozen=True)
class Generation:
    output: Any
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: Decimal | None = None


class ModelClient(Protocol):
    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation: ...


_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render(template: str, item_input: Any) -> str:
    """Fill ``{field}`` placeholders from a dict input; ``{input}`` is the whole input as JSON.

    Only ``{identifier}`` is substituted. Every other brace is left alone, so templates can
    contain JSON examples like ``{"label": "billing"}`` without escaping. Unknown placeholders
    are kept verbatim; dict and list values are inserted as JSON.
    """
    values: dict[str, Any] = dict(item_input) if isinstance(item_input, dict) else {}
    values.setdefault("input", item_input)

    def fill(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            return match.group(0)
        value = values[key]
        if isinstance(value, str):
            return value
        return json.dumps(value, sort_keys=True, default=str)

    return _PLACEHOLDER.sub(fill, template)


class FakeModels:
    """``fake/echo`` returns the rendered prompt; ``fake/oracle`` returns the expected output."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        if params.get("permanent_fail"):
            raise PermanentError("fake model configured to fail permanently")
        if self._rng.random() < float(params.get("fail_rate", 0)):
            raise TransientError("fake model injected a transient failure")
        name = model.removeprefix("fake/")
        if name == "echo":
            output: Any = {"text": prompt}
        elif name == "oracle":
            output = expected
        elif name == "judge":
            score = params.get("judge_score", 4)
            output = {"text": json.dumps({"score": score, "reason": "fake judge"})}
        else:
            raise PermanentError(
                f"unknown fake model {model!r}; use fake/echo, fake/oracle or fake/judge"
            )
        tokens = len(prompt.split())
        return Generation(
            output=output,
            latency_ms=int(params.get("latency_ms", 5)),
            input_tokens=tokens,
            output_tokens=tokens,
            cost_usd=Decimal("0.000001") * tokens,
        )


class OpenAICompatible:
    """Chat Completions over HTTP. Works with OpenAI, AI gateways, vLLM and Ollama."""

    def __init__(
        self, base_url: str, api_key: str | None, strip_provider: bool, client: httpx.Client
    ) -> None:
        self._base = base_url.rstrip("/")
        self._key = api_key
        self._strip = strip_provider
        self._http = client

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        model_id = model.split("/", 1)[1] if self._strip and "/" in model else model
        body = {"model": model_id, "messages": [{"role": "user", "content": prompt}], **params}
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        started = time.perf_counter()
        try:
            resp = self._http.post(f"{self._base}/chat/completions", json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise TransientError(f"model call timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"model call failed: {exc.__class__.__name__}") from exc
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"model returned HTTP {resp.status_code}")
        if resp.status_code >= 400:
            raise PermanentError(f"model returned HTTP {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise PermanentError("model response did not match the Chat Completions shape") from exc
        usage = data.get("usage") or {}
        return Generation(
            output={"text": text},
            latency_ms=latency_ms,
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


class Router:
    """Sends ``fake/*`` to FakeModels and everything else to the configured HTTP endpoint."""

    def __init__(self, fake: FakeModels, http: ModelClient | None) -> None:
        self._fake = fake
        self._http = http

    def generate(
        self, model: str, prompt: str, params: dict[str, Any], expected: Any = None
    ) -> Generation:
        if model.startswith("fake/"):
            return self._fake.generate(model, prompt, params, expected)
        if self._http is None:
            raise PermanentError(f"no model endpoint configured for {model!r}; set MODEL_BASE_URL")
        return self._http.generate(model, prompt, params, expected)


def default_client() -> Router:
    s = get_settings()
    http: ModelClient | None = None
    if s.model_base_url:
        http = OpenAICompatible(
            s.model_base_url,
            s.model_api_key,
            s.model_strip_provider,
            httpx.Client(timeout=s.model_timeout_seconds),
        )
    return Router(FakeModels(), http)
