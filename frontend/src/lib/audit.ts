import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { inr } from "./format";

export interface AuditRow {
  id: number;
  at: string;
  action: string;
  entity: string | null;
  entity_id: number | null;
  detail: Record<string, unknown>;
  ip: string | null;
  user_email: string | null;
  user_name: string | null;
}

export const PAGE = 50;

export const ACTION_TEXT: Record<string, string> = {
  login: "Signed in",
  logout: "Signed out",
  "login.failed": "Failed sign-in",
  "login.locked": "Sign-in locked",
  "demo.load": "Loaded demo data",
  "upload.stage": "Uploaded a file",
  "upload.discard": "Discarded an upload",
  "mapping.suggest": "Asked for column suggestions",
  "mapping.approve": "Approved a column mapping",
  "mapping.rollback": "Rolled back a column mapping",
  "import.start": "Started an import",
  "rejects.download": "Downloaded rejected rows",
  "recon.run": "Ran the reconciliation",
  "review.accept": "Accepted a suggested match",
  "review.reject": "Rejected a suggested match",
  "review.link": "Linked a payment to an order",
  "review.undo": "Undid a link",
  "review.dismiss": "Dismissed a payment",
  "review.rerank": "Asked the AI for a second opinion",
  "rate_card.add": "Added a rate card",
  "claim.create": "Created a claim",
  "claim.add": "Added orders to a claim",
  "claim.remove": "Took an order out of a claim",
  "claim.discard": "Discarded a draft claim",
  "claim.submit": "Marked a claim as sent",
  "claim.recovered": "Recorded a claim as paid",
  "claim.partial": "Recorded a part payment",
  "claim.rejected": "Recorded a claim as rejected",
  "claim.written_off": "Wrote off a claim item",
  "claim.reopen": "Reopened a claim item",
  "finding.write_off": "Wrote off findings",
  "evidence.download": "Downloaded claim evidence",
};

export const actionText = (a: string): string =>
  ACTION_TEXT[a] ?? a.replace(/[._]/g, " ");

/** A detail value in words: paise as rupees, lists joined, anything nested summarised. */
export function detailValue(key: string, v: unknown): string {
  if (v === null || v === undefined || v === "") return "none";
  if (typeof v === "number" && key.endsWith("_paise")) return inr(v);
  if (Array.isArray(v) && v.length === 0) return "none";
  if (Array.isArray(v))
    return v.length > 8
      ? `${v.length} items`
      : v.map((x) => (typeof x === "object" ? "…" : String(x))).join(", ");
  if (typeof v === "object") return `${Object.keys(v as object).length} fields`;
  return String(v);
}

export function detailLabel(key: string): string {
  const t = key.replace(/_(paise|ids)$/, "").replace(/_/g, " ");
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** The screen an entry points at, when there is one. */
export function entityLink(
  r: Pick<AuditRow, "entity" | "entity_id">,
): string | null {
  if (r.entity === "claim" && r.entity_id)
    return `/claims?tab=claims&claim=${r.entity_id}`;
  if (r.entity === "partner" && r.entity_id)
    return `/rates?partner=${r.entity_id}`;
  return null;
}

/** One extra row is asked for so the screen knows whether there is a next page. */
export function useAudit(action: string, page: number) {
  return useQuery({
    queryKey: ["audit", action, page],
    queryFn: () =>
      api<AuditRow[]>("/audit", {
        query: { action, limit: PAGE + 1, offset: page * PAGE },
      }),
    placeholderData: keepPreviousData,
  });
}
