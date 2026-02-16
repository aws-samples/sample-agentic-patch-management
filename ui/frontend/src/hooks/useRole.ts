import { useState, useEffect } from "react";

export type Role = "operator" | "viewer";

/**
 * Fetch the authenticated user's role from the API.
 * Returns "operator" by default (auth disabled or fetch failure).
 */
export function useRole() {
  const [role, setRole] = useState<Role>("operator");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const headers: Record<string, string> = {};
    const apiKey = import.meta.env.VITE_API_KEY;
    if (apiKey) headers["X-API-Key"] = apiKey;

    fetch("/api/role", { headers })
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (data?.role === "viewer") setRole("viewer");
      })
      .catch(() => {
        // Default to operator on failure (backward compatible)
      })
      .finally(() => setLoading(false));
  }, []);

  return { role, roleLoading: loading };
}
