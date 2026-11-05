import { useEffect, useState, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { Banner } from "../components/ui/Banner";
import { Button } from "../components/ui/Button";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Skeleton,
} from "../components/ui/Section";
import { Stat } from "../components/ui/Stat";
import { useAi, useAiStatus } from "../lib/ai";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, dateTime, inr, inrShort, int } from "../lib/format";
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
import { OrderDrawer } from "./OrderDrawer";

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
  const orderId = sp.get("order") ? Number(sp.get("order")) : null;
  const { can } = useAuth();
  const exec = can("finance_exec");
  const ai = useAi();

  // Being on this screen counts as having seen the AI's answers; the highlights then fade away.
  useEffect(() => {
    if (ai.status !== "done") return;
    ai.markSeen();
    const t = setTimeout(ai.clearTouched, 6000);
    return () => clearTimeout(t);
  }, [ai.status]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch))
      v === null || v === "" ? next.delete(k) : next.set(k, v);
    if (!("page" in patch) && !("order" in patch)) next.delete("page");
    setSp(next, { replace: true });
  };

  // Orders open in a drawer on this page, so closing it leaves you exactly where you were.
  const openOrder = (id: number) => set({ order: String(id) });

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
  const s = summary.data;
  const rows = q.data?.rows ?? [];
  const total = q.data?.total ?? 0;
  const last = Math.max(0, Math.ceil(total / PAGE) - 1);

  return (
    <>
      <PageHeader
        title="Review queue"
        lead="Payments that arrived without an order ID. The rules rank the likeliest orders and an AI can give a second opinion, but nothing is linked until a person accepts it."
      />

      <AiPanel exec={exec} />

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
              <GroupCard
                key={g.id}
                g={g}
                exec={exec}
                onDone={setNotice}
                onOpen={openOrder}
              />
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

      <OrderDrawer id={orderId} onClose={() => set({ order: null })} />
    </>
  );
}

/** The AI feature up front: what it does, one button to ask, and live feedback while it works. */
function AiPanel({ exec }: { exec: boolean }) {
  const ai = useAi();
  const status = useAiStatus();
  const all = useReviewQueue({ status: "open", limit: 200 });
  const [scope, setScope] = useState<"all" | "unsure">("all");
  const ready = status.data?.configured === true;

  const askable = (all.data?.rows ?? []).filter(
    (g) => g.ai_verdict === null || g.ai_verdict === "error",
  );
  const unsure = askable.filter((g) => g.suggestions[0]?.confidence !== "high");
  const chosen = scope === "all" ? askable : unsure;
  const answered = (all.data?.rows ?? []).filter(
    (g) => g.ai_verdict === "picked" || g.ai_verdict === "none",
  );
  const running = ai.status === "running";
  const r = ai.result;
  const pct = ai.total ? Math.round((ai.done / ai.total) * 100) : 0;

  return (
    <section className="mt-8 border border-rule" aria-label="AI second opinion">
      <div className="flex items-start justify-between gap-8 px-6 py-5">
        <div className="max-w-[56ch]">
          <div className="eyebrow">AI second opinion</div>
          <h2 className="mt-1 text-[1.3rem]">
            Let the AI read the unclear payments
          </h2>
          <p className="mt-1 text-[0.95rem] text-muted">
            For each payment it reads the candidate orders, recommends one and
            says why. It never links anything: you still accept or reject.
          </p>
          <p className="mt-2 text-[0.85rem]" role="status">
            {status.isLoading ? (
              <span className="text-muted">Checking the AI connection…</span>
            ) : ready ? (
              <span className="text-muted">
                <span className="mr-2 inline-block h-2 w-2 bg-ok" aria-hidden />
                Connected to {status.data!.provider}, model {status.data!.model}
              </span>
            ) : (
              <span className="text-risk">
                <span
                  className="mr-2 inline-block h-2 w-2 bg-risk"
                  aria-hidden
                />
                Not set up: add GROQ_API_KEY to the backend .env and restart the
                API. The rules' ranking still works.
              </span>
            )}
          </p>
        </div>
        {exec && (
          <div className="flex shrink-0 flex-col items-end gap-3">
            <div role="group" aria-label="Which payments" className="flex">
              {(
                [
                  ["all", `All open (${askable.length})`],
                  ["unsure", `Only unsure (${unsure.length})`],
                ] as const
              ).map(([k, text]) => (
                <button
                  key={k}
                  aria-pressed={scope === k}
                  disabled={running}
                  onClick={() => setScope(k)}
                  className={`-ml-px border px-3 py-1.5 text-[0.88rem] transition-colors duration-150 first:ml-0 ${scope === k ? "border-accent bg-accent text-white" : "border-rule-strong hover:bg-wash"}`}
                >
                  {text}
                </button>
              ))}
            </div>
            <Button
              variant="primary"
              busy={running}
              disabled={chosen.length === 0 || !ready}
              onClick={() =>
                void ai.run(
                  chosen.map((g) => g.id),
                  scope === "all",
                )
              }
            >
              {!ready
                ? "AI not set up"
                : chosen.length === 0
                  ? "Nothing left to ask"
                  : `Ask the AI about ${chosen.length} ${chosen.length === 1 ? "payment" : "payments"}`}
            </Button>
            {ready && answered.length > 0 && (
              <button
                type="button"
                disabled={running}
                className="text-[0.85rem] text-accent underline underline-offset-4 disabled:opacity-50"
                onClick={() =>
                  void ai.run(
                    answered.map((g) => g.id),
                    true,
                    true,
                  )
                }
              >
                Ask again about the {answered.length} already answered
              </button>
            )}
          </div>
        )}
      </div>

      {running && (
        <div
          className="reveal border-t border-rule bg-wash px-6 py-4"
          role="status"
          aria-live="polite"
        >
          <div className="flex items-baseline justify-between">
            <span>
              <span className="dots mr-2 text-accent" aria-hidden>
                <span />
                <span />
                <span />
              </span>
              The AI is reading payments · {ai.done} of {ai.total} done
            </span>
            <span className="num text-muted">{pct}%</span>
          </div>
          <div
            className="sweep relative mt-3 h-[3px] overflow-hidden bg-rule-strong"
            aria-hidden
          >
            <span />
          </div>
          <p className="mt-2 text-[0.88rem] text-muted">
            About two seconds per payment. You can leave this page; the Review
            queue in the sidebar will flag it when it's done.
          </p>
        </div>
      )}

      {!running && ai.status === "done" && r && (
        <div
          className="reveal flex items-start justify-between gap-6 border-t border-rule bg-wash px-6 py-4"
          role="status"
          aria-live="polite"
        >
          <div>
            {r.asked > 0 ? (
              <>
                <strong>
                  The AI reviewed {r.asked}{" "}
                  {r.asked === 1 ? "payment" : "payments"}.
                </strong>{" "}
                <span className="text-ok">{r.agreed} agree with the rules</span>
                {" · "}
                <span className={r.other ? "font-bold text-warn" : ""}>
                  {r.other} prefer a different order
                </span>
                {" · "}
                <span>{r.unsure} unsure</span>
                {r.errors > 0 && (
                  <>
                    {" · "}
                    <span className="text-risk">
                      {r.errors} could not be answered
                    </span>
                  </>
                )}
                <div className="mt-1 text-[0.85rem] text-muted">
                  Answered by {status.data?.model ?? "the AI"} in{" "}
                  {r.seconds.toFixed(1)} s · {int(r.tokens)} tokens used
                </div>
              </>
            ) : (
              <strong>The AI had nothing new to review.</strong>
            )}
            {r.note && (
              <div className="mt-1 text-[0.9rem] text-risk">{r.note}</div>
            )}
            {r.other > 0 && (
              <div className="mt-1 text-[0.9rem] text-muted">
                Look first at the cards marked “AI prefers a different order”.
              </div>
            )}
          </div>
          <Button variant="quiet" onClick={ai.dismiss}>
            Dismiss
          </Button>
        </div>
      )}
    </section>
  );
}

/** One short line saying what the AI thinks of this payment, or that it is working on it. */
function AiVerdict({
  g,
  onOpen,
}: {
  g: ReviewGroup;
  onOpen: (id: number) => void;
}) {
  const ai = useAi();
  if (g.status !== "open") return null;
  const active = ai.activeIds.includes(g.id);
  const queued = ai.queuedIds.includes(g.id);
  if (active || queued) {
    return (
      <div
        className="flex items-center gap-3 border-b border-rule border-l-4 border-l-accent bg-wash px-6 py-2.5 text-[0.92rem] text-muted"
        role="status"
      >
        <span className="dots text-accent" aria-hidden>
          <span />
          <span />
          <span />
        </span>
        {active ? "The AI is reading this payment" : "Waiting for the AI"}
      </div>
    );
  }
  const pick = g.suggestions.find((s) => s.ai_pick);
  const ref = pick ? (
    <button
      type="button"
      className="text-accent underline underline-offset-4"
      onClick={() => onOpen(pick.order_id)}
    >
      {pick.order_ref_raw}
    </button>
  ) : null;
  let head: ReactNode = null;
  let tone = "border-l-accent";
  if (g.ai_verdict === "picked" && pick) {
    if (pick.rank === 1)
      head = <>The AI agrees with the top choice, order {ref}</>;
    else {
      head = <>The AI prefers order {ref}, not the rules' first choice</>;
      tone = "border-l-warn";
    }
  } else if (g.ai_verdict === "none") {
    head = "The AI is not sure which order this is";
    tone = "border-l-warn";
  } else if (g.ai_verdict === "error") {
    head = "The AI could not answer this one";
    tone = "border-l-risk";
  }
  if (!head) return null;
  return (
    <div
      className={`reveal border-b border-rule border-l-4 bg-wash px-6 py-3 ${tone}`}
    >
      <div className="flex items-baseline gap-3">
        <span className="eyebrow">AI</span>
        <span className="font-bold">{head}</span>
      </div>
      {g.ai_note && (
        <p className="mt-1 text-[0.92rem]">
          <span className="text-muted">Why: </span>
          {g.ai_note}
        </p>
      )}
      {g.ai_model && g.ai_at && g.ai_verdict !== "error" && (
        <p className="mt-1 text-[0.8rem] text-faint">
          Answered by {g.ai_model} · {dateTime(g.ai_at)}
        </p>
      )}
    </div>
  );
}

function GroupCard({
  g,
  exec,
  onDone,
  onOpen,
}: {
  g: ReviewGroup;
  exec: boolean;
  onDone: (m: string) => void;
  onOpen: (id: number) => void;
}) {
  const ai = useAi();
  const [lines, setLines] = useState(false);
  const [manual, setManual] = useState(false);
  const accept = useReviewAction((id: number) => reviewApi.accept(id));
  const reject = useReviewAction((id: number) => reviewApi.reject(id));
  const dismiss = useReviewAction((id: number) => reviewApi.dismiss(id));
  const undo = useReviewAction((id: number) => reviewApi.undo(id));
  const err = accept.error ?? reject.error ?? dismiss.error ?? undo.error;
  const open = g.status === "open";

  return (
    <li
      className={`border bg-paper ${ai.touched.includes(g.id) ? "ai-flash border-accent" : "border-rule"}`}
    >
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
      <AiVerdict g={g} onOpen={onOpen} />

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
                  onOpen={onOpen}
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
                <button
                  type="button"
                  className="text-accent underline underline-offset-4"
                  onClick={() => onOpen(g.linked_order_id!)}
                >
                  order #{g.linked_order_id}
                </button>
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
  onOpen,
}: {
  s: Suggestion;
  exec: boolean;
  busy: boolean;
  onAccept: () => void;
  onReject: () => void;
  onOpen: (id: number) => void;
}) {
  return (
    <li
      className={`flex items-start justify-between gap-6 border-b border-rule px-6 py-4 last:border-b-0 ${s.ai_pick ? "bg-wash" : ""}`}
    >
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
          <span className="text-[0.82rem] text-muted">#{s.rank}</span>
          <button
            type="button"
            onClick={() => onOpen(s.order_id)}
            className="text-[1.05rem] underline-offset-4 hover:text-accent hover:underline"
          >
            {s.order_ref_raw}
          </button>
          <span
            className={`inline-flex items-center gap-2 text-[0.9rem] ${CONF_TONE[s.confidence]}`}
          >
            <span aria-hidden className="inline-block h-2 w-2 bg-current" />
            <span className="text-ink">
              {CONF_TEXT[s.confidence]} · rules score{" "}
              {Math.round(Number(s.score) * 100)}%
            </span>
          </span>
          {s.ai_pick && (
            <span className="border border-accent px-2 py-0.5 text-[0.78rem] uppercase tracking-wider text-accent">
              AI recommends
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
