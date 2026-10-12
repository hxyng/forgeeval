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
