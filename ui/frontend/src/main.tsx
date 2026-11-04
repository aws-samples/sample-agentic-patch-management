import React from "react";
import ReactDOM from "react-dom/client";
import "@fontsource/ibm-plex-sans/400.css";
import "@fontsource/ibm-plex-sans/500.css";
import "@fontsource/ibm-plex-sans/600.css";
import "@fontsource/ibm-plex-mono/400.css";
import "./index.css";
import App from "./App";
import PopoutApp from "./PopoutApp";
import { ErrorBoundary } from "./components/ErrorBoundary";

// A change record or run opened in its own window (?pop=change|run).
const popout = new URLSearchParams(window.location.search).has("pop");

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      {popout ? <PopoutApp /> : <App />}
    </ErrorBoundary>
  </React.StrictMode>
);
