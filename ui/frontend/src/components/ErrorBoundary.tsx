import React from "react";

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends React.Component<React.PropsWithChildren, State> {
  constructor(props: React.PropsWithChildren) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center h-full min-h-[240px] gap-3 p-6 text-fg">
          <h2 className="text-base font-semibold">This part of Patchy stopped working</h2>
          <p className="text-fg-muted text-sm">{this.state.error?.message ?? "An unexpected error occurred."}</p>
          <button
            onClick={() => window.location.reload()}
            className="btn"
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
