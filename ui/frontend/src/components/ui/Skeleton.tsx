export function Skeleton({ className = "" }: { className?: string }) {
  return <div className={`rounded-lg bg-hover animate-shimmer ${className}`} />;
}

export function TableSkeleton({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-8 w-32 rounded-lg" />
      </div>
      <div className="rounded-lg border border-edge overflow-hidden">
        <div className="bg-surface-raised px-3 py-2.5 flex gap-4">
          {Array.from({ length: cols }).map((_, i) => (
            <Skeleton key={i} className="h-3 flex-1" />
          ))}
        </div>
        {Array.from({ length: rows }).map((_, r) => (
          <div key={r} className="px-3 py-3 flex gap-4 border-t border-edge">
            {Array.from({ length: cols }).map((_, c) => (
              <Skeleton key={c} className={`h-3.5 flex-1 ${c === 0 ? "max-w-[120px]" : ""}`} />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

export function CardsSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div className="space-y-3">
      <Skeleton className="h-5 w-32" />
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {Array.from({ length: count }).map((_, i) => (
          <div key={i} className="bg-surface-card rounded-xl border border-edge p-4 space-y-3">
            <div className="flex justify-between">
              <Skeleton className="h-4 w-16" />
              <Skeleton className="h-5 w-20 rounded-full" />
            </div>
            <div className="space-y-1.5">
              <Skeleton className="h-2.5 w-full" />
              <Skeleton className="h-1.5 w-full rounded-full" />
            </div>
            <div className="flex gap-1.5">
              <Skeleton className="h-5 w-20 rounded-full" />
              <Skeleton className="h-5 w-16 rounded-full" />
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

export function KpiSkeleton() {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
      {Array.from({ length: 6 }).map((_, i) => (
        <div key={i} className="bg-surface-card rounded-xl border border-edge p-3.5 space-y-2">
          <div className="flex items-center gap-1.5">
            <Skeleton className="h-4 w-4 rounded" />
            <Skeleton className="h-2.5 w-16" />
          </div>
          <Skeleton className="h-7 w-12" />
          <Skeleton className="h-2.5 w-24" />
        </div>
      ))}
    </div>
  );
}
