import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { RunDetailPage } from "./RunDetailPage";
import { makeRun, mockApi, renderApp } from "../test/helpers";

const METRICS = {
  run_id: "r",
  status: "succeeded",
  complete: true,
  evaluators: [
    {
      evaluator: "label-match",
      version: 1,
      evaluator_version_id: "ev-1",
      kind: "exact_match",
      higher_is_better: true,
      n: 30,
      mean: 0.5,
      p50: 0,
      p95: 1,
      pass_rate: 0.5,
      pass_n: 30,
    },
    {
      evaluator: "latency",
      version: 1,
      evaluator_version_id: "ev-2",
      kind: "latency",
      higher_is_better: false,
      n: 30,
      mean: 5.2,
      p50: 5,
      p95: 1500,
      pass_rate: 1,
      pass_n: 30,
    },
  ],
};

function renderDetail(id: string) {
  return renderApp(
    <Routes>
      <Route path="/runs/:id" element={<RunDetailPage />} />
    </Routes>,
    `/runs/${id}`,
  );
}

describe("RunDetailPage", () => {
  it("shows facts, polarity-aware metrics and a link to failing cases", async () => {
    const run = makeRun();
    mockApi({ [`/v1/runs/${run.id}`]: run, [`/v1/runs/${run.id}/metrics`]: METRICS });
    renderDetail(run.id);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    const accuracy = within(rows[1]!);
    expect(accuracy.getByText("50%")).toBeInTheDocument();
    expect(accuracy.getByText("(15/30)")).toBeInTheDocument();
    expect(accuracy.getByRole("link", { name: "15 failing cases" })).toHaveAttribute(
      "href",
      `/runs/${run.id}/items?failed_evaluator=label-match`,
    );
    const latency = within(rows[2]!);
    expect(latency.getByText("5.2 ms")).toBeInTheDocument();
    expect(latency.getByText("1.5 s")).toBeInTheDocument();
    expect(latency.getByText("lower is better")).toBeInTheDocument();
    expect(screen.getByText("$0.000889")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /cancel run/i })).not.toBeInTheDocument();
  });

  it("offers cancel for an active run and posts to the cancel endpoint", async () => {
    const run = makeRun({
      status: "running",
      finished_at: null,
      progress: {
        queued: 10,
        leased: 0,
        retry_wait: 0,
        succeeded: 20,
        dead_lettered: 0,
        cancelled: 0,
      },
    });
    let cancelled = false;
    const { calls } = mockApi({
      [`/v1/runs/${run.id}`]: () => (cancelled ? { ...run, status: "cancelled" } : run),
      [`/v1/runs/${run.id}/metrics`]: { ...METRICS, status: "running", complete: false },
      [`POST /v1/runs/${run.id}/cancel`]: () => {
        cancelled = true;
        return { ...run, status: "cancelled" };
      },
    });
    renderDetail(run.id);

    expect(await screen.findByText(/polling every 2 s/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /cancel run/i }));
    expect(await screen.findByText("cancelled", { selector: ".pill" })).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST" && c.path.endsWith("/cancel"))).toBe(true);
  });
});
