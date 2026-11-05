import { useState } from "react";
import { Banner } from "../../components/ui/Banner";
import { Button } from "../../components/ui/Button";
import { Drawer } from "../../components/ui/Drawer";
import { ErrorNote, Skeleton } from "../../components/ui/Section";
import { useAuth } from "../../lib/auth";
import {
  claimsApi,
  EVENT_TEXT,
  rupeesToPaise,
  STATUS_TEXT,
  useClaim,
  useClaimAction,
  useFindingHistory,
  type ClaimItem,
} from "../../lib/claims";
import { date, dateTime, inr, int, label } from "../../lib/format";
import { usePartners } from "../../lib/queries";
import { ConfirmButton, OutcomeTag, UrgencyTag } from "./bits";
import { urgency } from "./urgency";

const field =
  "border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";

/** One claim: its orders, what the partner decided about each, and the evidence to send. */
export function ClaimDrawer({
  id,
  onClose,
}: {
  id: number | null;
  onClose: () => void;
}) {
  const { can } = useAuth();
  const exec = can("finance_exec");
  const q = useClaim(id);
  const partners = usePartners();
  const c = q.data;
  const partner = partners.data?.find((p) => p.id === c?.partner_id);

  const [ref, setRef] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const submit = useClaimAction(claimsApi.submit);
  const discard = useClaimAction(claimsApi.discard);
  const remove = useClaimAction(claimsApi.remove);

  const claimed = c?.items.reduce((s, i) => s + i.claimed_paise, 0) ?? 0;
  const recovered = c?.items.reduce((s, i) => s + i.recovered_paise, 0) ?? 0;
  const waiting =
    c?.items
      .filter((i) => i.outcome === "pending")
      .reduce((s, i) => s + i.claimed_paise, 0) ?? 0;

  return (
    <Drawer
      open={id !== null}
      onClose={() => {
        setWarnings([]);
        setRef("");
        onClose();
      }}
      title={id ? `Claim #${id}` : ""}
      description={
        c
          ? [
              partner?.name,
              c.status === "draft"
                ? STATUS_TEXT.draft
                : `Sent ${date(c.submitted_on)}${c.status === "closed" ? ", all decided" : ""}`,
              c.partner_ref ? `their case ${c.partner_ref}` : null,
            ]
              .filter(Boolean)
              .join(" · ")
          : undefined
      }
    >
      {q.error ? (
        <ErrorNote error={q.error} />
      ) : !c ? (
        <Skeleton h="h-64" />
      ) : (
        <div className="space-y-7">
          <dl className="grid grid-cols-3 gap-6 border-y border-rule py-5">
            <div>
              <dt className="eyebrow">Claimed</dt>
              <dd className="num mt-1 text-[1.4rem]">{inr(claimed)}</dd>
            </div>
            <div>
              <dt className="eyebrow">Recovered</dt>
              <dd className="num mt-1 text-[1.4rem] text-ok">
                {inr(recovered)}
              </dd>
            </div>
            <div>
              <dt className="eyebrow">Waiting</dt>
              <dd className="num mt-1 text-[1.4rem] text-warn">
                {inr(waiting)}
              </dd>
            </div>
          </dl>
          {c.note && (
            <p className="text-[0.95rem] text-muted">Note: {c.note}</p>
          )}

          {c.status === "draft" && (
            <div className="space-y-3 border border-rule px-5 py-4">
              <h3 className="text-[1.1rem]">Ready to send?</h3>
              <p className="text-[0.92rem] text-muted">
                Download the evidence pack, send it to the partner, then mark
                the claim as sent. The claim window keeps running while it is a
                draft.
              </p>
              <div className="flex flex-wrap items-center gap-3">
                <a
                  href={`/api/claims/${c.id}/evidence`}
                  className="inline-flex items-center border border-rule-strong px-4 py-2 text-[0.95rem] hover:bg-wash"
                >
                  Download the evidence pack
                </a>
              </div>
              {exec && (
                <div className="flex flex-wrap items-end gap-3 pt-1">
                  <label className="text-[0.85rem] text-muted">
                    Partner’s case number (optional)
                    <input
                      className={`${field} mt-1 block w-64`}
                      value={ref}
                      maxLength={100}
                      onChange={(e) => setRef(e.target.value)}
                    />
                  </label>
                  <Button
                    variant="primary"
                    busy={submit.isPending}
                    onClick={() =>
                      submit.mutate(
                        { id: c.id, partner_ref: ref.trim() || undefined },
                        { onSuccess: (r) => setWarnings(r.warnings) },
                      )
                    }
                  >
                    Mark as sent
                  </Button>
                  <ConfirmButton
                    confirm="Click again to discard"
                    busy={discard.isPending}
                    variant="quiet"
                    onConfirm={() =>
                      discard.mutate(c.id, { onSuccess: onClose })
                    }
                  >
                    Discard draft
                  </ConfirmButton>
                </div>
              )}
              {!exec && (
                <p className="text-[0.88rem] text-muted">
                  Only the finance executive sends claims.
                </p>
              )}
              {(submit.error || discard.error) && (
                <ErrorNote error={submit.error ?? discard.error} />
              )}
            </div>
          )}

          {warnings.length > 0 && (
            <Banner tone="warn">
              <strong>Sent, but the partner may refuse some orders.</strong>{" "}
              Their claim window had already closed:
              <ul className="mt-1 list-disc pl-5 text-[0.9rem]">
                {warnings.slice(0, 8).map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
              {warnings.length > 8 && (
                <span className="text-[0.88rem] text-muted">
                  and {int(warnings.length - 8)} more.
                </span>
              )}
            </Banner>
          )}

          {c.status !== "draft" && (
            <a
              href={`/api/claims/${c.id}/evidence`}
              className="inline-block text-[0.95rem] text-accent underline underline-offset-4"
            >
              Download the evidence pack
            </a>
          )}

          <section>
            <h3 className="mb-2 text-[1.15rem]">
              Orders in this claim ({int(c.items.length)})
            </h3>
            {remove.error && (
              <div className="mb-3">
                <ErrorNote error={remove.error} />
              </div>
            )}
            <ul className="border-t border-rule">
              {c.items.map((it) => (
                <Item
                  key={it.id}
                  item={it}
                  status={c.status}
                  onRemove={() =>
                    remove.mutate({ claim: c.id, finding: it.finding_id })
                  }
                />
              ))}
            </ul>
          </section>
        </div>
      )}
    </Drawer>
  );
}

function Item({
  item,
  status,
  onRemove,
}: {
  item: ClaimItem;
  status: string;
  onRemove: () => void;
}) {
  const { can } = useAuth();
  const exec = can("finance_exec");
  const cofounder = can("cofounder");
  const [part, setPart] = useState(false);
  const [amount, setAmount] = useState("");
  const [history, setHistory] = useState(false);
  const act = useClaimAction(claimsApi.outcome);
  const reopen = useClaimAction(claimsApi.reopen);
  const hist = useFindingHistory(item.finding_id, history);

  const paise = rupeesToPaise(amount);
  const badAmount =
    amount !== "" &&
    (paise === null || paise <= 0 || paise >= item.claimed_paise);
  const pending = item.outcome === "pending" && status === "submitted";
  const err = act.error ?? reopen.error;

  return (
    <li className="border-b border-rule py-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="text-[1.05rem]">{item.order_ref}</div>
          <div className="text-[0.9rem] text-muted">
            {label(item.type)} · claim by {date(item.claim_deadline)}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="num text-[1.05rem]">{inr(item.claimed_paise)}</div>
          {item.recovered_paise > 0 && (
            <div className="num text-[0.88rem] text-ok">
              Recovered {inr(item.recovered_paise)}
            </div>
          )}
        </div>
      </div>

      <div className="mt-2 flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-1 text-[0.92rem]">
          {status === "draft" ? (
            <UrgencyTag
              days={item.days_left}
              urgency={urgency(item.days_left)}
            />
          ) : (
            <OutcomeTag outcome={item.outcome} />
          )}
          {status !== "draft" && item.outcome === "pending" && (
            <UrgencyTag
              days={item.days_left}
              urgency={urgency(item.days_left)}
            />
          )}
          {item.note && <span className="text-muted">{item.note}</span>}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {status === "draft" && exec && (
            <Button variant="quiet" onClick={onRemove}>
              Remove
            </Button>
          )}
          {pending && exec && (
            <>
              <Button
                busy={act.isPending}
                onClick={() =>
                  act.mutate({ item: item.id, outcome: "recovered" })
                }
              >
                Paid in full
              </Button>
              <Button onClick={() => setPart((v) => !v)} aria-expanded={part}>
                Part paid
              </Button>
              <Button
                busy={act.isPending}
                onClick={() =>
                  act.mutate({ item: item.id, outcome: "rejected" })
                }
              >
                Rejected
              </Button>
            </>
          )}
          {pending && cofounder && (
            <ConfirmButton
              confirm="Click again to write off"
              busy={act.isPending}
              onConfirm={() =>
                act.mutate({ item: item.id, outcome: "written_off" })
              }
            >
              Write off
            </ConfirmButton>
          )}
          {status !== "draft" &&
            item.outcome !== "pending" &&
            item.outcome !== "withdrawn" &&
            exec && (
              <Button
                variant="quiet"
                busy={reopen.isPending}
                onClick={() => reopen.mutate(item.id)}
              >
                Undo
              </Button>
            )}
          {status !== "draft" && (
            <Button
              variant="quiet"
              onClick={() => setHistory((v) => !v)}
              aria-expanded={history}
            >
              {history ? "Hide history" : "History"}
            </Button>
          )}
        </div>
      </div>

      {part && pending && exec && (
        <div className="reveal mt-3 flex flex-wrap items-end gap-3 border border-rule px-4 py-3">
          <label className="text-[0.85rem] text-muted">
            Amount the partner paid (₹)
            <input
              inputMode="decimal"
              className={`${field} mt-1 block w-48 ${badAmount ? "border-risk" : ""}`}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder={`less than ${inr(item.claimed_paise)}`}
            />
          </label>
          <Button
            variant="primary"
            busy={act.isPending}
            disabled={paise === null || badAmount}
            onClick={() =>
              act.mutate(
                { item: item.id, outcome: "partial", recovered_paise: paise! },
                {
                  onSuccess: () => {
                    setPart(false);
                    setAmount("");
                  },
                },
              )
            }
          >
            Save part payment
          </Button>
          {badAmount && (
            <span className="text-[0.88rem] text-risk">
              Enter more than 0 and less than {inr(item.claimed_paise)}.
            </span>
          )}
        </div>
      )}

      {history && (
        <div className="reveal mt-3 border-l-2 border-rule-strong pl-4 text-[0.9rem]">
          {hist.isLoading ? (
            <span className="text-muted">Loading…</span>
          ) : (hist.data ?? []).length === 0 ? (
            <span className="text-muted">No history yet.</span>
          ) : (
            <ul className="space-y-1">
              {hist.data!.map((e, i) => (
                <li key={i}>
                  <span className="text-muted">{dateTime(e.at)}</span> ·{" "}
                  {EVENT_TEXT[e.kind] ?? label(e.kind)}
                  {e.detail &&
                  typeof e.detail.note === "string" &&
                  e.detail.note ? (
                    <span className="text-muted"> ({e.detail.note})</span>
                  ) : null}
                  {e.detail &&
                  typeof e.detail.partner_ref === "string" &&
                  e.detail.partner_ref ? (
                    <span className="text-muted">
                      {" "}
                      (their case {e.detail.partner_ref})
                    </span>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {err && (
        <div className="mt-3">
          <ErrorNote error={err} />
        </div>
      )}
    </li>
  );
}
