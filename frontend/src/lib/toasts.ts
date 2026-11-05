/** The one-line confirmation shown after a change succeeds, chosen by what was called. Reads
 *  stay silent; a screen that already explains itself needs no entry here. */

const OK: [string, RegExp, string][] = [
  [
    "POST",
    /^\/claims$/,
    "Claim created. It stays a draft until you mark it as sent.",
  ],
  ["POST", /^\/claims\/write-off$/, "Written off."],
  ["POST", /^\/claims\/\d+\/items$/, "Added to the claim."],
  ["DELETE", /^\/claims\/\d+\/items\/\d+$/, "Taken out of the claim."],
  ["DELETE", /^\/claims\/\d+$/, "Draft discarded."],
  ["POST", /^\/claims\/\d+\/submit$/, "Marked as sent."],
  ["POST", /^\/claims\/items\/\d+\/outcome$/, "Outcome recorded."],
  ["POST", /^\/claims\/items\/\d+\/reopen$/, "Reopened."],
  ["POST", /^\/review\/suggestions\/\d+\/accept$/, "Match accepted."],
  ["POST", /^\/review\/suggestions\/\d+\/reject$/, "Suggestion rejected."],
  ["POST", /^\/review\/groups\/\d+\/link$/, "Payment linked to the order."],
  ["POST", /^\/review\/groups\/\d+\/undo$/, "Link undone."],
  ["POST", /^\/review\/groups\/\d+\/dismiss$/, "Payment dismissed."],
  ["POST", /^\/rate-cards\/(channel|courier)$/, "Rate card saved."],
  ["POST", /^\/recon\/run$/, "Reconciliation finished."],
  ["POST", /^\/uploads\/\d+\/(approve|import)$/, "Import started."],
  ["DELETE", /^\/uploads\/\d+$/, "Upload discarded."],
  ["POST", /^\/demo\/load$/, "Demo data loaded."],
];

export function successMessage(method: string, path: string): string | null {
  const clean = path.split("?")[0];
  return OK.find(([m, re]) => m === method && re.test(clean))?.[2] ?? null;
}

/** Worth interrupting someone for: the server failed, or a run is blocked by another job. A
 *  refusal the screen already explains in place (a 4xx) is not repeated as a toast. */
export function worthAToast(status: number | null, message = ""): boolean {
  return (
    status === null ||
    status >= 500 ||
    (status === 409 && /in progress/i.test(message))
  );
}
