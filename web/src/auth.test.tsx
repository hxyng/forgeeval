import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { App } from "./App";
import { getApiKey, setApiKey } from "./api/session";
import { jsonError, mockApi, renderApp, WHOAMI } from "./test/helpers";

describe("API key flow", () => {
  it("shows the sign-in form when no key is stored", () => {
    mockApi({});
    renderApp(<App />);
    expect(screen.getByLabelText("API key")).toBeInTheDocument();
  });

  it("rejects a bad key with the server's detail and stores nothing", async () => {
    mockApi({ "/v1/whoami": () => jsonError(401, "Invalid API key") });
    renderApp(<App />);
    await userEvent.type(screen.getByLabelText("API key"), "fe_bad");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid API key");
    expect(getApiKey()).toBeNull();
    expect(window.localStorage.length).toBe(0);
  });

  it("validates with /v1/whoami, keeps the key in sessionStorage and shows project and scopes", async () => {
    const { calls } = mockApi({
      "/v1/whoami": WHOAMI,
      "/v1/runs": { items: [], next_cursor: null },
    });
    renderApp(<App />);
    await userEvent.type(screen.getByLabelText("API key"), "  fe_good  ");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByRole("heading", { name: "Runs" })).toBeInTheDocument();
    expect(window.sessionStorage.getItem("forgeeval.apiKey")).toBe("fe_good");
    expect(window.localStorage.length).toBe(0);
    expect(calls[0]).toMatchObject({ path: "/v1/whoami", auth: "Bearer fe_good" });
    // Authenticated requests carry the key.
    await waitFor(() => expect(calls.some((c) => c.path === "/v1/runs")).toBe(true));
    expect(calls.find((c) => c.path === "/v1/runs")?.auth).toBe("Bearer fe_good");

    await userEvent.click(screen.getByText(/^project/, { selector: "summary" }));
    expect(screen.getByText(WHOAMI.project_id)).toBeInTheDocument();
    expect(screen.getByText("runs:write")).toBeInTheDocument();
  });

  it("signs out and clears the key", async () => {
    setApiKey("fe_good");
    mockApi({ "/v1/whoami": WHOAMI, "/v1/runs": { items: [], next_cursor: null } });
    renderApp(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(getApiKey()).toBeNull();
  });

  it("returns to sign-in when any request gets a 401", async () => {
    setApiKey("fe_expired");
    mockApi({
      "/v1/whoami": WHOAMI,
      "/v1/runs": () => jsonError(401, "Invalid API key"),
    });
    renderApp(<App />);
    expect(await screen.findByLabelText("API key")).toBeInTheDocument();
    expect(screen.getByText(/session ended/i)).toBeInTheDocument();
    expect(getApiKey()).toBeNull();
  });
});
