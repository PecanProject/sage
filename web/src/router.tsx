import {
  createRootRoute,
  createRoute,
  createRouter,
  notFound,
  Outlet,
  redirect,
} from "@tanstack/react-router"
import { $api, fetchClient, queryClient } from "@/api/client"
import { SettingsDialog } from "@/components/settings-dialog"
import { SmallScreen } from "@/components/small-screen"
import { TopBar } from "@/components/top-bar"
import { Button } from "@/components/ui/button"
import { fitsApp, useMedia } from "@/lib/media"
import { loadPaper } from "@/lib/paper-view"
import { loadPdfViewer } from "@/lib/pdf-chunk"
import { zoomFrom } from "@/lib/pdf-zoom"
import { HomePage } from "@/pages/home"
import { NotFound, PaperPage } from "@/pages/paper"

function Root() {
  const fits = useMedia(fitsApp)
  if (!fits) return <SmallScreen />
  return (
    <>
      <Outlet />
      <SettingsDialog />
    </>
  )
}

function ErrorPage({ error }: { error: unknown }) {
  return (
    <div className="min-h-svh">
      <TopBar title="Something broke" />
      <div className="mx-auto mt-24 max-w-md space-y-4 text-center">
        <p className="text-title">Something broke</p>
        <p className="text-caption text-muted-ink">
          {error instanceof Error ? error.message : String(error)}
        </p>
        <Button variant="outline" onClick={() => window.location.reload()}>
          Reload
        </Button>
      </div>
    </div>
  )
}

const loadSettings = () =>
  queryClient.ensureQueryData($api.queryOptions("get", "/api/settings")).catch(() => null)

const rootRoute = createRootRoute({
  component: Root,
  errorComponent: ErrorPage,
  notFoundComponent: () => <NotFound message="There's no page at this address" />,
  loader: loadSettings,
})

const loadPapers = () => void queryClient.prefetchQuery($api.queryOptions("get", "/api/papers"))

const homeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/",
  component: HomePage,
  loader: loadPapers,
})
const settingsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/settings",
  component: HomePage,
  loader: loadPapers,
})

export type PaperSearch = {
  mode?: "check" | "data" | "read"
  item?: string
  /** Check mode lists every item, not only those still to check. */
  filter?: "all"
  /** Data mode's tab: the record type its table shows. */
  type?: string
  run?: string
  details?: 1
  extract?: 1
  continue?: string
  stop?: 1
  settings?: 1
  /** The PDF's zoom, a multiple of Fit page. Undefined at Fit page. */
  zoom?: number
}

const one = (v: unknown): 1 | undefined =>
  v === 1 || v === "1" || v === true || v === "true" ? 1 : undefined
const text = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined)

export const paperRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/papers/$paperId",
  component: PaperPage,
  loaderDeps: ({ search }) => ({ run: search.run }),
  loader: ({ params, deps }) => {
    void loadPdfViewer()
    return loadPaper(params.paperId, deps.run)
  },
  validateSearch: (s: Record<string, unknown>): PaperSearch => ({
    mode: s.mode === "check" || s.mode === "data" || s.mode === "read" ? s.mode : undefined,
    item: text(s.item),
    filter: s.filter === "all" ? "all" : undefined,
    type: text(s.type),
    run: text(s.run),
    details: one(s.details),
    extract: one(s.extract),
    continue: text(s.continue),
    stop: one(s.stop),
    zoom: zoomFrom(s.zoom),
    settings: one(s.settings),
  }),
  // Another paper is a fresh screen: the viewer, zoom anchor and drafts all belong to the one before.
  remountDeps: ({ params }) => params.paperId,
})

/** Old links and CLI hand-offs: an extraction opens on its paper, with the details drawer. */
const runRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/runs/$runId",
  beforeLoad: async ({ params }) => {
    const { data } = await fetchClient.GET("/api/runs/{run_id}", {
      params: { path: { run_id: params.runId } },
    })
    if (!data) throw notFound()
    throw redirect({
      to: "/papers/$paperId",
      params: { paperId: data.paper_id },
      search: { run: data.id, details: 1 },
      replace: true,
    })
  },
  notFoundComponent: () => <NotFound message="There's no such extraction" />,
})

const routeTree = rootRoute.addChildren([homeRoute, settingsRoute, paperRoute, runRoute])

export const router = createRouter({ routeTree, defaultPreload: "intent" })

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router
  }
}
