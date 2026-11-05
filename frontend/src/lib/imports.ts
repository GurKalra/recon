import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

/** Everything the Imports screen needs from the API, plus the small pure helpers that let a person
    correct a suggested mapping column by column. */

export const SOURCE_KINDS = [
  {
    value: "orders",
    label: "Orders",
    hint: "Your order export, one row per order",
  },
  {
    value: "settlement",
    label: "Settlement or payout",
    hint: "What a marketplace or gateway paid you",
  },
  {
    value: "cod_remittance",
    label: "COD remittance",
    hint: "Cash a courier collected and passed on",
  },
  {
    value: "freight_invoice",
    label: "Freight invoice",
    hint: "What a courier billed you",
  },
  {
    value: "warehouse_receipt",
    label: "Warehouse receipts",
    hint: "Returns received back at the warehouse",
  },
] as const;

export type Obj = Record<string, unknown>;

export interface UploadColumn {
  name: string;
  looks_like: string;
  filled: number;
  distinct: number;
  samples: string[];
  sensitive: boolean;
}
export interface Detected {
  source_id: number;
  name: string;
  kind: string;
  partner_id: number;
  partner_code: string;
  partner_name: string;
  version: number;
}
export interface UploadInfo {
  id: number;
  filename: string;
  kind: string;
  file_bytes: number;
  created_at: string;
  profile: {
    header_row: number;
    rows: number;
    file_type: string;
    fingerprint: string;
    columns: UploadColumn[];
  };
  detected: Detected[];
}
export interface MappingNote {
  field: string;
  value: string;
  confidence: "high" | "medium" | "low";
  reason: string;
}
export type AiStatus = "off" | "no_key" | "not_needed" | "used" | "asked_kept" | "unavailable";
export interface Suggestion {
  ai_status: AiStatus;
  mapping: Obj;
  produced_by: string;
  check: {
    ok: boolean;
    errors: string[];
    rows_checked: number;
    rows_rejected: number;
  };
  notes: MappingNote[];
  warnings: string[];
  unmapped_columns: string[];
  model: string | null;
  tokens: number;
}
export interface Preview {
  rows_in_file: number;
  rows_checked: number;
  rows_rejected: number;
  reject_rate: number;
  ok: boolean;
  columns: string[];
  rows: Record<string, unknown>[];
  rejects: { row_no: number; reason: string; raw: Record<string, unknown> }[];
  warnings: string[];
}
export interface ImportRow {
  id: number;
  filename: string;
  status: "queued" | "running" | "succeeded" | "failed";
  stage: string | null;
  progress: number;
  rows_total: number | null;
  rows_loaded: number | null;
  rows_rejected: number | null;
  rows_duplicate: number | null;
  created_at: string;
  finished_at: string | null;
  source_name: string;
  partner_code: string;
  uploaded_by: string | null;
  error?: string | null;
}
export interface SourceRow {
  id: number;
  name: string;
  kind: string;
  partner_id: number;
  partner_code: string;
  partner_name: string;
  approved_version: number | null;
  approved_at: string | null;
  last_import_at: string | null;
  imports: number;
}
export interface MappingVersion {
  id: number;
  version: number;
  status: "approved" | "superseded";
  note: string | null;
  approved_at: string | null;
  approved_by: string | null;
}

const live = (s: string) => s === "queued" || s === "running";

export const useSources = () =>
  useQuery({
    queryKey: ["sources"],
    queryFn: () => api<SourceRow[]>("/sources"),
  });

export const useImports = () =>
  useQuery({
    queryKey: ["imports"],
    queryFn: () => api<ImportRow[]>("/imports", { query: { limit: 30 } }),
    refetchInterval: (q) =>
      q.state.data?.some((i) => live(i.status)) ? 1200 : false,
  });

export const useImport = (id: number | null) =>
  useQuery({
    queryKey: ["import", id],
    queryFn: () => api<ImportRow>(`/imports/${id}`),
    enabled: id !== null,
    refetchInterval: (q) =>
      q.state.data && !live(q.state.data.status) ? false : 800,
  });

export const useMappingHistory = (sourceId: number | null) =>
  useQuery({
    queryKey: ["mappings", sourceId],
    queryFn: () => api<MappingVersion[]>(`/sources/${sourceId}/mappings`),
    enabled: sourceId !== null,
  });

export function useRollback(sourceId: number) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (version: number) =>
      api(`/sources/${sourceId}/mappings/${version}/rollback`, {
        method: "POST",
      }),
    onSuccess: () =>
      qc
        .invalidateQueries({ queryKey: ["mappings"] })
        .then(() => qc.invalidateQueries({ queryKey: ["sources"] })),
  });
}

export function uploadFile(file: File): Promise<UploadInfo> {
  const form = new FormData();
  form.append("file", file);
  return api<UploadInfo>("/uploads", { method: "POST", body: form });
}

// ---- editing a mapping, one column at a time ------------------------------------------------------

export interface ColRef {
  path: (string | number)[];
  key: string; // the note field this row corresponds to
  label: string;
  optional: boolean;
  value: string | null;
}

const LABEL: Record<string, string> = {
  order_ref: "Order ID",
  awb: "AWB (tracking number)",
  reference: "Payment reference (UTR)",
  date: "Payment date",
  channel: "Channel",
  ordered_on: "Order date",
  payment_mode: "Payment mode",
  value: "Order value",
  delivery_status: "Delivery status",
  pincode: "Pincode",
  weight_g: "Weight (g)",
  length_cm: "Length (cm)",
  width_cm: "Width (cm)",
  height_cm: "Height (cm)",
  courier: "Courier",
  delivered_on: "Delivered on",
  returned_on: "Returned on",
  received_on: "Received on",
  condition: "Condition on arrival",
};
const TEXT_KEYS: Record<string, string[]> = {
  ledger: ["order_ref", "awb", "reference"],
  orders: [
    "order_ref",
    "channel",
    "payment_mode",
    "value",
    "delivery_status",
    "pincode",
    "weight_g",
    "length_cm",
    "width_cm",
    "height_cm",
    "courier",
    "awb",
  ],
  receipts: ["order_ref", "condition"],
};
const DATE_KEYS: Record<string, string[]> = {
  ledger: ["date"],
  orders: ["ordered_on", "delivered_on", "returned_on"],
  receipts: ["received_on"],
};
const REQUIRED = new Set([
  "order_ref",
  "channel",
  "payment_mode",
  "value",
  "delivery_status",
  "date",
  "ordered_on",
  "received_on",
]);

export const norm = (s: string) =>
  s.trim().toLowerCase().split(/\s+/).join(" ");

/** The rows of the column editor: every place the mapping names a column of the file. */
export function columnRefs(spec: Obj): ColRef[] {
  const kind = String(spec.kind ?? "");
  const block = (spec[kind] ?? {}) as Obj;
  const out: ColRef[] = [];
  const str = (v: unknown) => (typeof v === "string" ? v : null);
  for (const key of DATE_KEYS[kind] ?? []) {
    const d = block[key] as Obj | undefined;
    if (d && typeof d === "object") {
      out.push({
        path: [kind, key, "column"],
        key,
        label: LABEL[key] ?? key,
        optional: !REQUIRED.has(key),
        value: str(d.column),
      });
    }
  }
  for (const key of TEXT_KEYS[kind] ?? []) {
    out.push({
      path: [kind, key],
      key,
      label: LABEL[key] ?? key,
      optional: !REQUIRED.has(key),
      value: str(block[key]),
    });
  }
  const emitters = block.emitters;
  if (Array.isArray(emitters)) {
    emitters.forEach((e: Obj, i) => {
      const t =
        typeof e.line_type === "string"
          ? String(e.line_type).replace(/_/g, " ")
          : "chosen by a type column";
      out.push({
        path: [kind, "emitters", i, "amount"],
        key: `emit:${str(e.amount)}`,
        label: `Amount (${t})`,
        optional: false,
        value: str(e.amount),
      });
    });
  }
  const net = block.net_check as Obj | undefined;
  if (net && typeof net === "object") {
    out.push({
      path: [kind, "net_check", "column"],
      key: "net_check",
      label: "Net amount (used to check signs)",
      optional: true,
      value: str(net.column),
    });
  }
  const extras = block.extras as Obj | undefined;
  if (extras && typeof extras === "object") {
    for (const k of Object.keys(extras)) {
      out.push({
        path: [kind, "extras", k],
        key: `extra:${k}`,
        label: `Kept as is: ${k.replace(/_/g, " ")}`,
        optional: true,
        value: str(extras[k]),
      });
    }
  }
  return out;
}

/** A copy of the mapping with one column reference changed (null removes an optional one). */
export function setColumn(
  spec: Obj,
  path: (string | number)[],
  value: string | null,
): Obj {
  const next = structuredClone(spec) as Obj;
  let cur: any = next; // eslint-disable-line @typescript-eslint/no-explicit-any
  for (const step of path.slice(0, -1)) {
    if (cur[step] === undefined || cur[step] === null) cur[step] = {};
    cur = cur[step];
  }
  const last = path[path.length - 1];
  if (value === null) {
    if (path.length >= 2 && path[path.length - 1] === "column") {
      // an optional date column: drop the whole date block
      let parent: any = next; // eslint-disable-line @typescript-eslint/no-explicit-any
      for (const step of path.slice(0, -2)) parent = parent[step];
      delete parent[path[path.length - 2]];
    } else delete cur[last];
  } else cur[last] = value;
  return next;
}
