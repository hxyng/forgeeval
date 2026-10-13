"""``forgeeval`` command line: a CI regression gate.

    forgeeval gate --prompt triage --template-file prompt.txt --model openai/gpt-5-mini \
        --dataset triage --evaluator label-match --evaluator latency

Registers the prompt version, runs an evaluation, waits for it, checks the project's regression
gates and exits non-zero when one fails.

Exit codes: 0 pass; 1 a gate failed; 2 inconclusive (only with --fail-on-inconclusive);
3 usage, API or network error, or timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn

import httpx

DEFAULT_BASE_URL = "http://localhost:8000"
TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})
ICONS = {"pass": "✅", "fail": "❌", "inconclusive": "⚠️"}
EXIT_PASS, EXIT_FAIL, EXIT_INCONCLUSIVE, EXIT_ERROR = 0, 1, 2, 3
GATE_CHECK_ATTEMPTS = 5
MAX_NEWLY_FAILING = 10

# Indirection so tests can fast-forward time.
_sleep = time.sleep
_monotonic = time.monotonic


class CliError(Exception):
    """A usage, API or network failure; reported on stderr with exit code 3."""


class ApiError(CliError):
    def __init__(self, status: int, detail: str, what: str) -> None:
        super().__init__(f"{what} failed: HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(EXIT_ERROR, f"{self.prog}: error: {message}\n")


def parse_ref(text: str) -> dict[str, Any]:
    """``NAME`` or ``NAME@VERSION`` -> ``{"name": ..., "version"?: int}``."""
    name, sep, version = text.rpartition("@")
    if not sep:
        return {"name": text}
    if not name or not version.isdigit():
        raise CliError(f"invalid reference {text!r}; expected NAME or NAME@VERSION")
    return {"name": name, "version": int(version)}


def parse_param(text: str) -> tuple[str, Any]:
    key, sep, raw = text.partition("=")
    if not sep or not key:
        raise CliError(f"invalid --param {text!r}; expected key=value")
    try:
        return key, json.loads(raw)
    except ValueError:
        return key, raw


def pr_number_from_ref(ref: str | None) -> int | None:
    match = re.fullmatch(r"refs/pull/(\d+)/(?:merge|head)", ref or "")
    return int(match.group(1)) if match else None


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="forgeeval", description="ForgeEval command line")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    gate = sub.add_parser("gate", help="run an evaluation and enforce regression gates")
    gate.add_argument("--prompt", required=True, help="prompt name")
    gate.add_argument("--template-file", required=True, type=Path)
    gate.add_argument("--model", required=True)
    gate.add_argument("--param", action="append", default=[], metavar="KEY=VALUE")
    gate.add_argument("--dataset", required=True, metavar="NAME[@VERSION]")
    gate.add_argument(
        "--evaluator", action="append", required=True, default=[], metavar="NAME[@VERSION]"
    )
    gate.add_argument("--baseline-run", metavar="RUN_ID")
    gate.add_argument("--max-failure-ratio", type=float)
    gate.add_argument("--timeout", type=float, default=900.0, metavar="SECONDS")
    gate.add_argument("--poll-interval", type=float, default=3.0, metavar="SECONDS")
    gate.add_argument("--git-sha")
    gate.add_argument("--repo")
    gate.add_argument("--pr", type=int)
    gate.add_argument("--summary-file", type=Path)
    gate.add_argument("--fail-on-inconclusive", action="store_true")
    gate.add_argument(
        "--early-stop",
        action="store_true",
        help="stop the run as soon as the gates are decided (needs a finished baseline run)",
    )
    gate.add_argument("--api-key", help="default: $FORGEEVAL_API_KEY")
    gate.add_argument("--base-url", help=f"default: $FORGEEVAL_BASE_URL or {DEFAULT_BASE_URL}")
    return parser


class Api:
    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def call(
        self,
        method: str,
        path: str,
        what: str,
        *,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        ok: Sequence[int] = (),
    ) -> tuple[int, Any]:
        try:
            resp = self._client.request(method, path, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise CliError(f"{what} failed: {type(exc).__name__}: {exc}") from exc
        if resp.status_code >= 400 and resp.status_code not in ok:
            raise ApiError(resp.status_code, _detail(resp), what)
        try:
            body = resp.json()
        except ValueError:
            body = None
        return resp.status_code, body


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("detail") is not None:
        return _detail_of(body)
    return resp.text[:500] or resp.reason_phrase


def _detail_of(body: Any) -> str:
    detail = body.get("detail") if isinstance(body, dict) else None
    return detail if isinstance(detail, str) else json.dumps(detail)


def _md(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _num(value: object, digits: int = 4, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(str(value)):{'+' if signed else ''}.{digits}f}"
    except ValueError:
        return str(value)


def wait_for_run(api: Api, run_id: str, timeout: float, interval: float) -> dict[str, Any]:
    deadline = _monotonic() + timeout
    last = ""
    while True:
        _, run = api.call("GET", f"/v1/runs/{run_id}", "Fetching run")
        if run["status"] in TERMINAL_STATUSES:
            return dict(run)
        line = f"run {run_id}: {run['status']} {json.dumps(run.get('progress') or {})}"
        if line != last:
            print(line, flush=True)
            last = line
        if _monotonic() + interval > deadline:
            raise CliError(
                f"timed out after {timeout:g}s waiting for run {run_id} (status {run['status']})"
            )
        _sleep(interval)


def check_gates(api: Api, body: Mapping[str, Any], interval: float) -> dict[str, Any]:
    for attempt in range(GATE_CHECK_ATTEMPTS):
        status, result = api.call(
            "POST", "/v1/gates:check", "Checking gates", json_body=body, ok=(409,)
        )
        if status != 409:
            return dict(result)
        if attempt == GATE_CHECK_ATTEMPTS - 1:
            raise ApiError(409, _detail_of(result), "Checking gates")
        _sleep(interval)
    raise AssertionError("unreachable")


def render_summary(
    run: Mapping[str, Any], result: Mapping[str, Any], comparisons: Sequence[Mapping[str, Any]]
) -> str:
    verdict = result["verdict"]
    lines = [
        f"## ForgeEval regression gate: {ICONS.get(verdict, '')} {verdict}",
        "",
        f"Run `{run['id']}` finished with status **{run['status']}**.",
        "",
        "| Gate | Verdict | Reason |",
        "| --- | --- | --- |",
    ]
    for check in result["checks"]:
        icon = ICONS.get(check["verdict"], "")
        lines.append(
            f"| {_md(check['gate'])} | {icon} {check['verdict']} | {_md(check['reason'])} |"
        )
    if not result["checks"]:
        lines.append("| (none) | ⚠️ no gates | No gates are configured for this project. |")
    for comp in comparisons:
        lines += [
            "",
            f"### Paired comparison vs baseline `{comp['baseline_run_id']}`",
            "",
            "| Evaluator | Delta mean | 95% CI | Direction |",
            "| --- | --- | --- | --- |",
        ]
        for ev in comp.get("evaluators", []):
            ci = (
                f"[{_num(ev.get('ci95_low'), signed=True)}, "
                f"{_num(ev.get('ci95_high'), signed=True)}]"
            )
            lines.append(
                f"| {_md(ev['evaluator'])} | {_num(ev.get('delta_mean'), signed=True)} | {ci} "
                f"| {_md(ev.get('direction', ''))} |"
            )
        flips = comp.get("newly_failing", [])
        if flips:
            shown = min(len(flips), MAX_NEWLY_FAILING)
            lines += ["", f"Newly failing items ({shown} of {len(flips)}):", ""]
            for flip in flips[:MAX_NEWLY_FAILING]:
                lines.append(
                    f"- `{flip['dataset_item_id']}` ({_md(flip['evaluator'])}): "
                    f"{_num(flip['baseline_value'])} -> {_num(flip['candidate_value'])}"
                )
    return "\n".join(lines) + "\n"


def render_text(
    run: Mapping[str, Any], result: Mapping[str, Any], comparisons: Sequence[Mapping[str, Any]]
) -> str:
    lines = [f"run {run['id']} finished: {run['status']}"]
    for check in result["checks"]:
        icon = ICONS.get(check["verdict"], "")
        lines.append(f"  {icon} {check['gate']}: {check['verdict']} - {check['reason']}")
    if not result["checks"]:
        lines.append("  warning: no gates are configured for this project")
    for comp in comparisons:
        flips = comp.get("newly_failing", [])
        for flip in flips[:MAX_NEWLY_FAILING]:
            lines.append(f"  newly failing: {flip['dataset_item_id']} ({flip['evaluator']})")
        if len(flips) > MAX_NEWLY_FAILING:
            lines.append(f"  ... and {len(flips) - MAX_NEWLY_FAILING} more")
    lines.append(f"gate verdict: {result['verdict']}")
    return "\n".join(lines)


def run_gate(
    args: argparse.Namespace,
    env: Mapping[str, str],
    transport: httpx.BaseTransport | None,
) -> int:
    api_key = args.api_key or env.get("FORGEEVAL_API_KEY")
    if not api_key:
        raise CliError("no API key: set FORGEEVAL_API_KEY or pass --api-key")
    base_url = (args.base_url or env.get("FORGEEVAL_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    sha = args.git_sha or env.get("GITHUB_SHA") or None
    repo = args.repo or env.get("GITHUB_REPOSITORY") or None
    pr = args.pr if args.pr is not None else pr_number_from_ref(env.get("GITHUB_REF"))
    summary_path = args.summary_file or (
        Path(env["GITHUB_STEP_SUMMARY"]) if env.get("GITHUB_STEP_SUMMARY") else None
    )
    params = dict(parse_param(p) for p in args.param)
    dataset = parse_ref(args.dataset)
    evaluators = [parse_ref(e) for e in args.evaluator]
    try:
        template = args.template_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise CliError(f"cannot read template file: {exc}") from exc

    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(
        base_url=base_url, headers=headers, timeout=30.0, transport=transport
    ) as http:
        api = Api(http)
        prompt_body: dict[str, Any] = {"template": template, "model": args.model, "params": params}
        if sha:
            prompt_body["git_sha"] = sha
        _, prompt = api.call(
            "POST",
            f"/v1/prompts/{args.prompt}/versions",
            "Registering prompt version",
            json_body=prompt_body,
        )
        print(f"prompt {prompt['name']} version {prompt['version']}")

        run_body: dict[str, Any] = {
            "dataset": dataset,
            "prompt": {"name": prompt["name"], "version": prompt["version"]},
            "evaluators": evaluators,
            "trigger": "ci",
        }
        if sha:
            run_body["git_sha"] = sha
        if args.baseline_run:
            run_body["baseline_run_id"] = args.baseline_run
        if args.max_failure_ratio is not None:
            run_body["max_failure_ratio"] = args.max_failure_ratio
        if args.early_stop:
            run_body["early_stop"] = True
        _, created = api.call("POST", "/v1/runs", "Creating run", json_body=run_body)
        run_id = created["id"]
        print(f"run {run_id} created")

        run = wait_for_run(api, run_id, args.timeout, args.poll_interval)
        check_body: dict[str, Any] = {"run_id": run_id}
        for key, value in (
            ("baseline_run_id", args.baseline_run),
            ("repo", repo),
            ("pr_number", pr),
            ("commit_sha", sha),
        ):
            if value is not None:
                check_body[key] = value
        result = check_gates(api, check_body, args.poll_interval)

        comparisons: list[dict[str, Any]] = []
        baselines = dict.fromkeys(
            c["baseline_run_id"] for c in result["checks"] if c.get("baseline_run_id")
        )
        for baseline in baselines:
            try:
                _, comp = api.call(
                    "GET",
                    f"/v1/runs/{run_id}/compare",
                    "Fetching comparison",
                    params={"baseline_run_id": baseline},
                )
                comparisons.append(dict(comp))
            except CliError as exc:  # the comparison is advisory; the verdict stands without it
                print(f"warning: {exc}", file=sys.stderr)

    print(render_text(run, result, comparisons))
    if summary_path:
        try:
            with summary_path.open("a", encoding="utf-8") as fh:
                fh.write(render_summary(run, result, comparisons))
        except OSError as exc:
            print(f"warning: could not write summary: {exc}", file=sys.stderr)

    verdict = result["verdict"]
    if verdict == "fail" or any(c["verdict"] == "fail" for c in result["checks"]):
        return EXIT_FAIL
    if verdict == "inconclusive":
        if args.fail_on_inconclusive:
            return EXIT_INCONCLUSIVE
        print("warning: gates were inconclusive; not failing (use --fail-on-inconclusive)")
    return EXIT_PASS


def main(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    environ = os.environ if env is None else env
    for stream in (sys.stdout, sys.stderr):  # the icons must not crash a cp1252 console
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return run_gate(args, environ, transport)
    except CliError as exc:
        message = str(exc)
        key = args.api_key or environ.get("FORGEEVAL_API_KEY")
        if key:
            message = message.replace(key, "***")
        print(f"error: {message}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
