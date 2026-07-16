interface CardProps {
  children: React.ReactNode;
  className?: string;
  onClick?: () => void;
  hover?: boolean;
}

export function Card({ children, className = "", onClick, hover }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={`bg-surface-card rounded-xl border border-edge relative ${hover ? "hover:border-edge-strong hover:bg-surface-raised hover:-translate-y-0.5 transition-all duration-200 cursor-pointer" : ""} ${className}`}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === "Enter") onClick(); } : undefined}
    >
      {children}
    </div>
  );
}

export function CardHeader({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`px-4 pt-4 pb-2 ${className}`}>{children}</div>;
}

export function CardTitle({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <h3 className={`text-sm font-semibold text-fg ${className}`}>{children}</h3>;
}

export function CardDescription({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-fg-muted mt-0.5">{children}</p>;
}

export function CardContent({ children, className = "" }: { children: React.ReactNode; className?: string }) {
  return <div className={`px-4 pb-4 ${className}`}>{children}</div>;
}
