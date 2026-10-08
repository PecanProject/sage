"""
Context Bundles (Stage 4): the evidence packet a model is handed for one extraction or enumeration task, assembled
deterministically from the Document Map and Evidence Index BEFORE the model is called.

One framework, one table of per-entity strategies (`STRATEGIES`) -- not one builder per entity. A strategy is a list
of CONCEPTS the task needs evidence for ("coordinates", "soil", "thinning", "instrument", ...), each with the phrases
and typed signals that find it and whether its absence can be proven by a whole-document pattern scan.

Evidence is gathered along a bounded expansion ladder, and every item records the level and the reasons it was taken:
    L0 direct      the candidate's own anchors (from enumeration)
    L1 local       reading-order neighbours of L0
    L2 region      concept hits in the strategy's preferred regions (methods, abstract, ...)
    L3 tables      tables containing / referenced by the evidence so far: caption, header, notes
    L4 cross-ref   prose that refers to those tables (table -> prose)
    L5 document    whole-document search, only for concepts still missing

The COVERAGE AUDIT then states, per concept, one of:
    FOUND                        in the packet (with the anchors and the level it was found at)
    NOT_RETRIEVED                present in the document but not in the packet (the budget cut it)
    NOT_FOUND_AFTER_FULL_SEARCH  the ladder searched the whole document and found nothing -- NOT proof of absence
    ABSENT_BY_PATTERN            a typed detector scanned every block and fired nowhere (coordinates, DOI, ...): the
                                 only state that licenses "the paper does not state it"
so that "the model did not report X" can be told apart from "X was never shown to the model" and from "X is not in
the paper". Every item is a verbatim, anchored block: the packet adds evidence, it never paraphrases it, and grounding
of whatever the model reports is unchanged.

A bundle is immutable and identified: `bundle_id` hashes the document hash, task, entity type, target, strategy
version and the selected anchors, so a record can say exactly which evidence it was extracted from.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

from pipeline.document_map import DocBlock, build_document_map
from pipeline.evidence_index import EvidenceIndex, Hit, build_evidence_index, significant_words

STRATEGY_VERSION = "1"

FOUND = "FOUND"
NOT_RETRIEVED = "NOT_RETRIEVED"
NOT_FOUND_AFTER_FULL_SEARCH = "NOT_FOUND_AFTER_FULL_SEARCH"
ABSENT_BY_PATTERN = "ABSENT_BY_PATTERN"

DEFAULT_MAX_CHARS = 7000
MAX_BLOCK_CHARS = 1500
TABLE_HEADER_LINES = 4
# Extraction is about one record: its best few hits per concept. An enumeration must see every instance, so it is
# bounded by the packet budget alone (hits are taken best-first until the budget is spent).
HITS_PER_CONCEPT = {"extraction": 3, "enumeration": None}


@dataclass(frozen=True)
class Concept:
    name: str
    phrases: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()
    expected: bool = True                 # part of the coverage audit
    pattern_detectable: bool = False      # absence may be concluded from a whole-document signal scan
    use_target: bool = False              # the task's target (and its aliases) are phrases of this concept


@dataclass(frozen=True)
class Strategy:
    entity_type: str
    concepts: tuple[Concept, ...]
    regions: tuple[str, ...] = ("methods", "abstract", "front_matter", "introduction", "results")
    include_tables: bool = False          # attach linked tables' caption/header/notes (L3)
    all_tables: bool = False              # enumeration only: every table's caption + header, not only linked ones
    tables_first: bool = False            # tables before prose in the budget (table-first Variables)
    max_chars: int = DEFAULT_MAX_CHARS
    enumeration_max_chars: Optional[int] = None   # a whole-paper enumeration may need a larger (still bounded) packet
    full_table_lines: int = 0             # a table this short is given in full, not just its header (definition tables)


_MANAGEMENT_EVENTS = (
    ("planting", ("planted", "planting", "sown", "sowing", "seeded", "transplanted", "transplanting", "established")),
    ("harvest", ("harvested", "harvest", "clipped", "clipping")),
    ("fertilization", ("fertilizer", "fertilized", "fertilization", "kg n ha", "applied")),
    ("irrigation", ("irrigated", "irrigation", "watered", "watering")),
    ("tillage", ("tilled", "tillage", "disked", "disced", "plowed", "spader", "subsoiled", "cultivated")),
    ("thinning", ("thinned", "thinning", "stems ha", "stem ha")),
    ("mowing_cutting", ("mowed", "mown", "mowing", "was cut", "were cut", "cut at ground level")),
    ("incorporation", ("incorporated", "incorporation")),
    ("amendment", ("compost", "manure", "lime", "amendment")),
    ("weeding", ("weeded", "weeding", "weed control", "herbicide")),
    ("residue_removal", ("removed", "removal", "residue")),
)

STRATEGIES: dict[str, Strategy] = {
    "Site": Strategy("Site", (
        Concept("site_name", ("study site", "experimental site", "was conducted", "were performed", "located", "farm",
                              "station", "research center", "forest", "stand", "near"), ("site_term",)),
        Concept("coordinates", signals=("coordinate",), pattern_detectable=True),
        Concept("elevation", signals=("elevation",), pattern_detectable=True),
        Concept("soil", ("soil", "loam", "clay", "sandy", "silt", "andisol", "alfisol", "mollisol")),
        Concept("climate", ("rainfall", "precipitation", "mean annual temperature", "climate")),
        Concept("region", ("county", "state", "province", "region", "country", "valley", "mountains")),
    ), regions=("methods", "abstract", "front_matter", "introduction")),
    "Species": Strategy("Species", (
        Concept("scientific_names", signals=("binomial",), pattern_detectable=True),
        Concept("common_names", ("cultivar", "variety", "crop", "species", "tree", "saplings", "seedlings")),
    ), regions=("front_matter", "abstract", "methods", "introduction")),
    "Crop": Strategy("Crop", (
        Concept("cultivars", ("cultivar", "cultivars", "variety", "varieties", "genotype", "population", "populations",
                              "hybrid", "line")),
        Concept("species", signals=("binomial",), expected=False),
    ), regions=("methods", "abstract", "front_matter")),
    "Variable": Strategy("Variable", (
        Concept("definition", signals=("measurement_verb",), use_target=True),
        Concept("units", signals=("unit",), use_target=True),
        Concept("table_header", use_target=True, expected=False),
    ), regions=("methods", "results", "abstract"), include_tables=True, all_tables=True, tables_first=True, max_chars=9000),
    "Method": Strategy("Method", (
        Concept("procedure", signals=("measurement_verb",), use_target=True),
        Concept("instrument", signals=("instrument",)),
    ), regions=("methods",), include_tables=True, max_chars=9000, enumeration_max_chars=14000),
    "Management": Strategy("Management", tuple(
        Concept(name, phrases, ("management_verb",) if name == "planting" else (), expected=False)
        for name, phrases in _MANAGEMENT_EVENTS
    ) + (
        Concept("event_dates", signals=("date", "relative_time"), expected=False),
        Concept("amounts", signals=("amount",), expected=False),
    ), regions=("methods", "abstract", "front_matter")),
    "Treatment": Strategy("Treatment", (
        Concept("design", ("treatment", "treatments", "control", "levels", "classes", "compared", "compare",
                           "comparison", "versus", "contrasted", "gradient"), ("design_term",)),
        Concept("definitions", use_target=True, expected=False),
        Concept("pooling", signals=("pooling",), expected=False),
    ), regions=("methods", "abstract", "front_matter", "results"), include_tables=True, all_tables=True,
       tables_first=True, max_chars=9000, enumeration_max_chars=14000, full_table_lines=16),
    "Study": Strategy("Study", (
        Concept("design", ("randomized", "complete block", "split-plot", "factorial", "replicates", "experimental unit"),
                ("design_term",)),
        Concept("sample_size", ("replicates", "saplings were sampled", "plots"), ("statistic",)),
        Concept("statistics", ("analysis of variance", "anova", "regression", "mixed model"), ("statistic",),
                expected=False),
    ), regions=("methods", "abstract")),
}
# Tasks that get a packet. Citation keeps its own verbatim front-matter context; Observation (Stage 7), TreatmentPair
# and Coverage (a rollup) get none yet.
ENUMERATION_STRATEGIES = {"Site", "Species", "Crop", "Variable", "Method", "Management", "Treatment", "Study"}
EXTRACTION_STRATEGIES = ENUMERATION_STRATEGIES


@dataclass(frozen=True)
class EvidenceItem:
    anchor: str
    text: str
    evidence_type: str            # primary | supporting | caption | table | table_header | table_note | cross_reference
    block_type: str
    region: str
    section: Optional[str]
    page: Optional[int]
    level: int
    relation: Optional[str]       # e.g. "neighbour_of:b:0028", "caption_of:Table 3", "refers_to:Table 3"
    reasons: tuple[str, ...]
    truncated: bool = False


@dataclass(frozen=True)
class CoverageEntry:
    concept: str
    status: str
    anchors: tuple[str, ...] = ()
    level: Optional[int] = None
    note: Optional[str] = None


@dataclass(frozen=True)
class ContextBundle:
    bundle_id: str
    paper_id: str
    document_hash: str
    task: str                     # "enumeration" | "extraction"
    entity_type: str
    target: Optional[str]
    strategy_version: str
    items: tuple[EvidenceItem, ...]
    coverage: tuple[CoverageEntry, ...]
    terminology: tuple[tuple[str, tuple[str, ...]], ...]
    negative_evidence: tuple[str, ...]
    retrieval_metadata: tuple[tuple[str, Any], ...] = ()
    # The Observation's experimental context -- cell, variable, method, treatment/factor, time, site,
    # aggregation, statistics, design -- composed from the existing artifacts (Document Map, method map, design.json),
    # stored as a JSON string so the bundle stays immutable and hashable.
    experimental_context: Optional[str] = None

    @property
    def anchors(self) -> tuple[str, ...]:
        return tuple(item.anchor for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["retrieval_metadata"] = dict(self.retrieval_metadata)
        data["experimental_context"] = json.loads(self.experimental_context) if self.experimental_context else None
        data["terminology"] = {term: list(aliases) for term, aliases in self.terminology}
        return data

    def render(self) -> str:
        """The packet as it goes into a prompt: verbatim anchored blocks, why each was retrieved, the coverage audit,
        and the paper's own abbreviations. Rules for its use are stated in the packet itself."""
        lines = []
        if self.experimental_context:
            lines += [_render_experimental_context(json.loads(self.experimental_context)), ""]
        lines += [
            "EVIDENCE PACKET -- assembled by the pipeline from the paper's structure before this call "
            f"(bundle {self.bundle_id}). Every block below is quoted VERBATIM from content.md with its anchor: cite "
            "these anchors and quote from these texts. Read these first; use the read tools only for what the "
            "packet does not contain. Blocks marked [truncated] are cut: read the full block before quoting from "
            "the cut part.",
            "",
        ]
        for item in self.items:
            where = " / ".join(x for x in (item.region, item.section, f"p.{item.page}" if item.page else None) if x)
            why = "; ".join(item.reasons[:4])
            lines.append(f"[{item.anchor}] ({item.evidence_type}; {where}; retrieved: {why})")
            lines.append(item.text + (" [truncated]" if item.truncated else ""))
            lines.append("")
        lines.append("COVERAGE AUDIT (what the pipeline looked for):")
        for entry in self.coverage:
            if entry.status == FOUND:
                lines.append(f"  - {entry.concept}: found in {', '.join(entry.anchors[:5])}")
            elif entry.status == ABSENT_BY_PATTERN:
                lines.append(f"  - {entry.concept}: NOT STATED anywhere in the paper ({entry.note}) -- do not search for it")
            elif entry.status == NOT_RETRIEVED:
                lines.append(f"  - {entry.concept}: present in the paper but not included here ({', '.join(entry.anchors[:5])}) "
                             f"-- read those blocks if you need it")
            else:
                lines.append(f"  - {entry.concept}: not found by the pipeline's search ({entry.note}); if you find it "
                             f"yourself, cite its block")
        if self.terminology:
            lines.append("")
            lines.append("TERMINOLOGY (the paper's own abbreviations/definitions): " + "; ".join(
                f"{term} = {' / '.join(aliases)}" for term, aliases in self.terminology[:25]))
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def _concept_hit(index: EvidenceIndex, block: DocBlock, concept: Concept, target_phrases: list[str]) -> Optional[Hit]:
    phrases = list(concept.phrases) + (target_phrases if concept.use_target else [])
    if not phrases and not concept.signals:
        return None
    tier, reasons = index._match(block, phrases, list(concept.signals))
    if tier is None:
        return None
    if concept.use_target and target_phrases and concept.signals:
        # A target-conditioned concept ("the units OF Vcmax") needs the target in the same block, not just any unit.
        target_tier, _ = index._match(block, target_phrases, [])
        if target_tier is None or target_tier > 2:
            return None
    return Hit(block.anchor, tier, reasons)


def _target_phrases(index: EvidenceIndex, target: Optional[str]) -> list[str]:
    if not target:
        return []
    phrases = [target.replace("_", " ")]
    phrases += sorted(index.aliases(target))[:6]
    return [p for p in dict.fromkeys(phrases) if significant_words(p)]


def _table_header(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[:TABLE_HEADER_LINES]) if lines and lines[0].lstrip().startswith("|") else text[:400]


def build_context_bundle(
    paper_id: str, entity_type: str, task: str, target: Optional[str] = None, seed_anchors: Iterable[str] = (),
    papers_root: Optional[Path] = None, max_chars: Optional[int] = None,
) -> Optional[ContextBundle]:
    """The packet for one task, or None when the entity type has no strategy (or the paper is not prepared)."""
    strategy = STRATEGIES.get(entity_type)
    allowed = ENUMERATION_STRATEGIES if task == "enumeration" else EXTRACTION_STRATEGIES
    if strategy is None or entity_type not in allowed:
        return None
    try:
        dmap = build_document_map(paper_id, papers_root)
    except (FileNotFoundError, OSError):
        return None
    index = build_evidence_index(dmap)
    budget = max_chars or (strategy.enumeration_max_chars if task == "enumeration" and strategy.enumeration_max_chars
                           else strategy.max_chars)
    target_phrases = _target_phrases(index, target)

    caption_of = {a: (t.label or f"table {t.anchors[0]}") for t in dmap.tables for a in t.caption_anchors}
    selected: dict[str, EvidenceItem] = {}
    used = 0
    dropped: list[str] = []

    def take(block: Optional[DocBlock], evidence_type: str, level: int, relation: Optional[str], reasons: Iterable[str],
             text: Optional[str] = None) -> None:
        nonlocal used
        if block is None or block.anchor in selected or block.region == "references":
            return
        body = text if text is not None else block.text
        truncated = len(body) > MAX_BLOCK_CHARS
        body = body[:MAX_BLOCK_CHARS] if truncated else body
        if used + len(body) > budget:
            dropped.append(block.anchor)
            return
        used += len(body)
        if block.anchor in caption_of and evidence_type == "supporting":
            # A caption keeps its structural role whichever level first reached it.
            evidence_type, relation = "caption", f"caption_of:{caption_of[block.anchor]}"
        selected[block.anchor] = EvidenceItem(
            anchor=block.anchor, text=body, evidence_type=evidence_type, block_type=block.block_type,
            region=block.region, section=block.section_title, page=block.page, level=level, relation=relation,
            reasons=tuple(reasons), truncated=truncated,
        )

    # L0 -- the candidate's own evidence
    seeds = [dmap.block(a) for a in seed_anchors]
    seeds = [b for b in seeds if b is not None]
    for block in seeds:
        if block.block_type == "Table":
            continue   # a table is represented by its caption/header at L3
        take(block, "primary", 0, None, ("candidate anchor",))
    # L1 -- local neighbourhood
    for block in seeds:
        if block.block_type == "Table":
            continue
        for neighbour in dmap.neighbors(block.anchor, before=1, after=1):
            take(neighbour, "supporting", 1, f"neighbour_of:{block.anchor}", ("reading-order neighbour",))
    def level2() -> None:
        # L2 -- concept hits in the preferred regions. Within a tier, a block that matches MORE of the strategy's
        # concepts ranks first (stated in its reasons), so one specific block ("Bulk density was determined ... using
        # rings") is not crowded out by a long methods section of generic matches.
        region_blocks = dmap.region_blocks(*strategy.regions)
        cues = {b.anchor: sum(1 for c in strategy.concepts if _concept_hit(index, b, c, target_phrases)) for b in region_blocks}
        for concept in strategy.concepts:
            hits = []
            for block in region_blocks:
                hit = _concept_hit(index, block, concept, target_phrases)
                if hit:
                    rank = strategy.regions.index(block.region) if block.region in strategy.regions else len(strategy.regions)
                    hits.append(((hit.tier, -cues[block.anchor], -len(hit.reasons), rank, block.order), block, hit))
            for _key, block, hit in sorted(hits, key=lambda h: h[0])[:HITS_PER_CONCEPT[task]]:
                reasons = hit.reasons + ((f"matches {cues[block.anchor]} concepts",) if cues[block.anchor] > 1 else ())
                take(block, "supporting", 2, f"concept:{concept.name}", reasons)

    def level3() -> list:
        # L3 -- linked tables (or every table, for an enumeration that needs table headers)
        tables = []
        if strategy.include_tables:
            if task == "enumeration" and strategy.all_tables:
                tables = list(dmap.tables)
            else:
                linked = set()
                for anchor in list(selected) + [b.anchor for b in seeds]:
                    table = dmap.table_containing(anchor)
                    if table:
                        linked.add(table.anchors)
                    for ref in dmap.refs_from(anchor):
                        if ref.kind != "table" or not ref.target_anchors:
                            continue   # a figure reference names no table
                        for table in dmap.tables:
                            if set(table.anchors) & set(ref.target_anchors):
                                linked.add(table.anchors)
                if target_phrases:
                    for table in dmap.tables:
                        if any(index._match(dmap.block(a), target_phrases, [])[0] in (1, 2) for a in table.anchors + table.caption_anchors
                               if dmap.block(a)):
                            linked.add(table.anchors)
                tables = [t for t in dmap.tables if t.anchors in linked]
            for table in tables:
                label = table.label or f"table {table.anchors[0]}"
                for anchor in table.caption_anchors:
                    take(dmap.block(anchor), "caption", 3, f"caption_of:{label}", (f"{label} caption",))
                full = dmap.text(table.anchors[0])
                is_small = strategy.full_table_lines and len([l for l in full.splitlines() if l.strip()]) <= strategy.full_table_lines
                take(dmap.block(table.anchors[0]), "table" if is_small else "table_header", 3, f"header_of:{label}",
                     (f"{label} in full (small table)" if is_small else f"{label} header rows",),
                     text=full if is_small else _table_header(full))
                for anchor in table.note_anchors[:1]:
                    take(dmap.block(anchor), "table_note", 3, f"note_of:{label}", (f"{label} note",))
        return tables

    if strategy.tables_first:
        tables = level3()
        level2()
    else:
        level2()
        tables = level3()
    # L4 -- prose that refers to those tables (table -> prose)
    for table in tables:
        for ref in dmap.references_to(table)[:2]:
            take(dmap.block(ref.from_anchor), "cross_reference", 4, f"refers_to:{ref.label}", (f"mentions {ref.label}",))

    # Coverage audit, with L5 (whole-document) search for whatever is still missing.
    coverage: list[CoverageEntry] = []
    negative: list[str] = []
    for concept in strategy.concepts:
        in_packet = [(a, item.level) for a, item in selected.items()
                     if _concept_hit(index, dmap.block(a), concept, target_phrases)]
        if in_packet:
            coverage.append(CoverageEntry(concept.name, FOUND, tuple(a for a, _ in in_packet), min(l for _, l in in_packet)))
            continue
        if not concept.expected:
            continue   # an optional concept is only looked for in the preferred regions (no whole-document noise)
        document_hits = [b for b in dmap.blocks if b.region != "references" and _concept_hit(index, b, concept, target_phrases)]
        if document_hits:
            for block in document_hits[:2]:
                take(block, "supporting", 5, f"concept:{concept.name}", ("whole-document search",))
            now = [b.anchor for b in document_hits if b.anchor in selected]
            if now:
                coverage.append(CoverageEntry(concept.name, FOUND, tuple(now), 5))
            else:
                coverage.append(CoverageEntry(concept.name, NOT_RETRIEVED, tuple(b.anchor for b in document_hits[:5]),
                                              note="packet budget exhausted"))
            continue
        if concept.pattern_detectable and concept.signals and not any(index.signal_hits(s) for s in concept.signals):
            note = f"a scan of every block for {'/'.join(concept.signals)} found none"
            coverage.append(CoverageEntry(concept.name, ABSENT_BY_PATTERN, note=note))
            negative.append(f"{concept.name}: {note}")
        else:
            coverage.append(CoverageEntry(concept.name, NOT_FOUND_AFTER_FULL_SEARCH,
                                          note="searched every region except the references; absence is not proven"))

    ordered = tuple(sorted(selected.values(), key=lambda i: (i.level, dmap.block(i.anchor).order)))
    terms = []
    wanted = {w for p in target_phrases for w in [p.lower()]}
    for term, aliases in sorted(index.terminology.items()):
        if len(term) <= 12 and (not wanted or term in wanted or any(a in wanted for a in aliases)):
            terms.append((term, tuple(sorted(aliases))[:3]))
    digest = hashlib.sha256(json.dumps([
        dmap.document_hash, task, entity_type, target, STRATEGY_VERSION, [i.anchor for i in ordered],
    ]).encode()).hexdigest()[:12]
    return ContextBundle(
        bundle_id=digest, paper_id=paper_id, document_hash=dmap.document_hash, task=task, entity_type=entity_type,
        target=target, strategy_version=STRATEGY_VERSION, items=ordered, coverage=tuple(coverage),
        terminology=tuple(terms[:40]), negative_evidence=tuple(negative),
        retrieval_metadata=(("chars", used), ("budget", budget), ("dropped_for_budget", tuple(dropped)),
                            ("seed_anchors", tuple(b.anchor for b in seeds))),
    )


# --------------------------------------------------------------------------- #
# The Observation's experimental context (composed, not rediscovered)
# --------------------------------------------------------------------------- #

def _render_experimental_context(context: dict[str, Any]) -> str:
    """The established relationships for ONE observation, stated so the model uses them instead of re-deriving them.
    Every link says where it came from; an unresolved link says so -- it is never filled in here."""
    lines = ["EXPERIMENTAL CONTEXT -- established by the pipeline for THIS value (use these; do not re-derive them):"]
    cell = context.get("cell") or {}
    if cell:
        where = ", ".join(f"{k}={v}" for k, v in (cell.get("row_levels") or {}).items())
        lines.append(f"  cell: {cell.get('table_label') or cell.get('table_anchor')} ({cell.get('table_anchor')}), row "
                     f"[{where}], column {cell.get('column_label')!r}, literal cell text {cell.get('value_text')!r}")
    variable = context.get("variable") or {}
    if variable:
        lines.append(f"  variable: {variable.get('label')!r}" + (f" = {variable['name']!r}" if variable.get("name") else "")
                     + (f"; units in the header: {variable['header_units']!r}" if variable.get("header_units") else "")
                     + (f"; Variable record {variable['record_id']}" if variable.get("record_id") else ""))
    method = context.get("method") or {}
    if method:
        if method.get("status") == "linked":
            lines.append(f"  method: {method.get('record_id')} ({method.get('name')!r}) -- {method.get('tier')} link: {method.get('reason')}")
        else:
            lines.append(f"  method: NOT ESTABLISHED ({method.get('status')}: {method.get('reason')}) -- do not choose one")
    treatment = context.get("treatment") or {}
    if treatment:
        lines.append(f"  treatment: {treatment.get('record_id') or 'none'}"
                     + (f" ({treatment['name']!r}: {treatment.get('definition')!r})" if treatment.get("name") else "")
                     + (f"; factor levels {treatment['levels']}" if treatment.get("levels") else ""))
    time = context.get("time") or {}
    if time:
        lines.append("  time: " + "; ".join(f"{k}: {v}" for k, v in time.items()))
    if context.get("site"):
        lines.append(f"  site: {context['site']}")
    aggregation = context.get("aggregation") or {}
    if aggregation:
        lines.append(f"  aggregation: {aggregation.get('reported_effect_scope')}"
                     + (f" over {aggregation['aggregated_over_factors']} ({aggregation.get('basis')})"
                        if aggregation.get("aggregated_over_factors") else ""))
    statistics = context.get("statistics") or {}
    if statistics:
        parts = [f"mean {statistics.get('mean_text')!r}"]
        if statistics.get("statistic_name"):
            parts.append(f"{statistics['statistic_name']} = {statistics.get('statistic_value')} (the table states "
                         f"{statistics.get('statistic_source_text')!r} in {statistics.get('statistic_source_anchor')})")
        elif statistics.get("dispersion") is not None or statistics.get("interval"):
            parts.append("a dispersion the table never names -- do NOT label it SE/SD")
        if statistics.get("letters"):
            parts.append(f"mean-separation letters {statistics['letters']!r} ({statistics.get('letters_meaning')})")
        if statistics.get("sample_size_evidence"):
            parts.append(f"sample size stated at {statistics['sample_size_evidence']} (no IR field yet: do not put it in "
                         f"another field)")
        lines.append("  statistics: " + "; ".join(parts))
    design = context.get("design") or {}
    if design:
        lines.append(f"  design: table layout {design.get('layout')}; factors {design.get('factors')}"
                     + (f"; CONFLICTS on these factors: {design['conflicts']}" if design.get("conflicts") else ""))
    return "\n".join(lines)


def build_observation_bundle(
    paper_id: str, experimental_context: dict[str, Any], evidence_anchors: dict[str, list[str]],
    papers_root: Optional[Path] = None,
) -> Optional[ContextBundle]:
    """The Observation's packet: the verbatim, anchored blocks behind each link of its experimental context
    (`evidence_anchors`: {"table": [...], "method": [...], "treatment": [...], "variable": [...], "design": [...]}),
    with the context itself attached. Nothing here decides a link -- the links were established upstream."""
    try:
        dmap = build_document_map(paper_id, papers_root)
    except (FileNotFoundError, OSError):
        return None
    levels = {"table": 0, "variable": 2, "method": 2, "treatment": 2, "time": 2, "design": 2}
    items: list[EvidenceItem] = []
    seen: set[str] = set()
    used = 0
    for role, anchors in evidence_anchors.items():
        for anchor in anchors:
            block = dmap.block(anchor)
            if block is None or block.anchor in seen:
                continue
            text = _table_header(block.text) if block.block_type == "Table" else block.text
            text = text[:MAX_BLOCK_CHARS]
            if used + len(text) > DEFAULT_MAX_CHARS:
                break
            used += len(text)
            seen.add(block.anchor)
            items.append(EvidenceItem(
                anchor=block.anchor, text=text, evidence_type="table_header" if block.block_type == "Table" else role,
                block_type=block.block_type, region=block.region, section=block.section_title, page=block.page,
                level=levels.get(role, 2), relation=f"{role}_evidence", reasons=(f"{role} link evidence",),
                truncated=len(block.text) > MAX_BLOCK_CHARS and block.block_type != "Table",
            ))
    context_json = json.dumps(experimental_context, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(json.dumps([dmap.document_hash, "observation", context_json,
                                        [i.anchor for i in items], STRATEGY_VERSION]).encode()).hexdigest()[:12]
    return ContextBundle(
        bundle_id=digest, paper_id=paper_id, document_hash=dmap.document_hash, task="extraction",
        entity_type="Observation", target=(experimental_context.get("variable") or {}).get("label"),
        strategy_version=STRATEGY_VERSION, items=tuple(items), coverage=(), terminology=(), negative_evidence=(),
        retrieval_metadata=(("chars", used),), experimental_context=context_json,
    )
