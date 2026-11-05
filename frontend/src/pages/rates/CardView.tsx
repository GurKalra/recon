import { date, inr } from "../../lib/format";
import {
  bpsToPct,
  type ChannelCard,
  type CourierCard,
  type RatePartner,
} from "../../lib/rates";

function Row({ k, v }: { k: string; v: string }) {
  return (
    <div className="flex justify-between gap-6 border-b border-rule py-1.5 text-[0.95rem]">
      <dt className="text-muted">{k}</dt>
      <dd className="num">{v}</dd>
    </div>
  );
}

function tag(c: { in_force: boolean; effective_from: string }, today: string) {
  if (c.in_force) return <span className="text-ok">In force</span>;
  return c.effective_from > today ? (
    <span className="text-warn">Starts later</span>
  ) : (
    <span className="text-muted">Replaced</span>
  );
}

export function CardView({ partner }: { partner: RatePartner }) {
  const today = new Date().toLocaleDateString("en-CA");
  if (partner.cards.length === 0)
    return (
      <p className="border border-dashed border-rule-strong px-6 py-8 text-center text-muted">
        No card yet. Orders for this partner cannot be checked until one is
        added.
      </p>
    );
  return (
    <ol className="space-y-6">
      {partner.cards.map((c) => (
        <li key={c.id} className="border border-rule p-5">
          <div className="flex items-baseline justify-between">
            <h3 className="text-[1.15rem]">From {date(c.effective_from)}</h3>
            <span className="text-[0.92rem]">{tag(c, today)}</span>
          </div>
          {partner.kind === "channel" ? (
            <Channel c={c as ChannelCard} />
          ) : (
            <Courier c={c as CourierCard} />
          )}
          <p className="mt-3 text-[0.85rem] text-muted">
            {c.created_by ? `Added by ${c.created_by}` : "Added by the system"}
            {c.note ? ` · ${c.note}` : ""}
          </p>
        </li>
      ))}
    </ol>
  );
}

function Channel({ c }: { c: ChannelCard }) {
  return (
    <>
      <dl className="mt-3">
        <Row k="Commission" v={`${bpsToPct(c.commission_bps)}%`} />
        <Row k="Fixed fee per order" v={inr(c.fixed_paise)} />
        <Row k="Payment gateway fee" v={`${bpsToPct(c.pg_fee_bps)}%`} />
        <Row k="TCS" v={`${bpsToPct(c.tcs_bps)}%`} />
        <Row k="TDS" v={`${bpsToPct(c.tds_bps)}%`} />
        <Row k="Extra shipping per kg" v={inr(c.ship_extra_per_kg_paise)} />
      </dl>
      {c.ship_slabs.length > 0 && (
        <table className="tbl mt-3">
          <thead>
            <tr>
              <th>Parcel weight</th>
              <th className="r">Shipping fee</th>
            </tr>
          </thead>
          <tbody>
            {c.ship_slabs.map((s) => (
              <tr key={s.max_g}>
                <td>Up to {s.max_g} g</td>
                <td className="r">{inr(s.fee_paise)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

function Courier({ c }: { c: CourierCard }) {
  return (
    <>
      <dl className="mt-3">
        <Row k="COD flat fee" v={inr(c.cod_flat_paise)} />
        <Row k="COD percentage" v={`${bpsToPct(c.cod_bps)}%`} />
        <Row k="Returns charged at" v={`${c.return_pct}% of forward`} />
      </dl>
      <table className="tbl mt-3">
        <thead>
          <tr>
            <th>Zone</th>
            <th className="r">First 500 g</th>
            <th className="r">Each extra 500 g</th>
          </tr>
        </thead>
        <tbody>
          {c.zone_rates.map((z) => (
            <tr key={z.zone}>
              <td>{z.zone}</td>
              <td className="r">{inr(z.base_paise)}</td>
              <td className="r">{inr(z.addl_paise)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  );
}
