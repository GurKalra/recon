import { useEffect } from "react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { Shell } from "./components/Shell";
import { Button } from "./components/ui/Button";
import {
  EmptyState,
  ErrorNote,
  PageHeader,
  Skeleton,
} from "./components/ui/Section";
import { useAuth } from "./lib/auth";
import { useDashboard, useDemoStatus, useLoadDemo } from "./lib/queries";
import { Claims } from "./pages/Claims";
import { Imports } from "./pages/Imports";
import { Orders } from "./pages/Orders";
import { Overview } from "./pages/Overview";
import { Review } from "./pages/Review";
import { SignIn } from "./pages/SignIn";
import { Rates } from "./pages/Rates";
import { Audit } from "./pages/Audit";

const FIRST_RUN = "recon.first-run-seen";

/** On a workspace with no data, the first visit of a session opens Imports. After that, the
    Overview explains itself and points there. Once there is data, this is just the Overview. */
function Home() {
  const dash = useDashboard();
  if (dash.isLoading)
    return (
      <div className="grid gap-4">
        <Skeleton h="h-24" />
        <Skeleton h="h-64" />
      </div>
    );
  if (dash.error)
    return (
      <>
        <PageHeader title="Overview" />
        <div className="mt-8">
          <ErrorNote error={dash.error} />
        </div>
      </>
    );
  if (dash.data!.headline.orders === 0) {
    let seen = false;
    try {
      seen = sessionStorage.getItem(FIRST_RUN) === "1";
    } catch {
      /* private mode: fine */
    }
    return seen ? <NoData /> : <FirstRun />;
  }
  return <Overview />;
}

function FirstRun() {
  const nav = useNavigate();
  useEffect(() => {
    try {
      sessionStorage.setItem(FIRST_RUN, "1");
    } catch {
      /* ignore */
    }
    nav("/imports", { replace: true });
  }, [nav]);
  return null;
}

function NoData() {
  const { can } = useAuth();
  const demo = useDemoStatus();
  const load = useLoadDemo();
  const exec = can("finance_exec");
  return (
    <>
      <PageHeader
        title="Overview"
        lead="Money at risk by partner and age appears here once files are imported."
      />
      <div className="mt-10">
        <EmptyState
          title="No data yet"
          action={
            <span className="inline-flex gap-3">
              {exec && (
                <Link
                  to="/imports"
                  className="inline-flex items-center border border-accent bg-accent px-4 py-2 text-[0.95rem] text-white hover:bg-[#162b47]"
                >
                  Go to Imports
                </Link>
              )}
              {exec && demo.data?.available && (
                <Button busy={load.isPending} onClick={() => load.mutate()}>
                  Load demo workspace
                </Button>
              )}
            </span>
          }
        >
          {exec
            ? "Import your order, settlement, COD and freight files, or load the demo workspace to see the tool on sample data from eight partners."
            : "The finance executive imports the files. Numbers appear here once they have."}
        </EmptyState>
        {load.error && (
          <div className="mt-4">
            <ErrorNote error={load.error} />
          </div>
        )}
      </div>
    </>
  );
}

export function App() {
  const { user, loading } = useAuth();
  if (loading)
    return (
      <div className="p-12">
        <Skeleton h="h-40" />
      </div>
    );
  if (!user) return <SignIn />;
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<Home />} />
        <Route path="orders" element={<Orders />} />
        <Route path="review" element={<Review />} />
        <Route path="imports" element={<Imports />} />
        <Route path="claims" element={<Claims />} />
        <Route path="rates" element={<Rates />} />
        <Route path="audit" element={<Audit />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
