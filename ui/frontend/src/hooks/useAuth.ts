import { useState, useEffect } from "react";

interface AuthConfig {
  email: string | null;
  logoutUrl: string | null;
  cognitoEnabled: boolean;
}

/**
 * Fetches auth config from the backend (Cognito email, logout URL).
 * Returns null while loading, then the config object.
 */
export function useAuth() {
  const [auth, setAuth] = useState<AuthConfig | null>(null);

  useEffect(() => {
    fetch("/api/auth/config")
      .then((r) => (r.ok ? r.json() : null))
      .then((data) => setAuth(data ?? { email: null, logoutUrl: null, cognitoEnabled: false }))
      .catch(() => setAuth({ email: null, logoutUrl: null, cognitoEnabled: false }));
  }, []);

  return auth;
}
