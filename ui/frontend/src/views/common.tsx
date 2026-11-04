import { useEffect, useState, type ReactNode } from "react";
import type { Tone } from "../lib/view";

/** A status with its colour dot; the words stay in the text colour. */
export function State({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`state ${tone ?? ""}`}><span>{children}</span></span>;
}

export function Tag({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`tag ${tone ?? ""}`}>{children}</span>;
}

export function Mono({ children }: { children: ReactNode }) {
  return <span className="mono">{children}</span>;
}

/** A short ID with a button that copies the full one. With onOpen, the ID is a link (for a run, to its report). */
export function CopyId({ id, n = 8, label, onOpen, openTitle }:
    { id: string; n?: number; label: string; onOpen?: () => void; openTitle?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <>
      {onOpen ? <button type="button" className="link mono" title={openTitle ?? id} onClick={onOpen}>{id.slice(0, n)}</button>
        : <span className="mono" title={id}>{id.slice(0, n)}</span>}
      <button type="button" className="copy" aria-label={copied ? "Copied" : `Copy ${label}`} title={copied ? "Copied" : `Copy ${id}`}
        onClick={() => navigator.clipboard?.writeText(id).then(() => { setCopied(true); setTimeout(() => setCopied(false), 1500); }, () => undefined)}>
        {copied ? "✓" : "⧉"}
      </button>
    </>
  );
}

/** Re-render on an interval, for "14 min left" and "running for 4 min". */
export function useTick(ms: number, enabled = true): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!enabled) return;
    setNow(Date.now());  // a clock that was off is stale when it turns on
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms, enabled]);
  return now;
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="empty-state">
      <div className="t">{title}</div>
      {children && <div className="mt-1">{children}</div>}
    </div>
  );
}

export function Callout({ tone = "watch", title, children, onDismiss }:
    { tone?: "watch" | "risk"; title: string; children?: ReactNode; onDismiss?: () => void }) {
  return (
    <div className={`callout ${tone === "risk" ? "risk" : ""}`} role={tone === "risk" ? "alert" : "status"}>
      <div className="grow"><b>{title}</b>{children && <div className="d mt-0.5">{children}</div>}</div>
      {onDismiss && <button type="button" onClick={onDismiss} aria-label="Dismiss">{"✕"}</button>}
    </div>
  );
}
