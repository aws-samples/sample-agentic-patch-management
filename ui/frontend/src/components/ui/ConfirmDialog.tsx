import { useEffect, useRef } from "react";
import { ExclamationTriangleIcon } from "@heroicons/react/24/outline";

interface Props {
  open: boolean;
  title: string;
  description: string;
  confirmLabel?: string;
  cancelLabel?: string;
  variant?: "default" | "destructive";
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open, title, description,
  confirmLabel = "Confirm", cancelLabel = "Cancel",
  variant = "default",
  onConfirm, onCancel,
}: Props) {
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  // Callers pass a new onCancel each render; keep the latest without re-running the
  // effect, which would move focus back to Cancel on every tick.
  const cancelLatest = useRef(onCancel);
  cancelLatest.current = onCancel;

  useEffect(() => {
    if (!open) return;
    // Focus goes back to whatever opened the dialog when it closes.
    const opener = document.activeElement as HTMLElement | null;
    // Cancel has focus, so Enter on an unread dialog doesn't confirm a change.
    cancelRef.current?.focus();
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") cancelLatest.current();
      // Focus trap: Tab cycles between cancel and confirm buttons
      if (e.key === "Tab") {
        const focusable = dialogRef.current?.querySelectorAll<HTMLElement>("button");
        if (!focusable || focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    };
    document.addEventListener("keydown", handleKey);
    return () => {
      document.removeEventListener("keydown", handleKey);
      if (opener?.isConnected) opener.focus();
    };
  }, [open]);

  if (!open) return null;

  const confirmStyles = variant === "destructive"
    ? "btn stop"
    : "btn primary";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onCancel}>
      <div className="absolute inset-0" style={{ background: "var(--overlay)" }} />
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-desc"
        className="relative bg-surface-card border border-edge-strong rounded-lg p-6 max-w-md w-full mx-4 shadow-2xl animate-fade-in"
        onClick={(e) => e.stopPropagation()}
      >
        {variant === "destructive" && (
          <div className="w-10 h-10 rounded-lg flex items-center justify-center mb-4" style={{ background: "var(--risk-soft)" }}>
            <ExclamationTriangleIcon className="w-5 h-5 text-risk" />
          </div>
        )}
        <h3 id="confirm-dialog-title" className="text-lg font-semibold text-fg">{title}</h3>
        <p id="confirm-dialog-desc" className="text-sm text-fg-muted mt-2 leading-relaxed">{description}</p>
        <div className="flex justify-end gap-2 mt-6">
          <button
            ref={cancelRef}
            onClick={onCancel}
            className="btn"
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            className={confirmStyles}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
