// The API key lives in sessionStorage only: it dies with the tab and is never written to disk.
const KEY = "forgeeval.apiKey";

type Listener = () => void;
const listeners = new Set<Listener>();
let memoryKey: string | null = null; // fallback when storage is unavailable

function read(): string | null {
  try {
    return window.sessionStorage.getItem(KEY) ?? memoryKey;
  } catch {
    return memoryKey;
  }
}

export function getApiKey(): string | null {
  return read();
}

export function setApiKey(key: string): void {
  memoryKey = key;
  try {
    window.sessionStorage.setItem(KEY, key);
  } catch {
    /* storage blocked: keep the key in memory for this page load */
  }
  listeners.forEach((l) => l());
}

export function clearApiKey(): void {
  memoryKey = null;
  try {
    window.sessionStorage.removeItem(KEY);
  } catch {
    /* nothing to clear */
  }
  listeners.forEach((l) => l());
}

export function subscribeApiKey(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

// Called by the HTTP client when an authenticated request comes back 401.
type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler = () => clearApiKey();
export function setUnauthorizedHandler(h: UnauthorizedHandler): void {
  onUnauthorized = h;
}
export function notifyUnauthorized(): void {
  onUnauthorized();
}
