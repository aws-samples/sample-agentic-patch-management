import { Menu, MenuButton, MenuItem, MenuItems } from "@headlessui/react";
import type { AccountScope } from "../lib/api";
import type { Role } from "../hooks/useRole";
import type { Theme } from "../hooks/useTheme";
import { forgetSession } from "../lib/session";

export function Brand() {
  return (
    <a href="/" className="brand" aria-label="Patchy home">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M12 3 4.5 6v5.5c0 4.4 3.1 8.2 7.5 9.5 4.4-1.3 7.5-5.1 7.5-9.5V6L12 3Z" />
        <path d="m8.8 12.2 2.2 2.2 4.4-4.6" />
      </svg>
      <span>Patchy</span>
    </a>
  );
}

interface Props {
  scopes: AccountScope[];
  account: string;
  onAccount: (account: string) => void;
  theme: Theme;
  onTheme: () => void;
  email: string | null;
  role: Role;
  roleKnown: boolean;
  logoutUrl: string | null;
  onPalette?: () => void;
}

export function UserControls({ theme, onTheme, email, role, roleKnown, logoutUrl }:
    Pick<Props, "theme" | "onTheme" | "email" | "role" | "roleKnown" | "logoutUrl">) {
  return (
    <div className="user">
      <button type="button" className="topbtn" onClick={onTheme} aria-label={theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}>
        {theme === "dark" ? "Light theme" : "Dark theme"}
      </button>
      {email && <span className="who" title={email}>{email}</span>}
      {roleKnown && <span className="role" title={role === "operator" ? "You can approve and stop changes" : "You can see everything; operators make changes"}>
        {role === "operator" ? "Operator" : "Viewer, read-only"}
      </span>}
      {logoutUrl && (
        <button type="button" className="topbtn" onClick={() => {
          // Sign-out always starts a fresh conversation.
          forgetSession();
          window.location.replace(logoutUrl);
        }}>Sign out</button>
      )}
    </div>
  );
}

export default function TopBar(props: Props) {
  const { scopes, account, onAccount, onPalette } = props;
  const accounts = [...new Set(scopes.map((s) => s.accountId))];
  const regions = new Set(scopes.map((s) => s.region)).size;
  const mac = typeof navigator !== "undefined" && /Mac/.test(navigator.platform);
  return (
    <header className="top">
      <Brand />
      {accounts.length > 0 && (
        <Menu as="div" className="relative">
          <MenuButton className="scope" aria-label="Choose the accounts shown">
            {account === "ALL" ? `All accounts (${accounts.length})` : <span className="mono" style={{ color: "var(--ink)" }}>{account}</span>}
            <span className="sep">·</span>{regions} Region{regions === 1 ? "" : "s"}
            {accounts.length > 1 && <span className="caret" aria-hidden="true">▾</span>}
          </MenuButton>
          {accounts.length > 1 && (
            <MenuItems className="menu" anchor="bottom start">
              <div className="menu-note">Show the overview and findings for</div>
              <MenuItem><button type="button" className={account === "ALL" ? "on" : ""} onClick={() => onAccount("ALL")}>All accounts</button></MenuItem>
              {accounts.map((a) => (
                <MenuItem key={a}>
                  <button type="button" className={account === a ? "on" : ""} onClick={() => onAccount(a)}>
                    <span className="mono">{a}</span>
                    <span className="d">{scopes.filter((s) => s.accountId === a).length} Region{scopes.filter((s) => s.accountId === a).length === 1 ? "" : "s"}</span>
                  </button>
                </MenuItem>
              ))}
            </MenuItems>
          )}
        </Menu>
      )}
      {onPalette && (
        <button type="button" className="topbtn" onClick={onPalette} aria-label="Open the list of things to ask">
          Ask… <kbd>{mac ? "⌘K" : "Ctrl K"}</kbd>
        </button>
      )}
      <UserControls {...props} />
      <a className="topbtn" href="https://github.com/aws-samples/sample-agentic-patch-management" target="_blank" rel="noopener noreferrer">Help</a>
    </header>
  );
}
