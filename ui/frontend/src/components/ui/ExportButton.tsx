import { useState, useRef, useEffect } from "react";

interface Props {
  data: Record<string, unknown>[];
  filename: string;
  columns?: { key: string; label: string }[];
}

function toCSV(data: Record<string, unknown>[], columns?: { key: string; label: string }[]): string {
  if (data.length === 0) return "";

  const cols = columns ?? Object.keys(data[0]).map((key) => ({ key, label: key }));
  const headers = cols.map((c) => escapeCsvValue(c.label));
  const rows = data.map((row) =>
    cols.map((c) => escapeCsvValue(String(row[c.key] ?? ""))).join(",")
  );

  return [headers.join(","), ...rows].join("\n");
}

function escapeCsvValue(value: string): string {
  if (value.includes(",") || value.includes('"') || value.includes("\n")) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function triggerDownload(content: string, filename: string, mimeType: string) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export default function ExportButton({ data, filename, columns }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) {
      document.addEventListener("mousedown", handleClickOutside);
      return () => document.removeEventListener("mousedown", handleClickOutside);
    }
  }, [open]);

  function exportJSON() {
    const content = JSON.stringify(data, null, 2);
    triggerDownload(content, `${filename}.json`, "application/json");
    setOpen(false);
  }

  function exportCSV() {
    const content = toCSV(data, columns);
    triggerDownload(content, `${filename}.csv`, "text/csv");
    setOpen(false);
  }

  if (!data || data.length === 0) return null;

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="text-[11px] font-medium text-fg-muted hover:text-fg-secondary bg-surface-card border border-edge rounded-lg px-2 py-1 transition flex items-center gap-1"
        aria-label="Export data"
      >
        <svg
          width="14"
          height="14"
          viewBox="0 0 14 14"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
          className="shrink-0"
        >
          <path
            d="M7 1.75v7.5M7 9.25L4.375 6.625M7 9.25l2.625-2.625M2.625 12.25h8.75"
            stroke="currentColor"
            strokeWidth="1.25"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        Export
      </button>
      {open && (
        <div className="absolute right-0 top-full mt-1 bg-surface-raised border border-edge rounded-lg shadow-xl z-50 py-1">
          <div
            onClick={exportCSV}
            className="px-3 py-1.5 text-xs text-fg-muted hover:text-fg hover:bg-hover cursor-pointer"
          >
            CSV
          </div>
          <div
            onClick={exportJSON}
            className="px-3 py-1.5 text-xs text-fg-muted hover:text-fg hover:bg-hover cursor-pointer"
          >
            JSON
          </div>
        </div>
      )}
    </div>
  );
}
