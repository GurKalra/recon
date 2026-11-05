import { describe, expect, it } from "vitest";
import { columnRefs, norm, setColumn, type Obj } from "./imports";

const spec: Obj = {
  kind: "ledger",
  ledger: {
    order_ref: "Order No",
    date: { column: "Payout Date", format: "dmy" },
    emitters: [
      { amount: "Sale Value", line_type: "sale" },
      { amount: "Platform Fee", line_type: "commission", sign: -1 },
    ],
    net_check: { column: "Net Payout" },
    extras: { store_city: "Store City" },
  },
};

describe("column editor", () => {
  it("lists every column the mapping reads", () => {
    const refs = columnRefs(spec);
    expect(refs.map((r) => r.value)).toEqual(
      expect.arrayContaining([
        "Order No",
        "Payout Date",
        "Sale Value",
        "Platform Fee",
        "Net Payout",
        "Store City",
      ]),
    );
    expect(refs.find((r) => r.key === "awb")?.value).toBeNull(); // optional and unused, but offered
  });

  it("changes one column without touching the original", () => {
    const next = setColumn(spec, ["ledger", "order_ref"], "Ref");
    expect((next.ledger as Obj).order_ref).toBe("Ref");
    expect((spec.ledger as Obj).order_ref).toBe("Order No");
  });

  it("sets a column that was unused and removes an optional one", () => {
    const added = setColumn(spec, ["ledger", "awb"], "AWB");
    expect((added.ledger as Obj).awb).toBe("AWB");
    const removed = setColumn(added, ["ledger", "net_check", "column"], null);
    expect((removed.ledger as Obj).net_check).toBeUndefined();
  });

  it("edits an amount column inside the list", () => {
    const next = setColumn(spec, ["ledger", "emitters", 1, "amount"], "Fee");
    expect(((next.ledger as Obj).emitters as Obj[])[1].amount).toBe("Fee");
  });

  it("matches header names the way the server does", () => {
    expect(norm("  Order   No ")).toBe("order no");
  });
});
