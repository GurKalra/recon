import { describe, expect, it } from "vitest";
import { ENTRIES, allowed, filterEntries, wantsOrders } from "./palette";

describe("quick-jump menu", () => {
  it("shows everything for a blank query", () => {
    expect(filterEntries(ENTRIES, "  ")).toHaveLength(ENTRIES.length);
  });
  it("matches words in the label or keywords, in any order", () => {
    expect(filterEntries(ENTRIES, "rate").map((e) => e.label)).toContain(
      "Rate cards",
    );
    expect(filterEntries(ENTRIES, "who did").map((e) => e.label)).toEqual([
      "Audit trail",
    ]);
    expect(
      filterEntries(ENTRIES, "reconciliation run").map((e) => e.label),
    ).toEqual(["Run the reconciliation"]);
    expect(filterEntries(ENTRIES, "zzz")).toEqual([]);
  });
  it("hides what a role cannot use", () => {
    const exec = (r: string) => r === "finance_exec";
    const co = (r: string) => r === "cofounder";
    const names = (can: (r: "finance_exec" | "cofounder") => boolean) =>
      ENTRIES.filter((e) => allowed(e, can)).map((e) => e.label);
    expect(names(exec)).toContain("Run the reconciliation");
    expect(names(exec)).not.toContain("Audit trail");
    expect(names(co)).toContain("Audit trail");
    expect(names(co)).not.toContain("Upload a file");
  });
  it("starts searching orders after three characters", () => {
    expect(wantsOrders("OD")).toBe(false);
    expect(wantsOrders(" OD1 ")).toBe(true);
  });
});
