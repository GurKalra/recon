import type { ReactNode } from "react";
import { useLocation } from "react-router-dom";

/** Remounts on a change of screen (not on a change of filters), so each screen settles in. */
export function PageTransition({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  return (
    <div key={pathname} className="page-in">
      {children}
    </div>
  );
}
