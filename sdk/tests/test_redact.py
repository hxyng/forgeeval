from __future__ import annotations

import re

from forgeeval_sdk import DEFAULT_REDACTORS, Redactor, luhn_valid
from forgeeval_sdk.redact import redact_text, scrub


def run(text: str) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}
    return redact_text(text, DEFAULT_REDACTORS, counts), counts


def test_email() -> None:
    out, counts = run("mail a.b+c@example.co.uk and bob@x.io now")
    assert out == "mail [REDACTED:email] and [REDACTED:email] now"
    assert counts == {"email": 2}


def test_phone_north_american_and_e164() -> None:
    for s in ["(415) 555-2671", "415-555-2671", "415.555.2671", "+1 415 555 2671", "4155552671"]:
        out, counts = run(f"call {s} please")
        assert out == "call [REDACTED:phone] please", s
        assert counts == {"phone": 1}
    out, counts = run("intl +442071838750 ok")
    assert out == "intl [REDACTED:phone] ok"
    assert counts == {"phone": 1}


def test_credit_card_luhn_valid_is_redacted() -> None:
    for s in ["4111111111111111", "4111 1111 1111 1111", "4111-1111-1111-1111"]:
        out, counts = run(f"card {s}.")
        assert out == "card [REDACTED:credit_card].", s
        assert counts == {"credit_card": 1}


def test_credit_card_luhn_invalid_is_kept() -> None:
    assert not luhn_valid("4111111111111112")
    out, counts = run("order 4111 1111 1111 1112 shipped")
    assert out == "order 4111 1111 1111 1112 shipped"
    assert "credit_card" not in counts


def test_ssn() -> None:
    out, counts = run("ssn 123-45-6789 here")
    assert out == "ssn [REDACTED:ssn] here"
    assert counts == {"ssn": 1}
    # invalid area numbers are not SSNs
    out, counts = run("id 000-12-3456 and 987-65-4321")
    assert "ssn" not in counts


def test_secret_tokens() -> None:
    text = "k1 sk-abcdefghijklmnopqrstuv k2 fe_abcd1234_s3cretvalue99 k3 AKIAIOSFODNN7EXAMPLE"
    out, counts = run(text)
    assert out == "k1 [REDACTED:secret] k2 [REDACTED:secret] k3 [REDACTED:secret]"
    assert counts == {"secret": 3}


def test_innocuous_text_untouched() -> None:
    text = "Order #12345 shipped on 2024-05-01, total 19.99 USD, version 1.2.3"
    out, counts = run(text)
    assert out == text
    assert counts == {}


def test_custom_regex_redactor() -> None:
    r = Redactor("employee_id", re.compile(r"EMP-\d{4}"))
    counts: dict[str, int] = {}
    assert redact_text("hi EMP-1234 and EMP-9999", [r], counts) == (
        "hi [REDACTED:employee_id] and [REDACTED:employee_id]"
    )
    assert counts == {"employee_id": 2}


def test_custom_callable_redactor() -> None:
    r = Redactor("shout", lambda s: s.replace("secret", "******"))
    counts: dict[str, int] = {}
    assert redact_text("a secret b", [r], counts) == "a ****** b"
    assert counts == {"shout": 1}
    assert redact_text("nothing", [r], counts) == "nothing"
    assert counts == {"shout": 1}


def test_scrub_recurses_and_makes_json_safe() -> None:
    class Thing:
        def __str__(self) -> str:
            return "thing for a@b.com"

    counts: dict[str, int] = {}
    value = {
        "msgs": [{"content": "mail me a@b.com"}, ("x", "c@d.org")],
        "n": 1,
        "bad": float("nan"),
        "obj": Thing(),
        1: "k",
    }
    out = scrub(value, DEFAULT_REDACTORS, counts)
    assert out["msgs"][0]["content"] == "mail me [REDACTED:email]"
    assert out["msgs"][1] == ["x", "[REDACTED:email]"]
    assert out["bad"] is None
    assert out["obj"] == "thing for [REDACTED:email]"
    assert out["1"] == "k"
    assert counts == {"email": 3}
