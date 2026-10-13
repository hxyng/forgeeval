from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from forgeeval_sdk import cli

KEY = "fe_testkey_SECRET123"
RUN_ID = "11111111-1111-1111-1111-111111111111"
BASE_ID = "22222222-2222-2222-2222-222222222222"


def check(
    gate: str, verdict: str, reason: str = "ok", baseline: str | None = None
) -> dict[str, Any]:
    return {
        "gate": gate,
        "verdict": verdict,
        "reason": reason,
        "observed": "0.5",
        "baseline": None,
        "delta": None,
        "baseline_run_id": baseline,
    }


class Server:
    """A scripted ForgeEval API behind httpx.MockTransport."""

    def __init__(
        self,
        checks: list[dict[str, Any]],
        verdict: str,
        *,
        statuses: list[str] | None = None,
        gate_responses: list[httpx.Response] | None = None,
    ) -> None:
        self.checks = checks
        self.verdict = verdict
        self.statuses = statuses or ["succeeded"]
        self.gate_responses = gate_responses or []
        self.requests: list[httpx.Request] = []
        self.bodies: dict[str, Any] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["authorization"] == f"Bearer {KEY}"
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.bodies[f"{request.method} {path}"] = body
        if path == "/v1/prompts/triage/versions":
            return httpx.Response(200, json={"name": "triage", "version": 4})
        if path == "/v1/runs" and request.method == "POST":
            return httpx.Response(201, json={"id": RUN_ID})
        if path == f"/v1/runs/{RUN_ID}":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(
                200, json={"id": RUN_ID, "status": status, "progress": {"queued": 1}}
            )
        if path == "/v1/gates:check":
            if self.gate_responses:
                return self.gate_responses.pop(0)
            return httpx.Response(200, json={"verdict": self.verdict, "checks": self.checks})
        if path == f"/v1/runs/{RUN_ID}/compare":
            assert request.url.params["baseline_run_id"] == BASE_ID
            flips = [
                {
                    "dataset_item_id": f"item-{i}",
                    "evaluator": "label-match",
                    "baseline_value": 1,
                    "candidate_value": 0,
                }
                for i in range(12)
            ]
            return httpx.Response(
                200,
                json={
                    "baseline_run_id": BASE_ID,
                    "evaluators": [
                        {
                            "evaluator": "label-match",
                            "delta_mean": -0.5,
                            "ci95_low": -0.6,
                            "ci95_high": -0.4,
                            "direction": "worse",
                        }
                    ],
                    "newly_failing": flips,
                },
            )
        return httpx.Response(404, json={"detail": "nope"})


@pytest.fixture
def template(tmp_path: Path) -> Path:
    path = tmp_path / "prompt.txt"
    path.write_text("Classify: {q}")
    return path


@pytest.fixture(autouse=True)
def fast_time(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(cli, "_sleep", sleep)
    monkeypatch.setattr(cli, "_monotonic", lambda: clock[0])


def run_cli(
    server: Any,
    template: Path,
    *extra: str,
    env: dict[str, str] | None = None,
) -> int:
    argv = [
        "gate",
        "--prompt",
        "triage",
        "--template-file",
        str(template),
        "--model",
        "fake/oracle",
        "--dataset",
        "triage",
        "--evaluator",
        "label-match",
        "--poll-interval",
        "1",
        *extra,
    ]
    environment = {"FORGEEVAL_API_KEY": KEY, "FORGEEVAL_BASE_URL": "http://api.test"}
    environment.update(env or {})
    return cli.main(argv, env=environment, transport=httpx.MockTransport(server))


def test_pass(template: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = Server([check("accuracy", "pass")], "pass")
    assert run_cli(server, template) == 0
    out = capsys.readouterr().out
    assert "gate verdict: pass" in out
    assert "warning" not in out
    run_body = server.bodies["POST /v1/runs"]
    assert run_body["trigger"] == "ci"
    assert run_body["prompt"] == {"name": "triage", "version": 4}
    assert run_body["dataset"] == {"name": "triage"}


def test_fail_lists_newly_failing_and_writes_summary(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    server = Server(
        [check("accuracy", "fail", "dropped | a lot", BASE_ID), check("p95", "pass")], "fail"
    )
    summary = tmp_path / "summary.md"
    assert run_cli(server, template, "--summary-file", str(summary)) == 1
    out = capsys.readouterr().out
    assert out.count("newly failing: item-") == 10
    assert "and 2 more" in out
    md = summary.read_text(encoding="utf-8")
    assert "❌ fail" in md
    assert "✅ pass" in md
    assert "dropped \\| a lot" in md
    assert "-0.5000" in md
    assert "[-0.6000, -0.4000]" in md
    assert md.count("- `item-") == 10
    assert RUN_ID in md


@pytest.mark.parametrize(("flag", "code"), [([], 0), (["--fail-on-inconclusive"], 2)])
def test_inconclusive(
    template: Path, capsys: pytest.CaptureFixture[str], flag: list[str], code: int
) -> None:
    server = Server([check("accuracy", "inconclusive", "no baseline")], "inconclusive")
    assert run_cli(server, template, *flag) == code
    out = capsys.readouterr().out
    assert ("warning: gates were inconclusive" in out) == (code == 0)
    assert "⚠️" in out


def test_polls_until_terminal(template: Path) -> None:
    server = Server([check("a", "pass")], "pass", statuses=["queued", "running", "succeeded"])
    assert run_cli(server, template) == 0
    polls = [r for r in server.requests if r.url.path == f"/v1/runs/{RUN_ID}"]
    assert len(polls) == 3


def test_timeout(template: Path, capsys: pytest.CaptureFixture[str]) -> None:
    server = Server([], "pass", statuses=["running"])
    assert run_cli(server, template, "--timeout", "10") == 3
    err = capsys.readouterr().err
    assert "timed out after 10s" in err
    assert RUN_ID in err
    assert not any(r.url.path == "/v1/gates:check" for r in server.requests)


def test_gate_check_409_then_success(template: Path) -> None:
    conflict = httpx.Response(409, json={"detail": "Run is not finished."})
    ok = httpx.Response(200, json={"verdict": "pass", "checks": [check("a", "pass")]})
    server = Server([], "pass", gate_responses=[conflict, conflict, ok])
    assert run_cli(server, template) == 0
    assert sum(r.url.path == "/v1/gates:check" for r in server.requests) == 3


def test_gate_check_409_forever(template: Path, capsys: pytest.CaptureFixture[str]) -> None:
    conflict = httpx.Response(409, json={"detail": "Run is not finished."})
    server = Server([], "pass", gate_responses=[conflict] * 10)
    assert run_cli(server, template) == 3
    assert "HTTP 409: Run is not finished." in capsys.readouterr().err


def test_api_error_exit_3_with_detail(template: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "Dataset 'triage' not found."})

    assert run_cli(handler, template) == 3
    err = capsys.readouterr().err
    assert "HTTP 422" in err
    assert "Dataset 'triage' not found." in err


def test_network_error_exit_3(template: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    assert run_cli(handler, template) == 3
    assert "connection refused" in capsys.readouterr().err


def test_github_defaults(template: Path, tmp_path: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    summary = tmp_path / "step_summary.md"
    env = {
        "GITHUB_SHA": "abc123",
        "GITHUB_REPOSITORY": "acme/bot",
        "GITHUB_REF": "refs/pull/42/merge",
        "GITHUB_STEP_SUMMARY": str(summary),
    }
    assert run_cli(server, template, env=env) == 0
    assert server.bodies["POST /v1/gates:check"] == {
        "run_id": RUN_ID,
        "repo": "acme/bot",
        "pr_number": 42,
        "commit_sha": "abc123",
    }
    assert server.bodies["POST /v1/runs"]["git_sha"] == "abc123"
    assert server.bodies["POST /v1/prompts/triage/versions"]["git_sha"] == "abc123"
    assert "ForgeEval regression gate" in summary.read_text(encoding="utf-8")


def test_flags_override_github_defaults(template: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    env = {"GITHUB_SHA": "abc", "GITHUB_REPOSITORY": "a/b", "GITHUB_REF": "refs/pull/1/merge"}
    run_cli(
        server,
        template,
        "--git-sha",
        "def",
        "--repo",
        "c/d",
        "--pr",
        "7",
        "--baseline-run",
        BASE_ID,
        "--max-failure-ratio",
        "0.1",
        env=env,
    )
    assert server.bodies["POST /v1/gates:check"] == {
        "run_id": RUN_ID,
        "baseline_run_id": BASE_ID,
        "repo": "c/d",
        "pr_number": 7,
        "commit_sha": "def",
    }
    run_body = server.bodies["POST /v1/runs"]
    assert run_body["baseline_run_id"] == BASE_ID
    assert run_body["max_failure_ratio"] == 0.1


def test_early_stop_flag_is_sent_only_when_given(template: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    run_cli(server, template)
    assert "early_stop" not in server.bodies["POST /v1/runs"]
    server = Server([check("a", "pass")], "pass")
    run_cli(server, template, "--early-stop")
    assert server.bodies["POST /v1/runs"]["early_stop"] is True


def test_non_pr_ref_has_no_pr_number(template: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    run_cli(server, template, env={"GITHUB_REF": "refs/heads/main"})
    assert "pr_number" not in server.bodies["POST /v1/gates:check"]


def test_param_and_ref_parsing(template: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    run_cli(
        server,
        template,
        "--param",
        "fail_rate=0.5",
        "--param",
        "permanent_fail=true",
        "--param",
        "style=terse",
        "--param",
        "eq=a=b",
    )
    assert server.bodies["POST /v1/prompts/triage/versions"]["params"] == {
        "fail_rate": 0.5,
        "permanent_fail": True,
        "style": "terse",
        "eq": "a=b",
    }
    assert server.bodies["POST /v1/prompts/triage/versions"]["template"] == "Classify: {q}"


def test_name_version_parsing(template: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    argv_extra = ["--evaluator", "latency@2"]
    run_cli(server, template, *argv_extra, "--dataset", "other@3")
    body = server.bodies["POST /v1/runs"]
    assert body["evaluators"] == [{"name": "label-match"}, {"name": "latency", "version": 2}]
    assert body["dataset"] == {"name": "other", "version": 3}


def test_parse_helpers() -> None:
    assert cli.parse_ref("a@1") == {"name": "a", "version": 1}
    assert cli.parse_ref("a") == {"name": "a"}
    with pytest.raises(cli.CliError):
        cli.parse_ref("a@x")
    with pytest.raises(cli.CliError):
        cli.parse_param("novalue")


def test_usage_error_is_exit_3(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["gate"], env={})
    assert exc.value.code == 3


def test_missing_key_is_exit_3(template: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = cli.main(
        ["gate", "--prompt", "p", "--template-file", str(template), "--model", "m"]
        + ["--dataset", "d", "--evaluator", "e"],
        env={},
    )
    assert code == 3
    assert "no API key" in capsys.readouterr().err


def test_key_never_leaks(
    template: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = tmp_path / "s.md"
    server = Server([check("a", "fail", "bad", BASE_ID)], "fail")
    run_cli(server, template, "--summary-file", str(summary))

    def echo_key(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": f"bad key {KEY}"})

    run_cli(echo_key, template, "--summary-file", str(summary))
    captured = capsys.readouterr()
    assert KEY not in captured.out
    assert KEY not in captured.err
    assert KEY not in summary.read_text(encoding="utf-8")
    assert "***" in captured.err


def test_api_key_flag(template: Path) -> None:
    server = Server([check("a", "pass")], "pass")
    argv = [
        "gate", "--prompt", "triage", "--template-file", str(template), "--model", "m",
        "--dataset", "d", "--evaluator", "e", "--api-key", KEY, "--base-url", "http://x.test/",
    ]  # fmt: skip
    assert cli.main(argv, env={}, transport=httpx.MockTransport(server)) == 0
    assert str(server.requests[0].url).startswith("http://x.test/v1/prompts")
