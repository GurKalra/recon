import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { rupeesToPaise } from "./claims";
import { inr } from "./format";
import { useRefreshAll } from "./queries";

export interface Slab {
  max_g: number;
  fee_paise: number;
}
export interface ZoneRate {
  zone: string;
  base_paise: number;
  addl_paise: number;
}

interface CardBase {
  id: number;
  effective_from: string;
  note: string | null;
  created_by: string | null;
  in_force: boolean;
}
export interface ChannelCard extends CardBase {
  commission_bps: number;
  fixed_paise: number;
  ship_extra_per_kg_paise: number;
  tcs_bps: number;
  tds_bps: number;
  pg_fee_bps: number;
  ship_slabs: Slab[];
}
export interface CourierCard extends CardBase {
  cod_flat_paise: number;
  cod_bps: number;
  return_pct: number;
  zone_rates: ZoneRate[];
}

interface PartnerBase {
  id: number;
  code: string;
  name: string;
  type: string;
}
export type RatePartner =
  | (PartnerBase & { kind: "channel"; cards: ChannelCard[] })
  | (PartnerBase & { kind: "courier"; cards: CourierCard[] });

export interface Saved {
  card_id: number;
  partner_code: string;
  changes: { field: string; from: number | null; to: number | null }[];
  orders_affected: number;
  recon_stale: boolean;
  note: string;
}

export const useRateCards = () =>
  useQuery({
    queryKey: ["rate-cards"],
    queryFn: () => api<RatePartner[]>("/rate-cards"),
  });

export function useSaveCard() {
  const refresh = useRefreshAll();
  return useMutation({
    mutationFn: (v: { kind: "channel" | "courier"; body: unknown }) =>
      api<Saved>(`/rate-cards/${v.kind}`, { method: "POST", body: v.body }),
    onSuccess: refresh,
  });
}

// ---- what a person types <-> what the server stores -------------------------------------------

/** "2.5" -> 250 basis points. Up to two decimals, 0 to 100. Anything else is null. */
export function pctToBps(text: string): number | null {
  const t = text.trim().replace(/%$/, "").trim();
  if (!/^\d{1,3}(\.\d{1,2})?$/.test(t)) return null;
  const bps = Math.round(Number(t) * 100);
  return bps >= 0 && bps <= 10_000 ? bps : null;
}

/** 250 -> "2.5", 1800 -> "18". */
export function bpsToPct(bps: number): string {
  return String(Number((bps / 100).toFixed(2)));
}

/** 12950 -> "129.5", 3000 -> "30". */
export function paiseToRupees(paise: number): string {
  return String(Number((paise / 100).toFixed(2)));
}

export interface ChannelForm {
  effective_from: string;
  commission: string;
  fixed: string;
  extra_kg: string;
  tcs: string;
  tds: string;
  pg: string;
  slabs: { max_g: string; fee: string }[];
  note: string;
}
export interface CourierForm {
  effective_from: string;
  cod_flat: string;
  cod_pct: string;
  return_pct: string;
  zones: { zone: string; base: string; addl: string }[];
  note: string;
}

type Built =
  { ok: true; body: Record<string, unknown> } | { ok: false; error: string };

const bad = (error: string): Built => ({ ok: false, error });

function money(text: string, name: string): number | string {
  const v = rupeesToPaise(text.trim() === "" ? "0" : text);
  return v === null
    ? `${name} must be an amount in rupees, like 30 or 12.50.`
    : v;
}
function percent(text: string, name: string): number | string {
  const v = pctToBps(text.trim() === "" ? "0" : text);
  return v === null
    ? `${name} must be a percentage from 0 to 100, like 2.5.`
    : v;
}

export function buildChannel(f: ChannelForm, partnerId: number): Built {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(f.effective_from))
    return bad("Choose the date this card starts.");
  const nums = {
    commission_bps: percent(f.commission, "Commission"),
    fixed_paise: money(f.fixed, "Fixed fee"),
    ship_extra_per_kg_paise: money(f.extra_kg, "Extra shipping per kg"),
    tcs_bps: percent(f.tcs, "TCS"),
    tds_bps: percent(f.tds, "TDS"),
    pg_fee_bps: percent(f.pg, "Payment gateway fee"),
  };
  for (const v of Object.values(nums)) if (typeof v === "string") return bad(v);
  if (
    Object.values(nums).every((v) => v === 0) &&
    f.slabs.every((x) => x.fee.trim() === "" || Number(x.fee) === 0)
  )
    return bad("Every figure is zero. Enter the rates this partner charges.");
  const slabs: { max_g: number; fee_paise: number }[] = [];
  for (const [i, s] of f.slabs.entries()) {
    if (s.max_g.trim() === "" && s.fee.trim() === "") continue;
    const g = Number(s.max_g);
    const fee = money(s.fee, `Fee for band ${i + 1}`);
    if (!Number.isInteger(g) || g <= 0)
      return bad(`Band ${i + 1}: the weight must be a whole number of grams.`);
    if (typeof fee === "string") return bad(fee);
    slabs.push({ max_g: g, fee_paise: fee });
  }
  if (new Set(slabs.map((s) => s.max_g)).size !== slabs.length)
    return bad("Two shipping bands end at the same weight.");
  return {
    ok: true,
    body: {
      partner_id: partnerId,
      effective_from: f.effective_from,
      ...nums,
      ship_slabs: slabs,
      note: f.note.trim() || null,
    },
  };
}

export function buildCourier(f: CourierForm, partnerId: number): Built {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(f.effective_from))
    return bad("Choose the date this card starts.");
  const ret = Number(f.return_pct);
  if (
    f.return_pct.trim() === "" ||
    !Number.isInteger(ret) ||
    ret < 0 ||
    ret > 1000
  )
    return bad("Returns charged must be a whole percentage from 0 to 1000.");
  const flat = money(f.cod_flat, "COD flat fee");
  const bps = percent(f.cod_pct, "COD percentage");
  if (typeof flat === "string") return bad(flat);
  if (typeof bps === "string") return bad(bps);
  if (
    f.zones.every((z) => Number(z.base || 0) === 0 && Number(z.addl || 0) === 0)
  )
    return bad("Enter the freight rate for at least one zone.");
  const zones: ZoneRate[] = [];
  for (const z of f.zones) {
    const base = money(z.base, `Zone ${z.zone} first 500 g`);
    const addl = money(z.addl, `Zone ${z.zone} each extra 500 g`);
    if (typeof base === "string") return bad(base);
    if (typeof addl === "string") return bad(addl);
    zones.push({ zone: z.zone, base_paise: base, addl_paise: addl });
  }
  return {
    ok: true,
    body: {
      partner_id: partnerId,
      effective_from: f.effective_from,
      cod_flat_paise: flat,
      cod_bps: bps,
      return_pct: ret,
      zone_rates: zones,
      note: f.note.trim() || null,
    },
  };
}

export function channelFormFrom(
  c: ChannelCard | undefined,
  today: string,
): ChannelForm {
  return {
    effective_from: today,
    commission: c ? bpsToPct(c.commission_bps) : "",
    fixed: c ? paiseToRupees(c.fixed_paise) : "",
    extra_kg: c ? paiseToRupees(c.ship_extra_per_kg_paise) : "",
    tcs: c ? bpsToPct(c.tcs_bps) : "",
    tds: c ? bpsToPct(c.tds_bps) : "",
    pg: c ? bpsToPct(c.pg_fee_bps) : "",
    slabs: c
      ? c.ship_slabs.map((s) => ({
          max_g: String(s.max_g),
          fee: paiseToRupees(s.fee_paise),
        }))
      : [{ max_g: "", fee: "" }],
    note: "",
  };
}

export function courierFormFrom(
  c: CourierCard | undefined,
  today: string,
): CourierForm {
  const by = new Map((c?.zone_rates ?? []).map((z) => [z.zone, z]));
  return {
    effective_from: today,
    cod_flat: c ? paiseToRupees(c.cod_flat_paise) : "",
    cod_pct: c ? bpsToPct(c.cod_bps) : "",
    return_pct: c ? String(c.return_pct) : "100",
    zones: ["A", "B", "C", "D"].map((zone) => ({
      zone,
      base: by.has(zone) ? paiseToRupees(by.get(zone)!.base_paise) : "",
      addl: by.has(zone) ? paiseToRupees(by.get(zone)!.addl_paise) : "",
    })),
    note: "",
  };
}

/** One change from the save result, in words: "Commission: 12% to 14%". */
export function describeChange(c: {
  field: string;
  from: number | null;
  to: number | null;
}): string {
  const show = (v: number | null) =>
    v === null
      ? "none"
      : c.field.endsWith("_bps")
        ? `${bpsToPct(v)}%`
        : c.field.endsWith("_paise")
          ? inr(v)
          : c.field === "return_pct"
            ? `${v}%`
            : String(v);
  const name = c.field.replace(/_(paise|bps)$/, "").replace(/_/g, " ");
  return `${name.charAt(0).toUpperCase()}${name.slice(1)}: ${show(c.from)} to ${show(c.to)}`;
}

/** The one-line terms shown in the table. */
export function terms(p: RatePartner): string {
  const c = p.cards.find((x) => x.in_force);
  if (!c) return "No card in force";
  if (p.kind === "channel") {
    const k = c as ChannelCard;
    return (
      `Commission ${bpsToPct(k.commission_bps)}%${k.fixed_paise ? ` + ${inr(k.fixed_paise)}` : ""}, ` +
      `payment fee ${bpsToPct(k.pg_fee_bps)}%, ${k.ship_slabs.length} shipping band${k.ship_slabs.length === 1 ? "" : "s"}`
    );
  }
  const k = c as CourierCard;
  const a = k.zone_rates.find((z) => z.zone === "A");
  return `${a ? `Zone A from ${inr(a.base_paise)}, ` : ""}COD ${inr(k.cod_flat_paise)} or ${bpsToPct(k.cod_bps)}%, returns at ${k.return_pct}%`;
}
