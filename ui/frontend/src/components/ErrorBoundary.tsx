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
        <div className="flex flex-col items-center justify-center h-screen gap-4 font-sans text-gray-700">
          <h2 className="text-xl font-semibold">Something went wrong</h2>
          <p className="text-gray-500">{this.state.error?.message ?? "An unexpected error occurred."}</p>
          <button
            onClick={() => window.location.reload()}
            className="px-5 py-2 border border-gray-300 rounded-lg bg-white hover:bg-gray-50 text-sm cursor-pointer transition"
          >
            Reload
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
