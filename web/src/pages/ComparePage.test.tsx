import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ComparePage } from "./ComparePage";
import { makeRun, mockApi, renderApp } from "../test/helpers";
import type { Schemas } from "../api/client";

const BASE_ID = "01a12ea4-9534-77d7-b556-3d06d90fcd5b";
const CAND = makeRun();
const BASE = makeRun({ id: BASE_ID, prompt_version: 1, trigger: "manual" });

function metrics(
  evaluator: string,
  kind: Schemas["EvaluatorKind"],
  higher: boolean,
  mean: number,
  passRate: number | null,
): Schemas["EvaluatorMetrics"] {
  return {
    evaluator,
    version: 1,
    evaluator_version_id: `ev-${evaluator}`,
    kind,
    higher_is_better: higher,
    n: 30,
    mean,
    p50: mean,
    p95: mean,
    pass_rate: passRate,
    pass_n: 30,
  };
}

function comparison(over: Partial<Schemas["Comparison"]> = {}): Schemas["Comparison"] {
  return {
    run_id: CAND.id,
    baseline_run_id: BASE_ID,
    complete: true,
    evaluators: [
      {
        evaluator: "label-match",
        version: 1,
        higher_is_better: true,
        candidate: metrics("label-match", "exact_match", true, 0, 0),
        baseline: metrics("label-match", "exact_match", true, 1, 1),
        paired_n: 30,
        delta_mean: -1,
        ci95_low: -1,
        ci95_high: -0.8,
        p_value: 0.000001,
        method: "mcnemar-newcombe",
        delta_pass_rate: -1,
        significant: true,
        direction: "regressed",
      },
      {
        evaluator: "latency",
        version: 1,
        higher_is_better: false,
        candidate: metrics("latency", "latency", false, 5, 1),
        baseline: metrics("latency", "latency", false, 5, 1),
        paired_n: 30,
        delta_mean: 0,
        ci95_low: -2,
        ci95_high: 2,
        p_value: 0.4,
        method: "paired-t",
        delta_pass_rate: 0,
        significant: false,
        direction: "unchanged",
      },
    ],
    not_comparable: [],
    newly_failing: [
      {
        dataset_item_id: "01a12ea4-47c7-721c-a64b-8ed581ed471b",
        evaluator: "label-match",
        baseline_value: 1,
        candidate_value: 0,
      },
    ],
    newly_passing: [
      {
        dataset_item_id: "01a12ea4-47c7-7351-a840-25739351bd81",
        evaluator: "label-match",
        baseline_value: 0,
        candidate_value: 1,
      },
    ],
    ...over,
  };
}

function renderCompare(route: string) {
  return renderApp(
    <Routes>
      <Route path="/runs/:id/compare" element={<ComparePage />} />
    </Routes>,
    route,
  );
}

describe("ComparePage", () => {
  it("defaults to the run's baseline and shows regressed and unchanged evaluators", async () => {
    const run = { ...CAND, baseline_run_id: BASE_ID };
    const { calls } = mockApi({
      [`/v1/runs/${run.id}`]: run,
      "/v1/runs": { items: [run, BASE], next_cursor: null },
      [`/v1/runs/${run.id}/compare`]: comparison(),
    });
    renderCompare(`/runs/${run.id}/compare`);

    const regressed = (await screen.findByText("label-match@1")).closest("tr")!;
    expect(within(regressed).getByText("regressed")).toBeInTheDocument();
    expect(within(regressed).getByText("−1")).toBeInTheDocument(); // delta
    const bar = within(regressed).getByRole("img", { name: /regressed/ });
    expect(bar).toHaveAttribute("data-direction", "regressed");
    expect(bar.querySelector(".ci-zero")).not.toBeNull(); // the zero line is drawn

    const flat = screen.getByText("latency@1").closest("tr")!;
    expect(within(flat).getByText("unchanged")).toBeInTheDocument();
    expect(within(flat).getByRole("img")).toHaveAttribute("data-crosses-zero", "true");
    expect(within(flat).getByText("lower is better")).toBeInTheDocument();

    expect(calls.find((c) => c.path.endsWith("/compare"))?.search).toContain(
      `baseline_run_id=${BASE_ID}`,
    );
  });

  it("links newly failing and newly passing into the items view", async () => {
    const run = { ...CAND, baseline_run_id: BASE_ID };
    mockApi({
      [`/v1/runs/${run.id}`]: run,
      "/v1/runs": { items: [run, BASE], next_cursor: null },
      [`/v1/runs/${run.id}/compare`]: comparison(),
    });
    renderCompare(`/runs/${run.id}/compare`);

    const failing = await screen.findByTestId("newly-failing");
    const link = within(failing).getByRole("link", { name: "81ed471b" });
    expect(link).toHaveAttribute(
      "href",
      `/runs/${run.id}/items?failed_evaluator=label-match&focus=01a12ea4-47c7-721c-a64b-8ed581ed471b`,
    );
    expect(within(failing).getByRole("link", { name: /open in items/ })).toHaveAttribute(
      "href",
      `/runs/${run.id}/items?failed_evaluator=label-match`,
    );
    const passing = screen.getByTestId("newly-passing");
    expect(within(passing).getByRole("link", { name: "9351bd81" })).toHaveAttribute(
      "href",
      expect.stringContaining("status=succeeded&focus=01a12ea4-47c7-7351"),
    );
  });

  it("lets the user pick a baseline from runs with the same dataset version", async () => {
    const other = makeRun({ id: "01a12ea5-ffff-7abe-a474-a0fea4c485f4", dataset_version: 2 });
    const { calls } = mockApi({
      [`/v1/runs/${CAND.id}`]: CAND,
      "/v1/runs": { items: [CAND, BASE, other], next_cursor: null },
      [`/v1/runs/${CAND.id}/compare`]: comparison(),
    });
    renderCompare(`/runs/${CAND.id}/compare`);

    expect(await screen.findByText("Pick a baseline run")).toBeInTheDocument();
    const select = screen.getByLabelText(/Baseline run/);
    const options = within(select)
      .getAllByRole("option")
      .map((o) => o.textContent);
    expect(options).toHaveLength(2); // placeholder + the one run on dataset version 1
    expect(calls.some((c) => c.path.endsWith("/compare"))).toBe(false);

    await userEvent.selectOptions(select, BASE_ID);
    expect(await screen.findByText("label-match@1")).toBeInTheDocument();
    expect(calls.find((c) => c.path.endsWith("/compare"))?.search).toContain(BASE_ID);
  });

  it("warns when the comparison is incomplete and lists non-comparable evaluators", async () => {
    const run = { ...CAND, baseline_run_id: BASE_ID, status: "running" as const };
    mockApi({
      [`/v1/runs/${run.id}`]: run,
      "/v1/runs": { items: [run, BASE], next_cursor: null },
      [`/v1/runs/${run.id}/compare`]: comparison({ complete: false, not_comparable: ["tone"] }),
    });
    renderCompare(`/runs/${run.id}/compare`);
    expect(await screen.findByText(/Incomplete comparison/)).toBeInTheDocument();
    expect(screen.getByText(/Not comparable:/).parentElement).toHaveTextContent("tone");
  });

  it("shows the server's detail when the runs cannot be compared", async () => {
    const run = { ...CAND, baseline_run_id: BASE_ID };
    mockApi({
      [`/v1/runs/${run.id}`]: run,
      "/v1/runs": { items: [run, BASE], next_cursor: null },
      [`/v1/runs/${run.id}/compare`]: () =>
        new Response(JSON.stringify({ detail: "Runs used different dataset versions." }), {
          status: 422,
        }),
    });
    renderCompare(`/runs/${run.id}/compare`);
    expect(await screen.findByRole("alert")).toHaveTextContent("different dataset versions");
  });
});
