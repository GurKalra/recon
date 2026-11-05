export type Role = "finance_exec" | "cofounder";

export interface User {
  id: number;
  email: string;
  full_name: string;
  role: Role;
}

export interface Session {
  user: User;
  csrf_token: string;
}

export interface Bucketed {
  findings: number;
  amount_paise: number;
}

export interface MatrixRow extends Bucketed {
  partner_code: string;
  partner_name: string;
  buckets: Record<string, Bucketed>;
}

export interface Dashboard {
  headline: {
    as_of: string;
    money_at_risk_paise: number;
    findings_at_risk: number;
    open_paise: number;
    claimed_paise: number;
    expiring_7d_paise: number;
    expiring_7d_count: number;
    expired_paise: number;
    expired_count: number;
    recovered_paise: number;
    orders: number;
    auto_match_pct: number;
    lines_to_review: number;
    last_run: {
      id: number;
      as_of: string;
      finished_at: string;
      orders_total: number;
    } | null;
  };
  matrix: {
    as_of: string;
    buckets: string[];
    partners: MatrixRow[];
    totals: Record<string, Bucketed>;
    amount_paise: number;
  };
  by_type: { type: string; findings: number; amount_paise: number }[];
  order_status: Record<string, number>;
}

export interface TaxRow {
  partner_code: string;
  partner_name: string;
  month: string;
  tcs_paise: number;
  tds_paise: number;
  sales_paise: number;
}

export interface Deadline {
  partner_code: string;
  partner_name: string;
  urgency: "expired" | "critical" | "soon" | "ok";
  findings: number;
  amount_paise: string | number;
  nearest_deadline: string;
}

export interface ReconStatus {
  stale: boolean;
  reasons: string[];
  never_run: boolean;
  last_run: { id: number; as_of: string; finished_at: string } | null;
}

export interface DemoStatus {
  loaded: boolean;
  available: boolean;
  as_of: string;
  files: {
    name: string;
    size_bytes: number;
    partner: string;
    kind: string;
    label: string;
  }[];
}

export interface Partner {
  id: number;
  code: string;
  name: string;
  type: string;
  payout_sla_days: number;
  claim_window_days: number;
  tolerance_paise: number;
}

export interface OrderRow {
  id: number;
  order_ref: string;
  ordered_on: string;
  payment_mode: "prepaid" | "cod";
  value_paise: number;
  delivery_status: string;
  channel: string;
  courier: string | null;
  status: string;
  expected_paise: number | null;
  received_paise: number | null;
  variance_paise: number | null;
  flags: string[];
  live_findings: number;
}

export interface Page<T> {
  total: number;
  limit: number;
  offset: number;
  rows: T[];
}

export interface WaterfallRow {
  grp: string;
  phase: number;
  line_type: string;
  partner_id: number;
  due_on: string | null;
  expected_paise: number;
  received_paise: string | number;
  n_received: number;
  is_expected: boolean;
  last_on: string | null;
}

export interface OrderDetail {
  order: OrderRow & {
    order_ref_raw: string;
    awb: string | null;
    delivered_on: string | null;
    returned_on: string | null;
    channel_name: string;
    weight_g: number | null;
  };
  waterfall: WaterfallRow[];
  lines: {
    id: number;
    partner_code: string;
    line_type: string;
    amount_paise: number;
    occurred_on: string;
    filename: string;
    source_row: number;
  }[];
  findings: {
    id: number;
    type: string;
    amount_paise: number;
    status: string;
    since_on: string;
    claim_deadline: string | null;
    recovered_paise: number;
    partner_code: string;
    days_left: number | null;
    history: { at: string; kind: string; detail: unknown }[];
  }[];
  receipts: { received_on: string; condition: string | null }[];
}

export interface ReturnRow {
  id: number;
  order_ref: string;
  channel: string;
  returned_on: string;
  value_paise: number;
  received_on: string | null;
  condition: string | null;
  days_since_return: number;
  state: "waiting" | "missing" | "received";
}

export interface ReturnsPage extends Page<ReturnRow> {
  wait_days: number;
  counts: Record<"waiting" | "missing" | "received", number>;
}
