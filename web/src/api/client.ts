import createClient, { type Middleware } from "openapi-fetch";
import type { components, paths } from "./schema";
import { getApiKey, notifyUnauthorized } from "./session";

export type Schemas = components["schemas"];

/** An error carrying the server's `detail` message. */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

function describeIssue(issue: unknown): string {
  if (issue && typeof issue === "object" && "msg" in issue) {
    const { msg, loc } = issue as { msg: unknown; loc?: unknown };
    const where = Array.isArray(loc)
      ? loc.filter((p) => p !== "body" && p !== "query").join(".")
      : "";
    return where ? `${where}: ${String(msg)}` : String(msg);
  }
  return JSON.stringify(issue);
}

/** Pull a human-readable message out of a FastAPI error body. */
export function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) return detail.map(describeIssue).join("; ");
  }
  return fallback;
}

const auth: Middleware = {
  onRequest({ request }) {
    const key = getApiKey();
    if (key && !request.headers.has("Authorization")) {
      request.headers.set("Authorization", `Bearer ${key}`);
    }
    return request;
  },
  onResponse({ request, response }) {
    if (response.status === 401) {
      const sent = request.headers.get("Authorization");
      // Only a 401 on the stored key ends the session; a bad key at sign-in is handled by the form.
      if (sent && sent === `Bearer ${getApiKey()}`) notifyUnauthorized();
    }
    return response;
  },
};

export const api = createClient<paths>({
  baseUrl: typeof window === "undefined" ? "http://localhost" : window.location.origin,
  // Resolve fetch at call time so tests can stub it.
  fetch: (request) => globalThis.fetch(request),
});
api.use(auth);

interface Result<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/** Return the typed body of a successful call, or throw an ApiError with the server's detail. */
export async function unwrap<T>(call: Promise<Result<T>>): Promise<T> {
  let res: Result<T>;
  try {
    res = await call;
  } catch (e) {
    throw new ApiError(0, e instanceof Error ? `Network error: ${e.message}` : "Network error");
  }
  if (res.error !== undefined || res.data === undefined) {
    const status = res.response.status;
    throw new ApiError(status, detailOf(res.error, `Request failed (${status})`));
  }
  return res.data;
}
