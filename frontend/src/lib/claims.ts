import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query";
import { api } from "./api";
import type { Deadline } from "./types";
import { useRefreshAll } from "./queries";

/** Everything the Claims screen needs: types, calls, and the small pure rules (which findings can
    be picked together, how rupees typed by a person become paise). */

export type Urgency = "expired" | "critical" | "soon" | "ok";
export type ClaimStatus = "draft" | "submitted" | "closed";
export type Outcome =
  | "pending"
  | "recovered"
  | "partial"
  | "rejected"
  | "written_off"
  | "withdrawn";

export interface Candidate {
  id: number;
  order_id: number;
  order_ref: string;
  partner_id: number;
  partner_code: string;
  partner_name: string;
  type: string;
  amount_paise: number;
  since_on: string;
  claim_deadline: string;
  status: string;
  days_left: number;
  urgency: Urgency;
  claim_id: number | null;
}
export interface ClaimRow {
  id: number;
  status: ClaimStatus;
  partner_ref: string | null;
  note: string | null;
  submitted_on: string | null;
  created_at: string;
  partner_code: string;
  partner_name: string;
  items: number;
  claimed_paise: number | string;
  recovered_paise: number | string;
  pending_paise: number | string;
  nearest_deadline: string | null;
  days_left: number | null;
}
export interface ClaimItem {
  id: number;
  finding_id: number;
  order_ref: string;
  type: string;
  claimed_paise: number;
  outcome: Outcome;
  recovered_paise: number;
  note: string | null;
  since_on: string;
  claim_deadline: string;
  days_left: number;
}
export interface ClaimDetail {
  id: number;
  partner_id: number;
  status: ClaimStatus;
  partner_ref: string | null;
  note: string | null;
  created_at: string;
  submitted_on: string | null;
  closed_at: string | null;
  items: ClaimItem[];
}
export interface FindingEvent {
  at: string;
  user_id: number | null;
  kind: string;
  detail: Record<string, unknown>;
}

type Q = Record<string, string | number | boolean | undefined>;

export const useCandidates = (query: Q) =>
  useQuery({
    queryKey: ["claims", "candidates", query],
    queryFn: () => api<Candidate[]>("/claims/candidates", { query }),
    placeholderData: keepPreviousData,
  });

export const useClaims = (status: string) =>
  useQuery({
    queryKey: ["claims", "list", status],
    queryFn: () =>
      api<ClaimRow[]>("/claims", {
        query: { status: status || undefined, limit: 200 },
      }),
    placeholderData: keepPreviousData,
  });

export const useClaim = (id: number | null) =>
  useQuery({
    queryKey: ["claims", "one", id],
    queryFn: () => api<ClaimDetail>(`/claims/${id}`),
    enabled: id !== null,
  });

export const useDraftClaims = (partnerId: number, enabled: boolean) =>
  useQuery({
    queryKey: ["claims", "drafts", partnerId],
    queryFn: () =>
      api<ClaimRow[]>("/claims", {
        query: { status: "draft", partner_id: partnerId, limit: 50 },
      }),
    enabled,
  });

export const useFindingHistory = (id: number, enabled: boolean) =>
  useQuery({
    queryKey: ["claims", "history", id],
    queryFn: () => api<FindingEvent[]>(`/claims/findings/${id}/history`),
    enabled,
  });

/** A change to claims changes the Overview numbers too, so everything refreshes. */
export function useClaimAction<V = void, R = unknown>(
  call: (v: V) => Promise<R>,
) {
  const refresh = useRefreshAll();
  return useMutation({ mutationFn: call, onSuccess: refresh });
}

export const claimsApi = {
  create: (v: { partner_id: number; finding_ids: number[]; note?: string }) =>
    api<{ id: number }>("/claims", {
      method: "POST",
      body: { ...v, note: v.note || null },
    }),
  writeOff: (v: { finding_ids: number[]; note?: string }) =>
    api<{ written_off: number }>("/claims/write-off", {
      method: "POST",
      body: { ...v, note: v.note || null },
    }),
  submit: (v: { id: number; partner_ref?: string }) =>
    api<{ warnings: string[] }>(`/claims/${v.id}/submit`, {
      method: "POST",
      body: { partner_ref: v.partner_ref || null },
    }),
  addItems: (v: { claim: number; finding_ids: number[] }) =>
    api(`/claims/${v.claim}/items`, {
      method: "POST",
      body: { finding_ids: v.finding_ids },
    }),
  discard: (id: number) => api(`/claims/${id}`, { method: "DELETE" }),
  remove: (v: { claim: number; finding: number }) =>
    api(`/claims/${v.claim}/items/${v.finding}`, { method: "DELETE" }),
  outcome: (v: {
    item: number;
    outcome: string;
    recovered_paise?: number;
    note?: string;
  }) =>
    api(`/claims/items/${v.item}/outcome`, {
      method: "POST",
      body: {
        outcome: v.outcome,
        recovered_paise: v.recovered_paise ?? null,
        note: v.note || null,
      },
    }),
  reopen: (item: number) =>
    api(`/claims/items/${item}/reopen`, { method: "POST" }),
};

// ---- small pure rules -----------------------------------------------------------------------------

export const URGENCIES: { key: Urgency; title: string; hint: string }[] = [
  { key: "critical", title: "Closes within 7 days", hint: "Send these first" },
  { key: "soon", title: "Closes within 14 days", hint: "Next in line" },
  { key: "ok", title: "More than 14 days left", hint: "Plenty of time" },
  { key: "expired", title: "Past the claim window", hint: "May be refused" },
];

/** Rupees typed by a person ("1,299.50") to integer paise. Anything else is null. */
export function rupeesToPaise(text: string): number | null {
  const m = /^(\d+)(?:\.(\d{1,2}))?$/.exec(text.trim().replace(/,/g, ""));
  if (!m) return null;
  return Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0"));
}

/** Totals per urgency, for one partner (by code) or for all. */
export function urgencyTotals(
  rows: Deadline[],
  partnerCode?: string,
): Record<Urgency, { findings: number; paise: number }> {
  const out: Record<Urgency, { findings: number; paise: number }> = {
    expired: { findings: 0, paise: 0 },
    critical: { findings: 0, paise: 0 },
    soon: { findings: 0, paise: 0 },
    ok: { findings: 0, paise: 0 },
  };
  for (const r of rows) {
    if (partnerCode && r.partner_code !== partnerCode) continue;
    out[r.urgency].findings += r.findings;
    out[r.urgency].paise += Number(r.amount_paise);
  }
  return out;
}

/** The view to open first: the most urgent group that can still be claimed, else everything. */
export function firstBucket(
  t: Record<Urgency, { findings: number }>,
): Urgency | "" {
  for (const k of ["critical", "soon", "ok"] as const)
    if (t[k].findings > 0) return k;
  return "";
}

/** A claim covers one partner, so once something is picked only that partner's findings can be. */
export function canPick(
  row: Pick<Candidate, "partner_id" | "claim_id">,
  pickedPartner: number | null,
): boolean {
  if (row.claim_id !== null) return false;
  return pickedPartner === null || pickedPartner === row.partner_id;
}

export const OUTCOME_TEXT: Record<Outcome, string> = {
  pending: "Waiting for the partner",
  recovered: "Paid in full",
  partial: "Part paid",
  rejected: "Rejected",
  written_off: "Written off",
  withdrawn: "Not needed",
};

export const STATUS_TEXT: Record<ClaimStatus, string> = {
  draft: "Draft",
  submitted: "Sent",
  closed: "Closed",
};

export const EVENT_TEXT: Record<string, string> = {
  claimed: "Sent to the partner",
  recovered: "Paid in full",
  partial: "Part paid",
  rejected: "Rejected by the partner",
  written_off: "Written off",
  outcome_undone: "Outcome undone",
  paid_by_partner: "Money arrived in a later file",
};
