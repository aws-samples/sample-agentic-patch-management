import { useState, useEffect } from "react";

interface AuthConfig {
  email: string | null;
  logoutUrl: string | null;
  cognitoEnabled: boolean;
}

/**
 * Fetches auth config from the backend (Cognito email, logout URL).
 * Returns null while loading, then the config object.
 * Retries once after 1s if logoutUrl is null (can happen on first load after login
 * when ALB auth headers aren't fully propagated yet).
 */
export function useAuth() {
  const [auth, setAuth] = useState<AuthConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const fetchAuth = () =>
      fetch("/api/auth/config")
        .then((r) => {
          if (!r.ok) throw new Error(`Auth config failed: ${r.status}`);
          return r.json();
        })
        .then((data) => data ?? { email: null, logoutUrl: null, cognitoEnabled: false });

    fetchAuth()
      .then((data) => {
        setAuth(data);
        setError(null);
        // Retry once if logoutUrl is missing (ALB headers may not be ready yet)
        if (data.email && !data.logoutUrl) {
          setTimeout(() => fetchAuth().then(setAuth).catch(() => {}), 1500);
        }
      })
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Auth unavailable");
        setAuth({ email: null, logoutUrl: null, cognitoEnabled: false });
      });
  }, []);

  return { auth, authError: error };
}
