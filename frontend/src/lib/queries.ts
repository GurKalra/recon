import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import type {
  Dashboard,
  DemoStatus,
  Deadline,
  ReconStatus,
  TaxRow,
} from "./types";

export const useDashboard = () =>
  useQuery({
    queryKey: ["dashboard"],
    queryFn: () => api<Dashboard>("/dashboard"),
  });
export const useTax = () =>
  useQuery({ queryKey: ["tax"], queryFn: () => api<TaxRow[]>("/tax") });
export const useDeadlines = () =>
  useQuery({
    queryKey: ["deadlines"],
    queryFn: () => api<Deadline[]>("/claims/deadlines"),
  });
export const useReconStatus = () =>
  useQuery({
    queryKey: ["recon-status"],
    queryFn: () => api<ReconStatus>("/recon/status"),
  });
export const useDemoStatus = () =>
  useQuery({
    queryKey: ["demo-status"],
    queryFn: () => api<DemoStatus>("/demo/status"),
  });

/** Anything that changes the books invalidates every number on screen. */
export function useRefreshAll() {
  const qc = useQueryClient();
  return () => qc.invalidateQueries();
}

export function useRunRecon() {
  const refresh = useRefreshAll();
  return useMutation({
    mutationFn: () => api("/recon/run", { method: "POST" }),
    onSuccess: refresh,
  });
}

export function useLoadDemo() {
  const refresh = useRefreshAll();
  return useMutation({
    mutationFn: () => api("/demo/load", { method: "POST" }),
    onSuccess: refresh,
  });
}

import { keepPreviousData } from "@tanstack/react-query";
import type {
  OrderDetail,
  OrderRow,
  Page,
  Partner,
  ReturnsPage,
} from "./types";

export const usePartners = () =>
  useQuery({
    queryKey: ["partners"],
    queryFn: () => api<Partner[]>("/partners"),
    staleTime: 300_000,
  });

export function useOrders(
  query: Record<string, string | number | boolean | undefined>,
  enabled = true,
) {
  return useQuery({
    queryKey: ["orders", query],
    queryFn: () => api<Page<OrderRow>>("/orders", { query }),
    placeholderData: keepPreviousData,
    enabled,
  });
}

export const useOrder = (id: number | null) =>
  useQuery({
    queryKey: ["order", id],
    queryFn: () => api<OrderDetail>(`/orders/${id}`),
    enabled: id !== null,
  });

export function useReturns(query: Record<string, string | number | undefined>) {
  return useQuery({
    queryKey: ["returns", query],
    queryFn: () => api<ReturnsPage>("/returns", { query }),
    placeholderData: keepPreviousData,
  });
}

import type { ReviewDetail, ReviewGroup, ReviewSummary } from "./types";

export const useReviewSummary = () =>
  useQuery({
    queryKey: ["review", "summary"],
    queryFn: () => api<ReviewSummary>("/review/summary"),
  });

export function useReviewQueue(
  query: Record<string, string | number | undefined>,
) {
  return useQuery({
    queryKey: ["review", "queue", query],
    queryFn: () =>
      api<{ total: number; rows: ReviewGroup[] }>("/review/queue", { query }),
    placeholderData: keepPreviousData,
  });
}

export const useReviewDetail = (id: number, enabled: boolean) =>
  useQuery({
    queryKey: ["review", "group", id],
    queryFn: () => api<ReviewDetail>(`/review/groups/${id}`),
    enabled,
  });

/** Every decision changes what other screens show, so refresh them all. */
export function useReviewAction<V = void>(call: (v: V) => Promise<unknown>) {
  const refresh = useRefreshAll();
  return useMutation({ mutationFn: call, onSuccess: refresh });
}

export const reviewApi = {
  accept: (id: number) =>
    api(`/review/suggestions/${id}/accept`, { method: "POST" }),
  reject: (id: number) =>
    api(`/review/suggestions/${id}/reject`, { method: "POST" }),
  link: (v: { group: number; order: number }) =>
    api(`/review/groups/${v.group}/link`, {
      method: "POST",
      body: { order_id: v.order },
    }),
  undo: (id: number) => api(`/review/groups/${id}/undo`, { method: "POST" }),
  dismiss: (id: number) =>
    api(`/review/groups/${id}/dismiss`, { method: "POST", body: {} }),
  rerank: (ids: number[]) =>
    api<{ asked?: number }>("/review/rerank", {
      method: "POST",
      body: { group_ids: ids },
    }),
};
