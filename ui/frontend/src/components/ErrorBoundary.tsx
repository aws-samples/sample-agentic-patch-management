import { Component } from "react";
import type { ReactNode, ErrorInfo } from "react";
import Box from "@cloudscape-design/components/box";
import Button from "@cloudscape-design/components/button";
import Alert from "@cloudscape-design/components/alert";

interface Props {
  children: ReactNode;
  fallbackLabel?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[ErrorBoundary] ${this.props.fallbackLabel ?? "Component"} crashed:`, error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return (
        <Box padding="l">
          <Alert
            type="error"
            header={`${this.props.fallbackLabel ?? "Component"} encountered an error`}
            action={
              <Button onClick={() => this.setState({ hasError: false, error: null })}>
                Try again
              </Button>
            }
          >
            {this.state.error?.message ?? "An unexpected error occurred."}
          </Alert>
        </Box>
      );
    }
    return this.props.children;
  }
}
