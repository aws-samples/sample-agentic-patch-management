import { useState, useCallback, useRef } from "react";

interface ToastItem {
  id: string;
  message: string;
  type: "success" | "error" | "info";
}

const ICONS: Record<string, string> = {
  success: "text-emerald-400",
  error: "text-red-400",
  info: "text-accent",
};

export function useToast() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const counterRef = useRef(0);

  const toast = useCallback((message: string, type: "success" | "error" | "info" = "info") => {
    const id = `toast-${++counterRef.current}`;
    setToasts((prev) => [...prev, { id, message, type }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  }, []);

  const dismiss = useCallback((id: string) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  return { toasts, toast, dismiss };
}

export function ToastContainer({ toasts, onDismiss }: { toasts: ToastItem[]; onDismiss: (id: string) => void }) {
  if (toasts.length === 0) return null;

  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div
          key={t.id}
          className="flex items-center gap-2.5 px-4 py-3 bg-surface-raised border border-edge-strong rounded-xl shadow-xl animate-slide-up cursor-pointer"
          onClick={() => onDismiss(t.id)}
        >
          <svg className={`w-4 h-4 shrink-0 ${ICONS[t.type]}`} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
            {t.type === "success" && <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />}
            {t.type === "error" && <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />}
            {t.type === "info" && <path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" />}
          </svg>
          <span className="text-sm text-fg">{t.message}</span>
        </div>
      ))}
    </div>
  );
}
