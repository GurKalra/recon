import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { StatusBadge } from "../components/ui/Badge";
import { Button } from "../components/ui/Button";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Skeleton,
} from "../components/ui/Section";
import { date, findings, inr, int, label } from "../lib/format";
import { useOrders, usePartners, useReturns } from "../lib/queries";
import type { OrderRow } from "../lib/types";
import { OrderDrawer } from "./OrderDrawer";

const PAGE = 50;
const STATUSES = [
  "short_paid",
  "unpaid_past_due",
  "freight_overcharged",
  "return_not_received",
  "settled",
  "pending",
];
const AGES = ["0-7", "8-15", "16-30", "31-60", "60+"];
const SORTS: [string, string][] = [
  ["variance", "Biggest shortfall"],
  ["newest", "Newest first"],
  ["oldest", "Oldest first"],
  ["value", "Highest value"],
];

const field =
  "w-full border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";

export function Orders() {
  const [sp, setSp] = useSearchParams();
  const tab = sp.get("tab") === "returns" ? "returns" : "orders";
  const orderId = sp.get("order") ? Number(sp.get("order")) : null;

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch))
      v === null || v === "" ? next.delete(k) : next.set(k, v);
    if (!("page" in patch) && !("order" in patch) && !("tab" in patch))
      next.delete("page");
    setSp(next, { replace: true });
  };

  return (
    <>
      <PageHeader
        title="Orders"
        lead="Every order with what we expected to be paid and what actually arrived. Click a row for the full breakdown."
      />
      <div role="tablist" className="mt-6 flex gap-8 border-b border-rule">
        {[
          ["orders", "All orders"],
          ["returns", "Returns tracker"],
        ].map(([k, text]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            onClick={() => set({ tab: k === "orders" ? null : k, page: null })}
            className={`-mb-px border-b-2 pb-3 text-[1.02rem] ${tab === k ? "border-accent font-bold text-accent" : "border-transparent text-muted hover:text-ink"}`}
          >
            {text}
          </button>
        ))}
      </div>
      {tab === "orders" ? (
        <OrdersTab sp={sp} set={set} />
      ) : (
        <ReturnsTab sp={sp} set={set} />
      )}
      <OrderDrawer id={orderId} onClose={() => set({ order: null })} />
    </>
  );
}

type TabProps = {
  sp: URLSearchParams;
  set: (p: Record<string, string | null>) => void;
};

function OrdersTab({ sp, set }: TabProps) {
  const partners = usePartners();
  const partner = sp.get("partner") ?? "";
  const age = sp.get("age") ?? "";
  const open = sp.get("open") === "1";
  const status = sp.get("status") ?? "";
  const mode = sp.get("mode") ?? "";
  const from = sp.get("from") ?? "";
  const to = sp.get("to") ?? "";
  const sort = sp.get("sort") ?? "variance";
  const page = Number(sp.get("page") ?? 0) || 0;

  const [search, setSearch] = useState(sp.get("q") ?? "");
  useEffect(() => {
    const t = setTimeout(() => {
      if ((sp.get("q") ?? "") !== search.trim())
        set({ q: search.trim() || null });
    }, 350);
    return () => clearTimeout(t);
  }, [search]); // eslint-disable-line react-hooks/exhaustive-deps

  // From the Overview, "partner" means the one that owes the money; on its own it means the channel.
  const pid = partners.data?.find((p) => p.code === partner)?.id;
  const owes = open || !!age;
  const ready = !partner || !!partners.data;
  const q = useOrders(
    {
      q: sp.get("q") ?? undefined,
      status: status || undefined,
      payment_mode: mode || undefined,
      date_from: from || undefined,
      date_to: to || undefined,
      sort,
      problems_only: open || undefined,
      age: age || undefined,
      partner_id: !owes ? pid : undefined,
      risk_partner_id: owes ? pid : undefined,
      limit: PAGE,
      offset: page * PAGE,
    },
    ready,
  );

  const chips: [string, string, string[]][] = [];
  if (partner)
    chips.push([
      owes ? `Owed by ${partner}` : `Channel ${partner}`,
      "partner",
      owes ? ["partner"] : ["partner"],
    ]);
  if (age) chips.push([`Overdue ${age} days`, "age", ["age"]]);
  if (open && !age) chips.push(["Open findings only", "open", ["open"]]);
  const clear = (...keys: string[]) =>
    set(Object.fromEntries(keys.map((k) => [k, null])));

  const total = q.data?.total ?? 0;
  const rows = q.data?.rows ?? [];
  const last = Math.max(0, Math.ceil(total / PAGE) - 1);

  return (
    <>
      <div className="mt-5 grid grid-cols-4 items-end gap-x-4 gap-y-3">
        <label className="text-[0.85rem] text-muted">
          Search
          <input
            className={`${field} mt-1 block`}
            placeholder="Order reference or AWB"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </label>
        <Select
          label="Status"
          value={status}
          onChange={(v) => set({ status: v || null })}
          options={[
            ["", "All statuses"],
            ...STATUSES.map((s): [string, string] => [s, label(s)]),
          ]}
        />
        <Select
          label="Channel"
          value={owes ? "" : partner}
          onChange={(v) => set({ partner: v || null, open: null, age: null })}
          options={[
            ["", "All channels"],
            ...(partners.data ?? []).map((p): [string, string] => [
              p.code,
              p.name,
            ]),
          ]}
        />
        <Select
          label="Payment"
          value={mode}
          onChange={(v) => set({ mode: v || null })}
          options={[
            ["", "Prepaid and COD"],
            ["prepaid", "Prepaid"],
            ["cod", "Cash on delivery"],
          ]}
        />
        <label className="text-[0.85rem] text-muted">
          From
          <input
            type="date"
            className={`${field} mt-1 block`}
            value={from}
            onChange={(e) => set({ from: e.target.value || null })}
          />
        </label>
        <label className="text-[0.85rem] text-muted">
          To
          <input
            type="date"
            className={`${field} mt-1 block`}
            value={to}
            onChange={(e) => set({ to: e.target.value || null })}
          />
        </label>
        <Select
          label="Sort"
          value={sort}
          onChange={(v) => set({ sort: v })}
          options={SORTS}
        />
        <label className="text-[0.85rem] text-muted">
          Age
          <select
            className={`${field} mt-1 block`}
            value={age}
            onChange={(e) => set({ age: e.target.value || null })}
          >
            <option value="">Any age</option>
            {AGES.map((a) => (
              <option key={a} value={a}>
                {a} days
              </option>
            ))}
          </select>
        </label>
      </div>

      {chips.length > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-[0.9rem]">
          {chips.map(([text, , keys]) => (
            <button
              key={text}
              onClick={() => clear(...keys)}
              className="border border-rule-strong px-2.5 py-1 hover:bg-wash"
              aria-label={`Remove filter ${text}`}
            >
              {text}{" "}
              <span aria-hidden className="ml-1 text-muted">
                ×
              </span>
            </button>
          ))}
        </div>
      )}

      <div className="mt-5">
        {q.error ? (
          <ErrorNote error={q.error} />
        ) : !q.data ? (
          <Skeleton h="h-96" />
        ) : rows.length === 0 ? (
          <EmptyState title="No orders match">
            Try removing a filter, or check the spelling of an order reference.
          </EmptyState>
        ) : (
          <OrderTable rows={rows} onOpen={(id) => set({ order: String(id) })} />
        )}
      </div>

      {total > 0 && (
        <div className="mt-4 flex items-center justify-between text-[0.92rem] text-muted">
          <span>
            {int(page * PAGE + 1)}–{int(Math.min(total, (page + 1) * PAGE))} of{" "}
            {int(total)} orders
          </span>
          <span className="flex gap-2">
            <Button
              disabled={page === 0}
              onClick={() => set({ page: String(page - 1) })}
            >
              Previous
            </Button>
            <Button
              disabled={page >= last}
              onClick={() => set({ page: String(page + 1) })}
            >
              Next
            </Button>
          </span>
        </div>
      )}
    </>
  );
}

function Select({
  label: text,
  value,
  onChange,
  options,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <label className="text-[0.85rem] text-muted">
      {text}
      <select
        className={`${field} mt-1 block`}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        {options.map(([v, t]) => (
          <option key={v} value={v}>
            {t}
          </option>
        ))}
      </select>
    </label>
  );
}

function OrderTable({
  rows,
  onOpen,
}: {
  rows: OrderRow[];
  onOpen: (id: number) => void;
}) {
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>Order</th>
          <th>Date</th>
          <th>Channel</th>
          <th>Status</th>
          <th className="r">Expected</th>
          <th className="r">Received</th>
          <th className="r">Difference</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => {
          const bad = (r.variance_paise ?? 0) <= -1000;
          return (
            <tr
              key={r.id}
              className="cursor-pointer"
              onClick={() => onOpen(r.id)}
            >
              <td>
                <button
                  className="text-left underline-offset-4 hover:text-accent hover:underline"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpen(r.id);
                  }}
                >
                  {r.order_ref}
                </button>
                <div className="text-[0.82rem] text-muted">
                  {r.payment_mode === "cod" ? "COD" : "Prepaid"}
                  {r.courier ? ` · ${r.courier}` : ""}
                  {r.live_findings > 0 ? ` · ${findings(r.live_findings)}` : ""}
                </div>
              </td>
              <td className="whitespace-nowrap">{date(r.ordered_on)}</td>
              <td>{r.channel}</td>
              <td>
                <StatusBadge status={r.status} />
              </td>
              <td className="r">{inr(r.expected_paise)}</td>
              <td className="r">{inr(r.received_paise)}</td>
              <td className={`r ${bad ? "font-bold text-risk" : "text-muted"}`}>
                {inr(r.variance_paise)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function ReturnsTab({ sp, set }: TabProps) {
  const state = sp.get("state") ?? "";
  const page = Number(sp.get("page") ?? 0) || 0;
  const q = useReturns({
    state: state || undefined,
    limit: PAGE,
    offset: page * PAGE,
  });
  const d = q.data;
  const last = d ? Math.max(0, Math.ceil(d.total / PAGE) - 1) : 0;
  const tabs: [string, string, number | undefined][] = [
    [
      "",
      "All returns",
      d ? d.counts.waiting + d.counts.missing + d.counts.received : undefined,
    ],
    ["missing", "Not received", d?.counts.missing],
    ["waiting", "In grace period", d?.counts.waiting],
    ["received", "Received", d?.counts.received],
  ];
  return (
    <>
      <p className="mt-5 max-w-[70ch] text-muted">
        A returned parcel should reach the warehouse within {d?.wait_days ?? 7}{" "}
        days. After that with no scan, the value is flagged as a return not
        received and goes on the claims list.
      </p>
      <div className="mt-4 flex flex-wrap gap-2">
        {tabs.map(([k, text, n]) => (
          <button
            key={k}
            onClick={() => set({ state: k || null, page: null })}
            aria-pressed={state === k}
            className={`border px-3 py-1.5 text-[0.95rem] ${state === k ? "border-accent bg-accent text-white" : "border-rule-strong hover:bg-wash"}`}
          >
            {text}
            {n !== undefined ? ` · ${int(n)}` : ""}
          </button>
        ))}
      </div>
      <div className="mt-5">
        {q.error ? (
          <ErrorNote error={q.error} />
        ) : !d ? (
          <Skeleton h="h-80" />
        ) : d.rows.length === 0 ? (
          <EmptyState title="Nothing here">
            No returned orders in this state.
          </EmptyState>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Order</th>
                <th>Channel</th>
                <th>Returned</th>
                <th>Days since</th>
                <th>Warehouse scan</th>
                <th className="r">Value</th>
              </tr>
            </thead>
            <tbody>
              {d.rows.map((r) => (
                <tr
                  key={r.id}
                  className="cursor-pointer"
                  onClick={() => set({ order: String(r.id) })}
                >
                  <td>
                    <button
                      className="underline-offset-4 hover:text-accent hover:underline"
                      onClick={(e) => {
                        e.stopPropagation();
                        set({ order: String(r.id) });
                      }}
                    >
                      {r.order_ref}
                    </button>
                  </td>
                  <td>{r.channel}</td>
                  <td className="whitespace-nowrap">{date(r.returned_on)}</td>
                  <td className="num">{r.days_since_return}</td>
                  <td>
                    <StatusBadge status={r.state} />
                    {r.received_on && (
                      <div className="text-[0.82rem] text-muted">
                        {date(r.received_on)}
                        {r.condition ? ` · ${r.condition}` : ""}
                      </div>
                    )}
                  </td>
                  <td className="r">{inr(r.value_paise)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      {d && d.total > PAGE && (
        <div className="mt-4 flex items-center justify-between text-[0.92rem] text-muted">
          <span>
            {int(page * PAGE + 1)}–{int(Math.min(d.total, (page + 1) * PAGE))}{" "}
            of {int(d.total)}
          </span>
          <span className="flex gap-2">
            <Button
              disabled={page === 0}
              onClick={() => set({ page: String(page - 1) })}
            >
              Previous
            </Button>
            <Button
              disabled={page >= last}
              onClick={() => set({ page: String(page + 1) })}
            >
              Next
            </Button>
          </span>
        </div>
      )}
    </>
  );
}
