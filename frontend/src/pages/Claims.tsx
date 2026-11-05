import { useSearchParams } from "react-router-dom";
import { PageHeader } from "../components/ui/Section";
import { Candidates } from "./claims/Candidates";
import { ClaimDrawer } from "./claims/ClaimDrawer";
import { ClaimsList } from "./claims/ClaimsList";
import { OrderDrawer } from "./OrderDrawer";

export function Claims() {
  const [sp, setSp] = useSearchParams();
  const tab = sp.get("tab") === "claims" ? "claims" : "find";
  const claimId = sp.get("claim") ? Number(sp.get("claim")) : null;
  const orderId = sp.get("order") ? Number(sp.get("order")) : null;

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch))
      v === null || v === "" ? next.delete(k) : next.set(k, v);
    setSp(next, { replace: true });
  };

  return (
    <>
      <PageHeader
        title="Claims"
        lead="Money partners owe you, most urgent first. Group it into a claim for one partner, send it with the evidence, then record what they decided."
      />
      <div role="tablist" className="mt-6 flex gap-8 border-b border-rule">
        {[
          ["find", "To claim"],
          ["claims", "Claims"],
        ].map(([k, text]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            onClick={() => set({ tab: k === "find" ? null : k, claim: null })}
            className={`-mb-px border-b-2 pb-3 text-[1.02rem] ${tab === k ? "border-accent font-bold text-accent" : "border-transparent text-muted hover:text-ink"}`}
          >
            {text}
          </button>
        ))}
      </div>
      {tab === "find" ? (
        <Candidates sp={sp} set={set} />
      ) : (
        <ClaimsList sp={sp} set={set} />
      )}
      <ClaimDrawer id={claimId} onClose={() => set({ claim: null })} />
      <OrderDrawer id={orderId} onClose={() => set({ order: null })} />
    </>
  );
}
