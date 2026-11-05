import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "../../components/ui/Button";
import { ErrorNote } from "../../components/ui/Section";
import { useAuth } from "../../lib/auth";
import {
  claimsApi,
  OUTCOME_TEXT,
  rupeesToPaise,
  useClaimAction,
  useDraftClaims,
  type Outcome,
} from "../../lib/claims";
import { inr } from "../../lib/format";
import type { OrderDetail } from "../../lib/types";
import { ConfirmButton } from "./bits";

type Finding = OrderDetail["findings"][number];
const field =
  "border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";
const claimLink = (id: number) => `/claims?tab=claims&claim=${id}`;

/** What can be done about one finding, right where it is shown: open a claim for it, close it with
    the partner's decision, or give up on it. Every button is the same call the Claims screen makes. */
export function FindingActions({ f }: { f: Finding }) {
  const { can } = useAuth();
  const exec = can("finance_exec");
  const cofounder = can("cofounder");
  const open = f.status === "open" && f.claim_id === null;
  const inDraft = f.item_outcome === "pending" && f.claim_status === "draft";
  const sent = f.item_outcome === "pending" && f.claim_status === "submitted";
  const decided =
    f.item_outcome !== null &&
    f.item_outcome !== "pending" &&
    f.item_outcome !== "withdrawn";

  const [adding, setAdding] = useState(false);
  const [target, setTarget] = useState("new");
  const [part, setPart] = useState(false);
  const [amount, setAmount] = useState("");
  const drafts = useDraftClaims(f.partner_id, adding);

  const create = useClaimAction(claimsApi.create);
  const addTo = useClaimAction(claimsApi.addItems);
  const remove = useClaimAction(claimsApi.remove);
  const outcome = useClaimAction(claimsApi.outcome);
  const reopen = useClaimAction(claimsApi.reopen);
  const writeOff = useClaimAction(claimsApi.writeOff);
  const err =
    create.error ??
    addTo.error ??
    remove.error ??
    outcome.error ??
    reopen.error ??
    writeOff.error;

  const paise = rupeesToPaise(amount);
  const badAmount =
    amount !== "" && (paise === null || paise <= 0 || paise >= f.amount_paise);
  const closed = f.days_left !== null && f.days_left < 0;

  if (!open && !inDraft && !sent && !decided) return null;

  return (
    <div className="mt-3 space-y-2 border-t border-rule pt-3">
      {inDraft && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-[0.92rem]">
            In draft{" "}
            <Link
              className="text-accent underline underline-offset-4"
              to={claimLink(f.claim_id!)}
            >
              claim #{f.claim_id}
            </Link>
            , not sent yet.
          </span>
          {exec && (
            <Button
              variant="quiet"
              busy={remove.isPending}
              onClick={() =>
                remove.mutate({ claim: f.claim_id!, finding: f.id })
              }
            >
              Take out of the claim
            </Button>
          )}
        </div>
      )}

      {sent && (
        <>
          <div className="text-[0.92rem]">
            Sent in{" "}
            <Link
              className="text-accent underline underline-offset-4"
              to={claimLink(f.claim_id!)}
            >
              claim #{f.claim_id}
            </Link>
            . Waiting for the partner.
          </div>
          {exec && (
            <div className="flex flex-wrap items-center gap-2">
              <Button
                busy={outcome.isPending}
                onClick={() =>
                  outcome.mutate({ item: f.item_id!, outcome: "recovered" })
                }
              >
                Paid in full
              </Button>
              <Button onClick={() => setPart((v) => !v)} aria-expanded={part}>
                Part paid
              </Button>
              <Button
                busy={outcome.isPending}
                onClick={() =>
                  outcome.mutate({ item: f.item_id!, outcome: "rejected" })
                }
              >
                Rejected
              </Button>
            </div>
          )}
          {cofounder && (
            <ConfirmButton
              confirm="Click again to write off"
              busy={outcome.isPending}
              onConfirm={() =>
                outcome.mutate({ item: f.item_id!, outcome: "written_off" })
              }
            >
              Write off
            </ConfirmButton>
          )}
          {part && exec && (
            <div className="reveal flex flex-wrap items-end gap-3 border border-rule px-4 py-3">
              <label className="text-[0.85rem] text-muted">
                Amount the partner paid (₹)
                <input
                  inputMode="decimal"
                  className={`${field} mt-1 block w-48 ${badAmount ? "border-risk" : ""}`}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder={`less than ${inr(f.amount_paise)}`}
                />
              </label>
              <Button
                variant="primary"
                busy={outcome.isPending}
                disabled={paise === null || badAmount}
                onClick={() =>
                  outcome.mutate(
                    {
                      item: f.item_id!,
                      outcome: "partial",
                      recovered_paise: paise!,
                    },
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
                  Enter more than 0 and less than {inr(f.amount_paise)}.
                </span>
              )}
            </div>
          )}
        </>
      )}

      {decided && (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <span className="text-[0.92rem]">
            {OUTCOME_TEXT[f.item_outcome as Outcome]} in{" "}
            <Link
              className="text-accent underline underline-offset-4"
              to={claimLink(f.claim_id!)}
            >
              claim #{f.claim_id}
            </Link>
            .
          </span>
          {exec && f.claim_status !== "draft" && (
            <Button
              variant="quiet"
              busy={reopen.isPending}
              onClick={() => reopen.mutate(f.item_id!)}
            >
              Undo
            </Button>
          )}
        </div>
      )}

      {open && (
        <>
          {closed && (
            <p className="text-[0.88rem] text-risk">
              The partner’s claim window has closed. You can still claim, but
              they may refuse.
            </p>
          )}
          {!adding && (
            <div className="flex flex-wrap items-center gap-2">
              {exec && (
                <Button variant="primary" onClick={() => setAdding(true)}>
                  Add to a claim
                </Button>
              )}
              {cofounder && (
                <ConfirmButton
                  confirm="Click again to write off"
                  busy={writeOff.isPending}
                  onConfirm={() => writeOff.mutate({ finding_ids: [f.id] })}
                >
                  Write off
                </ConfirmButton>
              )}
              {!exec && !cofounder && (
                <span className="text-[0.88rem] text-muted">
                  Claims are raised by the finance executive.
                </span>
              )}
            </div>
          )}
          {adding && exec && (
            <div className="reveal flex flex-wrap items-end gap-3 border border-rule px-4 py-3">
              <label className="text-[0.85rem] text-muted">
                Which claim
                <select
                  className={`${field} mt-1 block w-64`}
                  value={target}
                  onChange={(e) => setTarget(e.target.value)}
                >
                  <option value="new">A new claim</option>
                  {(drafts.data ?? []).map((c) => (
                    <option key={c.id} value={String(c.id)}>
                      Draft #{c.id} ({c.items}{" "}
                      {c.items === 1 ? "order" : "orders"})
                    </option>
                  ))}
                </select>
              </label>
              <Button
                variant="primary"
                busy={create.isPending || addTo.isPending}
                onClick={() =>
                  target === "new"
                    ? create.mutate(
                        { partner_id: f.partner_id, finding_ids: [f.id] },
                        { onSuccess: () => setAdding(false) },
                      )
                    : addTo.mutate(
                        { claim: Number(target), finding_ids: [f.id] },
                        { onSuccess: () => setAdding(false) },
                      )
                }
              >
                {target === "new" ? "Create claim" : "Add"}
              </Button>
              <Button variant="quiet" onClick={() => setAdding(false)}>
                Cancel
              </Button>
            </div>
          )}
        </>
      )}
      {err && <ErrorNote error={err} />}
    </div>
  );
}
