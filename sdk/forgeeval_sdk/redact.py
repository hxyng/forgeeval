"""Local PII / secret redaction.

Everything here runs in the caller's thread, before a trace is queued, so raw values never
leave the process.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

MAX_DEPTH = 50

Matcher = str | re.Pattern[str] | Callable[[str], str]


@dataclass(frozen=True)
class Redactor:
    """One named redaction rule.

    ``matcher`` is either a regex (string or compiled) or a callable ``str -> str``.
    A regex match is replaced with ``[REDACTED:<name>]`` and each match counts as one hit.
    A callable returns the rewritten text itself; it counts as one hit when the text changed.
    ``validate`` (regex only) can veto a match, e.g. a Luhn check for card numbers.
    """

    name: str
    matcher: Matcher
    validate: Callable[[str], bool] | None = None

    def apply(self, text: str) -> tuple[str, int]:
        matcher = self.matcher
        if callable(matcher):
            out = matcher(text)
            return (out, 1) if out != text else (text, 0)
        pattern = re.compile(matcher) if isinstance(matcher, str) else matcher
        hits = 0
        replacement = f"[REDACTED:{self.name}]"

        def _sub(m: re.Match[str]) -> str:
            nonlocal hits
            if self.validate is not None and not self.validate(m.group(0)):
                return m.group(0)
            hits += 1
            return replacement

        return pattern.sub(_sub, text), hits


def luhn_valid(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _ssn_valid(candidate: str) -> bool:
    area, group, serial = candidate.split("-")
    if area in {"000", "666"} or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


# Order matters: more specific / longer-digit rules run before the phone rule.
DEFAULT_REDACTORS: tuple[Redactor, ...] = (
    Redactor(
        "secret",
        r"\b(?:sk-[A-Za-z0-9_-]{16,}|fe_[A-Za-z0-9]{8}_[A-Za-z0-9_-]{8,}|AKIA[0-9A-Z]{16})\b",
    ),
    Redactor("email", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"),
    Redactor("credit_card", r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])", validate=luhn_valid),
    Redactor("ssn", r"(?<![\d-])\d{3}-\d{2}-\d{4}(?![\d-])", validate=_ssn_valid),
    Redactor(
        "phone",
        r"(?<![\w+])(?:\+[1-9]\d{7,14}(?!\d)"
        r"|(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s?|\d{3}[\s.-]?)\d{3}[\s.-]?\d{4}(?!\d))",
    ),
)


def as_redactor(item: Redactor | tuple[str, Matcher]) -> Redactor:
    if isinstance(item, Redactor):
        return item
    name, matcher = item
    return Redactor(name, matcher)


def redact_text(text: str, redactors: Sequence[Redactor], counts: dict[str, int]) -> str:
    for r in redactors:
        text, hits = r.apply(text)
        if hits:
            counts[r.name] = counts.get(r.name, 0) + hits
    return text


def scrub(
    value: Any, redactors: Sequence[Redactor], counts: dict[str, int], _depth: int = 0
) -> Any:
    """Return a JSON-safe deep copy of ``value`` with strings redacted.

    Dict keys are left alone. Unknown objects become ``str(obj)``; non-finite floats become
    ``None``; sets/tuples become lists.
    """
    if _depth > MAX_DEPTH:
        return "[max depth exceeded]"
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return redact_text(value, redactors, counts)
    if isinstance(value, Mapping):
        return {str(k): scrub(v, redactors, counts, _depth + 1) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [scrub(v, redactors, counts, _depth + 1) for v in value]
    return redact_text(str(value), redactors, counts)
