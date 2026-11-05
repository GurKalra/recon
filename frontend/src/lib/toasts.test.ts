import { describe, expect, it } from "vitest";
import { successMessage, worthAToast } from "./toasts";

describe("toast wording", () => {
  it("confirms changes by what was called", () => {
    expect(successMessage("POST", "/claims")).toMatch(/draft/);
    expect(successMessage("POST", "/review/suggestions/12/accept")).toBe(
      "Match accepted.",
    );
    expect(successMessage("POST", "/rate-cards/courier")).toBe(
      "Rate card saved.",
    );
    expect(successMessage("DELETE", "/claims/3")).toBe("Draft discarded.");
  });
  it("stays quiet for reads and unknown calls", () => {
    expect(successMessage("GET", "/claims")).toBeNull();
    expect(successMessage("POST", "/uploads")).toBeNull();
    expect(successMessage("POST", "/claims/7/evidence")).toBeNull();
  });
  it("ignores the query string", () => {
    expect(successMessage("POST", "/uploads/4/import?source_id=2")).toBe(
      "Import started.",
    );
  });
  it("only interrupts for failures the screen cannot explain", () => {
    expect(worthAToast(null)).toBe(true);
    expect(worthAToast(500)).toBe(true);
    expect(worthAToast(409, "An import or another run is in progress.")).toBe(
      true,
    );
    expect(worthAToast(409, "This claim was already sent.")).toBe(false);
    expect(worthAToast(422)).toBe(false);
    expect(worthAToast(403)).toBe(false);
  });
});
