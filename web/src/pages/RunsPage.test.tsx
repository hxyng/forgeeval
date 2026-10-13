import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { RunsPage } from "./RunsPage";
import { jsonError, makeRun, mockApi, renderApp } from "../test/helpers";

describe("RunsPage", () => {
  it("renders status, progress, dataset/prompt versions, trigger, short sha, age and cost", async () => {
    mockApi({
      "/v1/runs": {
        items: [
          makeRun(),
          makeRun({
            id: "01a12ea5-aaaa-7abe-a474-a0fea4c485f4",
            status: "running",
            trigger: "manual",
            git_sha: null,
            progress: {
              queued: 10,
              leased: 2,
              retry_wait: 0,
              succeeded: 18,
              dead_lettered: 0,
              cancelled: 0,
            },
          }),
        ],
        next_cursor: null,
      },
    });
    renderApp(<RunsPage />);

    const rows = await screen.findAllByRole("row");
    const done = within(rows[1]!);
    expect(done.getByText("succeeded")).toBeInTheDocument();
    expect(done.getByText("triage@1")).toBeInTheDocument();
    expect(done.getByText("triage@2")).toBeInTheDocument();
    expect(done.getByText("9585a58")).toBeInTheDocument();
    expect(done.getByText("ci")).toBeInTheDocument();
    expect(done.getByText("5 minutes ago")).toBeInTheDocument();
    expect(done.getByText("$0.000889")).toBeInTheDocument();
    expect(done.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "30");
    expect(done.getByRole("link")).toHaveAttribute(
      "href",
      "/runs/01a12ea5-133e-7abe-a474-a0fea4c485f4",
    );

    const live = within(rows[2]!);
    expect(live.getByText("running")).toBeInTheDocument();
    expect(live.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "18");
    expect(screen.getByText(/refreshing every 2 s/i)).toBeInTheDocument();
  });

  it("paginates with the cursor", async () => {
    const { calls } = mockApi({
      "/v1/runs": (url: URL) =>
        url.searchParams.get("cursor")
          ? {
              items: [makeRun({ id: "01a12ea5-bbbb-7abe-a474-a0fea4c485f4", dataset: "second" })],
              next_cursor: null,
            }
          : { items: [makeRun()], next_cursor: "cursor-1" },
    });
    renderApp(<RunsPage />);
    await userEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByText("second@1")).toBeInTheDocument();
    expect(calls.at(-1)?.search).toContain("cursor=cursor-1");
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    mockApi({ "/v1/runs": { items: [], next_cursor: null } });
    renderApp(<RunsPage />);
    expect(await screen.findByText("No runs yet")).toBeInTheDocument();
  });

  it("shows the server's detail on error", async () => {
    mockApi({ "/v1/runs": () => jsonError(500, "database is down") });
    renderApp(<RunsPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("database is down");
  });
});
