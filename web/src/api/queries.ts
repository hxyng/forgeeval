import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { api, unwrap, type Schemas } from "./client";

export type RunStatus = Schemas["RunStatus"];
export type Run = Schemas["RunOut"];

export const ACTIVE: ReadonlySet<RunStatus> = new Set<RunStatus>(["queued", "running"]);
export const isActive = (s: RunStatus): boolean => ACTIVE.has(s);
export const POLL_MS = 2000;

export function useWhoAmI(key: string | null) {
  return useQuery({
    queryKey: ["whoami", key],
    queryFn: () => unwrap(api.GET("/v1/whoami")),
    enabled: key !== null,
    staleTime: 60_000,
  });
}

export function useRuns() {
  return useInfiniteQuery({
    queryKey: ["runs"],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      unwrap(api.GET("/v1/runs", { params: { query: { limit: 25, cursor: pageParam } } })),
    getNextPageParam: (last) => (last.items.length > 0 ? last.next_cursor : null),
    // Keep refreshing while any visible run is still in flight.
    refetchInterval: (q) =>
      q.state.data?.pages.some((p) => p.items.some((r) => isActive(r.status))) ? POLL_MS : false,
  });
}

export function useRun(id: string) {
  return useQuery({
    queryKey: ["run", id],
    queryFn: () => unwrap(api.GET("/v1/runs/{run_id}", { params: { path: { run_id: id } } })),
    refetchInterval: (q) => (q.state.data && isActive(q.state.data.status) ? POLL_MS : false),
  });
}

export function useRunMetrics(id: string, status: RunStatus | undefined) {
  return useQuery({
    // Including the status refetches once more when the run turns terminal.
    queryKey: ["run", id, "metrics", status],
    queryFn: () =>
      unwrap(api.GET("/v1/runs/{run_id}/metrics", { params: { path: { run_id: id } } })),
    enabled: status !== undefined,
    refetchInterval: status !== undefined && isActive(status) ? POLL_MS : false,
    placeholderData: keepPreviousData,
  });
}

export interface ItemFilters {
  status?: string;
  failed_evaluator?: string;
  tag?: string;
}

export function useRunItems(id: string, filters: ItemFilters) {
  return useInfiniteQuery({
    queryKey: ["run", id, "items", filters],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/runs/{run_id}/items", {
          params: {
            path: { run_id: id },
            query: {
              limit: 50,
              cursor: pageParam,
              status: (filters.status || undefined) as Schemas["ItemStatus"] | undefined,
              failed_evaluator: filters.failed_evaluator || undefined,
              tag: filters.tag || undefined,
            },
          },
        }),
      ),
    getNextPageParam: (last) => (last.items.length > 0 ? last.next_cursor : null),
  });
}

export function useCancelRun(id: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () =>
      unwrap(api.POST("/v1/runs/{run_id}/cancel", { params: { path: { run_id: id } } })),
    onSuccess: (run) => {
      qc.setQueryData(["run", id], run);
      void qc.invalidateQueries({ queryKey: ["run", id] });
      void qc.invalidateQueries({ queryKey: ["runs"] });
    },
  });
}

/** Recent runs for pickers (baseline, gate check, compare chooser). */
export function useRunChoices() {
  return useQuery({
    queryKey: ["runs", "choices"],
    queryFn: () => unwrap(api.GET("/v1/runs", { params: { query: { limit: 100 } } })),
    select: (page) => page.items,
  });
}

export function useComparison(id: string, baselineId: string | null) {
  return useQuery({
    queryKey: ["run", id, "compare", baselineId],
    queryFn: () =>
      unwrap(
        api.GET("/v1/runs/{run_id}/compare", {
          params: { path: { run_id: id }, query: { baseline_run_id: baselineId ?? "" } },
        }),
      ),
    enabled: baselineId !== null,
    retry: false,
  });
}

export function useGates() {
  return useQuery({
    queryKey: ["gates"],
    queryFn: () => unwrap(api.GET("/v1/gates")),
  });
}

export function useGateChecks(name: string, enabled: boolean) {
  return useQuery({
    queryKey: ["gates", name, "checks"],
    queryFn: () => unwrap(api.GET("/v1/gates/{name}/checks", { params: { path: { name } } })),
    enabled,
  });
}

export function useCheckAllGates() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => unwrap(api.POST("/v1/gates:check", { body: { run_id: runId } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["gates"] }),
  });
}

export function useEvaluators() {
  return useQuery({
    queryKey: ["evaluators"],
    queryFn: () => unwrap(api.GET("/v1/evaluators")),
    staleTime: 60_000,
  });
}

export function useDatasets() {
  return useQuery({
    queryKey: ["datasets"],
    queryFn: () => unwrap(api.GET("/v1/datasets")),
    staleTime: 30_000,
  });
}

export function useDatasetVersions(name: string) {
  return useQuery({
    queryKey: ["datasets", name, "versions"],
    queryFn: () => unwrap(api.GET("/v1/datasets/{name}/versions", { params: { path: { name } } })),
    enabled: name !== "",
  });
}

export function usePrompts() {
  return useQuery({
    queryKey: ["prompts"],
    queryFn: () => unwrap(api.GET("/v1/prompts")),
    staleTime: 30_000,
  });
}

export function usePromptVersions(name: string) {
  return useQuery({
    queryKey: ["prompts", name, "versions"],
    queryFn: () => unwrap(api.GET("/v1/prompts/{name}/versions", { params: { path: { name } } })),
    enabled: name !== "",
  });
}

export function useCreateRun() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: Schemas["RunIn"]) => unwrap(api.POST("/v1/runs", { body })),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["runs"] }),
  });
}

export interface TraceFilters {
  status?: string;
  model?: string;
}

export function useTraces(filters: TraceFilters) {
  return useInfiniteQuery({
    queryKey: ["traces", filters],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam }) =>
      unwrap(
        api.GET("/v1/traces", {
          params: {
            query: {
              limit: 50,
              cursor: pageParam,
              status: (filters.status || undefined) as Schemas["TraceStatus"] | undefined,
              model: filters.model || undefined,
            },
          },
        }),
      ),
    getNextPageParam: (last) => (last.items.length > 0 ? last.next_cursor : null),
  });
}

export function useTrace(id: string) {
  return useQuery({
    queryKey: ["trace", id],
    queryFn: () => unwrap(api.GET("/v1/traces/{trace_id}", { params: { path: { trace_id: id } } })),
  });
}
