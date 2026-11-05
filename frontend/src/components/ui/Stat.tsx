import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";

type Tone = "plain" | "risk" | "warn" | "ok";
const COLOR: Record<Tone, string> = {
  plain: "text-ink",
  risk: "text-risk",
  warn: "text-warn",
  ok: "text-ok",
};

/** Eases a number from its previous value to the new one. Jumps straight there if motion is reduced. */
function useCountUp(target: number, ms = 800): number {
  const [shown, setShown] = useState(0);
  const from = useRef(0);
  useEffect(() => {
    const reduce = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;
    if (reduce || from.current === target) {
      from.current = target;
      setShown(target);
      return;
    }
    const start = performance.now();
    const a = from.current;
    let raf = 0;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / ms);
      const eased = 1 - Math.pow(1 - t, 3);
      setShown(a + (target - a) * eased);
      if (t < 1) raf = requestAnimationFrame(tick);
      else from.current = target;
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, ms]);
  return shown;
}

// "₹74,790", "99.4%" or "72": a plain figure we can count up to while keeping its format.
const FIGURE = /^(−?₹)?([\d,]+)(\.\d+)?(%)?$/;

function Figure({ text }: { text: string }) {
  const m = FIGURE.exec(text);
  const target = m ? parseFloat(m[2].replace(/,/g, "") + (m[3] ?? "")) : 0;
  const shown = useCountUp(target);
  if (!m) return <>{text}</>;
  const digits = m[3] ? m[3].length - 1 : 0;
  const out = new Intl.NumberFormat("en-IN", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(shown);
  return <>{`${m[1] ?? ""}${out}${m[4] ?? ""}`}</>;
}

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
        {typeof value === "string" ? <Figure text={value} /> : value}
      </div>
      {note && <div className="mt-1 text-[0.88rem] text-muted">{note}</div>}
    </>
  );
  if (!to) return <div className="min-w-0">{body}</div>;
  return (
    <Link
      to={to}
      className="-m-3 block min-w-0 p-3 transition-colors duration-150 hover:bg-wash"
    >
      {body}
    </Link>
  );
}
