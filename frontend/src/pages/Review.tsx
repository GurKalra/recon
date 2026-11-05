import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Banner } from "../components/ui/Banner";
import { Button } from "../components/ui/Button";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Skeleton,
} from "../components/ui/Section";
import { Stat } from "../components/ui/Stat";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, inr, inrShort, int } from "../lib/format";
import {
  reviewApi,
  useReviewAction,
  useReviewDetail,
  useReviewQueue,
  useReviewSummary,
  usePartners,
  useRunRecon,
} from "../lib/queries";
import type { OrderRow, Page, ReviewGroup, Suggestion } from "../lib/types";

const PAGE = 10;
const CONF_TEXT = {
  high: "High confidence",
  medium: "Medium",
  low: "Low",
  none: "No likely order",
} as const;
const CONF_TONE = {
  high: "text-ok",
  medium: "text-warn",
  low: "text-muted",
  none: "text-risk",
} as const;
const field =
  "w-full border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";

export function Review() {
  const [sp, setSp] = useSearchParams();
  const status = (sp.get("status") ?? "open") as
    | "open"
    | "linked"
    | "dismissed";
  const confidence = sp.get("confidence") ?? "";
  const partner = sp.get("partner") ?? "";
  const page = Number(sp.get("page") ?? 0) || 0;
  const { can } = useAuth();
  const exec = can("finance_exec");

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch))
      v === null || v === "" ? next.delete(k) : next.set(k, v);
    if (!("page" in patch)) next.delete("page");
    setSp(next, { replace: true });
  };

  const summary = useReviewSummary();
  const partners = usePartners();
  const q = useReviewQueue({
    status,
    confidence: confidence || undefined,
    partner_id: partners.data?.find((p) => p.code === partner)?.id,
    limit: PAGE,
    offset: page * PAGE,
  });

  const [notice, setNotice] = useState<string | null>(null);
  const run = useRunRecon();
  const ask = useReviewAction((ids: number[]) => reviewApi.rerank(ids));
  const s = summary.data;
  const rows = q.data?.rows ?? [];
  const total = q.data?.total ?? 0;
  const last = Math.max(0, Math.ceil(total / PAGE) - 1);

  return (
    <>
      <PageHeader
        title="Review queue"
        lead="Payments that arrived without an order ID. The rules rank the likeliest orders and an AI can give a second opinion, but nothing is linked until a person accepts it."
        actions={
          exec && status === "open" && rows.length > 0 ? (
            <Button
              busy={ask.isPending}
              onClick={() => ask.mutate(rows.slice(0, 10).map((r) => r.id))}
            >
              Ask the AI about these
            </Button>
          ) : undefined
        }
      />

      {(s?.recon_stale || notice) && (
        <div className="mt-6">
          <Banner
            tone="warn"
            action={
              exec ? (
                <Button
                  busy={run.isPending}
                  onClick={() =>
                    run.mutate(undefined, { onSuccess: () => setNotice(null) })
                  }
                >
                  Run reconciliation
                </Button>
              ) : undefined
            }
          >
            {notice ?? "Order statuses are out of date after your decisions."}{" "}
            Run the reconciliation to update them.
          </Banner>
        </div>
      )}
      {ask.error && (
        <div className="mt-4">
          <ErrorNote error={ask.error} />
        </div>
      )}

      <div className="mt-8 grid grid-cols-4 gap-10 border-b border-rule pb-8">
        <Stat
          label="Waiting for a decision"
          value={int(s?.by_status.open.payments)}
          note={`${inrShort(s?.by_status.open.value_paise)} in payments`}
        />
        <Stat
          tone="ok"
          label="Rules are confident"
          value={int(s?.open_by_confidence.high)}
          note="Check and accept"
        />
        <Stat
          tone="warn"
          label="Need a closer look"
          value={int(
            (s?.open_by_confidence.medium ?? 0) +
              (s?.open_by_confidence.low ?? 0) +
              (s?.open_by_confidence.none ?? 0),
          )}
          note="Several orders could fit"
        />
        <Stat
          label="AI second opinions"
          value={int((s?.open_by_ai.picked ?? 0) + (s?.open_by_ai.none ?? 0))}
          note={`${int(s?.open_by_ai.not_asked)} not yet asked`}
        />
      </div>

      <div className="mt-6 flex flex-wrap items-end justify-between gap-4">
        <div role="tablist" className="flex gap-2">
          {(["open", "linked", "dismissed"] as const).map((k) => (
            <button
              key={k}
              role="tab"
              aria-selected={status === k}
              onClick={() => set({ status: k === "open" ? null : k })}
              className={`border px-3 py-1.5 text-[0.95rem] capitalize transition-colors duration-150 ${status === k ? "border-accent bg-accent text-white" : "border-rule-strong hover:bg-wash"}`}
            >
              {k}
              {s ? ` · ${int(s.by_status[k].payments)}` : ""}
            </button>
          ))}
        </div>
        <div className="flex gap-3">
          <label className="text-[0.85rem] text-muted">
            Confidence
            <select
              className={`${field} mt-1 block`}
              value={confidence}
              onChange={(e) => set({ confidence: e.target.value || null })}
            >
              <option value="">Any</option>
              {(["high", "medium", "low", "none"] as const).map((c) => (
                <option key={c} value={c}>
                  {CONF_TEXT[c]}
                </option>
              ))}
            </select>
          </label>
          <label className="text-[0.85rem] text-muted">
            Partner
            <select
              className={`${field} mt-1 block`}
              value={partner}
              onChange={(e) => set({ partner: e.target.value || null })}
            >
              <option value="">All partners</option>
              {(partners.data ?? []).map((p) => (
                <option key={p.code} value={p.code}>
                  {p.name}
                </option>
              ))}
            </select>
          </label>
        </div>
      </div>

      <div className="mt-6">
        {q.error ? (
          <ErrorNote error={q.error} />
        ) : !q.data ? (
          <Skeleton h="h-80" />
        ) : rows.length === 0 ? (
          <EmptyState
            title={
              status === "open" ? "Nothing waiting for you" : "Nothing here yet"
            }
          >
            {status === "open"
              ? "Every payment either carried its order ID or has been decided."
              : "Decisions appear here once you make them."}
          </EmptyState>
        ) : (
          <ul
            key={`${status}-${confidence}-${partner}-${page}`}
            className="stagger space-y-5"
          >
            {rows.map((g) => (
              <GroupCard key={g.id} g={g} exec={exec} onDone={setNotice} />
            ))}
          </ul>
        )}
      </div>

      {total > PAGE && (
        <div className="mt-5 flex items-center justify-between text-[0.92rem] text-muted">
          <span>
            {int(page * PAGE + 1)}–{int(Math.min(total, (page + 1) * PAGE))} of{" "}
            {int(total)} payments
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

function GroupCard({
  g,
  exec,
  onDone,
}: {
  g: ReviewGroup;
  exec: boolean;
  onDone: (m: string) => void;
}) {
  const [lines, setLines] = useState(false);
  const [manual, setManual] = useState(false);
  const accept = useReviewAction((id: number) => reviewApi.accept(id));
  const reject = useReviewAction((id: number) => reviewApi.reject(id));
  const dismiss = useReviewAction((id: number) => reviewApi.dismiss(id));
  const undo = useReviewAction((id: number) => reviewApi.undo(id));
  const err = accept.error ?? reject.error ?? dismiss.error ?? undo.error;
  const open = g.status === "open";

  return (
    <li className="border border-rule bg-paper">
      <div className="flex items-start justify-between gap-6 border-b border-rule px-6 py-4">
        <div>
          <div className="text-[1.15rem]">{g.partner_name}</div>
          <div className="text-[0.9rem] text-muted">
            Paid {date(g.occurred_on)} · {g.n_lines}{" "}
            {g.n_lines === 1 ? "line" : "lines"}
            {g.reference ? ` · ref ${g.reference}` : " · no order reference"}
            {g.has_refund ? " · includes a refund" : ""}
          </div>
          <button
            className="mt-1 text-[0.88rem] text-accent underline underline-offset-4"
            onClick={() => setLines((v) => !v)}
            aria-expanded={lines}
          >
            {lines ? "Hide payment lines" : "Show payment lines"}
          </button>
        </div>
        <div className="text-right">
          <div className="eyebrow">Payment</div>
          <div className="num text-[1.5rem]">{inr(g.sale_paise)}</div>
          <div className="num text-[0.88rem] text-muted">
            net {inr(g.net_paise)} after fees
          </div>
        </div>
      </div>

      {lines && <PaymentLines id={g.id} />}

      {g.ai_note && (
        <p className="reveal border-b border-rule bg-wash px-6 py-3 text-[0.92rem]">
          <span className="eyebrow mr-2">AI note</span>
          {g.ai_note}
        </p>
      )}

      {open ? (
        <>
          {g.suggestions.length === 0 ? (
            <p className="px-6 py-5 text-muted">
              No likely order found. You can find one by hand, or dismiss this
              payment.
            </p>
          ) : (
            <ul>
              {g.suggestions.map((s) => (
                <SuggestionRow
                  key={s.id}
                  s={s}
                  exec={exec}
                  busy={accept.isPending || reject.isPending}
                  onAccept={() =>
                    accept.mutate(s.id, {
                      onSuccess: () =>
                        onDone(`Linked the payment to ${s.order_ref_raw}.`),
                    })
                  }
                  onReject={() => reject.mutate(s.id)}
                />
              ))}
            </ul>
          )}
          {exec && (
            <div className="flex flex-wrap items-center gap-3 border-t border-rule px-6 py-3">
              <Button
                variant="quiet"
                onClick={() => setManual((v) => !v)}
                aria-expanded={manual}
              >
                Find the order by hand
              </Button>
              <Button
                variant="quiet"
                busy={dismiss.isPending}
                onClick={() => dismiss.mutate(g.id)}
              >
                Dismiss, no order exists
              </Button>
            </div>
          )}
          {manual && <ManualLink groupId={g.id} onDone={onDone} />}
        </>
      ) : (
        <div className="flex items-center justify-between gap-4 px-6 py-4">
          <p>
            {g.status === "linked" ? (
              <>
                Linked to{" "}
                <Link
                  className="text-accent underline underline-offset-4"
                  to={`/orders?order=${g.linked_order_id}`}
                >
                  order #{g.linked_order_id}
                </Link>
                {g.link_method ? ` (${g.link_method.replace(/_/g, " ")})` : ""}.
              </>
            ) : (
              "Dismissed. No matching order."
            )}
          </p>
          {exec && (
            <Button
              busy={undo.isPending}
              onClick={() =>
                undo.mutate(g.id, {
                  onSuccess: () => onDone("Decision undone."),
                })
              }
            >
              Undo
            </Button>
          )}
        </div>
      )}
      {err && (
        <div className="px-6 pb-4">
          <ErrorNote error={err} />
        </div>
      )}
    </li>
  );
}

function SuggestionRow({
  s,
  exec,
  busy,
  onAccept,
  onReject,
}: {
  s: Suggestion;
  exec: boolean;
  busy: boolean;
  onAccept: () => void;
  onReject: () => void;
}) {
  return (
    <li
      className={`flex items-start justify-between gap-6 border-b border-rule px-6 py-4 last:border-b-0 ${s.ai_pick ? "bg-wash" : ""}`}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <span className="text-[0.82rem] text-muted">#{s.rank}</span>
          <Link
            to={`/orders?order=${s.order_id}`}
            className="text-[1.05rem] underline-offset-4 hover:text-accent hover:underline"
          >
            {s.order_ref_raw}
          </Link>
          <span
            className={`inline-flex items-center gap-2 text-[0.9rem] ${CONF_TONE[s.confidence]}`}
          >
            <span aria-hidden className="inline-block h-2 w-2 bg-current" />
            <span className="text-ink">
              {CONF_TEXT[s.confidence]} · {Math.round(Number(s.score) * 100)}%
            </span>
          </span>
          {s.ai_pick && (
            <span className="border border-accent px-2 py-0.5 text-[0.78rem] uppercase tracking-wider text-accent">
              AI prefers this
            </span>
          )}
        </div>
        <div className="mt-0.5 text-[0.88rem] text-muted">
          {inr(s.value_paise)} · ordered {date(s.ordered_on)} ·{" "}
          {s.payment_mode === "cod" ? "COD" : "prepaid"} · {s.delivery_status}
        </div>
        <ul className="mt-2 list-disc space-y-0.5 pl-5 text-[0.9rem] marker:text-faint">
          {s.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
        {s.ai_reason && (
          <p className="mt-2 text-[0.9rem] text-muted">
            <span className="eyebrow mr-2">AI</span>
            {s.ai_reason}
          </p>
        )}
      </div>
      {exec && (
        <div className="flex shrink-0 gap-2">
          <Button variant="primary" disabled={busy} onClick={onAccept}>
            Accept
          </Button>
          <Button disabled={busy} onClick={onReject}>
            Reject
          </Button>
        </div>
      )}
    </li>
  );
}

function PaymentLines({ id }: { id: number }) {
  const q = useReviewDetail(id, true);
  if (q.isLoading)
    return (
      <div className="px-6 py-3">
        <Skeleton h="h-16" />
      </div>
    );
  if (q.error)
    return (
      <div className="px-6 py-3">
        <ErrorNote error={q.error} />
      </div>
    );
  return (
    <div className="reveal border-b border-rule px-6 py-3">
      <table className="tbl">
        <thead>
          <tr>
            <th>Line</th>
            <th>Narration</th>
            <th className="r">Amount</th>
          </tr>
        </thead>
        <tbody>
          {q.data!.lines.map((l) => (
            <tr key={l.id}>
              <td>{l.line_type.replace(/_/g, " ")}</td>
              <td className="text-[0.9rem] text-muted">
                {typeof l.extras?.narration === "string"
                  ? l.extras.narration
                  : "–"}
              </td>
              <td className="r">{inr(l.amount_paise)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ManualLink({
  groupId,
  onDone,
}: {
  groupId: number;
  onDone: (m: string) => void;
}) {
  const [text, setText] = useState("");
  const [found, setFound] = useState<OrderRow[] | null>(null);
  const [err, setErr] = useState<unknown>(null);
  const link = useReviewAction((v: { group: number; order: number }) =>
    reviewApi.link(v),
  );

  async function search() {
    setErr(null);
    try {
      const r = await api<Page<OrderRow>>("/orders", {
        query: { q: text.trim(), limit: 5 },
      });
      setFound(r.rows);
    } catch (e) {
      setErr(e);
    }
  }

  return (
    <div className="reveal border-t border-rule bg-wash px-6 py-4">
      <form
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          if (text.trim()) void search();
        }}
      >
        <input
          className={`${field} max-w-xs`}
          placeholder="Order reference or AWB"
          value={text}
          onChange={(e) => setText(e.target.value)}
          aria-label="Order reference"
        />
        <Button type="submit">Find</Button>
      </form>
      {err != null && (
        <div className="mt-2">
          <ErrorNote error={err} />
        </div>
      )}
      {found &&
        (found.length === 0 ? (
          <p className="mt-3 text-muted">No order matches that reference.</p>
        ) : (
          <ul className="mt-3 border-t border-rule">
            {found.map((o) => (
              <li
                key={o.id}
                className="flex items-center justify-between gap-4 border-b border-rule py-2"
              >
                <span>
                  {o.order_ref}{" "}
                  <span className="text-[0.88rem] text-muted">
                    · {inr(o.value_paise)} · {date(o.ordered_on)} · {o.channel}
                  </span>
                </span>
                <Button
                  busy={link.isPending}
                  onClick={() =>
                    link.mutate(
                      { group: groupId, order: o.id },
                      {
                        onSuccess: () =>
                          onDone(`Linked the payment to ${o.order_ref}.`),
                      },
                    )
                  }
                >
                  Link to this order
                </Button>
              </li>
            ))}
          </ul>
        ))}
      {link.error && (
        <div className="mt-2">
          <ErrorNote error={link.error} />
        </div>
      )}
    </div>
  );
}
