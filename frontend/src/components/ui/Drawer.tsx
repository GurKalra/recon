import * as Dialog from "@radix-ui/react-dialog";
import type { ReactNode } from "react";

/** A panel that slides in from the right over a dimmed page. Radix handles focus and Escape. */
export function Drawer({
  open,
  onClose,
  title,
  description,
  children,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
}) {
  return (
    <Dialog.Root open={open} onOpenChange={(o) => !o && onClose()}>
      <Dialog.Portal>
        <Dialog.Overlay className="drawer-overlay fixed inset-0 z-40 bg-ink/30" />
        <Dialog.Content
          aria-describedby={undefined}
          className="drawer-panel fixed inset-y-0 right-0 z-50 flex w-[44rem] max-w-full flex-col border-l border-rule-strong bg-paper"
        >
          <header className="flex items-start justify-between gap-4 border-b border-rule px-8 py-6">
            <div className="min-w-0">
              <Dialog.Title className="truncate text-[1.5rem] leading-tight">
                {title}
              </Dialog.Title>
              {description && <p className="mt-1 text-muted">{description}</p>}
            </div>
            <Dialog.Close
              aria-label="Close"
              className="flex h-8 w-8 shrink-0 items-center justify-center border border-rule-strong text-muted hover:text-ink"
            >
              <svg
                width="14"
                height="14"
                viewBox="0 0 14 14"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.4"
                aria-hidden
              >
                <path d="M2 2l10 10M12 2L2 12" />
              </svg>
            </Dialog.Close>
          </header>
          <div className="min-h-0 flex-1 overflow-y-auto px-8 pb-10 pt-6">
            {children}
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
