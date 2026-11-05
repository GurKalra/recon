import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { Button } from "../components/ui/Button";
import { FileDrop } from "../components/ui/FileDrop";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Section,
  Skeleton,
} from "../components/ui/Section";
import { api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { date, dateTime, int, label } from "../lib/format";
import {
  SOURCE_KINDS,
  uploadFile,
  useImports,
  useSources,
  type ImportRow,
  type SourceRow,
  type UploadInfo,
} from "../lib/imports";
import { useDemoStatus, useLoadDemo } from "../lib/queries";
import { History } from "./imports/History";
import { Wizard } from "./imports/Wizard";

const kindLabel = (k: string) =>
  SOURCE_KINDS.find((x) => x.value === k)?.label ?? label(k);

export function Imports() {
  const { can } = useAuth();
  const exec = can("finance_exec");
  const demo = useDemoStatus();
  const load = useLoadDemo();
  const sources = useSources();
  const imports = useImports();
  const [staged, setStaged] = useState<UploadInfo | null>(null);
  const [history, setHistory] = useState<SourceRow | null>(null);
  const [showSamples, setShowSamples] = useState(false);

  const upload = useMutation({ mutationFn: uploadFile, onSuccess: setStaged });
  const discard = () => {
    if (staged)
      void api(`/uploads/${staged.id}`, { method: "DELETE" }).catch(
        () => undefined,
      );
    setStaged(null);
    upload.reset();
  };
  const finish = () => {
    setStaged(null);
    upload.reset();
  };

  const empty = demo.data && !demo.data.loaded;

  return (
    <>
      <PageHeader
        title="Imports"
        lead="Upload a partner file, check how its columns map to ours, and import it. Every import is logged, repeat files are ignored, and refused rows come back as a report."
      />

      {!exec && (
        <div className="mt-8">
          <EmptyState title="Read only">
            Only the finance executive can import files. You can still see every
            import and mapping below.
          </EmptyState>
        </div>
      )}

      {exec && (
        <div className="mt-8 space-y-6">
          {staged ? (
            <Wizard
              upload={staged}
              onClose={discard}
              onFinished={finish}
              key={staged.id}
            />
          ) : (
            <>
              {empty && (
                <div className="reveal grid grid-cols-[1fr_auto] items-center gap-8 border border-rule border-l-4 border-l-accent px-8 py-6">
                  <div>
                    <h2 className="text-[1.3rem]">
                      Start with the demo workspace
                    </h2>
                    <p className="mt-1 max-w-[62ch] text-muted">
                      Nothing is imported yet. Load the sample data from eight
                      partners in one click and every screen fills in, or upload
                      your own files below. You can also download the sample
                      files and push them through the same steps by hand.
                    </p>
                  </div>
                  {demo.data?.available && (
                    <Button
                      variant="primary"
                      busy={load.isPending}
                      onClick={() => load.mutate()}
                    >
                      Load demo workspace
                    </Button>
                  )}
                </div>
              )}
              {load.error && <ErrorNote error={load.error} />}
              <FileDrop
                busy={upload.isPending}
                onFile={(f) => upload.mutate(f)}
              />
              {upload.error && <ErrorNote error={upload.error} />}
              {demo.data?.available && demo.data.files.length > 0 && (
                <div>
                  <button
                    className="text-[0.92rem] text-accent underline underline-offset-4"
                    aria-expanded={showSamples}
                    onClick={() => setShowSamples((v) => !v)}
                  >
                    {showSamples
                      ? "Hide the sample files"
                      : "Download sample files to try the upload"}
                  </button>
                  {showSamples && (
                    <ul className="reveal mt-3 grid grid-cols-2 gap-x-10 border-t border-rule">
                      {demo.data.files.map((f) => (
                        <li
                          key={f.name}
                          className="flex items-baseline justify-between gap-4 border-b border-rule py-2 text-[0.92rem]"
                        >
                          <span>
                            {f.label}{" "}
                            <span className="text-muted">
                              · {kindLabel(f.kind)}
                            </span>
                          </span>
                          <a
                            className="whitespace-nowrap text-accent underline underline-offset-4"
                            href={`/api/demo/files/${encodeURIComponent(f.name)}`}
                          >
                            {f.name}
                          </a>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </>
          )}
        </div>
      )}

      <Section
        title="Sources"
        note="Each source remembers the column mapping you approved. A file with the same columns imports in one click."
      >
        {sources.isLoading ? (
          <Skeleton h="h-32" />
        ) : sources.error ? (
          <ErrorNote error={sources.error} />
        ) : (sources.data ?? []).length === 0 ? (
          <EmptyState title="No sources yet">
            A source is created the first time you approve a mapping.
          </EmptyState>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>Partner</th>
                <th>Source</th>
                <th>Kind</th>
                <th className="r">Mapping</th>
                <th className="r">Imports</th>
                <th>Last import</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {sources.data!.map((s) => (
                <tr key={s.id}>
                  <td>{s.partner_name}</td>
                  <td>{s.name}</td>
                  <td className="text-muted">{kindLabel(s.kind)}</td>
                  <td className="r">
                    {s.approved_version ? `v${s.approved_version}` : "–"}
                  </td>
                  <td className="r">{int(s.imports)}</td>
                  <td className="whitespace-nowrap text-muted">
                    {s.last_import_at ? date(s.last_import_at) : "–"}
                  </td>
                  <td className="r">
                    <button
                      className="text-accent underline underline-offset-4"
                      onClick={() => setHistory(s)}
                    >
                      History
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section
        title="Recent imports"
        note="Newest first. Running imports update by themselves."
      >
        {imports.isLoading ? (
          <Skeleton h="h-32" />
        ) : imports.error ? (
          <ErrorNote error={imports.error} />
        ) : (imports.data ?? []).length === 0 ? (
          <EmptyState title="Nothing imported yet">
            Files you import appear here with how many rows were saved and
            refused.
          </EmptyState>
        ) : (
          <table className="tbl">
            <thead>
              <tr>
                <th>File</th>
                <th>Source</th>
                <th>Status</th>
                <th className="r">Entries</th>
                <th className="r">Duplicates</th>
                <th className="r">Refused</th>
                <th>When</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {imports.data!.map((i) => (
                <tr key={i.id}>
                  <td className="max-w-[16rem] truncate">{i.filename}</td>
                  <td className="text-muted">{i.source_name}</td>
                  <td>
                    <ImportStatus i={i} />
                  </td>
                  <td className="r">{int(i.rows_loaded)}</td>
                  <td className="r">{int(i.rows_duplicate)}</td>
                  <td
                    className={`r ${(i.rows_rejected ?? 0) > 0 ? "text-warn" : ""}`}
                  >
                    {int(i.rows_rejected)}
                  </td>
                  <td className="whitespace-nowrap text-muted">
                    {dateTime(i.finished_at ?? i.created_at)}
                  </td>
                  <td className="r">
                    {(i.rows_rejected ?? 0) > 0 && (
                      <a
                        className="text-accent underline underline-offset-4"
                        href={`/api/imports/${i.id}/rejects.csv`}
                      >
                        Reject report
                      </a>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <History source={history} exec={exec} onClose={() => setHistory(null)} />
    </>
  );
}

function ImportStatus({ i }: { i: ImportRow }) {
  if (i.status === "queued" || i.status === "running") {
    return (
      <span className="inline-flex items-center gap-2 text-accent">
        <span className="dots" aria-hidden>
          <span />
          <span />
          <span />
        </span>
        <span className="text-ink">
          {i.status === "queued"
            ? "Waiting"
            : `Running ${Math.round(i.progress * 100)}%`}
        </span>
      </span>
    );
  }
  const ok = i.status === "succeeded";
  return (
    <span
      className={`inline-flex items-center gap-2 ${ok ? "text-ok" : "text-risk"}`}
      title={i.error ?? undefined}
    >
      <span aria-hidden className="inline-block h-2 w-2 bg-current" />
      <span className="text-ink">{ok ? "Done" : "Failed"}</span>
    </span>
  );
}
