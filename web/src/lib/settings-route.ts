import { useCanGoBack, useLocation, useNavigate, useRouter } from "@tanstack/react-router"

/** Settings is a dialog addressed by the URL: /settings over Home, `?settings=1` over a paper. */
export function useSettingsRoute() {
  const location = useLocation()
  const navigate = useNavigate()
  const router = useRouter()
  const canGoBack = useCanGoBack()
  const onHome = location.pathname === "/settings"
  const search = location.search as { settings?: unknown }
  return {
    isOpen: onHome || search.settings === 1,
    open: () =>
      onHome || location.pathname === "/"
        ? navigate({ to: "/settings" })
        : navigate({ to: ".", search: (s: object) => ({ ...s, settings: 1 as const }) }),
    close: () => {
      if (!onHome)
        return navigate({ to: ".", search: (s: object) => ({ ...s, settings: undefined }), replace: true })
      if (canGoBack) return router.history.back()
      return navigate({ to: "/" })
    },
  }
}
