import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Banner } from "../components/ui/Banner";
import { Button } from "../components/ui/Button";
import { Drawer } from "../components/ui/Drawer";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Section,
  Skeleton,
} from "../components/ui/Section";
import { useAuth } from "../lib/auth";
import { date, int } from "../lib/format";
import { useReconStatus, useRunRecon } from "../lib/queries";
import {
  describeChange,
  terms,
  useRateCards,
  type RatePartner,
  type Saved,
} from "../lib/rates";
import { CardForm } from "./rates/CardForm";
import { CardView } from "./rates/CardView";

export function Rates() {
  const q = useRateCards();
  const status = useReconStatus();
  const run = useRunRecon();
  const exec = useAuth().can("finance_exec");
  const [sp, setSp] = useSearchParams();
  const [adding, setAdding] = useState(false);
  const [saved, setSaved] = useState<Saved | null>(null);

  const partners = q.data ?? [];
  const open = partners.find((p) => String(p.id) === sp.get("partner")) ?? null;
  const close = () => {
    setSp({});
    setAdding(false);
  };
  const groups: [string, string, RatePartner[]][] = [
    [
      "Channels",
      "Marketplaces, the website and quick-commerce apps. Their cards say what they take out of each payout.",
      partners.filter((p) => p.kind === "channel"),
    ],
    [
      "Couriers",
      "Freight, COD and return charges. Their cards say what each shipment should cost.",
      partners.filter((p) => p.kind === "courier"),
    ],
  ];

  return (
    <>
      <PageHeader
        title="Rate cards"
        lead="What each partner is supposed to charge. Every payout and freight bill is checked against the card that applied on the order date, so a rate change never rewrites history."
      />

      {(saved || status.data?.stale) && (
        <div className="mt-6">
          <Banner
            tone="warn"
            action={
              exec ? (
                <Button
                  busy={run.isPending}
                  onClick={() =>
                    run.mutate(undefined, { onSuccess: () => setSaved(null) })
                  }
                >
                  Run reconciliation
                </Button>
              ) : undefined
            }
          >
            {saved
              ? `Card saved. ${int(saved.orders_affected)} order${saved.orders_affected === 1 ? "" : "s"} will be judged by it when the reconciliation is run.`
              : "Order statuses are out of date."}
          </Banner>
        </div>
      )}

      {q.error ? (
        <div className="mt-8">
          <ErrorNote error={q.error} />
        </div>
      ) : !q.data ? (
        <div className="mt-8">
          <Skeleton h="h-72" />
        </div>
      ) : (
        groups.map(([title, note, rows]) => (
          <Section key={title} title={title} note={note}>
            {rows.length === 0 ? (
              <EmptyState title={`No ${title.toLowerCase()} yet`}>
                Partners appear here once they exist.
              </EmptyState>
            ) : (
              <div className="border border-rule">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Partner</th>
                      <th>In force from</th>
                      <th>Terms</th>
                      <th className="r">Cards</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((p) => {
                      const cur = p.cards.find((c) => c.in_force);
                      return (
                        <tr
                          key={p.id}
                          className="cursor-pointer"
                          onClick={() => setSp({ partner: String(p.id) })}
                        >
                          <td>
                            <button
                              className="text-accent underline underline-offset-4"
                              onClick={(e) => {
                                e.stopPropagation();
                                setSp({ partner: String(p.id) });
                              }}
                            >
                              {p.name}
                            </button>
                            <div className="text-[0.82rem] text-muted">
                              {p.code}
                            </div>
                          </td>
                          <td>
                            {cur ? (
                              date(cur.effective_from)
                            ) : (
                              <span className="text-risk">No card</span>
                            )}
                          </td>
                          <td className="text-muted">{terms(p)}</td>
                          <td className="r">{int(p.cards.length)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Section>
        ))
      )}

      <Drawer
        open={open !== null}
        onClose={close}
        title={open?.name ?? ""}
        description={
          open
            ? open.kind === "channel"
              ? "Channel rate cards, newest first."
              : "Courier rate cards, newest first."
            : undefined
        }
      >
        {open && (
          <div className="space-y-6">
            {saved && !adding && (
              <div className="border border-rule border-l-4 border-l-ok px-4 py-3 text-[0.95rem]">
                <p>
                  Saved.{" "}
                  {saved.changes.length
                    ? "What changed from the card before it:"
                    : "This is the first card for this partner."}
                </p>
                {saved.changes.length > 0 && (
                  <ul className="mt-1 list-disc pl-5">
                    {saved.changes.map((c) => (
                      <li key={c.field}>{describeChange(c)}</li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {exec && !adding && (
              <Button
                variant="primary"
                onClick={() => {
                  setSaved(null);
                  setAdding(true);
                }}
              >
                Add a new card
              </Button>
            )}
            {!exec && (
              <p className="text-[0.92rem] text-muted">
                Only the finance team can add cards. You can see every card and
                when it applies.
              </p>
            )}
            {adding && (
              <CardForm
                partner={open}
                onCancel={() => setAdding(false)}
                onSaved={(s) => {
                  setSaved(s);
                  setAdding(false);
                }}
              />
            )}
            <CardView partner={open} />
          </div>
        )}
      </Drawer>
    </>
  );
}
