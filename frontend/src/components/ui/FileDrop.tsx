import { useRef, useState } from "react";

/** Drop a file or choose one. Keyboard users get the native file button. */
export function FileDrop({
  onFile,
  busy,
  accept = ".csv,.xlsx",
}: {
  onFile: (f: File) => void;
  busy?: boolean;
  accept?: string;
}) {
  const [over, setOver] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  return (
    <label
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        const f = e.dataTransfer.files[0];
        if (f && !busy) onFile(f);
      }}
      className={`flex cursor-pointer flex-col items-center gap-2 border border-dashed px-8 py-12 text-center transition-colors duration-150 focus-within:outline focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-accent ${
        over ? "border-accent bg-wash" : "border-rule-strong hover:bg-wash"
      } ${busy ? "pointer-events-none opacity-70" : ""}`}
    >
      <svg
        width="28"
        height="28"
        viewBox="0 0 18 18"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.2"
        strokeLinecap="square"
        className="text-accent"
        aria-hidden
      >
        <path d="M9 12V2M5 6l4-4 4 4M2 12v4h14v-4" />
      </svg>
      {busy ? (
        <span className="text-[1.1rem]">
          <span className="dots mr-2 text-accent" aria-hidden>
            <span />
            <span />
            <span />
          </span>
          Reading the file
        </span>
      ) : (
        <>
          <span className="text-[1.15rem]">
            Drop a partner file here, or{" "}
            <span className="text-accent underline underline-offset-4">
              choose one
            </span>
          </span>
          <span className="text-[0.9rem] text-muted">
            .csv or .xlsx, up to 100 MB. Settlements, orders, COD remittances,
            freight invoices, warehouse receipts.
          </span>
        </>
      )}
      <input
        ref={input}
        type="file"
        accept={accept}
        className="sr-only"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) onFile(f);
          if (input.current) input.current.value = "";
        }}
      />
    </label>
  );
}
