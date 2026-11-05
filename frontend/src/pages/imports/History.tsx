import { Button } from "../../components/ui/Button";
import { Drawer } from "../../components/ui/Drawer";
import { ErrorNote, Skeleton } from "../../components/ui/Section";
import { dateTime } from "../../lib/format";
import {
  useMappingHistory,
  useRollback,
  type SourceRow,
} from "../../lib/imports";

/** Every version of a source's column mapping. Rolling back adds a new version copied from an old
    one; nothing is ever rewritten. */
export function History({
  source,
  exec,
  onClose,
}: {
  source: SourceRow | null;
  exec: boolean;
  onClose: () => void;
}) {
  const q = useMappingHistory(source?.id ?? null);
  const roll = useRollback(source?.id ?? 0);
  return (
    <Drawer
      open={source !== null}
      onClose={onClose}
      title={source?.name ?? ""}
      description={
        source ? `${source.partner_name} · mapping history` : undefined
      }
    >
      {q.isLoading ? (
        <Skeleton h="h-40" />
      ) : q.error ? (
        <ErrorNote error={q.error} />
      ) : (
        <ol className="space-y-0 border-t border-rule">
          {(q.data ?? []).map((v) => (
            <li
              key={v.id}
              className="flex items-start justify-between gap-4 border-b border-rule py-4"
            >
              <div>
                <div className="flex items-baseline gap-3">
                  <span className="text-[1.15rem]">Version {v.version}</span>
                  {v.status === "approved" && (
                    <span className="border border-accent px-2 py-0.5 text-[0.75rem] uppercase tracking-wider text-accent">
                      Current
                    </span>
                  )}
                </div>
                <div className="text-[0.9rem] text-muted">
                  {v.approved_by ? `Approved by ${v.approved_by}` : "Approved"}{" "}
                  · {dateTime(v.approved_at)}
                </div>
                {v.note && <div className="mt-1 text-[0.92rem]">{v.note}</div>}
              </div>
              {exec && v.status !== "approved" && (
                <Button
                  busy={roll.isPending}
                  onClick={() => roll.mutate(v.version)}
                >
                  Make current
                </Button>
              )}
            </li>
          ))}
        </ol>
      )}
      {roll.error && (
        <div className="mt-4">
          <ErrorNote error={roll.error} />
        </div>
      )}
      <p className="mt-6 text-[0.88rem] text-muted">
        Making an older version current saves it as a new version, so the full
        history stays.
      </p>
    </Drawer>
  );
}
