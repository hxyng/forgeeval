import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { App } from "./App";
import { AuthProvider } from "./auth";
import { makeQueryClient } from "./queryClient";
import "./styles.css";

const ROUTER_FUTURE = { v7_startTransition: true, v7_relativeSplatPath: true };

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={makeQueryClient()}>
      <BrowserRouter future={ROUTER_FUTURE}>
        <AuthProvider>
          <App />
        </AuthProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
