import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AuthProvider } from "./auth/AuthProvider";
import App from "./App";
import "./styles.css";

const container = document.getElementById("root");

if (!container) {
  throw new Error("Root element #root was not found.");
}

createRoot(container).render(
  <StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>,
);
