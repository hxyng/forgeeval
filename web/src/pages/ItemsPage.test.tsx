import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { ItemsPage } from "./ItemsPage";
import { makeRun, mockApi, renderApp } from "../test/helpers";
import type { Schemas } from "../api/client";

const RUN = makeRun();

const FAILING: Schemas["RunItemOut"] = {
  id: "item-1",
  dataset_item_id: "01a12ea4-47c7-721c-a64b-8ed581ed471b",
  status: "succeeded",
  attempt: 2,
  input: { subject: "Refund please", body: "I was charged twice" },
  expected_output: "billing",
  context: null,
  tags: ["billing"],
  output: "I was charged twice",
  latency_ms: 5,
  cost_usd: "0.00003",
  last_error: "timeout after 30s",
  scores: [
    {
      evaluator: "label-match",
      version: 1,
      value: "0",
      passed: false,
      rationale: "expected billing, got something else",
    },
    { evaluator: "latency", version: 1, value: "5", passed: true, rationale: null },
  ],
};

function renderItems(route: string) {
  return renderApp(
    <Routes>
      <Route path="/runs/:id/items" element={<ItemsPage />} />
    </Routes>,
    route,
  );
}

describe("ItemsPage", () => {
  it("expands a row to show input, expected, actual, scores, error and attempts", async () => {
    mockApi({
      [`/v1/runs/${RUN.id}`]: RUN,
      [`/v1/runs/${RUN.id}/items`]: { items: [FAILING], next_cursor: null },
    });
    renderItems(`/runs/${RUN.id}/items?failed_evaluator=label-match`);

    const toggle = await screen.findByRole("button", { name: /expand item 81ed471b/i });
    expect(screen.queryByTestId("item-detail")).not.toBeInTheDocument();
    await userEvent.click(toggle);

    const detail = screen.getByTestId("item-detail");
    expect(within(detail).getByRole("region", { name: "Input" })).toHaveTextContent(
      '"subject": "Refund please"',
    );
    expect(within(detail).getByRole("region", { name: "Expected" })).toHaveTextContent("billing");
    expect(within(detail).getByRole("region", { name: "Actual output" })).toHaveTextContent(
      "I was charged twice",
    );
    expect(within(detail).getByText("expected billing, got something else")).toBeInTheDocument();
    expect(within(detail).getByText("fail")).toBeInTheDocument();
    expect(within(detail).getByText("pass")).toBeInTheDocument();
    expect(within(detail).getByText("timeout after 30s")).toBeInTheDocument();
    expect(within(detail).getByText("Attempts").nextSibling).toHaveTextContent("2");

    await userEvent.click(screen.getByRole("button", { name: /collapse item/i }));
    expect(screen.queryByTestId("item-detail")).not.toBeInTheDocument();
  });

  it("sends the filters from the URL and offers the run's evaluators", async () => {
    const { calls } = mockApi({
      [`/v1/runs/${RUN.id}`]: RUN,
      [`/v1/runs/${RUN.id}/items`]: { items: [FAILING], next_cursor: null },
    });
    renderItems(`/runs/${RUN.id}/items?failed_evaluator=label-match&status=succeeded&tag=billing`);
    await screen.findByRole("button", { name: /expand item/i });
    const itemCall = calls.find((c) => c.path.endsWith("/items"))!;
    expect(itemCall.search).toContain("failed_evaluator=label-match");
    expect(itemCall.search).toContain("status=succeeded");
    expect(itemCall.search).toContain("tag=billing");
    const select = screen.getByLabelText("Failed evaluator");
    expect(within(select).getByRole("option", { name: "latency" })).toBeInTheDocument();
  });

  it("changing the evaluator filter refetches", async () => {
    const { calls } = mockApi({
      [`/v1/runs/${RUN.id}`]: RUN,
      [`/v1/runs/${RUN.id}/items`]: { items: [FAILING], next_cursor: null },
    });
    renderItems(`/runs/${RUN.id}/items`);
    await screen.findByRole("button", { name: /expand item/i });
    await userEvent.selectOptions(screen.getByLabelText("Failed evaluator"), "latency");
    await screen.findByRole("button", { name: /expand item/i });
    expect(calls.filter((c) => c.path.endsWith("/items")).at(-1)?.search).toContain(
      "failed_evaluator=latency",
    );
  });

  it("shows an empty state when filters match nothing", async () => {
    mockApi({
      [`/v1/runs/${RUN.id}`]: RUN,
      [`/v1/runs/${RUN.id}/items`]: { items: [], next_cursor: null },
    });
    renderItems(`/runs/${RUN.id}/items?tag=nope`);
    expect(await screen.findByText("No items match these filters")).toBeInTheDocument();
  });
});
