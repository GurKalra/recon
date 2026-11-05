import type { ReactNode } from "react";

type Tone = "warn" | "risk" | "info";

const BAR: Record<Tone, string> = {
  warn: "border-l-warn",
  risk: "border-l-risk",
  info: "border-l-accent",
};

/** A single-line notice with a coloured rule on the left. No fills, no icons. */
export function Banner({
  tone = "info",
  children,
  action,
}: {
  tone?: Tone;
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div
      role="status"
      className={`flex items-center justify-between gap-4 border border-rule border-l-4 bg-paper px-4 py-3 ${BAR[tone]}`}
    >
      <div className="text-[0.95rem]">{children}</div>
      {action}
    </div>
  );
}
