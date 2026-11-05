import { useMutation } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Banner } from "../../components/ui/Banner";
import { Button } from "../../components/ui/Button";
import { ErrorNote } from "../../components/ui/Section";
import { Stepper } from "../../components/ui/Stepper";
import { api, ApiError } from "../../lib/api";
import { date, inr, int, pct } from "../../lib/format";
import {
  SOURCE_KINDS,
  useImport,
  type Obj,
  type Preview,
  type Suggestion,
  type UploadInfo,
} from "../../lib/imports";
import { usePartners, useRefreshAll } from "../../lib/queries";
import { MappingEditor } from "./MappingEditor";

const STEPS = ["File", "Columns", "Check", "Import"];
const field =
  "border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";
const STAGE: Record<string, string> = {
  read: "Reading the file",
  transform: "Checking every row",
  load: "Saving to the database",
  link: "Linking payments to orders",
  done: "Done",
};
const WARNING: Record<string, string> = {
  rows_without_order_ref:
    "Some rows carry no order ID. Those payments will wait in the review queue until someone matches them.",
};
const warn = (w: string) =>
  WARNING[w] ??
  (w.includes(" ")
    ? w
    : w.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase()));
const HEAD: Record<string, string> = {
  order_ref_raw: "Order ID",
  amount_paise: "Amount",
  value_paise: "Order value",
  line_type: "Line",
  occurred_on: "Date",
  ordered_on: "Ordered",
  delivered_on: "Delivered",
  returned_on: "Returned",
  received_on: "Received",
};
const HIDDEN = new Set(["emit_idx", "order_ref"]);
const head = (c: string) =>
  HEAD[c] ??
  c
    .replace(/_paise$/, "")
    .replace(/_/g, " ")
    .replace(/^./, (x) => x.toUpperCase());
function cell(c: string, v: unknown): string {
  if (v === null || v === undefined || v === "") return "–";
  if (c.endsWith("_paise") && typeof v === "number") return inr(v);
  if (c.endsWith("_on") && typeof v === "string") return date(v);
  return String(v);
}
const AI_LINE: Record<string, string> = {
  off: "The AI was switched off, so the rules worked this out.",
  no_key: "The AI is not set up on the server (no GROQ_API_KEY), so the rules worked this out.",
  not_needed: "The rules mapped every column and the arithmetic of the file checked out, so the AI was not needed.",
  asked_kept: "The AI was asked as well, but the rules' answer was as good or better, so it was kept.",
  unavailable: "The AI service did not answer, so this comes from the rules.",
};
const aiLine = (s: Suggestion) =>
  s.ai_status === "used"
    ? `Suggested by the AI (${s.model ?? "model"}, ${int(s.tokens)} tokens), then checked by the rules.`
    : AI_LINE[s.ai_status] ?? "Suggested by the rules.";
const mb = (b: number) =>
  b >= 1 << 20
    ? `${(b / (1 << 20)).toFixed(1)} MB`
    : `${Math.max(1, Math.round(b / 1024))} KB`;

type Queued = { import_id: number; status: string };

export function Wizard({
  upload,
  onClose,
  onFinished,
}: {
  upload: UploadInfo;
  onClose: () => void;
  onFinished: () => void;
}) {
  const det = upload.detected[0];
  const partners = usePartners();
  const [step, setStep] = useState(0);
  const [partnerId, setPartnerId] = useState<number | "">(
    det?.partner_id ?? "",
  );
  const [kind, setKind] = useState<string>(det?.kind ?? "settlement");
  const [nameEdit, setNameEdit] = useState<string | null>(null);
  const [useAi, setUseAi] = useState(true);
  const [spec, setSpec] = useState<Obj | null>(null);
  const [sugg, setSugg] = useState<Suggestion | null>(null);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [note, setNote] = useState("");
  const [queued, setQueued] = useState<Queued | null>(null);

  const headers = upload.profile.columns.map((c) => c.name);
  const partner = partners.data?.find((p) => p.id === partnerId);
  const kindLabel = SOURCE_KINDS.find((k) => k.value === kind)?.label ?? kind;
  const name =
    nameEdit ??
    det?.name ??
    (partner ? `${partner.name} ${kindLabel.toLowerCase()}` : "");

  const suggest = useMutation({
    mutationFn: () =>
      api<Suggestion>(`/uploads/${upload.id}/suggest`, {
        method: "POST",
        body: { partner_id: partnerId, source_kind: kind, use_ai: useAi },
      }),
    onSuccess: (r) => {
      setSugg(r);
      setSpec(r.mapping ?? null);
      setPreview(null);
    },
  });
  const check = useMutation({
    mutationFn: () =>
      api<Preview>(`/uploads/${upload.id}/preview`, {
        method: "POST",
        body: { mapping: spec, source_kind: kind },
      }),
    onSuccess: (r) => {
      setPreview(r);
      setStep(2);
    },
  });
  const approve = useMutation({
    mutationFn: () =>
      api<Queued>(`/uploads/${upload.id}/approve`, {
        method: "POST",
        body: {
          partner_id: partnerId,
          source_kind: kind,
          name,
          mapping: spec,
          note: note.trim() || null,
        },
      }),
    onSuccess: (r) => {
      setQueued(r);
      setStep(3);
    },
  });
  const importKnown = useMutation({
    mutationFn: (sourceId: number) =>
      api<Queued>(`/uploads/${upload.id}/import`, {
        method: "POST",
        query: { source_id: sourceId },
      }),
    onSuccess: (r) => {
      setQueued(r);
      setStep(3);
    },
  });

  const shown = (preview?.columns ?? []).filter(
    (c) =>
      !HIDDEN.has(c) ||
      (c === "order_ref" && !preview?.columns.includes("order_ref_raw")),
  );
  const drift =
    importKnown.error instanceof ApiError
      ? (importKnown.error.detail as
          | {
              message?: string;
              drift?: { missing_columns: string[]; new_columns: string[] };
            }
          | undefined)
      : undefined;

  return (
    <section className="reveal border border-rule" aria-label="Import a file">
      <div className="flex items-center justify-between gap-6 border-b border-rule px-8 py-5">
        <div className="min-w-0">
          <div className="eyebrow">Importing</div>
          <div className="truncate text-[1.25rem]">{upload.filename}</div>
        </div>
        {step < 3 && (
          <Button variant="quiet" onClick={onClose}>
            Cancel
          </Button>
        )}
      </div>
      <div className="border-b border-rule px-8 py-5">
        <Stepper steps={STEPS} current={step} />
      </div>

      <div className="px-8 py-7">
        {step === 0 && (
          <div className="reveal space-y-6">
            <dl className="grid grid-cols-4 gap-8">
              {[
                ["Size", mb(upload.file_bytes)],
                ["Rows", int(upload.profile.rows)],
                ["Columns", int(upload.profile.columns.length)],
                ["Header on row", String(upload.profile.header_row + 1)],
              ].map(([k, v]) => (
                <div key={k}>
                  <dt className="eyebrow">{k}</dt>
                  <dd className="num mt-1 text-[1.4rem]">{v}</dd>
                </div>
              ))}
            </dl>

            {det && (
              <Banner
                tone="info"
                action={
                  <Button
                    variant="primary"
                    busy={importKnown.isPending}
                    onClick={() => importKnown.mutate(det.source_id)}
                  >
                    Import with version {det.version}
                  </Button>
                }
              >
                <strong>We know this layout.</strong> It matches{" "}
                {det.partner_name}, “{det.name}”, whose column mapping you
                approved (version {det.version}).
              </Banner>
            )}
            {drift?.message && (
              <div
                className="border border-rule border-l-4 border-l-warn px-4 py-3 text-[0.95rem]"
                role="alert"
              >
                <p className="font-bold">{drift.message}</p>
                {drift.drift && (
                  <p className="mt-1 text-muted">
                    Missing: {drift.drift.missing_columns.join(", ") || "none"}.
                    New: {drift.drift.new_columns.join(", ") || "none"}. Map the
                    columns again to continue.
                  </p>
                )}
              </div>
            )}
            {importKnown.error && !drift?.message && (
              <ErrorNote error={importKnown.error} />
            )}

            <div>
              <div className="eyebrow mb-2">What is in the file</div>
              <div className="max-h-72 overflow-auto border border-rule">
                <table className="tbl">
                  <thead>
                    <tr>
                      <th>Column</th>
                      <th>Looks like</th>
                      <th className="r">Filled</th>
                      <th>Examples</th>
                    </tr>
                  </thead>
                  <tbody>
                    {upload.profile.columns.map((c) => (
                      <tr key={c.name}>
                        <td className="whitespace-nowrap">{c.name}</td>
                        <td className="text-muted">
                          {c.looks_like.replace(/_/g, " ")}
                        </td>
                        <td className="r">{pct(c.filled * 100, 0)}</td>
                        <td className="max-w-[26rem] truncate text-[0.9rem] text-muted">
                          {c.sensitive
                            ? "Personal data, hidden"
                            : c.samples.join(", ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            <div className="flex justify-end">
              <Button
                variant={det ? "secondary" : "primary"}
                onClick={() => setStep(1)}
              >
                {det ? "Map the columns again" : "Map the columns"}
              </Button>
            </div>
          </div>
        )}

        {step === 1 && (
          <div className="reveal space-y-6">
            <p className="max-w-[70ch] text-[0.98rem]">
              Each partner names its columns differently. Here you tell the app which column of this file is the order ID, which is the amount, which is the date, and so on. The app makes a first guess, you fix anything wrong, and it then tests the guess on the real rows. Nothing is saved until you approve it on the next step.
            </p>
            <div className="grid grid-cols-3 gap-6">
              <label className="text-[0.85rem] text-muted">
                Partner
                <select
                  className={`${field} mt-1 block w-full`}
                  value={partnerId}
                  onChange={(e) =>
                    setPartnerId(e.target.value ? Number(e.target.value) : "")
                  }
                >
                  <option value="">Choose a partner</option>
                  {(partners.data ?? []).map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-[0.85rem] text-muted">
                What kind of file
                <select
                  className={`${field} mt-1 block w-full`}
                  value={kind}
                  onChange={(e) => {
                    setKind(e.target.value);
                    setSpec(null);
                    setSugg(null);
                  }}
                >
                  {SOURCE_KINDS.map((k) => (
                    <option key={k.value} value={k.value}>
                      {k.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="text-[0.85rem] text-muted">
                Name for this source
                <input
                  className={`${field} mt-1 block w-full`}
                  value={name}
                  onChange={(e) => setNameEdit(e.target.value)}
                  maxLength={80}
                />
              </label>
            </div>
            <p className="-mt-3 text-[0.88rem] text-muted">
              {SOURCE_KINDS.find((k) => k.value === kind)?.hint}.
            </p>

            {!spec && (
              <div className="border border-rule px-6 py-5">
                <h3 className="text-[1.15rem]">
                  Let the app suggest the columns
                </h3>
                <p className="mt-1 max-w-[62ch] text-[0.95rem] text-muted">
                  It first works the mapping out from the column names, the
                  values and the arithmetic of the file. With the AI switched
                  on, the model also reads the header and a few sample values to
                  fill the gaps. Names, phones, emails and addresses are masked
                  before anything is sent. You approve the result; nothing is
                  imported until you do.
                </p>
                <label className="mt-3 flex items-center gap-2 text-[0.95rem]">
                  <input
                    type="checkbox"
                    checked={useAi}
                    onChange={(e) => setUseAi(e.target.checked)}
                    className="h-4 w-4 accent-[#1f3a5f]"
                  />
                  Use the AI as well as the rules
                </label>
                <div className="mt-4 flex items-center gap-4">
                  <Button
                    variant="primary"
                    busy={suggest.isPending}
                    disabled={partnerId === "" || !name.trim()}
                    onClick={() => suggest.mutate()}
                  >
                    Suggest a mapping
                  </Button>
                  {suggest.isPending && (
                    <span className="text-[0.92rem] text-muted" role="status">
                      <span className="dots mr-2 text-accent" aria-hidden>
                        <span />
                        <span />
                        <span />
                      </span>
                      {useAi
                        ? "The AI is reading the columns, a few seconds"
                        : "Working it out from the file"}
                    </span>
                  )}
                </div>
                {suggest.error && (
                  <div className="mt-4">
                    <ErrorNote error={suggest.error} />
                  </div>
                )}
                {sugg && !spec && (
                  <div className="mt-4 space-y-2" role="alert">
                    <p className="border border-rule border-l-4 border-l-risk px-4 py-2 text-[0.95rem]">
                      A mapping could not be worked out for this file. Check the
                      partner and the kind of file, or ask for a different file.
                    </p>
                    {[...sugg.check.errors, ...sugg.warnings].map((w) => (
                      <p key={w} className="text-[0.9rem] text-muted">
                        {w}
                      </p>
                    ))}
                  </div>
                )}
              </div>
            )}

            {spec && sugg && (
              <div className="reveal space-y-5">
                <div className="flex flex-wrap items-baseline justify-between gap-3">
                  <h3 className="text-[1.15rem]">Check the columns</h3>
                  <span className="max-w-[48ch] text-right text-[0.88rem] text-muted">{aiLine(sugg)}</span>
                </div>
                {sugg.warnings.map((w) => (
                  <div
                    key={w}
                    className="border border-rule border-l-4 border-l-warn px-4 py-2 text-[0.92rem]"
                  >
                    {warn(w)}
                  </div>
                ))}
                {sugg.check.errors.map((w) => (
                  <div
                    key={w}
                    className="border border-rule border-l-4 border-l-risk px-4 py-2 text-[0.92rem]"
                  >
                    {w}
                  </div>
                ))}
                <div className="border border-rule">
                  <MappingEditor
                    spec={spec}
                    headers={headers}
                    notes={sugg.notes}
                    onChange={(s) => {
                      setSpec(s);
                      setPreview(null);
                    }}
                  />
                </div>
                {sugg.unmapped_columns.length > 0 && (
                  <p className="text-[0.88rem] text-muted">
                    Not used: {sugg.unmapped_columns.join(", ")}.
                  </p>
                )}
                {check.error && <ErrorNote error={check.error} />}
                <div className="flex items-center justify-between">
                  <Button
                    variant="quiet"
                    onClick={() => {
                      setSpec(null);
                      setSugg(null);
                    }}
                  >
                    Start again
                  </Button>
                  <Button
                    variant="primary"
                    busy={check.isPending}
                    onClick={() => check.mutate()}
                  >
                    Check this on the file
                  </Button>
                </div>
              </div>
            )}
            {!spec && (
              <div className="flex justify-start">
                <Button variant="quiet" onClick={() => setStep(0)}>
                  Back
                </Button>
              </div>
            )}
          </div>
        )}

        {step === 2 && preview && (
          <div className="reveal space-y-6">
            <div
              className={`border border-rule border-l-4 px-5 py-4 ${preview.ok ? "border-l-ok" : "border-l-risk"}`}
              role="status"
            >
              <p className="text-[1.1rem] font-bold">
                {preview.ok
                  ? "The mapping works on this file."
                  : "This mapping rejects too many rows."}
              </p>
              <p className="mt-1 text-[0.95rem] text-muted">
                Of the first {int(preview.rows_checked)} rows,{" "}
                {int(preview.rows_rejected)} would be refused (
                {pct(preview.reject_rate * 100, 1)}). Up to 2% is allowed. The
                file has {int(preview.rows_in_file)} rows in total.
              </p>
            </div>
            {preview.warnings.map((w) => (
              <div
                key={w}
                className="border border-rule border-l-4 border-l-warn px-4 py-2 text-[0.92rem]"
              >
                {warn(w)}
              </div>
            ))}

            <div>
              <div className="eyebrow mb-2">
                What would be saved (first {preview.rows.length} rows)
              </div>
              <div className="max-h-80 overflow-auto border border-rule">
                <table className="tbl">
                  <thead>
                    <tr>
                      {shown.map((c) => (
                        <th key={c} className={c.endsWith("_paise") ? "r" : ""}>
                          {head(c)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {preview.rows.map((r, i) => (
                      <tr key={i}>
                        {shown.map((c) => (
                          <td
                            key={c}
                            className={`max-w-[14rem] truncate whitespace-nowrap text-[0.9rem] ${c.endsWith("_paise") ? "r" : ""}`}
                          >
                            {cell(c, r[c])}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {preview.rejects.length > 0 && (
              <div>
                <div className="eyebrow mb-2">Rows that would be refused</div>
                <div className="max-h-52 overflow-auto border border-rule">
                  <table className="tbl">
                    <thead>
                      <tr>
                        <th>Row</th>
                        <th>Why</th>
                      </tr>
                    </thead>
                    <tbody>
                      {preview.rejects.map((r) => (
                        <tr key={r.row_no}>
                          <td className="num">{r.row_no}</td>
                          <td className="text-[0.92rem]">{r.reason}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            <label className="block text-[0.85rem] text-muted">
              Note for the mapping history (optional)
              <input
                className={`${field} mt-1 block w-full max-w-lg`}
                value={note}
                onChange={(e) => setNote(e.target.value)}
                maxLength={300}
                placeholder="For example: new column layout from October"
              />
            </label>
            {approve.error && <ErrorNote error={approve.error} />}
            <div className="flex items-center justify-between">
              <Button onClick={() => setStep(1)}>Back to the columns</Button>
              <Button
                variant="primary"
                busy={approve.isPending}
                disabled={!preview.ok}
                onClick={() => approve.mutate()}
              >
                Approve the mapping and import
              </Button>
            </div>
          </div>
        )}

        {step === 3 && queued && (
          <Progress queued={queued} onAnother={onFinished} />
        )}
      </div>
    </section>
  );
}

function Progress({
  queued,
  onAnother,
}: {
  queued: Queued;
  onAnother: () => void;
}) {
  const refresh = useRefreshAll();
  const imp = useImport(queued.import_id);
  const d = imp.data;
  const finished = d?.status === "succeeded" || d?.status === "failed";
  useEffect(() => {
    if (finished) void refresh();
  }, [finished]); // eslint-disable-line react-hooks/exhaustive-deps

  if (queued.status === "duplicate") {
    return (
      <div className="reveal space-y-4">
        <Banner tone="warn">
          This exact file was already imported, so nothing was added. Nothing is
          double counted.
        </Banner>
        <Button onClick={onAnother}>Import another file</Button>
      </div>
    );
  }
  if (!d) return <div className="text-muted">Starting…</div>;
  const pctDone = Math.round((d.status === "succeeded" ? 1 : d.progress) * 100);

  if (d.status === "failed") {
    return (
      <div className="reveal space-y-4">
        <ErrorNote error={new Error(d.error ?? "The import failed.")} />
        <Button onClick={onAnother}>Try another file</Button>
      </div>
    );
  }
  if (d.status === "succeeded") {
    return (
      <div className="reveal space-y-6">
        <div>
          <div className="eyebrow">Imported</div>
          <h3 className="text-[1.5rem]">{d.filename} is in.</h3>
        </div>
        <dl className="grid grid-cols-4 gap-8 border-y border-rule py-6">
          {[
            ["Rows read", d.rows_total],
            ["Entries saved", d.rows_loaded],
            ["Already had", d.rows_duplicate],
            ["Refused", d.rows_rejected],
          ].map(([k, v]) => (
            <div key={String(k)}>
              <dt className="eyebrow">{k}</dt>
              <dd className="num mt-1 text-[1.6rem]">{int(Number(v ?? 0))}</dd>
            </div>
          ))}
        </dl>
        <p className="-mt-2 text-[0.88rem] text-muted">
          A payment row can become several entries: the sale, then each fee and
          tax.
        </p>
        {(d.rows_rejected ?? 0) > 0 && (
          <p className="text-[0.95rem]">
            {int(d.rows_rejected ?? 0)} rows were refused and left out.{" "}
            <a
              className="text-accent underline underline-offset-4"
              href={`/api/imports/${d.id}/rejects.csv`}
            >
              Download the reject report
            </a>{" "}
            to send back to the partner.
          </p>
        )}
        <div className="flex gap-3">
          <Link
            to="/"
            className="inline-flex items-center border border-accent bg-accent px-4 py-2 text-[0.95rem] text-white"
          >
            See the overview
          </Link>
          <Link
            to="/review"
            className="inline-flex items-center border border-rule-strong px-4 py-2 text-[0.95rem] hover:bg-wash"
          >
            Open the review queue
          </Link>
          <Button onClick={onAnother}>Import another file</Button>
        </div>
      </div>
    );
  }
  return (
    <div className="reveal" role="status" aria-live="polite">
      <div className="flex items-baseline justify-between">
        <span>
          <span className="dots mr-2 text-accent" aria-hidden>
            <span />
            <span />
            <span />
          </span>
          {STAGE[d.stage ?? "read"] ?? "Working"}
        </span>
        <span className="num text-muted">{pctDone}%</span>
      </div>
      <div className="mt-3 h-[3px] bg-rule-strong" aria-hidden>
        <div
          className="h-full bg-accent transition-[width] duration-500 ease-out"
          style={{ width: `${Math.max(6, pctDone)}%` }}
        />
      </div>
      <p className="mt-3 text-[0.9rem] text-muted">
        Large files take a little longer. You can leave this page; the import
        carries on and appears under Recent imports.
      </p>
    </div>
  );
}
