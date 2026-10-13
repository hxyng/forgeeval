import { Link, Route, Routes } from "react-router-dom";
import { useAuth } from "./auth";
import { Shell } from "./components/Shell";
import { Empty } from "./components/ui";
import { ItemsPage } from "./pages/ItemsPage";
import { RunDetailPage } from "./pages/RunDetailPage";
import { RunsPage } from "./pages/RunsPage";
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
        <Route path="runs/:id" element={<RunDetailPage />} />
        <Route path="runs/:id/items" element={<ItemsPage />} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  );
}
