import { describe, expect, it } from "vitest";
import { actionText, detailLabel, detailValue, entityLink } from "./audit";

describe("audit wording", () => {
  it("names known actions and tidies unknown ones", () => {
    expect(actionText("claim.submit")).toBe("Marked a claim as sent");
    expect(actionText("new.thing_done")).toBe("new thing done");
  });
  it("shows money as rupees and lists plainly", () => {
    expect(detailValue("claimed_paise", 150000)).toBe("₹1,500.00");
    expect(detailValue("orders", [1, 2, 3])).toBe("1, 2, 3");
    expect(detailValue("orders", Array(20).fill(1))).toBe("20 items");
    expect(detailValue("changes", [])).toBe("none");
    expect(detailValue("note", null)).toBe("none");
    expect(detailLabel("claimed_paise")).toBe("Claimed");
  });
  it("links to the screen when it can", () => {
    expect(entityLink({ entity: "claim", entity_id: 4 })).toBe(
      "/claims?tab=claims&claim=4",
    );
    expect(entityLink({ entity: "partner", entity_id: 2 })).toBe(
      "/rates?partner=2",
    );
    expect(entityLink({ entity: "order", entity_id: 2 })).toBeNull();
  });
});
