import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "@/app/App";
import { createQueryClient } from "@/app/queryClient";
import { wire } from "@/app/wiring";
import { useAuth } from "@/features/auth";
import { initTheme } from "@/features/theme";
import "@/app/styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("index.html has no #root");

const queryClient = createQueryClient();
initTheme();
wire(queryClient);
// Handles a return from the authorization server; the first request waits for it (`ready`).
void useAuth.getState().init();

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
