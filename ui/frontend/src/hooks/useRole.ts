import { useState, useEffect } from "react";
import { apiFetch } from "../lib/http";

export type Role = "operator" | "viewer";

/**
 * The role the server grants (GET /api/role): from the user's Cognito groups,
 * or the API key. The UI shows it; it can't be changed here, and the server
 * enforces it on every request.
 */
export function useRole() {
  const [role, setRole] = useState<Role>("viewer");
  const [known, setKnown] = useState(false);
  const [auditor, setAuditor] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch("/api/role")
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        if (cancelled) return;
        if (d?.role === "operator" || d?.role === "viewer") setRole(d.role);
        // Auditors also see every operator's plans, read-only (GET /api/changes).
        setAuditor(d?.auditor === true);
        setKnown(true);
      })
      .catch(() => !cancelled && setKnown(true));
    return () => {
      cancelled = true;
    };
  }, []);

  return { role, known, auditor };
}
