import { label } from "../../lib/format";

const TONE: Record<string, string> = {
  settled: "text-ok",
  pending: "text-muted",
  short_paid: "text-risk",
  unpaid_past_due: "text-risk",
  return_not_received: "text-risk",
  freight_overcharged: "text-warn",
  // return tracker states
  waiting: "text-muted",
  missing: "text-risk",
  received: "text-ok",
};

const TEXT: Record<string, string> = {
  waiting: "In grace period",
  missing: "Not received",
  received: "Received",
};

/** A coloured square and a word. Colour is never the only signal. */
export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center gap-2 whitespace-nowrap ${TONE[status] ?? "text-muted"}`}
    >
      <span aria-hidden className="inline-block h-2 w-2 bg-current" />
      <span className="text-ink">{TEXT[status] ?? label(status)}</span>
    </span>
  );
}
