/** Lightweight CSS-only tooltip — no Radix dependency needed. */
import { useState, useRef, type ReactNode } from "react";

interface Props {
  content: string;
  children: ReactNode;
  side?: "top" | "bottom";
}

export function Tooltip({ content, children, side = "top" }: Props) {
  const [show, setShow] = useState(false);
  const timeout = useRef<ReturnType<typeof setTimeout>>();

  const enter = () => { timeout.current = setTimeout(() => setShow(true), 400); };
  const leave = () => { clearTimeout(timeout.current); setShow(false); };

  return (
    <span className="relative inline-flex" onMouseEnter={enter} onMouseLeave={leave} onFocus={enter} onBlur={leave}>
      {children}
      {show && (
        <span
          className={`absolute z-50 px-2.5 py-1.5 text-xs text-white bg-gray-900 rounded-lg shadow-lg whitespace-nowrap max-w-xs truncate pointer-events-none ${
            side === "top" ? "bottom-full mb-1.5 left-1/2 -translate-x-1/2" : "top-full mt-1.5 left-1/2 -translate-x-1/2"
          } animate-fade-in`}
          role="tooltip"
        >
          {content}
        </span>
      )}
    </span>
  );
}
