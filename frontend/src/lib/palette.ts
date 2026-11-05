/** What the quick-jump menu offers, and how typing narrows it. */

export interface Entry {
  id: string;
  label: string;
  hint?: string;
  group: "Go to" | "Do";
  needs?: "finance_exec" | "cofounder";
  keywords?: string;
}

export const ENTRIES: Entry[] = [
  {
    id: "go:/",
    label: "Overview",
    group: "Go to",
    keywords: "home dashboard money at risk",
  },
  {
    id: "go:/orders",
    label: "Orders",
    group: "Go to",
    keywords: "search order awb",
  },
  {
    id: "go:/review",
    label: "Review queue",
    group: "Go to",
    keywords: "payments without order id match",
  },
  {
    id: "go:/imports",
    label: "Imports",
    group: "Go to",
    keywords: "upload file csv xlsx",
  },
  {
    id: "go:/claims",
    label: "Claims",
    group: "Go to",
    keywords: "recover deadline",
  },
  {
    id: "go:/rates",
    label: "Rate cards",
    group: "Go to",
    keywords: "commission freight fees",
  },
  {
    id: "go:/audit",
    label: "Audit trail",
    group: "Go to",
    needs: "cofounder",
    keywords: "who did what history log",
  },
  {
    id: "do:upload",
    label: "Upload a file",
    group: "Do",
    needs: "finance_exec",
    keywords: "import",
  },
  {
    id: "do:recon",
    label: "Run the reconciliation",
    group: "Do",
    needs: "finance_exec",
    keywords: "refresh statuses",
  },
  { id: "do:signout", label: "Sign out", group: "Do" },
];

export function allowed(
  e: Entry,
  can: (r: "finance_exec" | "cofounder") => boolean,
): boolean {
  return !e.needs || can(e.needs);
}

/** Every typed word must appear in the label or its keywords. Blank shows everything. */
export function filterEntries(entries: Entry[], query: string): Entry[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length === 0) return entries;
  return entries.filter((e) => {
    const hay = `${e.label} ${e.keywords ?? ""}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** An order search starts once there is enough typed to mean something. */
export const wantsOrders = (query: string): boolean => query.trim().length >= 3;
