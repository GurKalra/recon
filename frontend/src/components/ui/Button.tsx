import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "secondary" | "quiet";

const STYLE: Record<Variant, string> = {
  primary: "bg-accent text-white border border-accent hover:bg-[#162b47]",
  secondary:
    "bg-paper text-ink border border-rule-strong hover:bg-wash hover:border-faint",
  quiet:
    "bg-transparent text-accent border border-transparent underline underline-offset-4 hover:bg-wash",
};

export function Button({
  variant = "secondary",
  busy,
  className = "",
  children,
  disabled,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: Variant;
  busy?: boolean;
}) {
  return (
    <button
      {...rest}
      disabled={disabled || busy}
      className={`inline-flex items-center justify-center gap-2 px-4 py-2 text-[0.95rem] leading-none transition-[background-color,border-color,transform] duration-150 active:translate-y-px disabled:cursor-not-allowed disabled:opacity-50 disabled:active:translate-y-0 ${STYLE[variant]} ${className}`}
    >
      {busy ? "Working…" : children}
    </button>
  );
}
