import { Link, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import { Shell } from "./components/Shell";
import { Empty } from "./components/ui";
import { CompareIndexPage, ComparePage } from "./pages/ComparePage";
import { GatesPage } from "./pages/GatesPage";
import { ItemsPage } from "./pages/ItemsPage";
import { NewRunPage } from "./pages/NewRunPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";
import { TraceDetailPage, TracesPage } from "./pages/TracesPage";
import { SignIn } from "./pages/SignIn";

function NotFound() {
  return (
    <Empty title="Page not found">
      <Link to="/">Back to runs</Link>
    </Empty>
  );
}

export function App() {
  const { apiKey } = useAuth();
  if (!apiKey) return <SignIn />;
  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<RunsPage />} />
        <Route path="runs/new" element={<NewRunPage />} />
        <Route path="runs/:id" element={<RunDetailPage />} />
        <Route path="runs/:id/items" element={<ItemsPage />} />
        <Route path="runs/:id/compare" element={<ComparePage />} />
        <Route path="compare" element={<CompareIndexPage />} />
        <Route path="gates" element={<GatesPage />} />
        <Route path="traces" element={<TracesPage />} />
        <Route path="traces/:id" element={<TraceDetailPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
