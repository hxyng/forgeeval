import { NavLink, Outlet } from "react-router-dom";
import { useWhoAmI } from "../api/queries";
import { useAuth } from "../auth";
import { shortId } from "../lib/format";

interface NavItem {
  to: string;
  label: string;
  enabled: boolean;
  end?: boolean;
}

export const NAV: NavItem[] = [
  { to: "/", label: "Runs", enabled: true, end: true },
  { to: "/compare", label: "Compare", enabled: true },
  { to: "/gates", label: "Gates", enabled: true },
  { to: "/traces", label: "Traces", enabled: true },
];

export function Shell() {
  const { apiKey, signOut } = useAuth();
  const who = useWhoAmI(apiKey);
  return (
    <div className="shell">
      <header className="topbar">
        <span className="brand">ForgeEval</span>
        <nav aria-label="Primary">
          {NAV.map((n) =>
            n.enabled ? (
              <NavLink key={n.to} to={n.to} end={n.end} className="nav-link">
                {n.label}
              </NavLink>
            ) : (
              <span
                key={n.to}
                className="nav-link nav-disabled"
                aria-disabled="true"
                title="Coming soon"
              >
                {n.label}
              </span>
            ),
          )}
        </nav>
        <div className="topbar-right">
          {who.data && (
            <details className="account">
              <summary>
                project <span className="mono">{shortId(who.data.project_id)}</span>
              </summary>
              <div className="account-panel">
                <dl className="facts facts-stack">
                  <div>
                    <dt>Project</dt>
                    <dd className="mono">{who.data.project_id}</dd>
                  </div>
                  <div>
                    <dt>Scopes</dt>
                    <dd>
                      <ul className="tags" aria-label="Scopes">
                        {who.data.scopes.map((s) => (
                          <li key={s} className="tag mono">
                            {s}
                          </li>
                        ))}
                      </ul>
                    </dd>
                  </div>
                </dl>
              </div>
            </details>
          )}
          <button type="button" className="btn btn-small" onClick={signOut}>
            Sign out
          </button>
        </div>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
