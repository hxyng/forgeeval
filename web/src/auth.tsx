import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from "react";
import type { ReactNode } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, unwrap, type Schemas } from "./api/client";
import {
  clearApiKey,
  getApiKey,
  setApiKey,
  setUnauthorizedHandler,
  subscribeApiKey,
} from "./api/session";

type Identity = Schemas["WhoAmI"];

interface AuthState {
  apiKey: string | null;
  /** Shown on the sign-in page after a session ends on its own (401). */
  notice: string | null;
  signIn: (key: string) => Promise<Identity>;
  signOut: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const qc = useQueryClient();
  const apiKey = useSyncExternalStore(subscribeApiKey, getApiKey, () => null);
  const [notice, setNotice] = useState<string | null>(null);

  const signOut = useCallback(() => {
    clearApiKey();
    qc.clear();
  }, [qc]);

  useEffect(() => {
    // A 401 on any request returns the user to sign-in.
    setUnauthorizedHandler(() => {
      setNotice("Your session ended: the API key was rejected. Sign in again.");
      signOut();
    });
  }, [signOut]);

  const signIn = useCallback(
    async (key: string) => {
      const trimmed = key.trim();
      // Validate before storing: the key goes in an explicit header so a bad key is not "a session".
      const identity = await unwrap(
        api.GET("/v1/whoami", { headers: { Authorization: `Bearer ${trimmed}` } }),
      );
      qc.clear();
      setNotice(null);
      qc.setQueryData(["whoami", trimmed], identity);
      setApiKey(trimmed);
      return identity;
    },
    [qc],
  );

  const value = useMemo(
    () => ({ apiKey, notice, signIn, signOut }),
    [apiKey, notice, signIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}
