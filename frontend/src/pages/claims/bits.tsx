import { useEffect, useState, type ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import { int } from "../../lib/format";
import { OUTCOME_TEXT, type Outcome, type Urgency } from "../../lib/claims";

const TONE: Record<Urgency, string> = {
  expired: "text-risk",
  critical: "text-risk",
  soon: "text-warn",
  ok: "text-ok",
};

/** "3 days left", "Closed 73 days ago": a coloured square and words. */
export function UrgencyTag({
  days,
  urgency,
}: {
  days: number;
  urgency: Urgency;
}) {
  const d = Math.abs(days);
  const text =
    days < 0
      ? `Closed ${int(d)} ${d === 1 ? "day" : "days"} ago`
      : days === 0
        ? "Closes today"
        : `${int(d)} ${d === 1 ? "day" : "days"} left`;
  return (
    <span
      className={`inline-flex items-center gap-2 whitespace-nowrap ${TONE[urgency]}`}
    >
      <span aria-hidden className="inline-block h-2 w-2 bg-current" />
      <span className="text-ink">{text}</span>
    </span>
  );
}

const OUT_TONE: Record<Outcome, string> = {
  pending: "text-warn",
  recovered: "text-ok",
  partial: "text-ok",
  rejected: "text-risk",
  written_off: "text-muted",
  withdrawn: "text-muted",
};
export function OutcomeTag({ outcome }: { outcome: Outcome }) {
  return (
    <span
      className={`inline-flex items-center gap-2 whitespace-nowrap ${OUT_TONE[outcome]}`}
    >
      <span aria-hidden className="inline-block h-2 w-2 bg-current" />
      <span className="text-ink">{OUTCOME_TEXT[outcome]}</span>
    </span>
  );
}

/** Two clicks for things that cannot be taken back: the first asks, the second does it. */
export function ConfirmButton({
  children,
  confirm,
  onConfirm,
  busy,
  variant = "secondary",
}: {
  children: ReactNode;
  confirm: string;
  onConfirm: () => void;
  busy?: boolean;
  variant?: "primary" | "secondary" | "quiet";
}) {
  const [asked, setAsked] = useState(false);
  useEffect(() => {
    if (!asked) return;
    const t = setTimeout(() => setAsked(false), 4000);
    return () => clearTimeout(t);
  }, [asked]);
  return (
    <Button
      variant={asked ? "primary" : variant}
      busy={busy}
      onClick={() => (asked ? (setAsked(false), onConfirm()) : setAsked(true))}
    >
      {asked ? confirm : children}
    </Button>
  );
}
