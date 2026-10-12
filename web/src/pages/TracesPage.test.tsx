import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { TraceDetailPage, TracesPage } from "./TracesPage";
import { mockApi, renderApp } from "../test/helpers";
import type { Schemas } from "../api/client";

const TRACE: Schemas["TraceOut"] = {
  id: "01a12ea4-aaaa-7abe-a474-a0fea4c485f4",
  client_id: "c-1",
  model: "gpt-test",
  prompt_version_id: null,
  status: "error",
  latency_ms: 1200,
  input_tokens: 10,
  output_tokens: null,
  cost_usd: "0.0021",
  started_at: new Date(Date.now() - 90_000).toISOString(),
  ingested_at: new Date().toISOString(),
};

describe("TracesPage", () => {
  it("lists traces and sends status and model filters", async () => {
    const { calls } = mockApi({ "/v1/traces": { items: [TRACE], next_cursor: "next" } });
    renderApp(<TracesPage />);
    const row = (await screen.findByText("gpt-test")).closest("tr")!;
    expect(within(row).getByText("error")).toBeInTheDocument();
    expect(within(row).getByText("1.2 s")).toBeInTheDocument();
    expect(within(row).getByText("10 / —")).toBeInTheDocument();
    expect(within(row).getByText("1 minute ago")).toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Status"), "error");
    await userEvent.type(screen.getByLabelText("Model"), "gpt-test{enter}");
    await screen.findByText("gpt-test");
    const last = calls.filter((c) => c.path === "/v1/traces").at(-1)!;
    expect(last.search).toContain("status=error");
    expect(last.search).toContain("model=gpt-test");
  });

  it("paginates with the cursor", async () => {
    const { calls } = mockApi({
      "/v1/traces": (url: URL) =>
        url.searchParams.get("cursor")
          ? {
              items: [{ ...TRACE, id: "01a12ea4-bbbb-7abe-a474-a0fea4c485f4", model: "second" }],
              next_cursor: null,
            }
          : { items: [TRACE], next_cursor: "cur-2" },
    });
    renderApp(<TracesPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("second")).toBeInTheDocument();
    expect(calls.at(-1)?.search).toContain("cursor=cur-2");
  });

  it("shows an empty state", async () => {
    mockApi({ "/v1/traces": { items: [], next_cursor: null } });
    renderApp(<TracesPage />);
    expect(await screen.findByText("No traces captured yet")).toBeInTheDocument();
  });
});

describe("TraceDetailPage", () => {
  it("shows input/output JSON and an indented span tree", async () => {
    const t0 = Date.parse("2026-10-12T12:00:00Z");
    const detail: Schemas["TraceDetailOut"] = {
      ...TRACE,
      status: "ok",
      redaction: {},
      otel_trace_id: null,
      input: { q: "hello" },
      output: { text: "world" },
      spans: [
        {
          id: "s2",
          parent_span_id: "s1",
          kind: "llm",
          name: "chat.completion",
          started_at: new Date(t0 + 10).toISOString(),
          duration_ms: 80,
          attributes: {},
        },
        {
          id: "s1",
          parent_span_id: null,
          kind: "other",
          name: "handle_request",
          started_at: new Date(t0).toISOString(),
          duration_ms: 100,
          attributes: {},
        },
      ],
    };
    mockApi({ [`/v1/traces/${TRACE.id}`]: detail });
    renderApp(
      <Routes>
        <Route path="/traces/:id" element={<TraceDetailPage />} />
      </Routes>,
      `/traces/${TRACE.id}`,
    );
    expect(await screen.findByRole("region", { name: "Input" })).toHaveTextContent('"q": "hello"');
    expect(screen.getByRole("region", { name: "Output" })).toHaveTextContent('"text": "world"');
    const items = within(screen.getByTestId("span-tree")).getAllByRole("listitem");
    expect(items.map((i) => i.getAttribute("data-depth"))).toEqual(["0", "1"]);
    expect(items[0]).toHaveTextContent("handle_request");
    expect(items[1]).toHaveTextContent("chat.completion");
  });
});
