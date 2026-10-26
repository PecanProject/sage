import { QueryClientProvider } from "@tanstack/react-query"
import { RouterProvider } from "@tanstack/react-router"
import { ThemeProvider } from "next-themes"
import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import "./index.css"
import { queryClient } from "@/api/client"
import { Toaster } from "@/components/ui/sonner"
import { TooltipProvider } from "@/components/ui/tooltip"
import { router } from "@/router"

const root = document.getElementById("root")
if (root) {
  createRoot(root).render(
    <StrictMode>
      {/* next-themes' own anti-flash script can't run in a client-only app; index.html carries one that can. A data
          block type keeps React from warning about the inert copy. */}
      <ThemeProvider
        attribute="class"
        defaultTheme="system"
        disableTransitionOnChange
        scriptProps={{ type: "application/json" }}
      >
        <QueryClientProvider client={queryClient}>
          <TooltipProvider>
            <RouterProvider router={router} />
            <Toaster position="bottom-center" />
          </TooltipProvider>
        </QueryClientProvider>
      </ThemeProvider>
    </StrictMode>,
  )
}
