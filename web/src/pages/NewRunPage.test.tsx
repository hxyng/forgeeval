import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { NewRunPage } from "./NewRunPage";
import { jsonError, makeRun, mockApi, renderApp } from "../test/helpers";

const REGISTRY = {
  "/v1/datasets": [
    {
      id: "d1",
      name: "triage",
      description: null,
      latest_version: 2,
      created_at: "2026-10-12T00:00:00Z",
    },
  ],
  "/v1/datasets/triage/versions": [
    {
      id: "dv1",
      dataset: "triage",
      version: 1,
      item_count: 30,
      content_hash: "x",
      frozen_at: null,
      created_at: "2026-10-12T00:00:00Z",
    },
    {
      id: "dv2",
      dataset: "triage",
      version: 2,
      item_count: 31,
      content_hash: "y",
      frozen_at: null,
      created_at: "2026-10-12T00:00:00Z",
    },
  ],
  "/v1/prompts": [{ name: "triage", latest_version: 2, versions: 2 }],
  "/v1/prompts/triage/versions": [
    {
      id: "p1",
      name: "triage",
      version: 1,
      template: "t",
      model: "fake/oracle",
      params: {},
      content_hash: "h",
      git_sha: null,
      created_at: "2026-10-12T00:00:00Z",
    },
  ],
  "/v1/evaluators": [
    {
      id: "e1",
      name: "label-match",
      kind: "exact_match",
      higher_is_better: true,
      latest_version: 1,
    },
    { id: "e2", name: "latency", kind: "latency", higher_is_better: false, latest_version: 1 },
  ],
  "/v1/runs": { items: [makeRun({ dataset_version: 2 })], next_cursor: null },
};

function renderForm() {
  return renderApp(
    <Routes>
      <Route path="/runs/new" element={<NewRunPage />} />
      <Route path="/runs/:id" element={<div>run page</div>} />
    </Routes>,
    "/runs/new",
  );
}

describe("NewRunPage", () => {
  it("validates required fields before calling the API", async () => {
    const { calls } = mockApi(REGISTRY);
    renderForm();
    await userEvent.click(await screen.findByRole("button", { name: "Start run" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Choose a dataset");
    expect(calls.some((c) => c.method === "POST")).toBe(false);
  });

  it("posts the chosen dataset, prompt, evaluators and ratio, then opens the run", async () => {
    let body: unknown;
    const created = makeRun({ id: "01a12ea5-9999-7abe-a474-a0fea4c485f4", status: "queued" });
    mockApi({
      ...REGISTRY,
      "POST /v1/runs": async (_url: URL, req: Request) => {
        body = await req.json();
        return created;
      },
    });
    renderForm();
    await userEvent.selectOptions(await screen.findByLabelText("Dataset"), "triage");
    await userEvent.selectOptions(screen.getByLabelText("Dataset version"), "1");
    await userEvent.selectOptions(screen.getByLabelText("Prompt"), "triage");
    await userEvent.click(screen.getByRole("checkbox", { name: /label-match/ }));
    await userEvent.click(screen.getByRole("checkbox", { name: /latency/ }));
    const ratio = screen.getByLabelText("Max failure ratio");
    await userEvent.clear(ratio);
    await userEvent.type(ratio, "0.1");
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));

    expect(await screen.findByText("run page")).toBeInTheDocument();
    expect(body).toMatchObject({
      dataset: { name: "triage", version: 1 },
      prompt: { name: "triage", version: null },
      evaluators: [
        { name: "label-match", version: null },
        { name: "latency", version: null },
      ],
      baseline_run_id: null,
      max_failure_ratio: 0.1,
      trigger: "manual",
    });
  });

  it("offers only baselines on the chosen dataset version", async () => {
    mockApi(REGISTRY);
    renderForm();
    await userEvent.selectOptions(await screen.findByLabelText("Dataset"), "triage");
    const baseline = screen.getByLabelText("Baseline run (optional)");
    // Latest is v2 and the one known run used v2.
    expect(baseline.querySelectorAll("option")).toHaveLength(2);
    await userEvent.selectOptions(screen.getByLabelText("Dataset version"), "1");
    expect(baseline.querySelectorAll("option")).toHaveLength(1);
  });

  it("shows the server's detail when creation fails", async () => {
    mockApi({ ...REGISTRY, "POST /v1/runs": () => jsonError(422, "Unknown evaluator 'x'") });
    renderForm();
    await userEvent.selectOptions(await screen.findByLabelText("Dataset"), "triage");
    await userEvent.selectOptions(screen.getByLabelText("Prompt"), "triage");
    await userEvent.click(screen.getByRole("checkbox", { name: /label-match/ }));
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Unknown evaluator");
  });
});
