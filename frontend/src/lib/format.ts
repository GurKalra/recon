const inrFmt = new Intl.NumberFormat("en-IN", {
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});
const inrWhole = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** Paise to "₹1,23,456.78". Money is integer paise everywhere; only this function divides. */
export function inr(paise: number | string | null | undefined): string {
  if (paise === null || paise === undefined || paise === "") return "—";
  const n = Number(paise);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "−" : ""}₹${inrFmt.format(Math.abs(n) / 100)}`;
}

/** Whole rupees, for headline figures where paise are noise. */
export function inrShort(paise: number | string | null | undefined): string {
  const n = Number(paise ?? 0);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "−" : ""}₹${inrWhole.format(Math.round(Math.abs(n) / 100))}`;
}

export function int(n: number | null | undefined): string {
  return inrWhole.format(n ?? 0);
}

export function pct(n: number | null | undefined, digits = 1): string {
  return n === null || n === undefined ? "—" : `${n.toFixed(digits)}%`;
}

const dateFmt = new Intl.DateTimeFormat("en-IN", {
  day: "numeric",
  month: "short",
  year: "numeric",
});

/** "2026-11-05" (a calendar date, no timezone) to "5 Nov 2026". */
export function date(iso: string | null | undefined): string {
  if (!iso) return "—";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return "—";
  return dateFmt.format(new Date(Date.UTC(y, m - 1, d, 12)));
}

export function monthLabel(ym: string): string {
  const [y, m] = ym.split("-").map(Number);
  if (!y || !m) return ym;
  return new Intl.DateTimeFormat("en-IN", {
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(new Date(Date.UTC(y, m - 1, 1)));
}

export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(iso));
}

export const FINDING_LABEL: Record<string, string> = {
  unpaid_past_due: "Unpaid past due",
  short_paid: "Short-paid",
  freight_overcharged: "Freight overcharged",
  return_not_received: "Return not received",
  settled: "Settled",
  pending: "Pending",
};

export function label(key: string): string {
  return FINDING_LABEL[key] ?? key.replace(/_/g, " ");
}

export function findings(n: number): string {
  return `${int(n)} ${n === 1 ? "finding" : "findings"}`;
}
