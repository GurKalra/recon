import { describe, expect, it } from "vitest";
import { date, inr, inrShort, pct } from "./format";

describe("format", () => {
  it("groups rupees the Indian way", () => {
    expect(inr(12345678)).toBe("₹1,23,456.78");
    expect(inrShort(7478974)).toBe("₹74,790");
  });
  it("shows negatives with a minus sign and accepts numeric strings", () => {
    expect(inr(-18000)).toBe("−₹180.00");
    expect(inr("1231957")).toBe("₹12,319.57");
  });
  it("handles missing values", () => {
    expect(inr(null)).toBe("—");
    expect(date(undefined)).toBe("—");
    expect(pct(null)).toBe("—");
  });
  it("formats calendar dates without timezone drift", () => {
    expect(date("2026-11-05")).toBe("5 Nov 2026");
  });
});
