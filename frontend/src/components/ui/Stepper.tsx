/** Numbered steps in a row. Flat squares and a rule: done is filled, current is outlined and bold. */
export function Stepper({
  steps,
  current,
}: {
  steps: string[];
  current: number;
}) {
  return (
    <ol className="flex items-center" aria-label="Progress">
      {steps.map((label, i) => {
        const done = i < current;
        const now = i === current;
        return (
          <li
            key={label}
            className="flex flex-1 items-center last:flex-none"
            aria-current={now ? "step" : undefined}
          >
            <span className="flex items-center gap-3">
              <span
                className={`flex h-7 w-7 items-center justify-center border text-[0.85rem] transition-colors duration-200 ${
                  done
                    ? "border-accent bg-accent text-white"
                    : now
                      ? "border-accent text-accent"
                      : "border-rule-strong text-faint"
                }`}
              >
                {done ? (
                  <svg
                    width="12"
                    height="12"
                    viewBox="0 0 12 12"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="1.6"
                    aria-hidden
                  >
                    <path d="M2 6.2l2.6 2.6L10 3.4" />
                  </svg>
                ) : (
                  i + 1
                )}
              </span>
              <span
                className={`text-[0.95rem] ${now ? "font-bold text-ink" : done ? "text-ink" : "text-faint"}`}
              >
                {label}
              </span>
            </span>
            {i < steps.length - 1 && (
              <span
                className={`mx-4 h-px flex-1 transition-colors duration-200 ${done ? "bg-accent" : "bg-rule-strong"}`}
                aria-hidden
              />
            )}
          </li>
        );
      })}
    </ol>
  );
}
