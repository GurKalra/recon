import type { ReactNode } from "react";

export function PageHeader({
  title,
  lead,
  actions,
}: {
  title: string;
  lead?: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <header className="flex items-end justify-between gap-6 border-b border-rule pb-5">
      <div>
        <h1 className="text-[2rem] leading-tight">{title}</h1>
        {lead && <p className="mt-1 max-w-[62ch] text-muted">{lead}</p>}
      </div>
      {actions && <div className="flex shrink-0 gap-2">{actions}</div>}
    </header>
  );
}

export function Section({
  title,
  note,
  right,
  children,
}: {
  title: string;
  note?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="mt-10">
      <div className="mb-3 flex items-baseline justify-between gap-4">
        <div>
          <h2 className="text-[1.25rem]">{title}</h2>
          {note && (
            <p className="mt-0.5 max-w-[70ch] text-[0.92rem] text-muted">
              {note}
            </p>
          )}
        </div>
        {right}
      </div>
      {children}
    </section>
  );
}

export function Skeleton({ h = "h-24" }: { h?: string }) {
  return <div className={`${h} animate-pulse bg-wash`} aria-hidden />;
}

export function EmptyState({
  title,
  children,
  action,
}: {
  title: string;
  children?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="border border-dashed border-rule-strong px-8 py-12 text-center">
      <h3 className="text-[1.3rem]">{title}</h3>
      {children && (
        <p className="mx-auto mt-2 max-w-[55ch] text-muted">{children}</p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

export function ErrorNote({ error }: { error: unknown }) {
  return (
    <p
      role="alert"
      className="border border-rule border-l-4 border-l-risk px-4 py-3 text-[0.95rem]"
    >
      {error instanceof Error ? error.message : "Something went wrong."}
    </p>
  );
}
