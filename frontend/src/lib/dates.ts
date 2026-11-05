/** Quick ranges for the Orders date filter. Dates are calendar days, written "YYYY-MM-DD". */

export interface Range {
  key: string;
  label: string;
  from: string;
  to: string;
}

const iso = (d: Date): string =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

export function presets(today: Date): Range[] {
  const day = (offset: number) =>
    new Date(today.getFullYear(), today.getMonth(), today.getDate() + offset);
  const first = new Date(today.getFullYear(), today.getMonth(), 1);
  const lastMonthStart = new Date(today.getFullYear(), today.getMonth() - 1, 1);
  const lastMonthEnd = new Date(today.getFullYear(), today.getMonth(), 0);
  return [
    { key: "7", label: "Last 7 days", from: iso(day(-6)), to: iso(today) },
    { key: "30", label: "Last 30 days", from: iso(day(-29)), to: iso(today) },
    { key: "month", label: "This month", from: iso(first), to: iso(today) },
    {
      key: "last",
      label: "Last month",
      from: iso(lastMonthStart),
      to: iso(lastMonthEnd),
    },
  ];
}

/** The preset that exactly matches the current filter, so its button can show as chosen. */
export function activePreset(
  from: string,
  to: string,
  today: Date,
): string | null {
  return (
    presets(today).find((r) => r.from === from && r.to === to)?.key ?? null
  );
}
