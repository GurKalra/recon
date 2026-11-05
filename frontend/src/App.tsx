import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./components/Shell";
import { Skeleton } from "./components/ui/Section";
import { useAuth } from "./lib/auth";
import { Orders } from "./pages/Orders";
import { Overview } from "./pages/Overview";
import { Review } from "./pages/Review";
import { SignIn } from "./pages/SignIn";
import { Soon } from "./pages/Soon";

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
        <Route index element={<Overview />} />
        <Route path="orders" element={<Orders />} />
        <Route path="review" element={<Review />} />
        <Route
          path="imports"
          element={
            <Soon
              title="Imports"
              lead="Upload files and approve column mappings."
            />
          }
        />
        <Route
          path="claims"
          element={
            <Soon title="Claims" lead="Batches per partner with evidence." />
          }
        />
        <Route
          path="rates"
          element={
            <Soon
              title="Rate cards"
              lead="Expected amounts per channel and courier."
            />
          }
        />
        <Route
          path="audit"
          element={<Soon title="Audit trail" lead="Who did what, and when." />}
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}
