import { useState } from "react";
import type { FormEvent } from "react";
import { useAuth } from "../auth";

export function SignIn() {
  const { signIn, notice } = useAuth();
  const [key, setKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!key.trim()) {
      setError("Paste a project API key (it starts with fe_).");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await signIn(key);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed.");
      setBusy(false);
    }
  }

  return (
    <main className="signin">
      <form onSubmit={submit} className="signin-form" aria-labelledby="signin-title">
        <h1 id="signin-title">ForgeEval</h1>
        <p className="muted">
          Sign in with a project API key. It is kept for this browser tab only.
        </p>
        {notice && (
          <div className="state state-warn" role="status">
            {notice}
          </div>
        )}
        <label htmlFor="api-key">API key</label>
        <input
          id="api-key"
          name="api-key"
          type="password"
          className="mono"
          autoComplete="off"
          spellCheck={false}
          placeholder="fe_…"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? "signin-error" : undefined}
        />
        {error && (
          <div id="signin-error" className="state state-error" role="alert">
            {error}
          </div>
        )}
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Checking…" : "Sign in"}
        </button>
        <p className="hint muted">
          Create a key with{" "}
          <span className="mono">docker compose exec api python -m forgeeval.admin bootstrap</span>.
        </p>
      </form>
    </main>
  );
}
