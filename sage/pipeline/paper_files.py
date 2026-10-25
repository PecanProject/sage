"""A paper's folder: adding the paper, and the files in it. SAGE writes paper.md and the page, table and figure images
for agents and people to read. Agents write tables/<anchor>.csv and pages/p<N>.md, and the effective document is the
parsed paper with those read in: each CSV grounded in the text layer as the paper's table, and each page transcription
as the blocks of a page with no text layer."""

import csv
import hashlib
import io
import json
import re
import unicodedata
from pathlib import Path

from pydantic import BaseModel

from sage.agents.paper_view import full_view
from sage.ir.files import FileIssue
from sage.parse import pdf
from sage.parse.document import Block, Box, Document, Page, Table
from sage.parse.grounding import GROUNDER_VERSION, Grid, attach_notes, ground, mark_table_blocks
from sage.settings import Settings
from sage.store import files
from sage.store.models import PaperInfo
from sage.store.paths import Home, RunPaths

REGION_PAD = 4.0
"""Points added around a table or figure region before cropping, so edge rules and minus signs stay in the image."""
CSV_META = re.compile(r"^#\s*(header_rows|notes)\s*:\s?(.*)$")
SCAN_BLOCK = re.compile(r"^\[p(\d+)\.b(\d+)\]\s?(.*)$")
PAGE_MD = re.compile(r"^p(\d+)\.md$")

_LETTERS = str.maketrans({"ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "ł": "l",
                          "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "Þ": "TH", "\u0131": "i"})  # fmt: skip
"""Latin letters NFKD doesn't split into a base letter and an accent."""


class _Grounded(BaseModel):
    key: str
    """What the table was grounded from: the CSV, the grounding code and settings, and the detected table."""
    table: Table


class _Rendered(BaseModel):
    key: str
    """What the images were rendered from: the PDF, the parser that found the regions, and the resolution."""


def add(home: Home, settings: Settings, filename: str, data: bytes) -> tuple[PaperInfo, bool]:
    """Add a PDF to the library, and say whether it is new: the same bytes again return the paper already there. A
    PDF that can't be read raises PdfError."""
    sha = hashlib.sha256(data).hexdigest()
    if (known := _paper_with(home, sha)) is not None:
        return known, False
    base = _slug(re.sub(r"(\.pdf)+$", "", filename, flags=re.IGNORECASE))
    pages = pdf.page_count(data)
    document = pdf.parse(data, paper_id=base, sha256=sha)
    title = pdf.title(data, document)
    # Parsing runs unlocked. The second look and the writes are one step, so the same file uploaded twice at once is
    # added once.
    with files.exclusive(home.add_lock):
        if (known := _paper_with(home, sha)) is not None:
            return known, False
        paper_id = _claim_id(home, base)
        files.write_bytes(home.pdf(paper_id), data)
        files.write_model(home.document(paper_id), document.model_copy(update={"paper_id": paper_id}))
        info = PaperInfo(id=paper_id, filename=filename, sha256=sha, pages=pages, title=title)
        files.write_model(home.paper_json(paper_id), info)
    # The page images, table crops and paper.md an agent reads. They are SAGE's to rewrite, so a failure here leaves a
    # paper the next start of SAGE renders again.
    materialize(home, paper_id, data, settings)
    return info, True


def library(home: Home) -> list[PaperInfo]:
    """Every paper, newest first. A folder with no paper.json is left out. One that won't parse raises
    UnreadableError."""
    infos = [files.read_model_or_none(home.paper_json(d.name), PaperInfo) for d in files.child_dirs(home.papers)]
    return sorted((i for i in infos if i is not None), key=lambda i: i.added_at, reverse=True)


def _paper_with(home: Home, sha: str) -> PaperInfo | None:
    return next((p for p in library(home) if p.sha256 == sha), None)


def _slug(stem: str) -> str:
    """Accents dropped rather than the letters ("évil" is "evil", "Straße" is "Strasse"); anything else that isn't an
    id character becomes "-". Short enough that `_claim_id`'s "-N" keeps it a valid id."""
    split = unicodedata.normalize("NFKD", stem.translate(_LETTERS))
    bare = "".join(c for c in split if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9._-]+", "-", bare)[:100].strip("-._") or "paper"


def _claim_id(home: Home, base: str) -> str:
    """Creates the paper's folder, so an upload running alongside with the same file name gets the next id."""
    candidate, n = base, 1
    while not files.make_new_dir(home.paper(candidate)):
        n += 1
        candidate = f"{base}-{n}"
    return candidate


def refresh(home: Home, settings: Settings) -> None:
    """Write the images and paper.md of every paper where they are missing or were rendered from something else. Cheap
    when there is nothing to do: one read of each document and a look for each image. A paper whose files can't be
    written is left as it is."""
    for d in files.child_dirs(home.papers):
        pid = d.name
        try:
            doc = files.read_model_or_none(home.document(pid), Document)
            if doc is not None and _stale(home, pid, doc, settings):
                materialize(home, pid, files.read_bytes(home.pdf(pid)), settings)
        except (OSError, files.UnreadableError, pdf.PdfError):
            continue


def keep_run_documents(home: Home, paper_id: str, doc: Document, *, skip: str | None = None) -> None:
    """Give every run of the paper without its own copy of the document `doc`, the one it was extracted against,
    before the paper's document.json is replaced, so its evidence still resolves where it was found."""
    for run_dir in files.child_dirs(home.runs(paper_id)):
        own = RunPaths(run_dir).document
        if run_dir.name != skip and not files.exists(own):
            files.write_model(own, doc)


def materialize(home: Home, paper_id: str, data: bytes, settings: Settings) -> None:
    """Write the paper's images and paper.md: pages/p<N>.png for every page and tables/<anchor>.png for every detected
    table at `Settings.page_render_dpi`, turned upright, figures/<anchor>.png for each
    figure whose drawings can be found, and paper.md from the effective document. An image already there is kept
    unless it was rendered from another PDF, parser or resolution. Call it when a paper is added and after its
    document is parsed again; `refresh` calls it for any paper missing a file."""
    doc = files.read_model(home.document(paper_id), Document)
    key = _render_key(doc, settings)
    old = files.read_disposable(home.rendered(paper_id), _Rendered)
    fresh = old is None or old.key != key
    scale = settings.page_render_dpi / 72
    pages = {p.number: p for p in doc.pages}

    def needed(path: Path) -> bool:
        return fresh or not files.exists(path)

    pdf.render_pages(
        data,
        [n for n in pages if needed(home.page_png(paper_id, n))],
        scale=scale,
        write=lambda n, png: files.write_bytes(home.page_png(paper_id, n), png),
    )
    crops: list[tuple[Path, Table | pdf.Figure]] = [(home.table_png(paper_id, t.anchor), t) for t in doc.tables]
    crops += [(home.figure_png(paper_id, f.anchor), f) for f in pdf.figures(data, doc)]
    for path, c in crops:
        if needed(path):
            box = _padded(c.region, pages[c.page])
            files.write_bytes(path, pdf.render(data, c.page, bbox=box, scale=scale, text_rotation=c.rotation))
    # The agent's folders are there from the start, so writing its first record needs no mkdir.
    files.make_dirs(home.records_dir(paper_id) / "observations", home.tables_dir(paper_id), home.pages_dir(paper_id))
    refresh_paper_md(home, paper_id, settings)
    files.write_model(home.rendered(paper_id), _Rendered(key=key))


def refresh_paper_md(home: Home, paper_id: str, settings: Settings, *, doc: Document | None = None) -> bool:
    """Write paper.md from the effective document, or from `doc` when the caller already has it, only when its text
    changes. True when it wrote."""
    if doc is None:
        doc, _ = effective_document(home, paper_id, settings)
    info = files.read_model_or_none(home.paper_json(paper_id), PaperInfo)
    text = full_view(doc, title=info.title if info is not None else None) + "\n"
    return files.write_text_if_changed(home.paper_md(paper_id), text)


def effective_document(home: Home, paper_id: str, settings: Settings) -> tuple[Document, list[FileIssue]]:
    """The parsed paper as the agent's files make it, and the problems found reading those files. Every detected table
    starts untranscribed; a table with a well-formed tables/<anchor>.csv is that grid, grounded in the text layer
    (kept in `.sage/tables/` until the CSV, the grounding code or its settings change). A page
    with no text layer gets the blocks of its pages/p<N>.md, each the size of the page. Blocks covered by a grounded
    table and each table's footnote blocks are worked out again over the whole."""
    base = files.read_model(home.document(paper_id), Document)
    issues: list[FileIssue] = []
    tables = _csv_tables(home, paper_id, base, settings, issues)
    blocks = sorted([*base.blocks, *_scan_blocks(home, paper_id, base, issues)], key=lambda b: b.page)
    staged = base.replace(blocks=blocks, tables=tables)
    marked = staged.replace(blocks=mark_table_blocks(staged))
    return marked.replace(tables=attach_notes(marked)), issues


def _csv_tables(home: Home, paper_id: str, base: Document, settings: Settings, issues: list[FileIssue]) -> list[Table]:
    stubs = {t.anchor: _untranscribed(t) for t in base.tables}
    grids: dict[str, tuple[Grid, str]] = {}
    for path in files.child_files(home.tables_dir(paper_id), ".csv"):
        rel = f"tables/{path.name}"
        anchor = path.name.removesuffix(".csv")
        if anchor not in stubs:
            known = ", ".join(stubs) or "none"
            issues.append(FileIssue(path=rel, line=1, message=f"no table {anchor} in this paper; tables: {known}"))
            continue
        try:
            text = files.read_text(path)
        except files.UnreadableError as e:
            issues.append(FileIssue(path=rel, line=1, message=str(e)))
            continue
        grid = _read_csv(text, rel, issues)
        if grid is not None:
            grids[anchor] = (grid, _ground_key(text, stubs[anchor], settings))
    grounded: dict[str, Table] = {}
    misses: list[str] = []
    for anchor, (_, key) in grids.items():
        cached = files.read_disposable(home.table_cache(paper_id, anchor), _Grounded)
        if cached is not None and cached.key == key:
            grounded[anchor] = cached.table
        else:
            misses.append(anchor)
    if misses:
        data = files.read_bytes(home.pdf(paper_id))
        chars = pdf.pages_chars(data, [stubs[a].page for a in misses])
        pages = {p.number: p for p in base.pages}
        captions = {b.anchor: b.bbox for b in base.blocks}
        for anchor in misses:
            t, (grid, key) = stubs[anchor], grids[anchor]
            page = pages[t.page]
            done = ground(
                t,
                grid,
                chars.get(t.page, []),
                caption=captions.get(t.caption_anchor),
                page_size=(page.width, page.height),
                fuzzy_threshold=settings.grounding_fuzzy_threshold,
                min_share=settings.table_min_grounded,
            )
            files.write_model(home.table_cache(paper_id, anchor), _Grounded(key=key, table=done))
            grounded[anchor] = done
    return [grounded.get(t.anchor, stubs[t.anchor]) for t in base.tables]


def _untranscribed(t: Table) -> Table:
    """The detected table alone, with no cells."""
    return t.model_copy(
        update={
            "rows": [],
            "header_rows": 1,
            "notes": None,
            "notes_anchors": [],
            "status": "pending",
            "grounded_share": 0.0,
        }
    )


def _read_csv(text: str, rel: str, issues: list[FileIssue]) -> Grid | None:
    lines = text.splitlines(keepends=True)
    found: list[FileIssue] = []
    header_rows: int | None = None
    header_line = 1
    notes: list[str] = []
    n = 0
    while n < len(lines) and lines[n].lstrip().startswith("#"):
        m = CSV_META.match(lines[n].strip())
        if m is None:
            found.append(FileIssue(path=rel, line=n + 1, message="a # line is `# header_rows: N` or `# notes: …`"))
        elif m[1] == "notes":
            notes.append(m[2].strip())
        elif m[2].strip().isdigit():
            header_rows, header_line = int(m[2].strip()), n + 1
        else:
            found.append(FileIssue(path=rel, line=n + 1, message="header_rows is a whole number, 0 or more"))
        n += 1
    if header_rows is None and not found:
        message = "the first line is `# header_rows: N`, the number of header rows above the body"
        found.append(FileIssue(path=rel, line=1, message=message))
    if found or header_rows is None:
        issues += found
        return None
    reader = csv.reader(io.StringIO("".join(lines[n:])))
    rows: list[list[str]] = []
    try:
        rows.extend(row for row in reader if row)
    except csv.Error as e:
        issues.append(FileIssue(path=rel, line=n + reader.line_num, message=f"not valid CSV: {e}"))
        return None
    if header_rows >= len(rows):
        message = f"header_rows is {header_rows}, but the table has {len(rows)} rows; it needs a body row below them"
        issues.append(FileIssue(path=rel, line=header_line, message=message))
        return None
    return Grid(rows=rows, header_rows=header_rows, notes=" ".join(n for n in notes if n) or None)


def _scan_blocks(home: Home, paper_id: str, base: Document, issues: list[FileIssue]) -> list[Block]:
    pages = {p.number: p for p in base.pages}
    out: list[Block] = []
    for path in files.child_files(home.pages_dir(paper_id), ".md"):
        rel = f"pages/{path.name}"
        m = PAGE_MD.match(path.name)
        page = pages.get(int(m[1])) if m is not None else None
        if m is None:
            issues.append(FileIssue(path=rel, line=1, message="a page transcription is named after its page: p<N>.md"))
        elif page is None:
            message = f"the paper has no page {m[1]}; its pages are 1 to {len(pages)}"
            issues.append(FileIssue(path=rel, line=1, message=message))
        elif page.has_text:
            message = f"page {page.number} has a text layer, which SAGE reads; transcribe only pages without one"
            issues.append(FileIssue(path=rel, line=1, message=message))
        else:
            try:
                out += _page_blocks(files.read_text(path), page, rel, issues)
            except files.UnreadableError as e:
                issues.append(FileIssue(path=rel, line=1, message=str(e)))
    return out


def _page_blocks(text: str, page: Page, rel: str, issues: list[FileIssue]) -> list[Block]:
    n = page.number
    started: dict[str, int] = {}
    parts: dict[str, list[str]] = {}
    current: str | None = None
    for i, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        m = SCAN_BLOCK.match(line.strip())
        if m is None and line.lstrip().startswith(f"[p{n}"):
            issues.append(FileIssue(path=rel, line=i, message=f"a block starts with its anchor: [p{n}.b1] …"))
            current = None
        elif m is None:
            if current is None:
                message = f"text before the first block; start each block with its anchor: [p{n}.b1] …"
                issues.append(FileIssue(path=rel, line=i, message=message))
            else:
                parts[current].append(line.strip())
        elif int(m[1]) != n:
            message = f"this file is page {n}, so its blocks are p{n}.b<K>, not p{m[1]}.b{m[2]}"
            issues.append(FileIssue(path=rel, line=i, message=message))
            current = None
        elif (anchor := f"p{n}.b{int(m[2])}") in started:
            issues.append(FileIssue(path=rel, line=i, message=f"{anchor} already starts on line {started[anchor]}"))
            current = None
        else:
            started[anchor], parts[anchor], current = i, [m[3].strip()] if m[3].strip() else [], anchor
    blocks: list[Block] = []
    for anchor, words in parts.items():
        if not words:
            issues.append(FileIssue(path=rel, line=started[anchor], message=f"{anchor} has no text"))
            continue
        blocks.append(Block(anchor=anchor, page=n, bbox=(0.0, 0.0, page.width, page.height), text=" ".join(words)))
    return blocks


def _stale(home: Home, paper_id: str, doc: Document, settings: Settings) -> bool:
    """Whether a file `materialize` writes is missing, or was rendered from something else."""
    old = files.read_disposable(home.rendered(paper_id), _Rendered)
    if old is None or old.key != _render_key(doc, settings) or not files.exists(home.paper_md(paper_id)):
        return True
    paths = [home.page_png(paper_id, p.number) for p in doc.pages]
    paths += [home.table_png(paper_id, t.anchor) for t in doc.tables]
    return not all(files.exists(p) for p in paths)


def _render_key(doc: Document, settings: Settings) -> str:
    return f"{doc.sha256}:{doc.parser_version}:{settings.page_render_dpi}"


def _ground_key(text: str, table: Table, settings: Settings) -> str:
    parts = [
        hashlib.sha256(text.encode()).hexdigest(),
        GROUNDER_VERSION,
        table.model_dump(mode="json"),
        settings.grounding_fuzzy_threshold,
        settings.table_min_grounded,
    ]
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _padded(region: Box, page: Page) -> Box:
    x0, y0, x1, y1 = region
    return (max(0.0, x0 - REGION_PAD), max(0.0, y0 - REGION_PAD), min(page.width, x1 + REGION_PAD),
            min(page.height, y1 + REGION_PAD))  # fmt: skip
