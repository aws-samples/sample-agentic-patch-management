import { useState, useEffect, useRef, useMemo } from "react";

const ACTIONS = [
  // Discover
  { id: "vulns-critical", label: "Show CRITICAL vulnerabilities", group: "Discover" },
  { id: "vulns-env", label: "Show vulnerabilities in prod", group: "Discover" },
  { id: "fleet-overview", label: "Show instances in prod", group: "Discover" },
  { id: "compliance-status", label: "Show patch compliance status for dev", group: "Discover" },
  // Plan
  { id: "plan-cve", label: "Create a patch plan for CVE-2026-XXXXX", group: "Plan" },
  // Patch
  { id: "patch-cve", label: "Patch CVE-2026-XXXXX in dev", group: "Patch" },
  { id: "patch-env", label: "Patch all prod", group: "Patch" },
  { id: "patch-severity", label: "Patch HIGH severity in staging", group: "Patch" },
  { id: "patch-account", label: "Patch dev in account 123412341234", group: "Patch" },
  { id: "patch-instance", label: "Patch i-0123456789abcdef0 in dev", group: "Patch" },
  // Preview
  { id: "preview-env", label: "Preview patches in prod", group: "Preview" },
  { id: "dry-run-dev", label: "Dry-run scan on dev for account 1231231231", group: "Preview" },
  // Compliance
  { id: "compliance-reports", label: "Show compliance reports", group: "Compliance" },
  { id: "sla-breaches", label: "Show SLA breaches this week", group: "Compliance" },
  // Verify & Rollback (kept last — these run after a patch operation)
  { id: "check-status", label: "Check status", group: "Verify & Rollback" },
  { id: "verify-health", label: "Verify health", group: "Verify & Rollback" },
  { id: "rollback-env", label: "Rollback dev patches", group: "Verify & Rollback" },
];

interface Props {
  open: boolean;
  onClose: () => void;
  onSend: (text: string) => void;
}

export function CommandPalette({ open, onClose, onSend }: Props) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setQuery("");
      setSelectedIndex(0);
      setTimeout(() => inputRef.current?.focus(), 50);
    }
  }, [open]);

  const filtered = useMemo(() => {
    if (!query.trim()) return ACTIONS;
    const q = query.toLowerCase();
    return ACTIONS.filter((a) => a.label.toLowerCase().includes(q) || a.group.toLowerCase().includes(q));
  }, [query]);

  const grouped = useMemo(() => {
    const groups: Record<string, typeof ACTIONS> = {};
    for (const item of filtered) {
      if (!groups[item.group]) groups[item.group] = [];
      groups[item.group].push(item);
    }
    return groups;
  }, [filtered]);

  const handleSelect = (label: string) => {
    onSend(label);
    onClose();
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { onClose(); return; }
    if (e.key === "ArrowDown") { e.preventDefault(); setSelectedIndex((i) => Math.min(i + 1, filtered.length - 1)); }
    if (e.key === "ArrowUp") { e.preventDefault(); setSelectedIndex((i) => Math.max(i - 1, 0)); }
    if (e.key === "Enter") {
      e.preventDefault();
      if (filtered[selectedIndex]) {
        handleSelect(filtered[selectedIndex].label);
      } else if (query.trim()) {
        handleSelect(query.trim());
      }
    }
  };

  if (!open) return null;

  let flatIndex = 0;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[18vh]" onClick={onClose}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        className="relative bg-surface-card border border-edge-strong rounded-2xl w-full max-w-lg mx-4 shadow-2xl animate-fade-in overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Search */}
        <div className="flex items-center gap-3 px-4 py-3.5 border-b border-edge">
          <svg className="w-5 h-5 text-fg-muted shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" />
          </svg>
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => { setQuery(e.target.value); setSelectedIndex(0); }}
            onKeyDown={handleKeyDown}
            placeholder="Search actions or type a question..."
            className="flex-1 bg-transparent text-sm text-fg placeholder:text-fg-faint outline-none"
          />
          <kbd className="text-[10px] text-fg-faint bg-hover px-1.5 py-0.5 rounded border border-edge font-mono">ESC</kbd>
        </div>

        {/* Results */}
        <div className="max-h-80 overflow-y-auto py-1.5 custom-scrollbar" role="listbox" aria-label="Available actions">
          {Object.entries(grouped).map(([group, items]) => (
            <div key={group}>
              <div className="px-4 py-1.5 text-[10px] font-semibold text-fg-faint uppercase tracking-wider">{group}</div>
              {items.map((item) => {
                const idx = flatIndex++;
                const isSelected = idx === selectedIndex;
                return (
                  <button
                    key={item.id}
                    role="option"
                    aria-selected={isSelected}
                    onClick={() => handleSelect(item.label)}
                    className={`w-full text-left px-4 py-2 text-sm flex items-center gap-3 transition ${
                      isSelected ? "bg-accent-muted text-accent-hover" : "text-fg-secondary hover:bg-input"
                    }`}
                  >
                    <svg className={`w-4 h-4 shrink-0 ${isSelected ? "text-accent" : "text-fg-faint"}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                    </svg>
                    {item.label}
                  </button>
                );
              })}
            </div>
          ))}
          {filtered.length === 0 && query.trim() && (
            <button
              onClick={() => handleSelect(query.trim())}
              className="w-full text-left px-4 py-3 text-sm text-fg-muted hover:bg-input transition"
            >
              Send: <span className="text-accent">&ldquo;{query.trim()}&rdquo;</span>
            </button>
          )}
        </div>

        {/* Footer */}
        <div className="px-4 py-2 border-t border-edge flex items-center gap-4 text-[10px] text-fg-faint">
          <span className="flex items-center gap-1"><kbd className="bg-hover px-1 py-0.5 rounded border border-edge font-mono">&uarr;&darr;</kbd> Navigate</span>
          <span className="flex items-center gap-1"><kbd className="bg-hover px-1 py-0.5 rounded border border-edge font-mono">Enter</kbd> Select</span>
        </div>
      </div>
    </div>
  );
}
