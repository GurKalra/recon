import { Toaster } from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { AiProvider } from "./lib/ai";
import { AuthProvider } from "./lib/auth";
import "./index.css";

const client = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 30_000, retry: 1, refetchOnWindowFocus: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <AuthProvider>
          <AiProvider>
            <App />
          </AiProvider>
        </AuthProvider>
        <Toaster
          position="bottom-right"
          duration={4000}
          style={{ fontFamily: "inherit" }}
          toastOptions={{
            unstyled: true,
            classNames: {
              toast:
                "flex w-[22rem] items-start border border-rule-strong border-l-4 border-l-accent bg-paper px-4 py-3 text-[0.97rem] text-ink",
              success: "!border-l-ok",
              error: "!border-l-risk",
              icon: "hidden",
            },
          }}
        />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
