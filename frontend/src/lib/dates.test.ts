import { describe, expect, it } from "vitest";
import { activePreset, presets } from "./dates";

const today = new Date(2026, 10, 5); // 5 Nov 2026

describe("date presets", () => {
  it("count days inclusively", () => {
    const r = Object.fromEntries(presets(today).map((p) => [p.key, p]));
    expect(r["7"]).toMatchObject({ from: "2026-10-30", to: "2026-11-05" });
    expect(r["30"]).toMatchObject({ from: "2026-10-07", to: "2026-11-05" });
    expect(r.month).toMatchObject({ from: "2026-11-01", to: "2026-11-05" });
    expect(r.last).toMatchObject({ from: "2026-10-01", to: "2026-10-31" });
  });
  it("handles the year boundary", () => {
    const jan = presets(new Date(2027, 0, 3));
    expect(jan.find((p) => p.key === "last")).toMatchObject({
      from: "2026-12-01",
      to: "2026-12-31",
    });
    expect(jan.find((p) => p.key === "7")?.from).toBe("2026-12-28");
  });
  it("knows which preset is on", () => {
    expect(activePreset("2026-10-30", "2026-11-05", today)).toBe("7");
    expect(activePreset("2026-10-31", "2026-11-05", today)).toBeNull();
    expect(activePreset("", "", today)).toBeNull();
  });
});
