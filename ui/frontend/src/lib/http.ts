/**
 * Requests to the Patchy API.
 *
 * In API-key mode the key is asked for at run time and kept in sessionStorage
 * for this tab; it is never built into the bundle, where anyone who can load
 * the page could read it. Cognito sign-in needs none of this: the ALB adds the
 * user's tokens.
 */

const KEY_STORAGE = "patchy-api-key";

function storedKey(): string | null {
  try {
    return sessionStorage.getItem(KEY_STORAGE);
  } catch {
    return null;
  }
}

function storeKey(key: string): void {
  try {
    sessionStorage.setItem(KEY_STORAGE, key);
  } catch {
    // sessionStorage unavailable: the key lasts for this request only
  }
}

export function apiHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const key = storedKey();
  if (key) headers["X-API-Key"] = key;
  return headers;
}

function forgetKey(): void {
  try {
    sessionStorage.removeItem(KEY_STORAGE);
  } catch {
    // nothing stored
  }
}

async function bodyOf(res: Response): Promise<{ auth?: string } | null> {
  try {
    return await res.clone().json();
  } catch {
    return null;
  }
}

async function needsApiKey(res: Response): Promise<boolean> {
  return res.status === 401 && (await bodyOf(res))?.auth === "api_key";
}

/**
 * The sign-in has expired: the ALB redirects to Cognito (seen as an opaque
 * redirect, since requests don't follow redirects), or the API refuses the
 * request with a 401 that isn't asking for an API key.
 */
export function signInExpired(res: { status: number; type?: string }, body: { auth?: string } | null): boolean {
  if (res.type === "opaqueredirect") return true;
  return res.status === 401 && body?.auth !== "api_key";
}

/** Only a 503 (the agent starting up) is safe to send again: a 5xx after the agent took the prompt could run it twice. */
export function retryable(status: number): boolean {
  return status === 503;
}

// After a cancelled or refused key, wait before asking again: 15 s, doubling to 5 min.
export function keyPromptDelay(failures: number): number {
  return failures <= 0 ? 0 : Math.min(5 * 60_000, 15_000 * 2 ** (failures - 1));
}

let keyFailures = 0;
let askAfter = 0;
let lastRefused: string | null = null;

function keyFailed(key: string | null): void {
  // Requests refused together count once.
  if (key !== null && key === lastRefused) return;
  lastRefused = key;
  keyFailures += 1;
  askAfter = Date.now() + keyPromptDelay(keyFailures);
}

// Requests made at the same moment share one prompt.
let asking: Promise<string | null> | null = null;

function askForKey(): Promise<string | null> {
  asking ??= Promise.resolve().then(() => {
    const key = window.prompt("This Patchy needs an API key. Enter it to continue:")?.trim() || null;
    if (key) storeKey(key);
    else keyFailed(null);
    asking = null;
    return key;
  });
  return asking;
}

const RELOAD_KEY = "patchy-signin-reload";

/** Reload so the ALB sends the user to sign in; at most once a minute, so a page that can't sign in doesn't loop. */
function signInAgain(): boolean {
  try {
    const last = Number(sessionStorage.getItem(RELOAD_KEY)) || 0;
    if (Date.now() - last < 60_000) return false;
    sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    return false;
  }
  window.location.reload();
  return true;
}

export class SignInExpired extends Error {
  constructor() {
    super("Your sign-in has expired. Reload the page to sign in again.");
    this.name = "SignInExpired";
  }
}

/**
 * fetch with the API headers. Asks for the API key when the server says it
 * needs one (backing off after a cancelled or wrong key), and sends the user to
 * sign in again when the Cognito session has expired.
 */
export async function apiFetch(url: string, init: RequestInit = {}): Promise<Response> {
  const send = () => fetch(url, { redirect: "manual", ...init, headers: { ...apiHeaders(), ...(init.headers ?? {}) } });
  const sentWith = storedKey();
  let res = await send();
  if (await needsApiKey(res)) {
    // Another request may have asked already; retry with its key before asking again.
    if (storedKey() && storedKey() !== sentWith) return send();
    // The stored key was refused: drop it and ask for another.
    if (sentWith) forgetKey();
    if (Date.now() < askAfter) return res;
    const key = await askForKey();
    if (!key) return res;
    res = await send();
    if (await needsApiKey(res)) { forgetKey(); keyFailed(key); }
    else { keyFailures = 0; lastRefused = null; }
    return res;
  }
  if (signInExpired(res, res.status === 401 ? await bodyOf(res) : null)) {
    signInAgain();
    throw new SignInExpired();
  }
  return res;
}
