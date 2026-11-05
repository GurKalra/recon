import { useState, type ReactNode } from "react";
import { Button } from "../../components/ui/Button";
import { ErrorNote } from "../../components/ui/Section";
import {
  buildChannel,
  buildCourier,
  channelFormFrom,
  courierFormFrom,
  useSaveCard,
  type ChannelCard,
  type ChannelForm,
  type CourierCard,
  type CourierForm,
  type RatePartner,
  type Saved,
} from "../../lib/rates";

const field =
  "w-full border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";

function Labelled({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="text-[0.92rem]">{label}</span>
      {children}
      {hint && (
        <span className="mt-0.5 block text-[0.82rem] text-muted">{hint}</span>
      )}
    </label>
  );
}

function Text({
  label,
  hint,
  value,
  onChange,
  suffix,
}: {
  label: string;
  hint?: string;
  value: string;
  onChange: (v: string) => void;
  suffix?: string;
}) {
  return (
    <Labelled label={label} hint={hint}>
      <span className="mt-1 flex items-center gap-2">
        <input
          className={`${field} num`}
          inputMode="decimal"
          value={value}
          onChange={(e) => onChange(e.target.value)}
        />
        {suffix && <span className="text-muted">{suffix}</span>}
      </span>
    </Labelled>
  );
}

type Props = {
  partner: RatePartner;
  onSaved: (s: Saved) => void;
  onCancel: () => void;
};

/** A new card starts as a copy of the one in force, so only what changed needs typing. */
export function CardForm({ partner, onSaved, onCancel }: Props) {
  const today = new Date().toLocaleDateString("en-CA");
  const base = partner.cards.find((c) => c.in_force) ?? partner.cards[0];
  const save = useSaveCard();
  const [error, setError] = useState<string | null>(null);
  const [ch, setCh] = useState<ChannelForm>(() =>
    channelFormFrom(
      partner.kind === "channel"
        ? (base as ChannelCard | undefined)
        : undefined,
      today,
    ),
  );
  const [co, setCo] = useState<CourierForm>(() =>
    courierFormFrom(
      partner.kind === "courier"
        ? (base as CourierCard | undefined)
        : undefined,
      today,
    ),
  );
  const date =
    partner.kind === "channel" ? ch.effective_from : co.effective_from;
  const note = partner.kind === "channel" ? ch.note : co.note;
  const setDate = (v: string) =>
    partner.kind === "channel"
      ? setCh({ ...ch, effective_from: v })
      : setCo({ ...co, effective_from: v });
  const setNote = (v: string) =>
    partner.kind === "channel"
      ? setCh({ ...ch, note: v })
      : setCo({ ...co, note: v });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const built =
      partner.kind === "channel"
        ? buildChannel(ch, partner.id)
        : buildCourier(co, partner.id);
    if (!built.ok) return setError(built.error);
    setError(null);
    save.mutate(
      { kind: partner.kind, body: built.body },
      {
        onSuccess: onSaved,
        onError: (err) =>
          setError(err instanceof Error ? err.message : "Could not save."),
      },
    );
  }

  return (
    <form
      onSubmit={submit}
      className="reveal border border-rule-strong p-6"
      aria-label="New rate card"
    >
      <h3 className="text-[1.15rem]">New card for {partner.name}</h3>
      <p className="mt-1 text-[0.92rem] text-muted">
        Cards are never edited. This one is added alongside the old ones and
        applies to orders placed on or after the start date. It starts as a copy
        of the current card.
      </p>

      <div className="mt-5 grid grid-cols-2 gap-4">
        <Labelled label="Starts on">
          <input
            type="date"
            className={`${field} mt-1`}
            value={date}
            onChange={(e) => setDate(e.target.value)}
          />
        </Labelled>
      </div>

      {partner.kind === "channel" ? (
        <>
          <div className="mt-4 grid grid-cols-2 gap-4">
            <Text
              label="Commission"
              suffix="%"
              value={ch.commission}
              onChange={(v) => setCh({ ...ch, commission: v })}
              hint="Of the order value."
            />
            <Text
              label="Fixed fee per order"
              suffix="₹"
              value={ch.fixed}
              onChange={(v) => setCh({ ...ch, fixed: v })}
            />
            <Text
              label="Payment gateway fee"
              suffix="%"
              value={ch.pg}
              onChange={(v) => setCh({ ...ch, pg: v })}
            />
            <Text
              label="Extra shipping per kg"
              suffix="₹"
              value={ch.extra_kg}
              onChange={(v) => setCh({ ...ch, extra_kg: v })}
              hint="For each kg above the heaviest band."
            />
            <Text
              label="TCS"
              suffix="%"
              value={ch.tcs}
              onChange={(v) => setCh({ ...ch, tcs: v })}
            />
            <Text
              label="TDS"
              suffix="%"
              value={ch.tds}
              onChange={(v) => setCh({ ...ch, tds: v })}
            />
          </div>
          <fieldset className="mt-5">
            <legend className="text-[0.92rem]">Shipping fee bands</legend>
            <p className="text-[0.82rem] text-muted">
              The fee charged for a parcel up to each weight.
            </p>
            <div className="mt-2 space-y-2">
              {ch.slabs.map((s, i) => (
                <div
                  key={i}
                  className="flex items-center gap-2 whitespace-nowrap"
                >
                  <span className="text-muted">Up to</span>
                  <input
                    aria-label={`Band ${i + 1} weight in grams`}
                    className={`${field} num w-24`}
                    inputMode="numeric"
                    value={s.max_g}
                    onChange={(e) =>
                      setCh({
                        ...ch,
                        slabs: ch.slabs.map((x, j) =>
                          j === i ? { ...x, max_g: e.target.value } : x,
                        ),
                      })
                    }
                  />
                  <span className="text-muted">g costs ₹</span>
                  <input
                    aria-label={`Band ${i + 1} fee in rupees`}
                    className={`${field} num w-28`}
                    inputMode="decimal"
                    value={s.fee}
                    onChange={(e) =>
                      setCh({
                        ...ch,
                        slabs: ch.slabs.map((x, j) =>
                          j === i ? { ...x, fee: e.target.value } : x,
                        ),
                      })
                    }
                  />
                  <button
                    type="button"
                    className="text-muted underline underline-offset-4 hover:text-ink"
                    onClick={() =>
                      setCh({
                        ...ch,
                        slabs: ch.slabs.filter((_, j) => j !== i),
                      })
                    }
                  >
                    Remove
                  </button>
                </div>
              ))}
            </div>
            <Button
              type="button"
              variant="quiet"
              className="mt-2 !px-0"
              onClick={() =>
                setCh({ ...ch, slabs: [...ch.slabs, { max_g: "", fee: "" }] })
              }
            >
              Add a band
            </Button>
          </fieldset>
        </>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-3 gap-4">
            <Text
              label="COD flat fee"
              suffix="₹"
              value={co.cod_flat}
              onChange={(v) => setCo({ ...co, cod_flat: v })}
            />
            <Text
              label="COD percentage"
              suffix="%"
              value={co.cod_pct}
              onChange={(v) => setCo({ ...co, cod_pct: v })}
            />
            <Text
              label="Returns charged at"
              suffix="%"
              value={co.return_pct}
              onChange={(v) => setCo({ ...co, return_pct: v })}
              hint="Of the forward freight."
            />
          </div>
          <fieldset className="mt-5">
            <legend className="text-[0.92rem]">Freight by zone</legend>
            <table className="tbl mt-2">
              <thead>
                <tr>
                  <th>Zone</th>
                  <th className="r">First 500 g (₹)</th>
                  <th className="r">Each extra 500 g (₹)</th>
                </tr>
              </thead>
              <tbody>
                {co.zones.map((z, i) => (
                  <tr key={z.zone}>
                    <td>{z.zone}</td>
                    <td>
                      <input
                        aria-label={`Zone ${z.zone} first 500 g`}
                        className={`${field} num ml-auto w-28 text-right`}
                        inputMode="decimal"
                        value={z.base}
                        onChange={(e) =>
                          setCo({
                            ...co,
                            zones: co.zones.map((x, j) =>
                              j === i ? { ...x, base: e.target.value } : x,
                            ),
                          })
                        }
                      />
                    </td>
                    <td>
                      <input
                        aria-label={`Zone ${z.zone} each extra 500 g`}
                        className={`${field} num ml-auto w-28 text-right`}
                        inputMode="decimal"
                        value={z.addl}
                        onChange={(e) =>
                          setCo({
                            ...co,
                            zones: co.zones.map((x, j) =>
                              j === i ? { ...x, addl: e.target.value } : x,
                            ),
                          })
                        }
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </fieldset>
        </>
      )}

      <div className="mt-4">
        <Labelled
          label="Why (optional)"
          hint="For example: new contract from the partner, rate revision email of 3 Nov."
        >
          <input
            className={`${field} mt-1`}
            maxLength={300}
            value={note}
            onChange={(e) => setNote(e.target.value)}
          />
        </Labelled>
      </div>

      {error && (
        <div className="mt-4">
          <ErrorNote error={new Error(error)} />
        </div>
      )}
      <div className="mt-5 flex gap-2">
        <Button type="submit" variant="primary" busy={save.isPending}>
          Save card
        </Button>
        <Button type="button" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
