import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAi } from "../lib/ai";
import { ROLE_LABEL, useAuth } from "../lib/auth";
import { useReviewSummary } from "../lib/queries";
import { PageTransition } from "./PageTransition";
import { Button } from "./ui/Button";

const Icon = ({ children }: { children: ReactNode }) => (
  <svg
    width="18"
    height="18"
    viewBox="0 0 18 18"
    fill="none"
    stroke="currentColor"
    strokeWidth="1.4"
    strokeLinecap="square"
    aria-hidden
    className="shrink-0"
  >
    {children}
  </svg>
);

const NAV: {
  to: string;
  label: string;
  icon: ReactNode;
  needs?: "cofounder";
}[] = [
  {
    to: "/",
    label: "Overview",
    icon: (
      <Icon>
        <rect x="2" y="2" width="5.5" height="5.5" />
        <rect x="10.5" y="2" width="5.5" height="5.5" />
        <rect x="2" y="10.5" width="5.5" height="5.5" />
        <rect x="10.5" y="10.5" width="5.5" height="5.5" />
      </Icon>
    ),
  },
  {
    to: "/orders",
    label: "Orders",
    icon: (
      <Icon>
        <path d="M2 4h14M2 9h14M2 14h14" />
      </Icon>
    ),
  },
  {
    to: "/review",
    label: "Review queue",
    icon: (
      <Icon>
        <circle cx="9" cy="9" r="7" />
        <path d="M5.5 9.2l2.4 2.4 4.6-5" />
      </Icon>
    ),
  },
  {
    to: "/imports",
    label: "Imports",
    icon: (
      <Icon>
        <path d="M9 12V2M5 6l4-4 4 4M2 12v4h14v-4" />
      </Icon>
    ),
  },
  {
    to: "/claims",
    label: "Claims",
    icon: (
      <Icon>
        <path d="M4 16V2M4 3h10l-2.5 3.5L14 10H4" />
      </Icon>
    ),
  },
  {
    to: "/rates",
    label: "Rate cards",
    icon: (
      <Icon>
        <rect x="2" y="3" width="14" height="12" />
        <path d="M2 7h14M6 11h3" />
      </Icon>
    ),
  },
  {
    to: "/audit",
    label: "Audit trail",
    needs: "cofounder",
    icon: (
      <Icon>
        <circle cx="9" cy="9" r="7" />
        <path d="M9 4.5V9l3 2" />
      </Icon>
    ),
  },
];

const KEY = "recon.sidebar.collapsed";

function useCollapsed(): [boolean, () => void] {
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(KEY) === "1";
    } catch {
      return false;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(KEY, collapsed ? "1" : "0");
    } catch {
      /* a light per-browser convenience; fine to lose */
    }
  }, [collapsed]);
  return [collapsed, () => setCollapsed((c) => !c)];
}

/** What is waiting in the review queue, and whether the AI is working or has just answered. */
function ReviewBadge({ collapsed }: { collapsed: boolean }) {
  const ai = useAi();
  const open = useReviewSummary().data?.by_status.open.payments ?? 0;

  if (ai.status === "running") {
    return (
      <span
        className="relative ml-auto flex h-2.5 w-2.5 shrink-0"
        title="The AI is reviewing payments"
        role="status"
        aria-label="AI is working"
      >
        <span className="ping absolute inset-0" />
        <span className="relative h-2.5 w-2.5 rounded-full bg-accent" />
      </span>
    );
  }
  if (ai.unseen > 0) {
    return (
      <span
        className={`shrink-0 bg-accent px-1.5 text-[0.75rem] leading-[1.35rem] text-white ${collapsed ? "absolute right-1 top-0.5" : "ml-auto"}`}
        title={`${ai.unseen} new AI opinions`}
        role="status"
        aria-label={`${ai.unseen} new AI opinions`}
      >
        {ai.unseen}
      </span>
    );
  }
  if (open > 0) {
    return (
      <span
        className={`num shrink-0 border border-rule-strong px-1.5 text-[0.75rem] leading-[1.2rem] text-muted ${collapsed ? "absolute right-1 top-0.5 bg-wash" : "ml-auto"}`}
        title={`${open} payments waiting for a decision`}
      >
        {open}
      </span>
    );
  }
  return null;
}

export function Shell() {
  const { user, signOut, can } = useAuth();
  const [collapsed, toggle] = useCollapsed();
  return (
    <div
      className="grid min-h-screen transition-[grid-template-columns] duration-200"
      style={{
        gridTemplateColumns: `${collapsed ? "4.5rem" : "14.5rem"} minmax(0,1fr)`,
      }}
    >
      <aside className="sticky top-0 flex h-screen flex-col overflow-hidden border-r border-rule bg-wash px-4 py-6">
        <div className="flex items-start justify-between gap-2 px-2">
          {!collapsed && (
            <div>
              <div className="text-[1.5rem] leading-none tracking-tight">
                Recon
              </div>
              <div className="eyebrow mt-1.5">Isolabs finance</div>
            </div>
          )}
          <button
            type="button"
            onClick={toggle}
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            aria-expanded={!collapsed}
            title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            className={`flex h-8 w-8 items-center justify-center border border-rule-strong bg-paper text-muted hover:text-ink ${collapsed ? "mx-auto" : ""}`}
          >
            <svg
              width="14"
              height="14"
              viewBox="0 0 14 14"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.4"
              aria-hidden
            >
              <path d={collapsed ? "M5 2l5 5-5 5" : "M9 2L4 7l5 5"} />
            </svg>
          </button>
        </div>
        <nav aria-label="Main" className="mt-8 flex flex-col gap-0.5">
          {NAV.filter((n) => !n.needs || can(n.needs)).map((n) => (
            <NavLink
              key={n.to}
              to={n.to}
              end={n.to === "/"}
              title={collapsed ? n.label : undefined}
              className={({ isActive }) =>
                `relative flex items-center gap-3 border-l-2 px-2.5 py-2 text-[1.02rem] transition-colors ${collapsed ? "justify-center" : ""} ${
                  isActive
                    ? "border-accent font-bold text-accent"
                    : "border-transparent text-muted hover:bg-paper hover:text-ink"
                }`
              }
            >
              {n.icon}
              {!collapsed && <span className="truncate">{n.label}</span>}
              {n.to === "/review" && <ReviewBadge collapsed={collapsed} />}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto border-t border-rule-strong px-2 pt-4 text-[0.9rem]">
          {!collapsed && (
            <>
              <div className="truncate">{user?.full_name}</div>
              <div className="text-muted">
                {user ? ROLE_LABEL[user.role] : ""}
              </div>
            </>
          )}
          <Button
            variant="quiet"
            className="!px-0 !pt-3"
            onClick={() => void signOut()}
            title="Sign out"
          >
            {collapsed ? "Out" : "Sign out"}
          </Button>
        </div>
      </aside>
      <main className="min-w-0 px-12 pb-20 pt-9">
        <div className="mx-auto max-w-[1180px]">
          <PageTransition>
            <Outlet />
          </PageTransition>
        </div>
      </main>
    </div>
  );
}
