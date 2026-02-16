import { useState, useCallback } from "react";

export type Role = "operator" | "viewer";

const ROLE_STORAGE_KEY = "patchy-role";

function getStoredRole(): Role {
  try {
    const stored = localStorage.getItem(ROLE_STORAGE_KEY);
    if (stored === "viewer") return "viewer";
  } catch {
    // localStorage unavailable
  }
  return "operator";
}

/**
 * Local role state with localStorage persistence.
 * The selected role is sent as X-Role header on every API request —
 * the backend enforces it server-side.
 */
export function useRole() {
  const [role, setRoleState] = useState<Role>(getStoredRole);

  const setRole = useCallback((newRole: Role) => {
    setRoleState(newRole);
    try {
      localStorage.setItem(ROLE_STORAGE_KEY, newRole);
    } catch {
      // best-effort
    }
  }, []);

  return { role, setRole };
}
