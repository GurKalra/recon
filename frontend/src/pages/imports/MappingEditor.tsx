import {
  columnRefs,
  norm,
  setColumn,
  type MappingNote,
  type Obj,
} from "../../lib/imports";

const DOT = { high: "bg-ok", medium: "bg-warn", low: "bg-risk" } as const;

/** One row per place the mapping names a column. Change a dropdown and the mapping changes. */
export function MappingEditor({
  spec,
  headers,
  notes,
  onChange,
}: {
  spec: Obj;
  headers: string[];
  notes: MappingNote[];
  onChange: (s: Obj) => void;
}) {
  const refs = columnRefs(spec);
  const byField = new Map(notes.map((n) => [n.field, n]));
  return (
    <table className="tbl">
      <thead>
        <tr>
          <th>What it is</th>
          <th>Column in your file</th>
          <th>How sure</th>
        </tr>
      </thead>
      <tbody>
        {refs.map((r) => {
          const hit = r.value
            ? headers.find((h) => norm(h) === norm(r.value!))
            : undefined;
          const missing = r.value !== null && !hit;
          const note =
            byField.get(r.key) ??
            (r.key.startsWith("emit:")
              ? notes.find(
                  (n) =>
                    n.field.startsWith("emit:") &&
                    norm(n.field.slice(5)) === norm(r.value ?? ""),
                )
              : undefined);
          return (
            <tr key={r.path.join(".")}>
              <td className="whitespace-nowrap">
                {r.label}
                {!r.optional && (
                  <span className="text-risk" title="Needed">
                    {" "}
                    *
                  </span>
                )}
              </td>
              <td>
                <select
                  aria-label={r.label}
                  className={`w-full max-w-xs border bg-paper px-2.5 py-1.5 text-[0.95rem] ${missing ? "border-risk" : "border-rule-strong"}`}
                  value={hit ?? r.value ?? ""}
                  onChange={(e) =>
                    onChange(
                      setColumn(
                        spec,
                        r.path,
                        e.target.value === "" ? null : e.target.value,
                      ),
                    )
                  }
                >
                  {r.optional && <option value="">Not used</option>}
                  {!r.optional && r.value === null && (
                    <option value="">Choose a column</option>
                  )}
                  {missing && (
                    <option value={r.value!}>
                      {r.value} (not in this file)
                    </option>
                  )}
                  {headers.map((h) => (
                    <option key={h} value={h}>
                      {h}
                    </option>
                  ))}
                </select>
              </td>
              <td className="text-[0.9rem]">
                {note ? (
                  <span className="inline-flex items-start gap-2">
                    <span
                      className={`mt-[0.45rem] inline-block h-2 w-2 shrink-0 ${DOT[note.confidence]}`}
                      aria-hidden
                    />
                    <span>
                      <span className="capitalize">{note.confidence}</span>
                      <span className="text-muted"> · {note.reason}</span>
                    </span>
                  </span>
                ) : (
                  <span className="text-faint">
                    {r.value ? "Set by you" : ""}
                  </span>
                )}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
