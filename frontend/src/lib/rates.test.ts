import { describe, expect, it } from "vitest";
import {
  bpsToPct,
  buildChannel,
  buildCourier,
  channelFormFrom,
  courierFormFrom,
  describeChange,
  paiseToRupees,
  pctToBps,
} from "./rates";

describe("percentages and rupees", () => {
  it("convert both ways without drifting", () => {
    expect(pctToBps("2.5")).toBe(250);
    expect(pctToBps("18%")).toBe(1800);
    expect(pctToBps("0.07")).toBe(7);
    expect(bpsToPct(250)).toBe("2.5");
    expect(bpsToPct(1800)).toBe("18");
    expect(paiseToRupees(12950)).toBe("129.5");
    expect(paiseToRupees(3000)).toBe("30");
  });
  it("refuses nonsense", () => {
    for (const x of ["", "abc", "-1", "100.5", "1.234", "101"])
      expect(pctToBps(x)).toBeNull();
  });
});

describe("building a channel card", () => {
  const form = {
    ...channelFormFrom(undefined, "2026-11-05"),
    commission: "12",
    pg: "2",
    tcs: "1",
    tds: "1",
  };
  it("sends integers and drops empty bands", () => {
    form.slabs = [
      { max_g: "500", fee: "45" },
      { max_g: "", fee: "" },
    ];
    const r = buildChannel(form, 3);
    expect(r.ok && r.body).toMatchObject({
      partner_id: 3,
      commission_bps: 1200,
      pg_fee_bps: 200,
      ship_slabs: [{ max_g: 500, fee_paise: 4500 }],
    });
  });
  it("refuses a card with nothing in it", () => {
    const empty = channelFormFrom(undefined, "2026-11-05");
    expect(buildChannel(empty, 3)).toEqual({
      ok: false,
      error: expect.stringContaining("zero"),
    });
    expect(buildCourier(courierFormFrom(undefined, "2026-11-05"), 9).ok).toBe(
      false,
    );
  });
  it("says what is wrong in words", () => {
    expect(buildChannel({ ...form, commission: "x" }, 3)).toEqual({
      ok: false,
      error: expect.stringContaining("Commission"),
    });
    expect(buildChannel({ ...form, effective_from: "" }, 3)).toEqual({
      ok: false,
      error: expect.stringContaining("date"),
    });
    expect(
      buildChannel(
        {
          ...form,
          slabs: [
            { max_g: "500", fee: "1" },
            { max_g: "500", fee: "2" },
          ],
        },
        3,
      ).ok,
    ).toBe(false);
  });
});

describe("building a courier card", () => {
  it("needs a whole return percentage and four zones", () => {
    const f = courierFormFrom(undefined, "2026-11-05");
    f.zones = f.zones.map((z) => ({ ...z, base: "40", addl: "30" }));
    const r = buildCourier(f, 9);
    expect(r.ok && (r.body.zone_rates as unknown[]).length).toBe(4);
    expect(buildCourier({ ...f, return_pct: "10.5" }, 9).ok).toBe(false);
  });
});

describe("describeChange", () => {
  it("reads like a sentence", () => {
    expect(
      describeChange({ field: "commission_bps", from: 1200, to: 1400 }),
    ).toBe("Commission: 12% to 14%");
    expect(describeChange({ field: "return_pct", from: 100, to: 50 })).toBe(
      "Return pct: 100% to 50%",
    );
  });
});
