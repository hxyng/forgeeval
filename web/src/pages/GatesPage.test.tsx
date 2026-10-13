import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { GatesPage } from "./GatesPage";
import { makeRun, mockApi, renderApp } from "../test/helpers";
import type { Schemas } from "../api/client";

const GATES: Schemas["GateOut"][] = [
  {
    id: "g1",
    name: "accuracy",
    evaluator: "label-match",
    evaluator_version: 1,
    evaluator_version_id: "ev-1",
    higher_is_better: true,
    aggregate: "mean",
    comparator: "max_drop_vs_baseline",
    threshold: "0.05",
    enabled: true,
  },
  {
    id: "g2",
    name: "p95-latency",
    evaluator: "latency",
    evaluator_version: 1,
    evaluator_version_id: "ev-2",
    higher_is_better: false,
    aggregate: "p95",
    comparator: "min_absolute",
    threshold: "1500",
    enabled: false,
  },
];

const EVALUATORS = [
  { id: "e1", name: "label-match", kind: "exact_match", higher_is_better: true, latest_version: 1 },
  { id: "e2", name: "latency", kind: "latency", higher_is_better: false, latest_version: 1 },
];

const CHECK: Schemas["CheckOut"] = {
  id: "c1",
  gate: "accuracy",
  verdict: "fail",
  reason: "mean 1 -> 0; worsened by 1, allowed 0.05",
  aggregate: "mean",
  comparator: "max_drop_vs_baseline",
  threshold: "0.05",
  observed: "0",
  baseline: "1",
  delta: "-1",
  run_id: "01a12ea5-133e-7abe-a474-a0fea4c485f4",
  baseline_run_id: "01a12ea4-9534-77d7-b556-3d06d90fcd5b",
  repo: "acme/app",
  pr_number: 42,
  commit_sha: "abcdef1234567",
  created_at: new Date(Date.now() - 3600_000).toISOString(),
};

describe("GatesPage", () => {
  it("lists gates with evaluator, aggregate, comparator, threshold and enabled", async () => {
    mockApi({
      "/v1/gates": GATES,
      "/v1/evaluators": EVALUATORS,
      "/v1/runs": { items: [], next_cursor: null },
    });
    renderApp(<GatesPage />);
    const row = (await screen.findByText("accuracy")).closest("tr")!;
    expect(within(row).getByText("label-match@1")).toBeInTheDocument();
    expect(within(row).getByText("max drop vs baseline")).toBeInTheDocument();
    expect(within(row).getByText("0.05")).toBeInTheDocument();
    const lat = screen.getByText("p95-latency").closest("tr")!;
    expect(within(lat).getByText("1.5 s")).toBeInTheDocument(); // latency threshold in ms units
    expect(within(lat).getByText("no")).toBeInTheDocument();
  });

  it("shows the check history for a gate", async () => {
    mockApi({
      "/v1/gates": GATES,
      "/v1/evaluators": EVALUATORS,
      "/v1/runs": { items: [], next_cursor: null },
      "/v1/gates/accuracy/checks": [CHECK],
    });
    renderApp(<GatesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Show checks for accuracy" }));
    const history = await screen.findByTestId("checks-accuracy");
    expect(within(history).getByText("fail")).toBeInTheDocument();
    expect(within(history).getByText(CHECK.reason)).toBeInTheDocument();
    expect(within(history).getByText("#42")).toBeInTheDocument();
    expect(within(history).getByText("abcdef1")).toBeInTheDocument();
    expect(within(history).getByText("1 hour ago")).toBeInTheDocument();
  });

  it("checks all gates for a chosen finished run", async () => {
    const run = makeRun();
    const { calls } = mockApi({
      "/v1/gates": GATES,
      "/v1/evaluators": EVALUATORS,
      "/v1/runs": {
        items: [run, makeRun({ id: "01a12ea5-0000-7abe-a474-a0fea4c485f4", status: "running" })],
        next_cursor: null,
      },
      "POST /v1/gates:check": { verdict: "fail", checks: [CHECK] },
    });
    renderApp(<GatesPage />);
    const select = await screen.findByLabelText("Finished run");
    await waitFor(() => expect(within(select).getAllByRole("option")).toHaveLength(2)); // placeholder + the finished run only
    await userEvent.selectOptions(select, run.id);
    await userEvent.click(screen.getByRole("button", { name: "Check gates" }));

    const result = await screen.findByTestId("check-result");
    expect(within(result).getByText(/Overall verdict/)).toBeInTheDocument();
    expect(within(result).getAllByText("fail").length).toBeGreaterThan(0);
    expect(calls.some((c) => c.method === "POST" && c.path === "/v1/gates:check")).toBe(true);
  });

  it("shows an empty state", async () => {
    mockApi({
      "/v1/gates": [],
      "/v1/evaluators": [],
      "/v1/runs": { items: [], next_cursor: null },
    });
    renderApp(<GatesPage />);
    expect(await screen.findByText("No gates defined")).toBeInTheDocument();
  });
});
