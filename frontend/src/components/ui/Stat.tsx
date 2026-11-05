import type { ReactNode } from "react";
import { Link } from "react-router-dom";

type Tone = "plain" | "risk" | "warn" | "ok";
const COLOR: Record<Tone, string> = {
  plain: "text-ink",
  risk: "text-risk",
  warn: "text-warn",
  ok: "text-ok",
};

export function Stat({
  label,
  value,
  note,
  tone = "plain",
  large,
  to,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  tone?: Tone;
  large?: boolean;
  to?: string;
}) {
  const body = (
    <>
      <div className="eyebrow">{label}</div>
      <div
        className={`num mt-1 ${large ? "text-[2.6rem] leading-[1.1]" : "text-[1.55rem] leading-tight"} ${COLOR[tone]}`}
      >
        {value}
      </div>
      {note && <div className="mt-1 text-[0.88rem] text-muted">{note}</div>}
    </>
  );
  if (!to) return <div className="min-w-0">{body}</div>;
  return (
    <Link
      to={to}
      className="-m-3 block min-w-0 p-3 transition-colors hover:bg-wash"
    >
      {body}
    </Link>
  );
}
