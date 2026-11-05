import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../../components/ui/Button";
import { EmptyState, ErrorNote, Skeleton } from "../../components/ui/Section";
import { useAuth } from "../../lib/auth";
import {
  claimsApi,
  firstBucket,
  canPick,
  URGENCIES,
  urgencyTotals,
  useCandidates,
  useClaimAction,
  type Urgency,
} from "../../lib/claims";
import { date, findings, inr, inrShort, int, label } from "../../lib/format";
import { useDeadlines, usePartners } from "../../lib/queries";
import { UrgencyTag } from "./bits";

const TYPES = [
  "unpaid_past_due",
  "short_paid",
  "freight_overcharged",
  "return_not_received",
];
const STEP = 100;
const MAX = 500;
const field =
  "w-full border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";

type Props = {
  sp: URLSearchParams;
  set: (p: Record<string, string | null>) => void;
};

/** Money the partners owe, most urgent first. Pick the findings for one partner and make them a claim. */
export function Candidates({ sp, set }: Props) {
  const nav = useNavigate();
  const { can } = useAuth();
  const exec = can("finance_exec");
  const partners = usePartners();
  const deadlines = useDeadlines();

  const partnerCode = sp.get("partner") ?? "";
  const partnerId = partners.data?.find((p) => p.code === partnerCode)?.id;
  const type = sp.get("type") ?? "";
  const totals = useMemo(
    () => urgencyTotals(deadlines.data ?? [], partnerCode || undefined),
    [deadlines.data, partnerCode],
  );
  const asked = sp.get("urgency");
  // Open on the most urgent group that can still be claimed, unless the link already said which.
  const bucket: Urgency | "" =
    asked === "all"
      ? ""
      : ["expired", "critical", "soon", "ok"].includes(asked ?? "")
        ? (asked as Urgency)
        : firstBucket(totals);
  const ready = !!deadlines.data && (!partnerCode || !!partners.data);

  const [limit, setLimit] = useState(STEP);
  const [picked, setPicked] = useState<
    Map<number, { partner: number; paise: number }>
  >(new Map());
  const [note, setNote] = useState("");
  const [writing, setWriting] = useState(false);

  const q = useCandidates({
    partner_id: partnerId,
    types: type || undefined,
    bucket: bucket || undefined,
    limit,
  });
  const rows = ready ? (q.data ?? []) : [];
  const pickedPartner = picked.size
    ? picked.values().next().value!.partner
    : null;
  const pickedPaise = [...picked.values()].reduce((s, v) => s + v.paise, 0);
  const pickedPartnerName =
    rows.find((r) => r.partner_id === pickedPartner)?.partner_name ??
    partners.data?.find((p) => p.id === pickedPartner)?.name ??
    "";

  const reset = () => {
    setPicked(new Map());
    setLimit(STEP);
    setWriting(false);
  };
  const change = (patch: Record<string, string | null>) => {
    reset();
    set(patch);
  };

  const toggle = (r: (typeof rows)[number]) =>
    setPicked((m) => {
      const n = new Map(m);
      if (n.has(r.id)) n.delete(r.id);
      else if (canPick(r, pickedPartner))
        n.set(r.id, { partner: r.partner_id, paise: r.amount_paise });
      return n;
    });
  const pickable = rows.filter((r) =>
    canPick(r, pickedPartner ?? partnerId ?? null),
  );
  const allPicked =
    pickable.length > 0 && pickable.every((r) => picked.has(r.id));
  const toggleAll = () =>
    setPicked((m) => {
      const n = new Map(m);
      if (allPicked) pickable.forEach((r) => n.delete(r.id));
      else
        pickable.forEach((r) =>
          n.set(r.id, { partner: r.partner_id, paise: r.amount_paise }),
        );
      return n;
    });

  const create = useClaimAction(claimsApi.create);
  const writeOff = useClaimAction(claimsApi.writeOff);
  const make = () =>
    create.mutate(
      {
        partner_id: pickedPartner!,
        finding_ids: [...picked.keys()],
        note: note.trim() || undefined,
      },
      {
        onSuccess: (r) => {
          reset();
          setNote("");
          nav(`/claims?tab=claims&claim=${r.id}`);
        },
      },
    );
  const giveUp = () =>
    writeOff.mutate(
      { finding_ids: [...picked.keys()], note: note.trim() || undefined },
      {
        onSuccess: () => {
          reset();
          setNote("");
        },
      },
    );

  const nothingAtAll = deadlines.data && deadlines.data.length === 0;

  return (
    <>
      <div className="mt-6 grid grid-cols-4 gap-px border border-rule bg-rule">
        {URGENCIES.map((u) => {
          const t = totals[u.key];
          const on = bucket === u.key;
          return (
            <button
              key={u.key}
              onClick={() => change({ urgency: on ? "all" : u.key })}
              aria-pressed={on}
              className={`border-b-[3px] bg-paper px-5 py-4 text-left transition-colors duration-150 hover:bg-wash ${on ? "border-b-accent" : "border-b-transparent"}`}
            >
              <div className="eyebrow">{u.title}</div>
              <div
                className={`num mt-1 text-[1.55rem] leading-tight ${u.key === "ok" ? "text-ink" : u.key === "soon" ? "text-warn" : "text-risk"}`}
              >
                {inrShort(t.paise)}
              </div>
              <div className="mt-1 text-[0.88rem] text-muted">
                {findings(t.findings)} · {u.hint}
              </div>
            </button>
          );
        })}
      </div>

      <div className="mt-5 grid grid-cols-[1fr_1fr_auto] items-end gap-x-4">
        <label className="text-[0.85rem] text-muted">
          Partner that owes the money
          <select
            className={`${field} mt-1 block`}
            value={partnerCode}
            onChange={(e) => change({ partner: e.target.value || null })}
          >
            <option value="">All partners</option>
            {(partners.data ?? []).map((p) => (
              <option key={p.id} value={p.code}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <label className="text-[0.85rem] text-muted">
          What went wrong
          <select
            className={`${field} mt-1 block`}
            value={type}
            onChange={(e) => change({ type: e.target.value || null })}
          >
            <option value="">All kinds</option>
            {TYPES.map((t) => (
              <option key={t} value={t}>
                {label(t)}
              </option>
            ))}
          </select>
        </label>
        <span className="pb-2 text-[0.88rem] text-muted">
          {bucket
            ? URGENCIES.find((u) => u.key === bucket)!.title
            : "Everything open"}
        </span>
      </div>

      <p className="mt-3 text-[0.9rem] text-muted">
        A claim covers one partner.{" "}
        {partnerCode
          ? "Tick the findings to include, or the box in the header for all of them."
          : "Pick a partner above to select all of its findings at once."}
      </p>

      <div className="mt-3">
        {q.error ? (
          <ErrorNote error={q.error} />
        ) : !ready || (q.isLoading && !q.data) ? (
          <Skeleton h="h-96" />
        ) : rows.length === 0 ? (
          <EmptyState
            title={
              nothingAtAll ? "Nothing to claim yet" : "Nothing in this view"
            }
          >
            {nothingAtAll
              ? "Findings appear here once files are imported and the reconciliation has run. Start from the Overview."
              : "Try another group above, or choose all partners."}
          </EmptyState>
        ) : (
          <div className="border border-rule">
            <table className="tbl">
              <thead>
                <tr>
                  <th className="w-10">
                    <input
                      type="checkbox"
                      aria-label="Select all shown"
                      className="h-4 w-4 accent-[#1f3a5f] disabled:opacity-40"
                      checked={allPicked}
                      disabled={!partnerCode && pickedPartner === null}
                      onChange={toggleAll}
                      title={
                        !partnerCode && pickedPartner === null
                          ? "Pick a partner first"
                          : undefined
                      }
                    />
                  </th>
                  <th>Order</th>
                  <th>Owed by</th>
                  <th>What went wrong</th>
                  <th className="r">Amount</th>
                  <th>Since</th>
                  <th>Claim by</th>
                  <th>Time left</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const on = picked.has(r.id);
                  const ok = canPick(r, pickedPartner);
                  return (
                    <tr key={r.id} className={on ? "bg-wash" : ""}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`Select ${r.order_ref}`}
                          className="h-4 w-4 accent-[#1f3a5f] disabled:opacity-30"
                          checked={on}
                          disabled={!on && !ok}
                          title={
                            r.claim_id !== null
                              ? `Already in claim ${r.claim_id}`
                              : !ok
                                ? "A claim covers one partner"
                                : undefined
                          }
                          onChange={() => toggle(r)}
                        />
                      </td>
                      <td>
                        <button
                          className="text-accent underline underline-offset-4"
                          onClick={() => set({ order: String(r.order_id) })}
                        >
                          {r.order_ref}
                        </button>
                      </td>
                      <td className={!ok && !on ? "text-faint" : ""}>
                        {r.partner_name}
                      </td>
                      <td>{label(r.type)}</td>
                      <td className="r">{inr(r.amount_paise)}</td>
                      <td className="whitespace-nowrap text-muted">
                        {date(r.since_on)}
                      </td>
                      <td className="whitespace-nowrap text-muted">
                        {date(r.claim_deadline)}
                      </td>
                      <td>
                        <UrgencyTag days={r.days_left} urgency={r.urgency} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
        {rows.length > 0 && (
          <div className="mt-3 flex items-center justify-between text-[0.92rem] text-muted">
            <span>
              Showing {int(rows.length)}
              {rows.length === limit && limit >= MAX
                ? ". Narrow the filters to see the rest."
                : ""}
            </span>
            {rows.length === limit && limit < MAX && (
              <Button onClick={() => setLimit((l) => Math.min(MAX, l + STEP))}>
                Show {int(STEP)} more
              </Button>
            )}
          </div>
        )}
      </div>

      {picked.size > 0 && (
        <div
          className="reveal sticky bottom-0 z-10 mt-6 border border-rule-strong bg-paper px-6 py-4"
          role="region"
          aria-label="Selection"
        >
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div>
              <div className="text-[1.1rem]">
                <span className="num">{int(picked.size)}</span>{" "}
                {picked.size === 1 ? "finding" : "findings"} ·{" "}
                <span className="num">{inr(pickedPaise)}</span>
              </div>
              <div className="text-[0.88rem] text-muted">
                Owed by {pickedPartnerName}
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-3">
              <input
                className={`${field} w-64`}
                placeholder="Note (optional)"
                maxLength={500}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                aria-label="Note"
              />
              <Button variant="quiet" onClick={() => setPicked(new Map())}>
                Clear
              </Button>
              {exec && (
                <Button
                  variant="primary"
                  busy={create.isPending}
                  onClick={make}
                >
                  Create claim
                </Button>
              )}
              {can("cofounder") && !writing && (
                <Button onClick={() => setWriting(true)}>Write off…</Button>
              )}
              {can("cofounder") && writing && (
                <Button
                  variant="primary"
                  busy={writeOff.isPending}
                  onClick={giveUp}
                >
                  Confirm write-off
                </Button>
              )}
            </div>
          </div>
          {writing && (
            <p className="mt-2 text-[0.9rem] text-risk">
              This gives up on the money without claiming it. It cannot be
              claimed afterwards.
            </p>
          )}
          {!exec && !can("cofounder")
            ? null
            : !exec && (
                <p className="mt-2 text-[0.88rem] text-muted">
                  Only the finance executive creates claims. You can write
                  findings off.
                </p>
              )}
          {(create.error || writeOff.error) && (
            <div className="mt-3">
              <ErrorNote error={create.error ?? writeOff.error} />
            </div>
          )}
        </div>
      )}
    </>
  );
}
