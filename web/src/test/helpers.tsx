import { render } from "@testing-library/react";
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { vi } from "vitest";
import { AuthProvider } from "../auth";
import { makeQueryClient } from "../queryClient";
import type { Schemas } from "../api/client";

type Handler = (url: URL, init: Request) => unknown | Response;

/** Stub global fetch with path-keyed handlers. Returns the call log. */
export function mockApi(routes: Record<string, Handler | unknown>) {
  const calls: { path: string; search: string; method: string; auth: string | null }[] = [];
  const fn = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = input instanceof Request ? input : new Request(input, init);
    const url = new URL(req.url);
    calls.push({
      path: url.pathname,
      search: url.search,
      method: req.method,
      auth: req.headers.get("Authorization"),
    });
    const hit = routes[`${req.method} ${url.pathname}`] ?? routes[url.pathname];
    if (hit === undefined) {
      return new Response(JSON.stringify({ detail: `no mock for ${url.pathname}` }), {
        status: 404,
      });
    }
    const body = typeof hit === "function" ? (hit as Handler)(url, req) : hit;
    if (body instanceof Response) return body;
    return new Response(JSON.stringify(body), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  });
  vi.stubGlobal("fetch", fn);
  return { calls, fn };
}

export function jsonError(status: number, detail: unknown): Response {
  return new Response(JSON.stringify({ detail }), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export function renderApp(ui: ReactElement, route = "/") {
  const client = makeQueryClient();
  client.setDefaultOptions({
    queries: { retry: false, staleTime: 0, refetchOnWindowFocus: false },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter
        initialEntries={[route]}
        future={{ v7_startTransition: true, v7_relativeSplatPath: true }}
      >
        <AuthProvider>{ui}</AuthProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

export const WHOAMI: Schemas["WhoAmI"] = {
  project_id: "01a12ea4-1292-7317-a4dc-e9c60e47d985",
  key_id: "key-1",
  scopes: ["project:read", "runs:write"],
};

export function makeRun(over: Partial<Schemas["RunOut"]> = {}): Schemas["RunOut"] {
  return {
    id: "01a12ea5-133e-7abe-a474-a0fea4c485f4",
    status: "succeeded",
    trigger: "ci",
    dataset: "triage",
    dataset_version: 1,
    prompt: "triage",
    prompt_version: 2,
    evaluators: [
      { name: "label-match", version: 1, evaluator_version_id: "ev-1" },
      { name: "latency", version: 1, evaluator_version_id: "ev-2" },
    ],
    baseline_run_id: null,
    total_items: 30,
    progress: {
      queued: 0,
      leased: 0,
      retry_wait: 0,
      succeeded: 30,
      dead_lettered: 0,
      cancelled: 0,
    },
    cost_usd: "0.000889",
    use_cache: true,
    cache_hits: 0,
    git_sha: "9585a58d1f2c",
    created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    started_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    finished_at: new Date(Date.now() - 4 * 60_000).toISOString(),
    early_stop: false,
    stopped_early_at: null,
    early_stop_reason: null,
    ...over,
  };
}
