import { Fragment, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Button } from "../components/ui/Button";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Skeleton,
} from "../components/ui/Section";
import {
  ACTION_TEXT,
  PAGE,
  actionText,
  detailLabel,
  detailValue,
  entityLink,
  useAudit,
} from "../lib/audit";
import { dateTime } from "../lib/format";

const field =
  "border border-rule-strong bg-paper px-2.5 py-1.5 text-[0.95rem] text-ink";

export function Audit() {
  const [sp, setSp] = useSearchParams();
  const action = sp.get("action") ?? "";
  const page = Number(sp.get("page") ?? 0) || 0;
  const [open, setOpen] = useState<number | null>(null);
  const q = useAudit(action, page);
  const rows = (q.data ?? []).slice(0, PAGE);
  const more = (q.data?.length ?? 0) > PAGE;
  const set = (p: Record<string, string | null>) => {
    const n = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(p))
      v === null ? n.delete(k) : n.set(k, v);
    setSp(n);
    setOpen(null);
  };

  return (
    <>
      <PageHeader
        title="Audit trail"
        lead="Every change and every sensitive download, with who did it and when. Entries are only ever added: nobody, including you, can edit or delete them."
      />
      <div className="mt-6 flex items-end gap-4">
        <label className="block text-[0.92rem]">
          Show
          <select
            className={`${field} mt-1 block`}
            value={action}
            onChange={(e) =>
              set({ action: e.target.value || null, page: null })
            }
          >
            <option value="">Everything</option>
            {Object.entries(ACTION_TEXT)
              .sort((a, b) => a[1].localeCompare(b[1]))
              .map(([k, t]) => (
                <option key={k} value={k}>
                  {t}
                </option>
              ))}
          </select>
        </label>
      </div>

      <div className="mt-4">
        {q.error ? (
          <ErrorNote error={q.error} />
        ) : !q.data ? (
          <Skeleton h="h-72" />
        ) : rows.length === 0 ? (
          <EmptyState title="Nothing here">
            No entries match. Choose another action above.
          </EmptyState>
        ) : (
          <div className="border border-rule">
            <table className="tbl">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Who</th>
                  <th>What</th>
                  <th>Where</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const link = entityLink(r);
                  const entries = Object.entries(r.detail ?? {});
                  return (
                    <Fragment key={r.id}>
                      <tr>
                        <td className="whitespace-nowrap">{dateTime(r.at)}</td>
                        <td>
                          {r.user_name ?? (
                            <span className="text-muted">Not signed in</span>
                          )}
                          {r.user_email && (
                            <div className="text-[0.82rem] text-muted">
                              {r.user_email}
                            </div>
                          )}
                        </td>
                        <td>
                          {actionText(r.action)}
                          {link && (
                            <>
                              {" "}
                              ·{" "}
                              <Link
                                className="text-accent underline underline-offset-4"
                                to={link}
                              >
                                Open
                              </Link>
                            </>
                          )}
                        </td>
                        <td className="text-muted">{r.ip ?? "–"}</td>
                        <td className="r">
                          {entries.length > 0 && (
                            <button
                              className="text-accent underline underline-offset-4"
                              aria-expanded={open === r.id}
                              onClick={() =>
                                setOpen(open === r.id ? null : r.id)
                              }
                            >
                              {open === r.id ? "Hide" : "Details"}
                            </button>
                          )}
                        </td>
                      </tr>
                      {open === r.id && (
                        <tr>
                          <td colSpan={5} className="bg-wash">
                            <dl className="grid max-w-[60rem] grid-cols-[14rem_1fr] gap-x-6 gap-y-1 py-1 text-[0.92rem]">
                              {entries.map(([k, v]) => (
                                <Fragment key={k}>
                                  <dt className="text-muted">
                                    {detailLabel(k)}
                                  </dt>
                                  <dd className="break-words">
                                    {detailValue(k, v)}
                                  </dd>
                                </Fragment>
                              ))}
                            </dl>
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="mt-4 flex items-center gap-3">
        <Button
          disabled={page === 0}
          onClick={() => set({ page: page <= 1 ? null : String(page - 1) })}
        >
          Newer
        </Button>
        <span className="text-[0.92rem] text-muted">Page {page + 1}</span>
        <Button
          disabled={!more}
          onClick={() => set({ page: String(page + 1) })}
        >
          Older
        </Button>
      </div>
    </>
  );
}
