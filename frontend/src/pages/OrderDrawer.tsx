import { Drawer } from "../components/ui/Drawer";
import { StatusBadge } from "../components/ui/Badge";
import { ErrorNote, Skeleton } from "../components/ui/Section";
import { date, inr, label } from "../lib/format";
import { useOrder } from "../lib/queries";
import { FindingActions } from "./claims/FindingActions";
import type { OrderDetail, WaterfallRow } from "../lib/types";

const TOLERANCE = 1000; // paise: the brief flags gaps over ₹10
const TAX = new Set(["tcs", "tds"]);
const GROUP: Record<string, string> = {
  channel: "Marketplace or gateway settlement",
  charges: "Courier charges",
  courier_cod: "Courier COD remittance",
};

const pretty = (s: string) =>
  s.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
const LINE: Record<string, string> = {
  sale: "Sale amount",
  tcs: "TCS (GST), deducted at source",
  tds: "TDS (income tax), deducted at source",
  cod_fee: "COD handling fee",
  return_freight: "Return freight",
};
const lineLabel = (t: string) => LINE[t] ?? pretty(t);

export function OrderDrawer({
  id,
  onClose,
}: {
  id: number | null;
  onClose: () => void;
}) {
  const q = useOrder(id);
  const o = q.data?.order;
  return (
    <Drawer
      open={id !== null}
      onClose={onClose}
      title={o ? o.order_ref_raw : "Order"}
      description={
        o ? `${o.channel_name} · ordered ${date(o.ordered_on)}` : undefined
      }
    >
      {q.isLoading && <Skeleton h="h-64" />}
      {q.error && <ErrorNote error={q.error} />}
      {q.data && <Body d={q.data} />}
    </Drawer>
  );
}

function Body({ d }: { d: OrderDetail }) {
  const o = d.order;
  return (
    <div className="space-y-9">
      <section className="grid grid-cols-3 gap-6">
        <Fact k="Verdict" v={<StatusBadge status={o.status} />} />
        <Fact k="Order value" v={inr(o.value_paise)} num />
        <Fact
          k="Payment"
          v={o.payment_mode === "cod" ? "Cash on delivery" : "Prepaid"}
        />
        <Fact k="Expected" v={inr(o.expected_paise)} num />
        <Fact k="Received" v={inr(o.received_paise)} num />
        <Fact
          k="Difference"
          num
          v={
            <span
              className={
                (o.variance_paise ?? 0) <= -TOLERANCE ? "text-risk" : ""
              }
            >
              {inr(o.variance_paise)}
            </span>
          }
        />
      </section>

      <Waterfall rows={d.waterfall} />
      <Findings d={d} />
      <Returned d={d} />
      <SourceLines d={d} />
    </div>
  );
}

function Fact({ k, v, num }: { k: string; v: React.ReactNode; num?: boolean }) {
  return (
    <div>
      <div className="eyebrow">{k}</div>
      <div className={`mt-1 text-[1.1rem] ${num ? "num" : ""}`}>{v}</div>
    </div>
  );
}

function H({ children, note }: { children: React.ReactNode; note?: string }) {
  return (
    <div className="mb-2">
      <h3 className="text-[1.15rem]">{children}</h3>
      {note && <p className="text-[0.9rem] text-muted">{note}</p>}
    </div>
  );
}

function Waterfall({ rows }: { rows: WaterfallRow[] }) {
  if (rows.length === 0) return null;
  const groups = [...new Set(rows.map((r) => r.grp))];
  return (
    <section>
      <H note="What each partner should have paid or charged, against what their files say. Tax deducted at source is shown, but is not counted as a shortfall.">
        Expected versus received
      </H>
      {groups.map((g) => (
        <div key={g} className="mb-5">
          <div className="eyebrow mb-1">{GROUP[g] ?? pretty(g)}</div>
          <table className="tbl">
            <thead>
              <tr>
                <th>Line</th>
                <th className="r">Expected</th>
                <th className="r">Received</th>
                <th className="r">Difference</th>
              </tr>
            </thead>
            <tbody>
              {rows
                .filter((r) => r.grp === g)
                .map((r) => {
                  const rec = Number(r.received_paise);
                  const diff = rec - r.expected_paise;
                  const missing =
                    r.is_expected &&
                    r.n_received === 0 &&
                    r.expected_paise !== 0;
                  const bad =
                    !TAX.has(r.line_type) && Math.abs(diff) > TOLERANCE;
                  return (
                    <tr key={`${r.line_type}-${r.phase}`}>
                      <td>
                        {lineLabel(r.line_type)}
                        {!r.is_expected && (
                          <div className="text-[0.82rem] text-warn">
                            Not on the rate card
                          </div>
                        )}
                        {r.due_on && (
                          <div className="text-[0.82rem] text-muted">
                            Due {date(r.due_on)}
                          </div>
                        )}
                      </td>
                      <td className="r">{inr(r.expected_paise)}</td>
                      <td className="r">
                        {missing ? (
                          <span className="text-risk">Not received</span>
                        ) : (
                          inr(rec)
                        )}
                      </td>
                      <td
                        className={`r ${bad ? (diff < 0 ? "font-bold text-risk" : "font-bold text-warn") : "text-muted"}`}
                      >
                        {missing
                          ? inr(-r.expected_paise)
                          : diff === 0
                            ? "–"
                            : inr(diff)}
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>
      ))}
    </section>
  );
}

function Findings({ d }: { d: OrderDetail }) {
  if (d.findings.length === 0) return null;
  return (
    <section>
      <H>Claims</H>
      <ul className="border-t border-rule">
        {d.findings.map((f) => {
          const closed = f.days_left !== null && f.days_left < 0;
          return (
            <li key={f.id} className="border-b border-rule py-3">
              <div className="flex items-start justify-between gap-4">
              <div>
                <StatusBadge status={f.type} />
                <div className="mt-0.5 text-[0.88rem] text-muted">
                  {pretty(f.status)} · owed by {f.partner_code} since{" "}
                  {date(f.since_on)}
                </div>
                {f.claim_deadline && (
                  <div
                    className={`text-[0.88rem] ${closed ? "text-risk" : (f.days_left ?? 99) <= 7 ? "text-warn" : "text-muted"}`}
                  >
                    {closed
                      ? `Claim window closed ${Math.abs(f.days_left!)} days ago (${date(f.claim_deadline)})`
                      : `Claim by ${date(f.claim_deadline)} (${f.days_left} ${f.days_left === 1 ? "day" : "days"} left)`}
                  </div>
                )}
              </div>
              <div className="num text-[1.1rem]">{inr(f.amount_paise)}</div>
              </div>
              <FindingActions f={f} />
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function Returned({ d }: { d: OrderDetail }) {
  const o = d.order;
  if (!o.returned_on) return null;
  return (
    <section>
      <H>Return</H>
      <p>
        Returned on {date(o.returned_on)}.{" "}
        {d.receipts.length > 0
          ? `Warehouse scanned it on ${date(d.receipts[0].received_on)}${d.receipts[0].condition ? ` (${d.receipts[0].condition})` : ""}.`
          : "No warehouse scan has been recorded."}
      </p>
    </section>
  );
}

function SourceLines({ d }: { d: OrderDetail }) {
  if (d.lines.length === 0) return null;
  return (
    <section>
      <H note="Every line we hold for this order, with the file and row it came from.">
        Evidence
      </H>
      <table className="tbl">
        <thead>
          <tr>
            <th>Line</th>
            <th>File and row</th>
            <th>Date</th>
            <th className="r">Amount</th>
          </tr>
        </thead>
        <tbody>
          {d.lines.map((l) => (
            <tr key={l.id}>
              <td>
                {label(l.line_type)}
                <div className="text-[0.82rem] text-muted">
                  {l.partner_code}
                </div>
              </td>
              <td className="text-[0.9rem]">
                {l.filename}
                <div className="text-[0.82rem] text-muted">
                  row {l.source_row}
                </div>
              </td>
              <td className="whitespace-nowrap">{date(l.occurred_on)}</td>
              <td className="r">{inr(l.amount_paise)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
