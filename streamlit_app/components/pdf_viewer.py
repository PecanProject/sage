
from __future__ import annotations
import base64
import html
import json

import streamlit as st
import os

_ASSETS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "assets")
_PDFJS_DIR = os.path.join(_ASSETS_DIR, "pdfjs")


@st.cache_data(show_spinner=False)
def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


@st.cache_data(show_spinner=False)
def _read_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def render_pdf_page(
    pdf_bytes: bytes,
    page_number: int,
    polygon: list[list[float]] | None,
    block_id: str | None = None,
    height: int = 760,
    key: str = "pdf_viewer",
):
    if not pdf_bytes:
        st.warning(
            "No source PDF available for this paper yet. Once the original "
            "PDF is connected, it will render here automatically."
        )
        return

    pdf_lib_js = _read_text(os.path.join(_PDFJS_DIR, "pdf.min.js"))
    worker_b64 = _read_b64(os.path.join(_PDFJS_DIR, "pdf.worker.min.js"))
    pdf_b64 = base64.b64encode(pdf_bytes).decode("ascii")
    polygon_json = json.dumps(polygon or [])
    page_number = max(1, int(page_number or 1))
    safe_block_id = html.escape(block_id or "")
    toolbar_h = 34
    holder_h = height - toolbar_h
    zoom_btn_style = (
        "font-family:'SFMono-Regular',Consolas,monospace;font-size:0.75rem;"
        "color:#444;background:#fff;border:1px solid #d7dbe0;border-radius:4px;"
        "padding:1px 8px;cursor:pointer;line-height:1.5;"
    )

    component_html = f"""
    <div id="pdf-wrap-{key}" style="width:100%;">
      <div id="pdf-toolbar-{key}" style="
            display:flex;align-items:center;gap:10px;height:{toolbar_h}px;
            font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
            font-size:0.85rem;color:#444;">
        <span>Page <span id="pdf-page-num-{key}">{page_number}</span>
          of <span id="pdf-page-count-{key}">…</span></span>
        <span id="pdf-block-badge-{key}" style="
              font-family:'SFMono-Regular',Consolas,monospace;font-size:0.75rem;
              color:#0b5fff;background:#e8f0fe;border:1px solid #cfe0fd;
              border-radius:4px;padding:1px 7px;">{safe_block_id}</span>
        <span style="flex:1 1 auto;"></span>
        <button id="pdf-zoom-out-{key}" type="button" title="Zoom out" style="{zoom_btn_style}">−</button>
        <span id="pdf-zoom-label-{key}" style="min-width:40px;text-align:center;font-variant-numeric:tabular-nums;">100%</span>
        <button id="pdf-zoom-in-{key}" type="button" title="Zoom in" style="{zoom_btn_style}">+</button>
        <button id="pdf-zoom-reset-{key}" type="button" title="Reset to fit width" style="{zoom_btn_style}">Fit</button>
        <span id="pdf-status-{key}" style="color:#9a6700;">Loading…</span>
      </div>
      <div id="pdf-canvas-holder-{key}" style="
            position:relative;border:1px solid #d7dbe0;border-radius:8px;
            overflow-y:auto;overflow-x:auto;background:#f5f6f8;height:{holder_h}px;">
      </div>
    </div>

    <script>{pdf_lib_js}</script>
    <script>
    (function() {{
      function b64ToUint8Array(b64) {{
        const raw = atob(b64);
        const arr = new Uint8Array(raw.length);
        for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
        return arr;
      }}

      const workerBytes = b64ToUint8Array("{worker_b64}");
      const workerBlobUrl = URL.createObjectURL(
        new Blob([workerBytes], {{type: "text/javascript"}})
      );
      window.pdfjsLib.GlobalWorkerOptions.workerSrc = workerBlobUrl;

      const pdfBytes = b64ToUint8Array("{pdf_b64}");
      const targetPage = {page_number};
      const targetPolygon = {polygon_json};
      const statusEl = document.getElementById("pdf-status-{key}");
      const pageCountEl = document.getElementById("pdf-page-count-{key}");
      const pageNumEl = document.getElementById("pdf-page-num-{key}");
      const holder = document.getElementById("pdf-canvas-holder-{key}");
      const zoomLabelEl = document.getElementById("pdf-zoom-label-{key}");
      const zoomInBtn = document.getElementById("pdf-zoom-in-{key}");
      const zoomOutBtn = document.getElementById("pdf-zoom-out-{key}");
      const zoomResetBtn = document.getElementById("pdf-zoom-reset-{key}");

      // Without this, the canvas pixel buffer is sized to CSS pixels (1x)
      // and the browser then stretches it to fill the same CSS box on any
      // HiDPI/Retina display (devicePixelRatio > 1) -- that upscale is
      // exactly what reads as "hazy". Rendering at scale*dpr and keeping
      // the CSS-visible size at the lower, fit-to-panel scale (via
      // canvas.style.width/height) gives a crisp image at the same layout
      // size on every display.
      const dpr = window.devicePixelRatio || 1;
      let zoomFactor = 1;
      let pdfDoc = null;
      let containerWidth = 0;
      const MIN_ZOOM = 0.5;
      const MAX_ZOOM = 3;

      function drawOverlay(svg, viewportWidth, viewportHeight, polygon) {{
        svg.setAttribute("viewBox", `0 0 ${{viewportWidth}} ${{viewportHeight}}`);
        svg.innerHTML = "";
        if (!polygon || polygon.length < 3) return;
        const pts = polygon.map(function(p) {{
          return (p[0] * viewportWidth) + "," + (p[1] * viewportHeight);
        }}).join(" ");
        const ns = "http://www.w3.org/2000/svg";
        const poly = document.createElementNS(ns, "polygon");
        poly.setAttribute("points", pts);
        poly.setAttribute("fill", "rgba(255, 214, 0, 0.35)");
        poly.setAttribute("stroke", "rgba(220, 140, 0, 0.95)");
        poly.setAttribute("stroke-width", "2.5");
        svg.appendChild(poly);
      }}

      // Tracks each rendered page's own top offset (px, within the holder)
      // so we can scroll to any page/highlight after the whole stack has
      // rendered, and so a plain scroll listener can report which page is
      // currently in view without re-querying layout on every frame.
      const pageOffsets = [];

      function currentTopPage() {{
        const scrollMid = holder.scrollTop + (holder.clientHeight / 3);
        let current = 1;
        for (let i = 0; i < pageOffsets.length; i++) {{
          if (pageOffsets[i] <= scrollMid) current = i + 1;
        }}
        return current;
      }}

      function updateVisiblePageLabel() {{
        pageNumEl.textContent = currentTopPage();
      }}
      holder.addEventListener("scroll", updateVisiblePageLabel, {{passive: true}});

      function renderAllPages(scrollToPage) {{
        holder.innerHTML = "";
        pageOffsets.length = 0;
        statusEl.textContent = "Loading…";
        statusEl.style.color = "#9a6700";

        let chain = Promise.resolve();
        let targetScrollTop = null;

        for (let pageIndex = 1; pageIndex <= pdfDoc.numPages; pageIndex++) {{
          (function(pageNum) {{
            chain = chain.then(function() {{
              return pdfDoc.getPage(pageNum).then(function(page) {{
                const unscaled = page.getViewport({{scale: 1}});
                const scale = (containerWidth / unscaled.width) * zoomFactor;
                const viewport = page.getViewport({{scale: scale}});
                const renderViewport = page.getViewport({{scale: scale * dpr}});

                const pageWrap = document.createElement("div");
                pageWrap.style.position = "relative";
                pageWrap.style.margin = "0 auto 10px auto";
                pageWrap.style.width = viewport.width + "px";

                const label = document.createElement("div");
                label.textContent = "p. " + pageNum;
                label.style.cssText =
                  "position:absolute;top:4px;left:4px;z-index:2;" +
                  "background:rgba(255,255,255,0.85);color:#57606a;" +
                  "font-size:0.68rem;padding:1px 5px;border-radius:3px;" +
                  "font-family:'SFMono-Regular',Consolas,monospace;";
                pageWrap.appendChild(label);

                const canvas = document.createElement("canvas");
                canvas.width = renderViewport.width;
                canvas.height = renderViewport.height;
                canvas.style.width = viewport.width + "px";
                canvas.style.height = viewport.height + "px";
                canvas.style.display = "block";
                pageWrap.appendChild(canvas);

                const ns = "http://www.w3.org/2000/svg";
                const svg = document.createElementNS(ns, "svg");
                svg.setAttribute("width", viewport.width);
                svg.setAttribute("height", viewport.height);
                svg.style.cssText = "position:absolute;top:0;left:0;pointer-events:none;";
                pageWrap.appendChild(svg);

                holder.appendChild(pageWrap);
                pageOffsets[pageNum - 1] = pageWrap.offsetTop;

                const ctx = canvas.getContext("2d");
                return page.render({{canvasContext: ctx, viewport: renderViewport}}).promise.then(function() {{
                  if (pageNum === targetPage) {{
                    drawOverlay(svg, viewport.width, viewport.height, targetPolygon);
                  }}
                  if (pageNum === scrollToPage) {{
                    let yOffset = 0;
                    if (pageNum === targetPage && targetPolygon && targetPolygon.length >= 3) {{
                      yOffset = (targetPolygon[0][1] * viewport.height) - 80;
                    }}
                    targetScrollTop = Math.max(0, pageWrap.offsetTop + yOffset);
                  }}
                }});
              }});
            }});
          }})(pageIndex);
        }}

        return chain.then(function() {{
          statusEl.textContent = "";
          if (targetScrollTop !== null) {{
            holder.scrollTop = targetScrollTop;
          }}
          updateVisiblePageLabel();
          zoomLabelEl.textContent = Math.round(zoomFactor * 100) + "%";
        }}).catch(function(err) {{
          statusEl.textContent = "Failed to render PDF: " + err.message;
          statusEl.style.color = "#b42318";
          console.error(err);
        }});
      }}

      function setZoom(newZoom) {{
        zoomFactor = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, newZoom));
        // Preserve whatever page the reviewer is currently looking at
        // rather than jumping back to the original highlight -- they may
        // have scrolled elsewhere to compare against it.
        const stayOnPage = currentTopPage();
        renderAllPages(stayOnPage);
      }}

      zoomInBtn.addEventListener("click", function() {{ setZoom(zoomFactor + 0.25); }});
      zoomOutBtn.addEventListener("click", function() {{ setZoom(zoomFactor - 0.25); }});
      zoomResetBtn.addEventListener("click", function() {{ setZoom(1); }});

      window.pdfjsLib.getDocument({{data: pdfBytes}}).promise.then(function(pdf) {{
        pdfDoc = pdf;
        pageCountEl.textContent = pdf.numPages;
        containerWidth = Math.max(200, holder.clientWidth - 4);
        renderAllPages(targetPage);
      }}).catch(function(err) {{
        statusEl.textContent = "Failed to load PDF: " + err.message;
        statusEl.style.color = "#b42318";
        console.error(err);
      }});
    }})();
    </script>
    """

    st.iframe(component_html, height=height + 10)
