import * as Dialog from "@radix-ui/react-dialog";
import { Command } from "cmdk";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../lib/auth";
import { label as statusLabel } from "../lib/format";
import { ENTRIES, allowed, filterEntries, wantsOrders } from "../lib/palette";
import { useOrders, useRunRecon } from "../lib/queries";

export const OPEN_PALETTE = "recon:palette";

const item =
  "flex cursor-pointer items-center justify-between gap-4 px-4 py-2.5 text-[1rem] data-[selected=true]:bg-wash data-[selected=true]:text-accent";
const heading = "eyebrow px-4 pb-1 pt-3";

/** Ctrl+K (or Cmd+K) anywhere: jump to a screen, do a common thing, or find an order. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const navigate = useNavigate();
  const { can, signOut } = useAuth();
  const run = useRunRecon();
  const orders = useOrders(
    { q: query.trim(), limit: 6, offset: 0 },
    open && wantsOrders(query),
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    const onOpen = () => setOpen(true);
    window.addEventListener("keydown", onKey);
    window.addEventListener(OPEN_PALETTE, onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener(OPEN_PALETTE, onOpen);
    };
  }, []);

  const close = () => {
    setOpen(false);
    setQuery("");
  };
  const choose = (id: string) => {
    close();
    if (id.startsWith("go:")) navigate(id.slice(3));
    else if (id.startsWith("order:")) navigate(`/orders?order=${id.slice(6)}`);
    else if (id === "do:upload") navigate("/imports");
    else if (id === "do:recon") run.mutate();
    else if (id === "do:signout") void signOut();
  };

  const shown = filterEntries(
    ENTRIES.filter((e) => allowed(e, can)),
    query,
  );
  const found = wantsOrders(query) ? (orders.data?.rows ?? []).slice(0, 6) : [];

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => (o ? setOpen(true) : close())}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay fixed inset-0 z-40 bg-ink/30" />
        <Dialog.Content
          aria-describedby={undefined}
          className="reveal fixed left-1/2 top-[16vh] z-50 w-[38rem] max-w-[92vw] -translate-x-1/2 border border-rule-strong bg-paper"
        >
          <Dialog.Title className="sr-only">Search or jump to</Dialog.Title>
          <Command shouldFilter={false} label="Search or jump to" loop>
            <Command.Input
              value={query}
              onValueChange={setQuery}
              autoFocus
              placeholder="Jump to a screen, or type an order reference or AWB"
              style={{ outlineOffset: "-2px" }}
              className="w-full border-b border-rule bg-paper px-4 py-3.5 text-[1.05rem]"
            />
            <Command.List className="max-h-[22rem] overflow-y-auto pb-2">
              <Command.Empty className="px-4 py-6 text-center text-muted">
                Nothing matches.
              </Command.Empty>
              {found.length > 0 && (
                <Command.Group heading={<div className={heading}>Orders</div>}>
                  {found.map((o) => (
                    <Command.Item
                      key={o.id}
                      value={`order:${o.id}`}
                      onSelect={choose}
                      className={item}
                    >
                      <span>{o.order_ref}</span>
                      <span className="text-[0.88rem] text-muted">
                        {o.channel} · {statusLabel(o.status)}
                      </span>
                    </Command.Item>
                  ))}
                </Command.Group>
              )}
              {(["Go to", "Do"] as const).map((g) => {
                const rows = shown.filter((e) => e.group === g);
                return rows.length === 0 ? null : (
                  <Command.Group
                    key={g}
                    heading={<div className={heading}>{g}</div>}
                  >
                    {rows.map((e) => (
                      <Command.Item
                        key={e.id}
                        value={e.id}
                        onSelect={choose}
                        className={item}
                      >
                        {e.label}
                      </Command.Item>
                    ))}
                  </Command.Group>
                );
              })}
            </Command.List>
          </Command>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
