"""
Canonical Document Map: the paper's structure, derived once and deterministically from the artifacts
document preparation already wrote -- `content.md` (the literal block texts the grounding validator checks against)
and `provenance.json` (block type, page, bbox, section path, table-cell row/column). Nothing is re-OCR'd, nothing is
inferred by a model, Marker is not touched.

What it adds on top of those files:
  - regions (front matter, abstract, methods, results, references, ...), found positionally from the headings in
    reading order, since Marker's `section_path` is not reliable for this;
  - table <-> caption links for the caption shapes Marker produces (a Caption block, a "#### Table 3" SectionHeader, a
    bare "*Table 7*" label, a caption inside a Picture block, a figure caption), plus the continuation chains;
  - cross references ("Table 2", "Fig. 3") from prose to the table/figure they name;
  - the section each block sits in (nearest preceding heading, including short run-in headings).

Everything is keyed by the content.md block anchor, so every downstream evidence item stays citable and grounded.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pipeline import content_reader

# Checked in this order against a heading's text; the first match decides. A heading that matches none (a topical
# subsection such as "Light-use efficiency") keeps the region it sits in.
_REGION_HEADING_RULES: tuple[tuple[str, re.Pattern], ...] = (
    ("references", re.compile(r"^\W*(references|literature cited|bibliography)\b", re.I)),
    ("acknowledgements", re.compile(r"^\W*(acknowledg|funding|author contributions)", re.I)),
    ("supplementary", re.compile(r"^\W*(supporting information|supplementary)", re.I)),
    ("conclusions", re.compile(r"^\W*(summary and conclusions?|conclusions?)\b", re.I)),
    ("abstract", re.compile(r"^\W*(abstract|summary)\b", re.I)),
    ("introduction", re.compile(r"^\W*(introduction|background)\b", re.I)),
    ("methods", re.compile(r"^\W*(materials?\b|methods?\b|materials? and methods|experimental (design|procedures?))", re.I)),
    ("results", re.compile(r"^\W*results?\b", re.I)),
    ("discussion", re.compile(r"^\W*discussion\b", re.I)),
)
_FURNITURE = {"PageHeader", "PageFooter"}
_PROSE_TYPES = {"Text", "ListItem", "Footnote", "Caption", "Equation"}
_TABLE_LABEL_RE = re.compile(r"^\W*(?:table|tab\.)\s*(\d+|[IVX]+)\b", re.I)
_FIGURE_LABEL_RE = re.compile(r"^\W*(?:fig(?:ure)?s?\.?)\s*(\d+)\b", re.I)
_EMBEDDED_CAPTION_RE = re.compile(r"Caption:\s*(?:\*)?\s*((?:table|tab\.|fig(?:ure)?\.?)\s*\d+[^\]]*)", re.I)
_TABLE_REF_RE = re.compile(r"\b(?:Tables?|Tab\.)\s+(\d+)((?:\s*(?:,|and|&|–|-)\s*\d+)*)", re.I)
_FIGURE_REF_RE = re.compile(r"\b(?:Fig(?:ure)?s?\.?)\s*(\d+)((?:\s*(?:,|and|&|–|-)\s*\d+)*)", re.I)
_RUN_IN_HEADING_MAX_CHARS = 60
_CAPTION_LOOKBACK_BLOCKS = 8   # prose blocks can sit between a caption and its table


@dataclass(frozen=True)
class DocBlock:
    anchor: str
    order: int
    block_type: str
    text: str                       # the literal rendered block text (whitespace-collapsed)
    page: Optional[int]             # 1-indexed physical PDF page
    region: str
    section_title: Optional[str]    # nearest preceding heading (SectionHeader or short run-in heading)
    section_anchor: Optional[str]
    section_path: tuple[str, ...]   # Marker's own path, kept as-is
    bbox: Optional[tuple[float, ...]] = None
    is_heading: bool = False


@dataclass(frozen=True)
class TableInfo:
    label: Optional[str]                 # "Table 2" when a caption/label names it
    anchors: tuple[str, ...]             # the logical table: head block + page-split continuations
    caption_anchors: tuple[str, ...]     # caption / label block(s), empty when none is found
    caption_text: Optional[str]
    note_anchors: tuple[str, ...]        # the bounded notes after the table (pooling_evidence's own window rule)
    page: Optional[int]
    region: str


@dataclass(frozen=True)
class FigureInfo:
    label: str
    anchor: str
    caption_text: str


@dataclass(frozen=True)
class CrossRef:
    from_anchor: str
    kind: str                       # "table" | "figure"
    label: str                      # "Table 2"
    target_anchors: tuple[str, ...]  # resolved table/figure anchors, empty when the paper has no such label


@dataclass
class DocumentMap:
    paper_id: str
    document_hash: str
    blocks: list[DocBlock]
    tables: list[TableInfo]
    figures: list[FigureInfo]
    cross_refs: list[CrossRef]
    _by_anchor: dict[str, DocBlock] = field(default_factory=dict, repr=False)
    _position: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self._by_anchor = {b.anchor: b for b in self.blocks}
        self._position = {b.anchor: i for i, b in enumerate(self.blocks)}

    # --- lookups ------------------------------------------------------------------------------------------------
    def block(self, anchor: str) -> Optional[DocBlock]:
        return self._by_anchor.get(content_reader._normalize_anchor(anchor)) if anchor else None

    def text(self, anchor: str) -> str:
        block = self.block(anchor)
        return block.text if block else ""

    def neighbors(self, anchor: str, before: int = 1, after: int = 1, prose_only: bool = True) -> list[DocBlock]:
        """The nearest `before`/`after` blocks around `anchor` in reading order (page furniture skipped)."""
        position = self._position.get(content_reader._normalize_anchor(anchor))
        if position is None:
            return []
        wanted = lambda b: b.block_type not in _FURNITURE and (not prose_only or b.block_type in _PROSE_TYPES)
        out: list[DocBlock] = []
        i, taken = position - 1, 0
        while i >= 0 and taken < before:
            if wanted(self.blocks[i]):
                out.insert(0, self.blocks[i])
                taken += 1
            i -= 1
        i, taken = position + 1, 0
        while i < len(self.blocks) and taken < after:
            if wanted(self.blocks[i]):
                out.append(self.blocks[i])
                taken += 1
            i += 1
        return out

    def region_blocks(self, *regions: str, prose_only: bool = True) -> list[DocBlock]:
        return [b for b in self.blocks if b.region in regions and (not prose_only or b.block_type in _PROSE_TYPES)]

    def table_containing(self, anchor: str) -> Optional[TableInfo]:
        """The logical table an anchor belongs to -- a Table block, a continuation, its caption or one of its cells."""
        normalized = content_reader._normalize_anchor(anchor)
        for table in self.tables:
            if normalized in table.anchors or normalized in table.caption_anchors:
                return table
        return None

    def references_to(self, table: TableInfo) -> list[CrossRef]:
        return [r for r in self.cross_refs if set(r.target_anchors) & set(table.anchors)]

    def refs_from(self, anchor: str) -> list[CrossRef]:
        normalized = content_reader._normalize_anchor(anchor)
        return [r for r in self.cross_refs if r.from_anchor == normalized]


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #

def _region_of_heading(text: str) -> Optional[str]:
    heading = text.lstrip("#* ").strip()
    for region, rule in _REGION_HEADING_RULES:
        if rule.match(heading):
            return region
    return None


def _is_run_in_heading(block_type: str, text: str) -> bool:
    """A short Text block that works as a heading ("Study site", "Soil sampling"): no sentence punctuation, starts
    with a capital letter, not a label, not a number."""
    if block_type != "Text" or not text or len(text) > _RUN_IN_HEADING_MAX_CHARS:
        return False
    if text.rstrip().endswith((".", ",", ";", ":")) or not text[0].isupper():
        return False
    return not (_TABLE_LABEL_RE.match(text) or _FIGURE_LABEL_RE.match(text))


def _bbox(entry: dict) -> Optional[tuple[float, ...]]:
    bbox = entry.get("bbox")
    return tuple(float(v) for v in bbox) if isinstance(bbox, list) and len(bbox) == 4 else None


def _table_label(text: str) -> Optional[str]:
    match = _TABLE_LABEL_RE.match(text)
    if match:
        return f"Table {match.group(1)}"
    embedded = _EMBEDDED_CAPTION_RE.search(text)
    if embedded:
        inner = _TABLE_LABEL_RE.match(embedded.group(1))
        return f"Table {inner.group(1)}" if inner else None
    return None


def _figure_label(text: str) -> Optional[str]:
    match = _FIGURE_LABEL_RE.match(text)
    if match:
        return f"Figure {match.group(1)}"
    embedded = _EMBEDDED_CAPTION_RE.search(text)
    if embedded:
        inner = _FIGURE_LABEL_RE.match(embedded.group(1))
        return f"Figure {inner.group(1)}" if inner else None
    return None


def _expand_refs(first: str, rest: str) -> list[str]:
    numbers = [first] + re.findall(r"\d+", rest or "")
    if re.search(r"[–-]", rest or "") and len(numbers) == 2 and int(numbers[1]) - int(numbers[0]) < 10:
        numbers = [str(n) for n in range(int(numbers[0]), int(numbers[1]) + 1)]
    return numbers


def build_document_map(paper_id: str, papers_root: Optional[Path] = None) -> DocumentMap:
    root = Path(papers_root) if papers_root is not None else _default_root()
    content_path = content_reader._content_md_path(paper_id, root)
    stat = content_path.stat()
    return _build_cached(paper_id, str(root), stat.st_mtime_ns, stat.st_size)


def _default_root() -> Path:
    from pipeline.validators import _papers_root  # resolved at call time, like every other reader

    return _papers_root()


@lru_cache(maxsize=32)
def _build_cached(paper_id: str, root: str, _mtime: int, _size: int) -> DocumentMap:
    papers_root = Path(root)
    content = content_reader._content_md_path(paper_id, papers_root).read_bytes()
    texts = content_reader._rendered_block_texts(paper_id, papers_root)
    provenance = content_reader._load_provenance(paper_id, papers_root) or {}
    ordered = [a for a in sorted(texts, key=content_reader._anchor_sort_key) if " ".join(texts[a].split())]

    blocks: list[DocBlock] = []
    region = "front_matter"
    section_title: Optional[str] = None
    section_anchor: Optional[str] = None
    for order, anchor in enumerate(ordered):
        entry = provenance.get(anchor) or {}
        text = " ".join(texts[anchor].split())
        block_type = entry.get("block_type") or ("SectionHeader" if text.startswith("#") else "Text")
        heading = block_type == "SectionHeader" or text.startswith("#")
        run_in = not heading and _is_run_in_heading(block_type, text)
        if heading or run_in:
            new_region = _region_of_heading(text)
            label_heading = bool(_TABLE_LABEL_RE.match(text.lstrip("#* ")) or _FIGURE_LABEL_RE.match(text.lstrip("#* ")))
            if new_region and not (run_in and new_region in ("abstract",)):
                region = new_region
            if not label_heading:
                section_title, section_anchor = text.lstrip("#* ").strip(), anchor
        blocks.append(DocBlock(
            anchor=anchor, order=order, block_type=block_type, text=text,
            page=content_reader.physical_page(entry.get("page_id")), region=region,
            section_title=section_title, section_anchor=section_anchor,
            section_path=tuple(entry.get("section_path") or ()), bbox=_bbox(entry), is_heading=heading or run_in,
        ))

    by_anchor = {b.anchor: b for b in blocks}
    position = {b.anchor: i for i, b in enumerate(blocks)}
    tables = _link_tables(paper_id, papers_root, blocks, by_anchor, position, provenance)
    figures = _figures(blocks)
    cross_refs = _cross_refs(blocks, tables, figures)
    return DocumentMap(
        paper_id=paper_id, document_hash=hashlib.sha256(content).hexdigest()[:16],
        blocks=blocks, tables=tables, figures=figures, cross_refs=cross_refs,
    )


def _link_tables(paper_id, papers_root, blocks, by_anchor, position, provenance) -> list[TableInfo]:
    from pipeline import pooling_evidence  # the same caption/note window rule the pooling detector uses

    continuation = content_reader.table_continuation_map(paper_id, papers_root)
    heads = [b for b in blocks if b.block_type == "Table" and b.anchor not in continuation]
    followers = {prev: cur for cur, prev in continuation.items()}
    out: list[TableInfo] = []
    for head in heads:
        chain = [head.anchor]
        while chain[-1] in followers:
            chain.append(followers[chain[-1]])
        caption_anchors: list[str] = []
        label: Optional[str] = None
        i, looked = position[head.anchor] - 1, 0
        while i >= 0 and looked < _CAPTION_LOOKBACK_BLOCKS:
            candidate = blocks[i]
            if candidate.block_type in _FURNITURE or candidate.block_type == "Footnote":
                i -= 1
                continue
            looked += 1
            if candidate.block_type == "Table":
                break
            found = _table_label(candidate.text)
            if found:
                label = found
                caption_anchors = [candidate.anchor]
                # A bare label ("#### Table 3", "*Table 7*") carries its caption text in the block(s) that follow it,
                # up to the table itself.
                if len(_TABLE_LABEL_RE.sub("", candidate.text.strip("#* "))) < 8:
                    caption_anchors += [b.anchor for b in blocks[i + 1: position[head.anchor]]
                                        if b.block_type in ("Text", "Caption")]
                break
            i -= 1
        caption_text = " ".join(by_anchor[a].text for a in caption_anchors) or None
        notes = tuple(a for pos, a in pooling_evidence.pooling_windows(paper_id, chain, papers_root) if pos != "own_caption")
        out.append(TableInfo(
            label=label, anchors=tuple(chain), caption_anchors=tuple(caption_anchors), caption_text=caption_text,
            note_anchors=notes, page=head.page, region=head.region,
        ))
    return out


def _figures(blocks: list[DocBlock]) -> list[FigureInfo]:
    figures: list[FigureInfo] = []
    seen: set[str] = set()
    for block in blocks:
        if block.block_type not in ("Caption", "Figure", "Picture", "SectionHeader", "Text"):
            continue
        label = _figure_label(block.text.lstrip("#* ")) if block.block_type != "Text" else None
        if block.block_type == "Text" and _FIGURE_LABEL_RE.match(block.text.lstrip("* ")) and block.text.startswith("*"):
            label = _figure_label(block.text.lstrip("* "))
        if label and label not in seen:
            seen.add(label)
            figures.append(FigureInfo(label=label, anchor=block.anchor, caption_text=block.text))
    return figures


def _cross_refs(blocks, tables, figures) -> list[CrossRef]:
    table_targets = {t.label.lower(): t.anchors for t in tables if t.label}
    figure_targets = {f.label.lower(): (f.anchor,) for f in figures}
    caption_anchors = {a for t in tables for a in t.caption_anchors}
    refs: list[CrossRef] = []
    for block in blocks:
        if block.block_type not in _PROSE_TYPES or block.anchor in caption_anchors or block.region == "references":
            continue
        for match in _TABLE_REF_RE.finditer(block.text):
            for number in _expand_refs(match.group(1), match.group(2)):
                label = f"Table {number}"
                refs.append(CrossRef(block.anchor, "table", label, tuple(table_targets.get(label.lower(), ()))))
        for match in _FIGURE_REF_RE.finditer(block.text):
            for number in _expand_refs(match.group(1), match.group(2)):
                label = f"Figure {number}"
                refs.append(CrossRef(block.anchor, "figure", label, tuple(figure_targets.get(label.lower(), ()))))
    return refs
