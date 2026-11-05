import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Banner } from "../components/ui/Banner";
import { Button } from "../components/ui/Button";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Section,
  Skeleton,
} from "../components/ui/Section";
import { Stat } from "../components/ui/Stat";
import { useAuth } from "../lib/auth";
import {
  date,
  findings,
  inr,
  inrShort,
  int,
  label,
  monthLabel,
  pct,
} from "../lib/format";
import {
  useDashboard,
  useDeadlines,
  useDemoStatus,
  useLoadDemo,
  useReconStatus,
  useRunRecon,
  useTax,
} from "../lib/queries";
import type { Dashboard } from "../lib/types";

// Five solid steps, light to dark. Solid fills only: no gradients, no opacity tricks.
const TINTS = ["#ffffff", "#fbeceb", "#f4cfcb", "#e8a49d", "#cf6a60"];
function tint(amount: number, max: number): { bg: string; fg: string } {
  if (amount <= 0 || max <= 0) return { bg: TINTS[0], fg: "#86867f" };
  const step = Math.min(4, 1 + Math.floor((amount / max) * 4));
  return { bg: TINTS[step], fg: step >= 4 ? "#ffffff" : "#161616" };
}

export function Overview() {
  const dash = useDashboard();
  const demo = useDemoStatus();
  const { can } = useAuth();

  if (dash.isLoading || demo.isLoading) {
    return (
      <>
        <PageHeader title="Overview" />
        <div className="mt-8 grid gap-4">
          <Skeleton h="h-32" />
          <Skeleton h="h-64" />
        </div>
      </>
    );
  }
  if (dash.error)
    return (
      <>
        <PageHeader title="Overview" />
        <div className="mt-8">
          <ErrorNote error={dash.error} />
        </div>
      </>
    );

  const d = dash.data!;
  if (d.headline.orders === 0)
    return (
      <Empty
        canLoad={can("finance_exec")}
        available={demo.data?.available ?? false}
      />
    );
  return <Loaded d={d} />;
}

function Empty({
  canLoad,
  available,
}: {
  canLoad: boolean;
  available: boolean;
}) {
  const load = useLoadDemo();
  return (
    <>
      <PageHeader
        title="Overview"
        lead="Money at risk by partner and age appears here once files are imported."
      />
      <div className="mt-10">
        <EmptyState
          title="No data yet"
          action={
            canLoad && available ? (
              <Button
                variant="primary"
                busy={load.isPending}
                onClick={() => load.mutate()}
              >
                Load demo workspace
              </Button>
            ) : undefined
          }
        >
          Import your order, settlement, COD and freight files, or load the demo
          workspace to see the tool on sample data from eight partners.
        </EmptyState>
        {load.error && (
          <div className="mt-4">
            <ErrorNote error={load.error} />
          </div>
        )}
      </div>
    </>
  );
}

function StaleBanner() {
  const status = useReconStatus();
  const run = useRunRecon();
  const { can } = useAuth();
  if (!status.data?.stale && !status.data?.never_run) return null;
  return (
    <div className="mt-6">
      <Banner
        tone="warn"
        action={
          can("finance_exec") ? (
            <Button
              variant="secondary"
              busy={run.isPending}
              onClick={() => run.mutate()}
            >
              Run reconciliation
            </Button>
          ) : undefined
        }
      >
        <strong>Figures are out of date.</strong>{" "}
        {status.data?.reasons.length
          ? status.data.reasons.join(" ")
          : "Reconciliation has not been run."}
      </Banner>
      {run.error && (
        <div className="mt-2">
          <ErrorNote error={run.error} />
        </div>
      )}
    </div>
  );
}

function Loaded({ d }: { d: Dashboard }) {
  const h = d.headline;
  return (
    <>
      <PageHeader
        title="Overview"
        lead={`Where money is leaking, as of ${date(h.as_of)}. Amounts are what partners owe, short, late or overcharged.`}
      />
      <StaleBanner />

      <div className="mt-8 grid grid-cols-[1.4fr_1fr_1fr] gap-x-10 gap-y-8 border-b border-rule pb-8">
        <Stat
          large
          to="/orders?open=1"
          tone="risk"
          label="Money at risk"
          value={inrShort(h.money_at_risk_paise)}
          note={`${findings(h.findings_at_risk)} open across ${int(h.orders)} orders`}
        />
        <Stat
          tone="warn"
          label="Claim window closes within 7 days"
          value={inrShort(h.expiring_7d_paise)}
          note={findings(h.expiring_7d_count)}
        />
        <Stat
          tone="risk"
          label="Already past the claim window"
          value={inrShort(h.expired_paise)}
          note={`${findings(h.expired_count)}, likely unrecoverable`}
        />
        <Stat
          label="Recovered"
          tone="ok"
          value={inrShort(h.recovered_paise)}
          note="Credited after a claim"
        />
        <Stat
          label="Matched automatically"
          value={pct(h.auto_match_pct)}
          note="Payments linked by order ID"
        />
        <Stat
          label="Payments to review"
          value={int(h.lines_to_review)}
          note="Suggested matches awaiting a person"
        />
      </div>

      <Matrix d={d} />
      <div className="grid grid-cols-2 gap-12">
        <ByType d={d} />
        <Deadlines />
      </div>
      <Tax />
    </>
  );
}

function Matrix({ d }: { d: Dashboard }) {
  const max = useMemo(
    () =>
      Math.max(
        0,
        ...d.matrix.partners.flatMap((p) =>
          Object.values(p.buckets).map((b) => b.amount_paise),
        ),
      ),
    [d],
  );
  const partners = [...d.matrix.partners].sort(
    (a, b) => b.amount_paise - a.amount_paise,
  );
  return (
    <Section
      title="By partner and age"
      note="Days since the order. The darker the cell, the more money sits there; older money is harder to claim."
    >
      <table className="tbl">
        <thead>
          <tr>
            <th>Partner</th>
            {d.matrix.buckets.map((b) => (
              <th key={b} className="r">
                {b} days
              </th>
            ))}
            <th className="r">Total</th>
          </tr>
        </thead>
        <tbody>
          {partners.map((p) => (
            <tr key={p.partner_code}>
              <td>
                <Link className="underline-offset-4 hover:text-accent hover:underline" to={`/orders?partner=${p.partner_code}&open=1`}>
                  {p.partner_name}
                </Link>
              </td>
              {d.matrix.buckets.map((b) => {
                const cell = p.buckets[b];
                const t = tint(cell?.amount_paise ?? 0, max);
                return (
                  <td
                    key={b}
                    className="r"
                    style={{ background: t.bg, color: t.fg }}
                    title={`${cell?.findings ?? 0} findings`}
                  >
                    {cell && cell.amount_paise > 0
                      ? inrShort(cell.amount_paise)
                      : "–"}
                  </td>
                );
              })}
              <td className="r font-bold">{inrShort(p.amount_paise)}</td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <td>All partners</td>
            {d.matrix.buckets.map((b) => (
              <td key={b} className="r">
                {inrShort(d.matrix.totals[b]?.amount_paise)}
              </td>
            ))}
            <td className="r">{inrShort(d.matrix.amount_paise)}</td>
          </tr>
        </tfoot>
      </table>
    </Section>
  );
}

function ByType({ d }: { d: Dashboard }) {
  const rows = [...d.by_type].sort((a, b) => b.amount_paise - a.amount_paise);
  const max = Math.max(1, ...rows.map((r) => r.amount_paise));
  return (
    <Section title="By kind of problem">
      <ul className="border-t border-rule">
        {rows.map((r) => (
          <li key={r.type} className="border-b border-rule">
            <Link to={`/orders?status=${r.type}`} className="block py-3 hover:bg-wash">
            <div className="flex items-baseline justify-between">
              <span>{label(r.type)}</span>
              <span className="num">{inrShort(r.amount_paise)}</span>
            </div>
            <div className="mt-1.5 h-[6px] bg-wash" aria-hidden>
              <div
                className="h-full bg-accent"
                style={{ width: `${(r.amount_paise / max) * 100}%` }}
              />
            </div>
            <div className="mt-1 text-[0.85rem] text-muted">
              {findings(r.findings)}
            </div>
            </Link>
          </li>
        ))}
      </ul>
    </Section>
  );
}

const URGENCY: Record<string, { text: string; cls: string }> = {
  expired: { text: "Past window", cls: "text-risk" },
  critical: { text: "Within 7 days", cls: "text-warn" },
  soon: { text: "Within 14 days", cls: "text-muted" },
  ok: { text: "Time left", cls: "text-ok" },
};

function Deadlines() {
  const q = useDeadlines();
  const rows = (q.data ?? [])
    .filter((r) => r.urgency !== "expired")
    .slice(0, 8);
  return (
    <Section
      title="Claim deadlines"
      note="Windows still open, soonest first. Partners set these, not us."
    >
      {q.error ? (
        <ErrorNote error={q.error} />
      ) : rows.length === 0 ? (
        <p className="text-muted">No open windows.</p>
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>Partner</th>
              <th>Closes</th>
              <th className="r">Amount</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.partner_code}-${r.urgency}`}>
                <td>
                  {r.partner_name}
                  <div className={`text-[0.82rem] ${URGENCY[r.urgency]?.cls}`}>
                    {URGENCY[r.urgency]?.text} · {findings(r.findings)}
                  </div>
                </td>
                <td className="whitespace-nowrap">
                  {date(r.nearest_deadline)}
                </td>
                <td className="r">{inrShort(r.amount_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}

function Tax() {
  const q = useTax();
  const months = useMemo(
    () => [...new Set((q.data ?? []).map((r) => r.month))].sort().reverse(),
    [q.data],
  );
  const [picked, setPicked] = useState<string | null>(null);
  const month = picked ?? months[0];
  const rows = (q.data ?? []).filter((r) => r.month === month);
  const tcs = rows.reduce((s, r) => s + r.tcs_paise, 0);
  const tds = rows.reduce((s, r) => s + r.tds_paise, 0);
  return (
    <Section
      title="Tax deducted at source"
      note="GST TCS and income-tax TDS are withheld by partners and credited to you on Form 26AS and GSTR-2A. They reduce the payout but are not leakage, so they are never counted in money at risk."
      right={
        months.length > 0 ? (
          <label className="text-[0.92rem] text-muted">
            Month{" "}
            <select
              value={month}
              onChange={(e) => setPicked(e.target.value)}
              className="ml-1 border border-rule-strong bg-paper px-2 py-1 text-ink"
            >
              {months.map((m) => (
                <option key={m} value={m}>
                  {monthLabel(m)}
                </option>
              ))}
            </select>
          </label>
        ) : undefined
      }
    >
      {q.error ? (
        <ErrorNote error={q.error} />
      ) : (
        <table className="tbl">
          <thead>
            <tr>
              <th>Partner</th>
              <th className="r">Sales</th>
              <th className="r">TCS (GST)</th>
              <th className="r">TDS (income tax)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.partner_code}>
                <td>{r.partner_name}</td>
                <td className="r">{inr(r.sales_paise)}</td>
                <td className="r">{inr(r.tcs_paise)}</td>
                <td className="r">{inr(r.tds_paise)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr>
              <td>Total</td>
              <td className="r">
                {inr(rows.reduce((s, r) => s + r.sales_paise, 0))}
              </td>
              <td className="r">{inr(tcs)}</td>
              <td className="r">{inr(tds)}</td>
            </tr>
          </tfoot>
        </table>
      )}
    </Section>
  );
}
