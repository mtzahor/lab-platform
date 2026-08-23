import { useMutation, useQuery, useQueryClient, type QueryKey } from "@tanstack/react-query";
import { apiFetch, type ApiRecord } from "../api/client";
import { useLive } from "../app/LiveProvider";

const LIVE_SAFETY_POLL_MS = 30_000;

export function useApiList(
  key: string,
  path: string,
  enabled = true,
  loader?: () => Promise<ApiRecord>,
) {
  const live = useLive();
  return useQuery({
    queryKey: [key, path],
    queryFn: loader ?? (() => apiFetch<ApiRecord>(path)),
    enabled,
    staleTime: 10_000,
    refetchInterval: live.state === "live" ? LIVE_SAFETY_POLL_MS : live.pollingIntervalMs,
  });
}

export function useApiDetail(
  key: string,
  path: string | undefined,
  enabled = true,
  loader?: () => Promise<ApiRecord>,
) {
  const live = useLive();
  return useQuery({
    queryKey: [key, path],
    queryFn: loader ?? (() => apiFetch<ApiRecord>(path!)),
    enabled: enabled && Boolean(path),
    staleTime: 5_000,
    refetchInterval: live.state === "live" ? LIVE_SAFETY_POLL_MS : live.pollingIntervalMs,
  });
}

export function useApiMutation<TVariables = void>(
  mutation: (variables: TVariables) => Promise<unknown>,
  invalidate: QueryKey[] = [],
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: mutation,
    onSuccess: async () => {
      await Promise.all(invalidate.map((queryKey) => queryClient.invalidateQueries({ queryKey })));
    },
  });
}
