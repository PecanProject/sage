"""
styles.py
================
Small shared style helpers. Keeps the app looking like a serious
scientific data-curation tool rather than a generic chatbot.
"""

import streamlit as st

STATUS_COLORS = {
    "READY": ("#1a7f37", "#e6f4ea"),
    "NEEDS REVIEW": ("#9a6700", "#fff5e0"),
    "UNRESOLVED": ("#b42318", "#fdeceb"),
    "ACCEPTED": ("#0b5fff", "#e8f0fe"),
    "REJECTED": ("#6e7781", "#f0f1f3"),
    "EXTRACTED": ("#3f6212", "#f0f6e8"),
    "EDITED": ("#0b5fff", "#e8f0fe"),
    "IN REVIEW": ("#9a6700", "#fff5e0"),
    "READY FOR EXPORT": ("#1a7f37", "#e6f4ea"),
    # Real record/field statuses (pipeline.results_store / api_client).
    "ready": ("#1a7f37", "#e6f4ea"),
    "unresolved": ("#b42318", "#fdeceb"),
    "blocked": ("#9a6700", "#fff5e0"),
    "error": ("#b42318", "#fdeceb"),
    "not_extracted": ("#6e7781", "#f0f1f3"),
    # Real review states (corrections_store actions -> effective review_status).
    "pending": ("#6e7781", "#f0f1f3"),
    "approved": ("#1a7f37", "#e6f4ea"),
    "corrected": ("#0b5fff", "#e8f0fe"),
    "relinked": ("#0b5fff", "#e8f0fe"),
    "relocated": ("#0b5fff", "#e8f0fe"),
    "confirmed_unresolved": ("#9a6700", "#fff5e0"),
    "reference": ("#6e7781", "#f0f1f3"),
}

# Compact 3-letter status labels (spec: "do not use large pill-shaped
# badges" -- these replace the old full-word EXTRACTED/UNRESOLVED text).
# Keyed by every raw status/provenance_label/review_status string this UI
# ever passes to compact_badge() -- see api_client.py's normalization.
_STATUS_ABBR = {
    "EXTRACTED": "EXT", "INFERRED": "INF", "UNRESOLVED": "UNR",
    "ready": "EXT", "unresolved": "UNR", "blocked": "BLK", "error": "ERR",
    "not_extracted": "—",
    "pending": "···", "approved": "APR", "corrected": "COR",
    "relinked": "LNK", "relocated": "LOC", "confirmed_unresolved": "UNR",
    "reference": "REF",
}


def inject_base_css():
    st.markdown(
        """
        <style>
        /* max-width: 1100px previously capped the ENTIRE app regardless of
           st.set_page_config(layout="wide") -- the primary cause of the
           review pane not using available desktop width. Removed. */
        .block-container {padding-top: 0.6rem; max-width: 100%;}
        /* Plain, grounded masthead -- deliberately close to a BETYdb-style
           institutional data-tool header (light gray bar, no gradients, no
           shadow), not a marketing/product landing look.

           No negative margins here on purpose -- an earlier version used
           `margin: -0.6rem -1rem ...` to stretch edge-to-edge past
           .block-container's own padding, which pulled the bar up
           underneath Streamlit's own fixed top toolbar and hid it
           entirely (confirmed the actual cause of it never appearing,
           not a stale-server issue -- st.html()'s structural presence in
           the page was verified all along, but that says nothing about
           whether negative-margin positioning left it visually hidden).
           A plain in-flow block is deliberately less "edge-to-edge" but
           guaranteed visible regardless of Streamlit's own toolbar height,
           which isn't a value this app controls or can safely assume. */
        .sage-masthead {
            background: #f6f8fa;
            border: 1px solid #d7dbe0;
            border-radius: 6px;
            margin: 0 0 1rem 0;
            padding: 0.7rem 1.25rem;
            display: flex;
            align-items: baseline;
            gap: 0.6rem;
        }
        .sage-masthead .title {
            font-size: 1.15rem;
            font-weight: 700;
            color: #1b1f23;
        }
        .sage-masthead .subtitle {
            font-size: 0.85rem;
            color: #57606a;
        }
        h1, h2, h3 { font-weight: 650; letter-spacing: -0.01em; }
        .subtle { color: #6e7781; font-size: 0.9rem; }
        .anchor-tag {
            display: inline-block;
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.78rem;
            color: #444;
            background: #eef1f4;
            border: 1px solid #d7dbe0;
            border-radius: 4px;
            padding: 1px 6px;
            margin: 0 2px;
        }
        .field-card {
            border: 1px solid #e3e6ea;
            border-radius: 8px;
            padding: 0.75rem 1rem;
            margin-bottom: 0.6rem;
            background: #ffffff;
        }
        .field-label {
            font-size: 0.78rem;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            color: #6e7781;
            margin-bottom: 2px;
        }
        .field-value {
            font-size: 1rem;
            color: #1b1f23;
            margin-bottom: 6px;
        }
        .record-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        hr {margin: 0.6rem 0;}

        /* Compact review workspace: rows, not cards. */
        .entity-heading {
            font-size: 0.82rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            color: #444;
            margin: 1.1rem 0 0.15rem 0;
            border-bottom: 1px solid #e3e6ea;
            padding-bottom: 2px;
        }
        /* Record row (components/record_row.py): the primary collapsed
           unit in the review pane now -- ONE compact line per record, an
           entity-specific summary (not raw JSON), a compact status label,
           and a minimal expand control. Grid tracks use minmax(0, ...)
           deliberately: flex/grid items default to min-width:auto, which
           refuses to shrink below the content's own natural width -- that
           lets a long summary overflow into the status column instead of
           wrapping/truncating otherwise. */
        .record-row-grid {
            display: grid;
            grid-template-columns: 92px minmax(0, 1fr) auto;
            column-gap: 10px;
            align-items: center;
            padding: 0.32rem 0.1rem;
            border-bottom: 1px solid #f0f1f3;
        }
        .record-row-type {
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.72rem;
            color: #8a95a1;
            text-transform: uppercase;
            letter-spacing: 0.03em;
        }
        .record-row-summary {
            font-size: 0.92rem;
            color: #1b1f23;
            line-height: 1.35;
            min-width: 0;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .record-row-summary.expanded {
            white-space: normal;
            overflow-wrap: break-word;
        }
        /* Minimal expand/collapse control: a plain small caret, not a
           full-width Streamlit button -- targeted via the row's own
           st-key so only THIS button shrinks, not every button on the
           page (spec section 12: compact expand control). */
        div[class*="st-key-rectoggle_"] button {
            padding: 0 4px !important;
            min-height: 1.6rem !important;
            font-size: 0.85rem !important;
            border: none !important;
            background: transparent !important;
            box-shadow: none !important;
            color: #6e7781 !important;
        }
        div[class*="st-key-rectoggle_"] button:hover {
            color: #1b1f23 !important;
            background: #eef1f4 !important;
        }

        /* Field detail (components/field_row.py) -- shown in full for
           every field once its parent record is expanded. */
        .field-detail-row {
            display: grid;
            grid-template-columns: 150px minmax(0, 1fr) auto;
            column-gap: 10px;
            align-items: start;
            padding: 0.25rem 0.1rem 0.1rem 0.1rem;
        }
        .field-detail-name {
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.78rem;
            color: #57606a;
            line-height: 1.3;
            padding-top: 0.1rem;
            overflow-wrap: normal;
            word-break: normal;
        }
        .field-detail-value {
            font-size: 0.88rem;
            color: #1b1f23;
            line-height: 1.35;
            min-width: 0;
            overflow-wrap: break-word;
            word-break: break-word;
        }
        .field-detail-status {
            display: flex;
            align-items: center;
            gap: 6px;
            padding-top: 0.1rem;
            white-space: nowrap;
        }
        .field-detail-conf {
            font-size: 0.7rem;
            color: #8a95a1;
        }
        .evidence-excerpt {
            font-size: 0.83rem;
            font-style: italic;
            color: #3a3f45;
            background: #fbfbfa;
            border-left: 2px solid #d7dbe0;
            padding: 0.2rem 0.5rem;
            margin: 0.15rem 0 0.1rem 0;
            line-height: 1.35;
        }
        .overview-line {
            font-family: "SFMono-Regular", Consolas, monospace;
            font-size: 0.92rem; color: #333; padding: 0.3rem 0;
        }
        /* Two-pane review workspace panes (components/workspace.py): both
           sides are st.container(height=N, key=...) -- Streamlit's own
           documented fixed-height/auto-scroll container mechanism (same
           technique already used for the library's scrollable lists), so
           each pane gets its own independent scrollbar with no CSS scroll
           hacking required. This block only adds visual polish. */
        div[class*="st-key-pdf_pane"], div[class*="st-key-data_pane"] {
            border-radius: 8px;
        }

        /* Library page: Stored Papers file-list rows. Streamlit assigns
           `st-key-<key>` as a real CSS class to a keyed st.container (this
           is documented Streamlit behavior, not a hack) -- every stored-
           paper row uses a key of the form "stored_row_<safe_paper_id>", so
           one wildcard attribute selector styles all of them without
           generating per-row CSS. Actions stay at opacity:0 (NOT
           display:none) so they still occupy their column -- the row's
           horizontal alignment never shifts on hover, only visibility
           changes. */
        div[class*="st-key-stored_row_"] {
            border-bottom: 1px solid #eef1f4;
        }
        div[class*="st-key-stored_row_"]:hover {
            background: #f6f8fa;
        }
        div[class*="st-key-stored_row_"] div[data-testid="stButton"],
        div[class*="st-key-stored_row_"] div[data-testid="stLinkButton"] {
            opacity: 0;
            transition: opacity 0.12s ease;
        }
        div[class*="st-key-stored_row_"]:hover div[data-testid="stButton"],
        div[class*="st-key-stored_row_"]:hover div[data-testid="stLinkButton"] {
            opacity: 1;
        }
        div[class*="st-key-stored_row_"] button,
        div[class*="st-key-stored_row_"] div[data-testid="stLinkButton"] a {
            padding: 0.15rem 0.5rem;
            font-size: 0.8rem;
        }
        .stored-row-name {
            font-size: 0.92rem;
            color: #1b1f23;
            padding: 0.35rem 0.2rem;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .stored-row-status {
            color: #6e7781;
            font-size: 0.76rem;
            margin-left: 0.6rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def masthead(subtitle: str = ""):
    """Plain institutional-style header bar (see .sage-masthead's own
    comment) -- one call at the top of app.py, not per-page. Uses
    st.markdown(unsafe_allow_html=True), the SAME mechanism every other
    piece of HTML in this app already renders through and is confirmed
    visible with (row names, status labels, entity headings) -- st.html()
    was untested territory in this codebase and is not used anywhere
    else, so switching to the proven mechanism removes one more variable
    while tracking down why this bar wasn't appearing."""
    subtitle_html = f'<span class="subtitle">{subtitle}</span>' if subtitle else ""
    st.markdown(
        f'<div class="sage-masthead"><span class="title">Sage</span>{subtitle_html}</div>',
        unsafe_allow_html=True,
    )


def status_badge(status: str) -> str:
    fg, bg = STATUS_COLORS.get(status, ("#333", "#eee"))
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 9px;'
        f'border-radius:999px;font-size:0.76rem;font-weight:600;'
        f'letter-spacing:0.03em;">{status}</span>'
    )


def compact_status(status: str) -> str:
    """The 3-4 char label (EXT/UNR/BLK/ERR/REF/...) for any raw status,
    review_status, or provenance_label string this UI encounters -- falls
    back to the first 3 letters uppercased for anything not in the table
    (never raises on an unrecognized status)."""
    if not status:
        return "?"
    return _STATUS_ABBR.get(status, status[:3].upper())


def compact_badge(status: str) -> str:
    """A small, non-pill status label -- monospace text with a subtle
    color tint, deliberately NOT the rounded-pill style status_badge()
    uses (spec: compact status representation should occupy only the
    space necessary to communicate state)."""
    fg, bg = STATUS_COLORS.get(status, ("#57606a", "#f0f1f3"))
    label = compact_status(status)
    return (
        f'<span style="background:{bg};color:{fg};padding:1px 5px;'
        f'border-radius:3px;font-family:\'SFMono-Regular\',Consolas,monospace;'
        f'font-size:0.72rem;font-weight:700;letter-spacing:0.02em;">{label}</span>'
    )


def anchor_tag(anchor: str) -> str:
    return f'<span class="anchor-tag">⟦{anchor}⟧</span>'