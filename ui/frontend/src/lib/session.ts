/**
 * The conversation keys a browser keeps: the session ID, the user it belongs
 * to, and per session the plan IDs it made (patchy-plans:<sid>). Messages
 * live in AgentCore Memory, never here.
 */
export const SESSION_KEY = "patchy-session-id";
export const SESSION_USER_KEY = "patchy-session-user";
const PLANS_PREFIX = "patchy-plans:";

export const plansKey = (sid: string) => `${PLANS_PREFIX}${sid}`;

type Store = Pick<Storage, "getItem" | "removeItem" | "key" | "length">;

function local(): Store | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** Sign-out and idle logout: drop the session, its user and every stored plan list, so none is left orphaned. */
export function forgetSession(store: Store | null = local()): void {
  if (!store) return;
  try {
    store.removeItem(SESSION_KEY);
    store.removeItem(SESSION_USER_KEY);
    const plans: string[] = [];
    for (let i = 0; i < store.length; i++) {
      const k = store.key(i);
      if (k?.startsWith(PLANS_PREFIX)) plans.push(k);
    }
    plans.forEach((k) => store.removeItem(k));
  } catch {
    // best-effort
  }
}
