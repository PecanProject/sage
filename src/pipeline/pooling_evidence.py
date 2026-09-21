"""Deterministic pooling-evidence detection (protocol Section 7.4).

A table's values are sometimes means POOLED over a factor that is absent from
its rows and columns, and the only place that says so is a sentence in the
table's own caption or in the note directly after it ("Data show the mean +/-
standard error for all <factor> treatments, ..."). The live model read that
sentence and still did not report the pooling, so it is recognised here, in
code, from grounded source text -- no model call.

Nothing in this module knows any paper, cultivar, treatment or phrase: it
recognises a small set of GENERIC linguistic frames (an aggregation verb or
"for all" / "regardless of", applied to a reported value) and takes the factor
name from the text itself.

Where it looks (`pooling_windows`) -- deliberately narrow, provenance-driven:
  * the table's OWN caption: the Caption block immediately before the first
    block of the (continuation) chain;
  * the note AFTER the table: up to `MAX_FOLLOWING_BLOCKS` Text/Footnote blocks
    after the last block of the chain, on the same or the next page. A Text
    block is ordinary prose and is eligible only when it is directly adjacent.
    The walk stops at a Table, Caption, Figure, Picture, SectionHeader or
    ListItem, and at a block that opens a new "Table N" / "Fig." label -- so a
    NEIGHBOURING table's caption, or a figure's, is never read as this table's.
Arbitrary prose elsewhere in the paper is never scanned.

Every accepted result cites its block anchor and a LITERAL slice of that
block's text (validated afterwards with the pipeline's existing grounding
check); anything doubtful is REJECTED with a stated reason and never guessed.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from pipeline import content_reader

MAX_FOLLOWING_BLOCKS = 3

# Block types that end the after-table window.
_STOP_BLOCK_TYPES = {"Table", "Caption", "Figure", "Picture", "SectionHeader", "ListItem"}
_FURNITURE_BLOCK_TYPES = {"PageHeader", "PageFooter"}  # skipped without counting
_LABEL_START_RE = re.compile(r"^\W*(table|tab\.|fig\.|figure)\s*[0-9IVX]+", re.IGNORECASE)

# What a sentence is ABOUT: the reported value.
_VALUE = r"(?:means?|averages?|data|values?|results|estimates|percentages|proportions|totals|measurements)"
_VALUE_RE = re.compile(rf"\b{_VALUE}\b", re.IGNORECASE)
_AGGREGATION_VERB = r"(?:averaged|pooled|combined|collapsed|aggregated|summed)"

# Frames. `own_caption` sentences describe the table's own values, so the value
# noun is implied there; a note must state one.
_FRAMES: list[tuple[str, re.Pattern[str]]] = [
    ("aggregation_verb_across", re.compile(
        rf"\b{_AGGREGATION_VERB}\s+(?:\w+\s+){{0,2}}?(?:across|over|among|amongst)\s+(?P<np>.+)", re.IGNORECASE)),
    ("mean_across", re.compile(
        r"\b(?:mean|means|average|averages)\b(?:\s*(?:±|\$?\\pm\$?)\s*(?:SE|SD|s\.e\.|standard (?:error|deviation)))?"
        r"\s+(?:across|among|amongst)\s+(?P<np>.+)", re.IGNORECASE)),
    ("value_for_all", re.compile(rf"\b{_VALUE}\b[^.;]{{0,60}}?\bfor all\s+(?P<np>.+)", re.IGNORECASE)),
    ("regardless_of", re.compile(rf"\b{_VALUE}\b[^.;]{{0,80}}?\b(?:regardless|irrespective) of\s+(?P<np>.+)", re.IGNORECASE)),
]

# Statistical-procedure sentences ("Means separations for all ANOVAs were
# performed by ...") are about the analysis, not about pooled values.
_STAT_SENTENCE_RE = re.compile(
    r"\b(anova|anovas|tukey|kramer|duncan|lsd|hsd|scheffe|bonferroni|post[- ]hoc|separations?|"
    r"multiple comparisons?|pairwise|regression|contrasts?)\b", re.IGNORECASE)
_VALUE_THEN_STAT_RE = re.compile(rf"\b{_VALUE}\s+(?:separations?|comparisons?|tests?|procedures?|squares?)\b", re.IGNORECASE)

_NP_END_RE = re.compile(
    r"\s*(?:,|;|\.(?:\s|$)|\(|\bsince\b|\bbecause\b|\bas\b|\bwhich\b|\bwhere\b|\bwhile\b|\bwith\b|\bwhen\b|"
    r"\bthus\b|\bso\b|\bbut\b|\bhence\b)", re.IGNORECASE)
_DETERMINERS = frozenset(
    "the all each both any every three two four five six seven eight nine ten of a an".split())
# Generic heads removed from a name ("<factor> treatments" -> "<factor>"); a name
# that is ONLY such a word says nothing about which factor was pooled.
_GENERIC_HEADS = frozenset({
    "treatment", "treatments", "level", "levels", "combination", "combinations",
    "group", "groups", "condition", "conditions", "value", "values", "data", "result", "results",
})
# A noun phrase containing any of these is a clause, not a factor name.
_DOUBTFUL_TOKENS = frozenset(
    "were was is are be been being by in on at to from with than that which presented performed used shown "
    "observed conducted during within between for per after before".split())

# Generic dimension vocabulary (singularised last token). Not paper-specific.
_DIMENSION_WORDS: dict[str, frozenset[str]] = {
    "site": frozenset({"location", "site", "environment", "locality", "region"}),
    "time": frozenset({"year", "season", "date", "stage", "maturity", "harvest", "month", "week", "day", "sampling", "time"}),
    "crop": frozenset({"cultivar", "variety", "population", "genotype", "hybrid", "accession", "line", "species"}),
    "replicate": frozenset({"block", "replicate", "replication", "plot", "rep", "repetition"}),
}


def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("s") and not w.endswith("ss") and len(w) > 3:
        return w[:-1]
    return w


def normalize_name(name: str) -> str:
    """Comparison key for a factor name: lowercase, punctuation-free, each word singularised."""
    return " ".join(_singular(t) for t in re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split())


def _rendered_order(paper_id: str, papers_root: Path) -> tuple[Optional[dict], dict[str, str], list[str]]:
    provenance = content_reader._load_provenance(paper_id, papers_root)
    if provenance is None:
        return None, {}, []
    try:
        texts = content_reader._rendered_block_texts(paper_id, papers_root)
    except OSError:
        return None, {}, []
    rendered = sorted((a for a in provenance if a in texts), key=content_reader._anchor_sort_key)
    return provenance, texts, rendered


def pooling_windows(paper_id: str, table_anchors: list[str], papers_root: Path = content_reader.DEFAULT_PAPERS_ROOT) -> list[tuple[str, str]]:
    """[(position, block anchor)] eligible to state this table's pooling: the
    own caption (`own_caption`) and the bounded following note
    (`after+1` ...). `table_anchors` is the whole logical table (a
    continuation chain); the caption belongs before its first block and the
    note after its last."""
    provenance, texts, rendered = _rendered_order(paper_id, papers_root)
    if provenance is None:
        return []
    tables = sorted((a for a in table_anchors if a in texts and provenance[a].get("block_type") == "Table"),
                    key=content_reader._anchor_sort_key)
    if not tables:
        return []
    position = {a: i for i, a in enumerate(rendered)}
    windows: list[tuple[str, str]] = []

    before = position[tables[0]] - 1
    while before >= 0 and provenance[rendered[before]].get("block_type") in _FURNITURE_BLOCK_TYPES:
        before -= 1
    if before >= 0 and provenance[rendered[before]].get("block_type") == "Caption":
        windows.append(("own_caption", rendered[before]))

    last = tables[-1]
    last_page = content_reader._page_number(provenance[last].get("page_id"))
    counted = 0
    for anchor in rendered[position[last] + 1:]:
        entry = provenance[anchor]
        block_type = entry.get("block_type")
        if block_type in _FURNITURE_BLOCK_TYPES:
            continue
        if block_type in _STOP_BLOCK_TYPES or block_type not in ("Text", "Footnote"):
            break
        if _LABEL_START_RE.match(texts.get(anchor, "")):
            break  # a new "Table N" / "Fig." label starts something else
        page = content_reader._page_number(entry.get("page_id"))
        if last_page is not None and page is not None and page - last_page not in (0, 1):
            break
        counted += 1
        if counted > MAX_FOLLOWING_BLOCKS:
            break
        if block_type == "Text" and counted > 1:
            break  # ordinary prose is only eligible when directly adjacent
        windows.append((f"after+{counted}", anchor))
    return windows


def _sentences(text: str) -> list[str]:
    normalized = " ".join(text.split())
    parts = re.split(r"(?<=[a-z\)\]\*])\.\s+(?=[\*A-Z$])", normalized)
    return [p.strip() for p in parts if p.strip()]


def _clean_excerpt(sentence: str) -> str:
    """The sentence as a literal slice of its block (markdown emphasis markers trimmed off the ends)."""
    return sentence.strip().strip("*_ ").strip() or sentence.strip()


def _factor_names(np_text: str) -> tuple[list[str], Optional[str]]:
    """(names, doubt): the factor names in a noun phrase, or a reason the phrase
    cannot be trusted as a factor name (never a guess)."""
    end = _NP_END_RE.search(np_text)
    np_text = (np_text[: end.start()] if end else np_text).strip(" *_.]")
    names: list[str] = []
    for part in re.split(r"\s*(?:,|\band\b|&|\bor\b)\s*", np_text):
        tokens = [w.strip("*_.]") for w in part.split()]
        tokens = [w for w in tokens if w and w.lower() not in _DETERMINERS]
        if any(t.lower() in _DOUBTFUL_TOKENS for t in tokens):
            return [], f"the noun phrase {part.strip()!r} contains a verb/preposition, so it is a clause, not a factor name"
        if any(re.search(r"[0-9$\\_]", t) for t in tokens):
            return [], f"the noun phrase {part.strip()!r} contains digits or markup, so it is not a factor name"
        if _STAT_SENTENCE_RE.search(part):
            return [], f"the noun phrase {part.strip()!r} names a statistical procedure"
        while len(tokens) > 1 and tokens[-1].lower() in _GENERIC_HEADS:
            tokens = tokens[:-1]
        if not tokens or (len(tokens) == 1 and tokens[0].lower() in _GENERIC_HEADS):
            return [], f"{part.strip()!r} is a generic term (no specific factor is named)"
        if len(tokens) > 4:
            return [], f"the noun phrase {part.strip()!r} is too long to be a factor name"
        names.append(" ".join(tokens))
    if not names:
        return [], "no factor name follows the pooling phrase"
    if len(names) > 3:
        return [], "too many candidate factor names"
    return names, None


# A designed mixture/composition level (raw_schema.MIXTURE_LEVEL_RULE): a treatment
# when the source itself calls the levels treatments, never `crop` just because
# cultivars are named in it, and never a treatment merely because of the word.
_MIXTURE_WORDS = frozenset({"mixture", "blend"})


def _dimension_of(name: str, original_np: str) -> str:
    """`treatment` when the source itself calls the pooled levels treatments;
    a mixture/composition name that the source does NOT call treatments is
    `other` (undetermined) -- never `crop`; else the generic vocabulary on the
    last word, else `other`."""
    if re.search(rf"\b{re.escape(name.split()[-1])}\s+treatments?\b", original_np, re.IGNORECASE):
        return "treatment"
    if any(_singular(token) in _MIXTURE_WORDS for token in name.split()):
        return "other"
    last = _singular(name.split()[-1])
    for dimension, words in _DIMENSION_WORDS.items():
        if last in words:
            return dimension
    return "other"


def scan_text(text: str, *, own_caption: bool = False) -> list[dict[str, Any]]:
    """Pooling statements in ONE eligible block's text, as accepted/rejected
    records without an anchor (the caller adds it). Only sentences that match
    a pooling frame produce a record."""
    records: list[dict[str, Any]] = []
    for sentence in _sentences(text):
        for pattern, frame in _FRAMES:
            match = frame.search(sentence)
            if match is None:
                continue
            excerpt = _clean_excerpt(sentence)
            base = {"pattern": pattern, "excerpt": excerpt}
            if _VALUE_THEN_STAT_RE.search(sentence) or _STAT_SENTENCE_RE.search(sentence):
                records.append({**base, "status": "rejected", "factor": None,
                                "rejection_reason": "statistical-procedure sentence, not a statement about pooled values"})
                break
            if pattern == "aggregation_verb_across" and not own_caption and not _VALUE_RE.search(sentence[: match.start()]):
                records.append({**base, "status": "rejected", "factor": None,
                                "rejection_reason": "no reported-value subject (mean/data/values/...) before the aggregation verb"})
                break
            np_text = match.group("np")
            names, doubt = _factor_names(np_text)
            if doubt:
                records.append({**base, "status": "rejected", "factor": None, "rejection_reason": doubt})
                break
            for name in names:
                dimension = _dimension_of(name, np_text)
                if dimension == "replicate":
                    records.append({**base, "status": "rejected", "factor": name,
                                    "rejection_reason": "pooling over replicates is not a meaningful pooled factor"})
                else:
                    records.append({**base, "status": "accepted", "factor": name, "dimension": dimension})
            break  # one frame per sentence
    return records


def detect_pooling_evidence(
    paper_id: str, table_anchors: list[str], papers_root: Path = content_reader.DEFAULT_PAPERS_ROOT,
) -> list[dict[str, Any]]:
    """Every pooling statement found in this table's eligible windows, each
    citing its `anchor` and window `position`; duplicates (same block, same
    factor) collapsed. Pure function of the source text."""
    _, texts, _ = _rendered_order(paper_id, papers_root)
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, Optional[str], str]] = set()
    for position, anchor in pooling_windows(paper_id, table_anchors, papers_root):
        for record in scan_text(texts.get(anchor, ""), own_caption=(position == "own_caption")):
            key = (anchor, normalize_name(record["factor"]) if record.get("factor") else None, record["status"])
            if key in seen:
                continue
            seen.add(key)
            out.append({"anchor": anchor, "position": position, **record})
    return out
