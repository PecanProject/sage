/** The PDF viewer's chunk. Importing it twice costs nothing, so the paper route calls this as it opens and the
 * canvas's lazy component reuses the same download. */
export const loadPdfViewer = () => import("@/components/pdf-viewer")
