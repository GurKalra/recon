import type { Urgency } from "../../lib/claims";

/** Same cut-offs as the server: closed, 7 days, 14 days. */
export const urgency = (daysLeft: number): Urgency =>
  daysLeft < 0
    ? "expired"
    : daysLeft <= 7
      ? "critical"
      : daysLeft <= 14
        ? "soon"
        : "ok";
