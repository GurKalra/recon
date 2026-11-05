import { describe, expect, it } from "vitest";
import { canPick, firstBucket, rupeesToPaise, urgencyTotals } from "./claims";
import type { Deadline } from "./types";

describe("rupeesToPaise", () => {
  it("turns what a person types into whole paise", () => {
    expect(rupeesToPaise("1299")).toBe(129900);
    expect(rupeesToPaise("1,299.5")).toBe(129950);
    expect(rupeesToPaise(" 0.07 ")).toBe(7);
  });
  it("refuses anything else", () => {
    for (const bad of ["", "abc", "-5", "1.234", "1..2", "₹5"])
      expect(rupeesToPaise(bad)).toBeNull();
  });
});

describe("picking findings", () => {
  const row = { partner_id: 3, claim_id: null };
  it("allows anything when nothing is picked", () =>
    expect(canPick(row, null)).toBe(true));
  it("allows only the same partner after the first pick", () => {
    expect(canPick(row, 3)).toBe(true);
    expect(canPick(row, 4)).toBe(false);
  });
  it("never allows a finding already in a live claim", () =>
    expect(canPick({ partner_id: 3, claim_id: 9 }, null)).toBe(false));
});

describe("deadline totals", () => {
  const rows: Deadline[] = [
    {
      partner_code: "A",
      partner_name: "A",
      urgency: "critical",
      findings: 2,
      amount_paise: "5000",
      nearest_deadline: "2026-11-06",
    },
    {
      partner_code: "B",
      partner_name: "B",
      urgency: "critical",
      findings: 1,
      amount_paise: 700,
      nearest_deadline: "2026-11-07",
    },
    {
      partner_code: "B",
      partner_name: "B",
      urgency: "expired",
      findings: 4,
      amount_paise: "100",
      nearest_deadline: "2026-08-01",
    },
  ];
  it("adds up per urgency, optionally for one partner", () => {
    expect(urgencyTotals(rows).critical).toEqual({ findings: 3, paise: 5700 });
    expect(urgencyTotals(rows, "B").critical).toEqual({
      findings: 1,
      paise: 700,
    });
  });
  it("opens on the most urgent group that can still be claimed", () => {
    expect(firstBucket(urgencyTotals(rows))).toBe("critical");
    expect(firstBucket(urgencyTotals(rows.slice(2)))).toBe("");
  });
});
