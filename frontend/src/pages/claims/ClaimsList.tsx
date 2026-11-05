import { EmptyState, ErrorNote, Skeleton } from "../../components/ui/Section";
import { STATUS_TEXT, useClaims, type ClaimStatus } from "../../lib/claims";
import { date, inr, int } from "../../lib/format";
import { UrgencyTag } from "./bits";
import { urgency } from "./urgency";

const FILTERS: [string, string][] = [
  ["", "All"],
  ["draft", "Drafts"],
  ["submitted", "Sent"],
  ["closed", "Closed"],
];
const STATUS_TONE: Record<ClaimStatus, string> = {
  draft: "text-muted",
  submitted: "text-warn",
  closed: "text-ok",
};

type Props = {
  sp: URLSearchParams;
  set: (p: Record<string, string | null>) => void;
};

export function ClaimsList({ sp, set }: Props) {
  const status = sp.get("status") ?? "";
  const q = useClaims(status);
  const rows = q.data ?? [];
  return (
    <>
      <div
        className="mt-6 flex items-center gap-2"
        role="group"
        aria-label="Show"
      >
        {FILTERS.map(([k, text]) => (
          <button
            key={k}
            aria-pressed={status === k}
            onClick={() => set({ status: k || null })}
            className={`border px-3 py-1.5 text-[0.92rem] ${status === k ? "border-accent bg-accent text-white" : "border-rule-strong hover:bg-wash"}`}
          >
            {text}
          </button>
        ))}
      </div>
      <div className="mt-4">
        {q.error ? (
          <ErrorNote error={q.error} />
        ) : !q.data ? (
          <Skeleton h="h-72" />
        ) : rows.length === 0 ? (
          <EmptyState
            title={status ? "No claims in this group" : "No claims yet"}
          >
            {status
              ? "Choose another group above."
              : "Pick findings on the “To claim” tab and create a claim. It starts as a draft you can check before sending."}
          </EmptyState>
        ) : (
          <div className="border border-rule">
            <table className="tbl">
              <thead>
                <tr>
                  <th>Claim</th>
                  <th>Partner</th>
                  <th>Status</th>
                  <th className="r">Orders</th>
                  <th className="r">Claimed</th>
                  <th className="r">Recovered</th>
                  <th className="r">Waiting</th>
                  <th>Closest deadline</th>
                  <th>Partner’s case</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((c) => (
                  <tr
                    key={c.id}
                    className="cursor-pointer"
                    onClick={() => set({ claim: String(c.id) })}
                  >
                    <td>
                      <button
                        className="text-accent underline underline-offset-4"
                        onClick={(e) => {
                          e.stopPropagation();
                          set({ claim: String(c.id) });
                        }}
                      >
                        #{c.id}
                      </button>
                      {c.submitted_on && (
                        <div className="text-[0.82rem] text-muted">
                          Sent {date(c.submitted_on)}
                        </div>
                      )}
                    </td>
                    <td>{c.partner_name}</td>
                    <td>
                      <span
                        className={`inline-flex items-center gap-2 ${STATUS_TONE[c.status]}`}
                      >
                        <span
                          aria-hidden
                          className="inline-block h-2 w-2 bg-current"
                        />
                        <span className="text-ink">
                          {STATUS_TEXT[c.status]}
                        </span>
                      </span>
                    </td>
                    <td className="r">{int(c.items)}</td>
                    <td className="r">{inr(c.claimed_paise)}</td>
                    <td className="r">{inr(c.recovered_paise)}</td>
                    <td className="r">
                      {Number(c.pending_paise) > 0 ? inr(c.pending_paise) : "–"}
                    </td>
                    <td>
                      {c.days_left === null ? (
                        <span className="text-faint">–</span>
                      ) : (
                        <UrgencyTag
                          days={c.days_left}
                          urgency={urgency(c.days_left)}
                        />
                      )}
                    </td>
                    <td className="text-muted">{c.partner_ref ?? "–"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
}
