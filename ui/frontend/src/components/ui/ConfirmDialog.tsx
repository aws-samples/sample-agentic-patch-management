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
  const confirmRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    confirmRef.current?.focus();
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
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
    return () => document.removeEventListener("keydown", handleKey);
  }, [open, onCancel]);

  if (!open) return null;

  const confirmStyles = variant === "destructive"
    ? "bg-red-500/20 text-red-400 hover:bg-red-500/30 ring-1 ring-red-500/30"
    : "bg-accent text-surface-base hover:bg-teal-300";

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center" onClick={onCancel}>
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" />
      <div
        ref={dialogRef}
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-desc"
        className="relative bg-surface-card border border-edge-strong rounded-2xl p-6 max-w-md w-full mx-4 shadow-2xl animate-fade-in"
        onClick={(e) => e.stopPropagation()}
      >
        {variant === "destructive" && (
          <div className="w-10 h-10 rounded-xl bg-red-500/10 border border-red-500/20 flex items-center justify-center mb-4">
            <ExclamationTriangleIcon className="w-5 h-5 text-red-400" />
          </div>
        )}
        <h3 id="confirm-dialog-title" className="text-lg font-semibold text-fg">{title}</h3>
        <p id="confirm-dialog-desc" className="text-sm text-fg-muted mt-2 leading-relaxed">{description}</p>
        <div className="flex justify-end gap-2 mt-6">
          <button
            ref={cancelRef}
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium text-fg-muted bg-hover border border-edge rounded-xl hover:bg-hover transition"
          >
            {cancelLabel}
          </button>
          <button
            ref={confirmRef}
            onClick={onConfirm}
            className={`px-4 py-2 text-sm font-medium rounded-xl transition ${confirmStyles}`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
