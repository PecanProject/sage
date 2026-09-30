"""
Experimental-design intermediate representation (Phase C / Stage 6): what the paper's tables and text say about the
design, derived deterministically BEFORE the final IR and kept even where the IR cannot yet represent it.

Three things the corpus showed the per-cell extraction could not see:

1. MAIN-EFFECT (marginal) TABLES. Philippe-2007-Six Tables 1-3 list PAR-class rows, then Year rows: each row sets only
   ONE of the two row factors, so each value is pooled over the other. The classification called all three tables
   "cell-level, nothing pooled"; unlocking their Observations as treatment means would have been wrong. Pooling is
   decided PER CELL: a column pooled over Year only if it has values in the Year rows (Philippe's BSD1/BSD6 are single
   years -- their Year rows are blank -- so they are not pooled over Year; INC is).
2. REPRESENTABILITY is decided separately from extraction: a pooled cell that keeps a treatment level is an
   `aggregated_mean` (representable today); a cell pooled over the treatment factor itself (a Year row; Kathryn's
   "Mean" row) has no Treatment to reference -- it is kept as a scientific fact and reported
   BLOCKED_BY_REPRESENTATION, never forced onto an invented Treatment.
3. CONFLICTS are objects: a factor's level bounds stated differently in the table and in the text (Philippe PARt:
   "0.2-0.35" in the tables, "0.2-0.37" in b:0046, "0.1 to 0.4" in b:0030, "0-0.35" in b:0006) are recorded with
   both sources and never silently resolved.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

from pipeline.evidence_index import EvidenceIndex, normalize

REPRESENTABLE = "representable"
BLOCKED_BY_REPRESENTATION = "blocked_by_representation"
UNIDENTIFIED_ROW = "unidentified_row"
L4_TREATMENT_POOLED = (
    "L4 (current-IR limitation, not an extraction failure): the value is pooled over the treatment factor itself "
    "(a main-effect row of a non-treatment factor, or a 'Mean' over treatments), so there is no Treatment for "
    "Observation.treatment_id; it is kept as an extracted aggregate and never assigned an invented Treatment"
)


@dataclass
class CellPooling:
    pooled_over: list[str]            # factors the value is averaged over (by layout)
    retained: dict[str, str]          # the factor levels the row does set
    has_treatment: bool
    representation: str               # representable | blocked_by_representation
    reason: Optional[str] = None
    evidence: str = ""


# Row labels that are statistics or summaries, never a condition of the experiment (Phase A5; the orchestrator's
# candidate builders use the same two patterns). A bare single letter ("P", "F", "n") is never taken as one -- it can
# be a real level (a population labelled "P").
STATISTIC_ROW_RE = re.compile(   # a statistic label, possibly followed by what it applies to ("P value PAR t", "LSD (0.05)")
    r"^\W*(?:p[- ]?values?|probability|significance|lsd|hsd|msd|s\.?e\.?m?|s\.?d\.?|standard (?:error|deviation)"
    r"|c\.?v\.?|f[- ]?values?|contrasts?)\b", re.IGNORECASE)
SUMMARY_ROW_RE = re.compile(    # a summary label that is the WHOLE label ("Mean", "Grand mean", "Total")
    r"^\W*(?:grand\s+)?(?:mean|means|average|overall|total|totals|pooled|all(?: treatments| systems)?)\W*$", re.IGNORECASE)


def is_statistic_label(text: Any) -> bool:
    return isinstance(text, str) and bool(STATISTIC_ROW_RE.match(text.strip()))


def _statistic_row(row: Any) -> bool:
    """A row of test results ("P value PAR t", "LSD (0.05)"): not a level of any factor of the design."""
    return any(is_statistic_label(v) for v in (row.factor_values or {}).values())


def _design_rows(classification: Any) -> list[Any]:
    return [r for r in classification.row_groups or [] if not _statistic_row(r)]


def _row_factors(classification: Any) -> list[str]:
    """Factors encoded in the rows: declared with encoding "rows", else every factor name the row groups use. A factor
    whose every level is a statistic label (Philippe Tables 2-3 "Statistic": "P value PAR t", ...) is not a factor of
    the design -- values are never "pooled over" it (the run of 20260926T162813 reported STAR sky as aggregated over
    [Year, Statistic])."""
    declared = [f.name for f in (classification.factors or []) if (f.encoding or "rows") == "rows"]
    seen = [k for row in classification.row_groups or [] for k in (row.factor_values or {})]
    names = declared or list(dict.fromkeys(seen))
    rows = _design_rows(classification)
    return [n for n in names if any((row.factor_values or {}).get(n) for row in rows)]


def _dimension(classification: Any, name: str) -> Optional[str]:
    return next((f.dimension for f in classification.factors or [] if f.name == name), None)


def is_main_effect_layout(classification: Any) -> bool:
    """>= 2 row factors, and no row sets more than one of them (each row is one factor's marginal level)."""
    factors = _row_factors(classification)
    if len(factors) < 2:
        return False
    rows = [r for r in _design_rows(classification) if any((r.factor_values or {}).get(f) for f in factors)]
    return bool(rows) and all(sum(1 for f in factors if (r.factor_values or {}).get(f)) == 1 for r in rows)


def cell_pooling(classification: Any, row: Any, value_column_id: str) -> Optional[CellPooling]:
    """How this cell's value is aggregated by the table's layout, or None when the table is not a main-effect table.
    A column counts as pooled over factor F only if it has values in F's own rows (it varies with F)."""
    if not is_main_effect_layout(classification):
        return None
    factors = _row_factors(classification)
    set_here = {f: (row.factor_values or {}).get(f) for f in factors if (row.factor_values or {}).get(f)}
    if not set_here:
        # A row that sets NO level of any design factor is not a marginal mean over all of them -- it is a row the
        # classification did not identify (Philippe run 20260926T190955_435c16fa: the P-value rows of Tables 2-3 came
        # back with empty factor_values, and 33 P-values were counted as blocked "measurements"). Never a candidate,
        # never counted as blocked.
        return CellPooling([], {}, False, UNIDENTIFIED_ROW,
                           "the row sets no level of the table's factors, so what its value is cannot be established",
                           "row with empty factor levels in a main-effect table")
    pooled = []
    for other in factors:
        if other in set_here:
            continue
        varies = any((r.factor_values or {}).get(other) and ((r.cells or {}).get(value_column_id) or "").strip()
                     for r in _design_rows(classification))
        if varies:
            pooled.append(other)
    if not pooled:
        return None
    treatment_factors = {f for f in factors if _dimension(classification, f) == "treatment"}
    has_treatment = bool(treatment_factors & set(set_here))
    pooled_treatment = bool(treatment_factors & set(pooled))
    representation = BLOCKED_BY_REPRESENTATION if (pooled_treatment and not has_treatment) else REPRESENTABLE
    evidence = (f"row {', '.join(f'{k}={v}' for k, v in set_here.items())} sets no {', '.join(pooled)} level while other "
                f"rows of the same table do: a main-effect (marginal) mean over {', '.join(pooled)}")
    return CellPooling(pooled, set_here, has_treatment, representation,
                       L4_TREATMENT_POOLED if representation == BLOCKED_BY_REPRESENTATION else None, evidence)


def summary_row_pooling(classification: Any, row: Any, summary_label: str) -> CellPooling:
    """A "Mean"/"Total" row: pooled over the factor(s) it replaces a level of (Kathryn Table 3 "Mean" over System)."""
    factors = [f for f, v in (row.factor_values or {}).items() if v == summary_label]
    treatment = any(_dimension(classification, f) == "treatment" for f in factors)
    representation = BLOCKED_BY_REPRESENTATION if treatment else REPRESENTABLE
    return CellPooling(factors, {}, False, representation,
                       L4_TREATMENT_POOLED if treatment else None,
                       f"row labelled {summary_label!r} summarises every level of {', '.join(factors)}")


def time_span(classification: Any, pooled_over: Iterable[str]) -> Optional[str]:
    """The time levels a pooled value covers, as the table lists them ("2001, 2002, 2003, 2004, 2006")."""
    levels = []
    for factor in pooled_over:
        if _dimension(classification, factor) != "time":
            continue
        for row in _design_rows(classification):
            value = (row.factor_values or {}).get(factor)
            if value and value not in levels:
                levels.append(value)
    return ", ".join(levels) if levels else None


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #

_RANGE_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)(?!\d)(?!\.\d)")


@dataclass
class Conflict:
    subject: str
    kind: str
    claim_a: str
    source_a: str
    claim_b: str
    source_b: str
    resolution_status: str = "unresolved"
    note: str = ""


def _ranges(text: str) -> list[tuple[float, float, str]]:
    out = []
    for match in _RANGE_RE.finditer(normalize(text)):
        low, high = float(match.group(1)), float(match.group(2))
        if low < high:
            out.append((low, high, match.group(0)))
    return out


DESIGN_REGIONS = ("methods", "abstract", "front_matter")


def factor_range_conflicts(classifications: Iterable[Any], index: EvidenceIndex) -> list[Conflict]:
    """For a treatment factor whose levels are numeric ranges ("PAR t 0.2-0.35"), a DESIGN statement (methods, abstract,
    front matter) about that factor that gives
      - a class of comparable width sharing exactly one bound with a table class ("0.2-0.37" vs "0.2-0.35"), or
      - an overall range (spanning most of the factor's range) different from the span of the table's classes
        ("0.1 to 0.4" vs 0-0.35)
    is a conflict. Ranges on another scale (years, wavelengths, stem densities) and narrative results/discussion text
    are never compared; nothing is inferred from words."""
    conflicts: list[Conflict] = []
    seen: set[tuple] = set()
    for classification in classifications:
        table = (classification.table_anchors or ["?"])[0]
        for factor in [f.name for f in classification.factors or [] if f.dimension == "treatment"]:
            levels = [(low, high, (row.factor_values or {}).get(factor))
                      for row in classification.row_groups or []
                      for low, high, _ in _ranges((row.factor_values or {}).get(factor) or "")]
            if len(levels) < 2:
                continue
            span_low, span_high = min(l for l, _, _ in levels), max(h for _, h, _ in levels)
            width = span_high - span_low
            factor_key = re.sub(r"\s+", "", normalize(factor))
            for block in index.dmap.blocks:
                if block.region not in DESIGN_REGIONS or block.block_type == "Table":
                    continue
                if factor_key not in re.sub(r"\s+", "", normalize(block.text)):
                    continue
                for low, high, text in _ranges(block.text):
                    if low < span_low - width or high > span_high + width:
                        continue                      # another scale
                    if (low, high) == (span_low, span_high) or any((low, high) == (a, b) for a, b, _ in levels):
                        continue                      # agrees with the table
                    for a, b, label in levels:
                        comparable = (high - low) <= 2 * (b - a) + 1e-9
                        if comparable and ((low == a) != (high == b)):
                            key = (factor_key, "class", re.sub(r"[\s–—-]+", "-", label), text)
                            if key not in seen:
                                seen.add(key)
                                conflicts.append(Conflict(factor, "class_bound_differs", label, table, text, block.anchor,
                                                          note=f"table class {label!r}, text {text!r}"))
                    if (high - low) >= 0.8 * width:
                        key = (factor_key, "span", text)
                        if key not in seen:
                            seen.add(key)
                            conflicts.append(Conflict(factor, "overall_range_differs",
                                                      f"{span_low:g}-{span_high:g} (span of the table's classes)", table,
                                                      text, block.anchor))
    return conflicts


# --------------------------------------------------------------------------- #
# The design artifact
# --------------------------------------------------------------------------- #

def design_summary(classifications: dict[str, Any], index: EvidenceIndex) -> dict[str, Any]:
    """Factors and levels per table, each table's layout, sample-size evidence and conflicts -- the run artifact the
    later schema decisions (Study design fields, pooled rows, conflicts) will map into the IR."""
    tables = []
    factors: dict[str, dict[str, Any]] = {}
    for key, classification in classifications.items():
        layout = "main_effects" if is_main_effect_layout(classification) else "cells"
        tables.append({"table": key, "anchors": classification.table_anchors, "role": classification.table_role,
                       "layout": layout, "factors": [f.name for f in classification.factors or []]})
        for f in classification.factors or []:
            levels = [v for v in (_levels(classification, f.name)) if not is_statistic_label(v)]
            if not levels:
                continue                                  # a column of test results, not a factor of the design
            entry = factors.setdefault(f.name, {"dimension": f.dimension, "levels": [], "tables": []})
            entry["tables"].append(key)
            for value in levels:
                if value not in entry["levels"]:
                    entry["levels"].append(value)
    sample_size = [
        {"anchor": b.anchor, "matches": list(index.signals[b.anchor].get("statistic", ()))}
        for b in index.dmap.blocks if b.region == "methods" and any(
            re.match(r"(n\s*=|\(\d)", m) for m in index.signals[b.anchor].get("statistic", ()))
    ]
    conflicts = factor_range_conflicts(classifications.values(), index)
    consistency = factor_consistency(classifications)
    for table in tables:
        if table["table"] in consistency.withheld:
            table["withheld"] = consistency.withheld[table["table"]]["kind"]
    return {"factors": factors, "factor_consistency": consistency.to_artifact(), "tables": tables, "sample_size_evidence": sample_size,
            "conflicts": [asdict(c) for c in conflicts]}


# --------------------------------------------------------------------------- #
# Cross-table factor consistency (F2)
# --------------------------------------------------------------------------- #
#
# Real failure (Philippe-2007-Six, run 20260926T162813_56de01a6): the retried classification of Table 3 (b:0193) put
# the PAR classes, the years and the P-value rows into ONE factor "Condition" of dimension "treatment". Tables 1-2 of
# the same paper declare "PAR t" (treatment) and "Year" (time) with exactly those levels. Trusted as is, "Year 2004"
# became a ready Treatment, "PARt 0-0.1" a second copy of Table 1's "0-0.1", and the table's main-effect layout was
# invisible. The paper's own tables are the evidence used here -- a level is read as another factor's level only when
# its text is that factor's NAME followed by one of that factor's LEVELS as declared somewhere in the paper (or, for
# time, a calendar year). Nothing is ever re-dimensioned: a table whose treatment factor is shown to hold another
# factor's levels is WITHHELD (its Treatments and Observations are not derived) with the finding recorded.

_TREATMENT_LIKE = ("treatment",)
_REGISTERED_DIMENSIONS = ("treatment", "time", "site", "crop")
_YEAR_RE = re.compile(r"^(?:1[89]|20)\d\d$")
_TIME_LEVEL_RE = re.compile(r"^(?:years?|yr|season|harvest year)\W*((?:1[89]|20)\d\d)$|^((?:1[89]|20)\d\d)$", re.IGNORECASE)


def _key(text: Any) -> str:
    """Space- and punctuation-insensitive: "PAR t", "PARt" and "PAR_t" are one name."""
    return re.sub(r"[^a-z0-9]+", "", normalize(str(text or "")).lower())


def _level_key(text: Any) -> str:
    return " ".join(re.sub(r"[^a-z0-9.]+", " ", normalize(str(text or "")).lower()).split())


def _levels(classification: Any, name: str) -> list[str]:
    out: list[str] = []
    for row in classification.row_groups or []:
        value = (row.factor_values or {}).get(name)
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value)
    for column in getattr(classification, "value_columns", None) or []:
        value = (getattr(column, "factor_levels", None) or {}).get(name)
        if isinstance(value, str) and value.strip() and value not in out:
            out.append(value)
    return out


@dataclass
class RegisteredFactor:
    name: str                                  # first spelling seen
    dimensions: set[str] = field(default_factory=set)
    levels: set[str] = field(default_factory=set)       # _level_key of every level
    tables: list[str] = field(default_factory=list)


@dataclass
class LevelReading:
    factor: str            # the registered factor whose NAME prefixes the level
    dimension: str
    level: str             # the remainder, as that factor's level
    evidence: str


@dataclass
class FactorConsistency:
    registry: dict[str, RegisteredFactor]
    withheld: dict[str, dict[str, Any]] = field(default_factory=dict)       # table key -> finding
    level_aliases: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)   # table -> factor -> {level: level}

    def to_artifact(self) -> dict[str, Any]:
        return {
            "registry": {k: {"name": f.name, "dimensions": sorted(f.dimensions), "levels": sorted(f.levels),
                             "tables": f.tables} for k, f in self.registry.items()},
            "withheld_tables": self.withheld,
            "level_aliases": self.level_aliases,
        }

    def canonical_level(self, table: str, factor: str, level: str) -> str:
        """The level with another table's spelling of the SAME treatment factor removed ("PARt 0-0.1" -> "0-0.1")."""
        return self.level_aliases.get(table, {}).get(factor, {}).get(level, level)


def _register(registry: dict[str, RegisteredFactor], table: str, name: str, dimension: str, levels: list[str]) -> None:
    entry = registry.setdefault(_key(name), RegisteredFactor(name))
    entry.dimensions.add(dimension)
    entry.levels |= {_level_key(v) for v in levels if not is_statistic_label(v)}
    if table not in entry.tables:
        entry.tables.append(table)


def read_level(level: str, own_factor: str, registry: dict[str, RegisteredFactor]) -> Optional[LevelReading]:
    """`level` read as "<name of another registered factor> <one of that factor's levels>", or None.

    The name must end on a boundary (never inside a word: factor "N" does not prefix "Nitrate"), and the remainder
    must be a level the paper declares for that factor -- or, for a time factor, a calendar year."""
    text = normalize(str(level or "")).strip()
    own = _key(own_factor)
    best: Optional[LevelReading] = None
    for key, entry in registry.items():
        if not key or key == own or not (entry.dimensions & set(_REGISTERED_DIMENSIONS)):
            continue
        # walk the level text consuming the factor name's characters, ignoring spaces/punctuation
        i = j = 0
        while i < len(text) and j < len(key):
            ch = text[i].lower()
            if not ch.isalnum():
                i += 1
                continue
            if ch != key[j]:
                break
            i += 1
            j += 1
        if j < len(key) or (i < len(text) and text[i].isalpha()):
            continue
        rest = text[i:].strip(" :=-–—_")
        if not rest:
            continue
        dimension = sorted(entry.dimensions & set(_REGISTERED_DIMENSIONS))[0] if len(entry.dimensions) == 1 else None
        if dimension is None:
            # declared with several dimensions across tables: a treatment reading never wins over a non-treatment one
            non_treatment = sorted(entry.dimensions - set(_TREATMENT_LIKE))
            dimension = non_treatment[0] if non_treatment else "treatment"
        known = _level_key(rest) in entry.levels
        year = dimension == "time" and bool(_YEAR_RE.match(rest))
        if not (known or year):
            continue
        reading = LevelReading(entry.name, dimension, rest,
                               f"{level!r} is {entry.name!r} ({dimension}, declared in {', '.join(entry.tables)}) = {rest!r}")
        if best is None or len(_key(entry.name)) > len(_key(best.factor)):
            best = reading
    return best


def factor_consistency(classifications: dict[str, Any]) -> FactorConsistency:
    """Check every treatment factor of every table against the factors the paper's tables declare.

    - A level that reads as another TREATMENT factor's level ("PARt 0-0.1" where Table 1 declares "PAR t" = "0-0.1")
      is the same Treatment: recorded as a level alias, so identity compares "0-0.1" with "0-0.1".
    - A level that reads as a NON-treatment factor's level ("Year 2004", Year = time elsewhere) is not a Treatment.
      The table is WITHHELD -- never re-dimensioned: which of its values belong to which factor is exactly what the
      classification got wrong. When its treatment levels read as two or more different factors it is a mixed factor.
    - A calendar year ("2004", "Year 2004") as a treatment level is time, the same way, even with no other table to
      cite.
    Statistic labels ("P value PAR t") are ignored here: they are never levels (the candidate builders drop them)."""
    registry: dict[str, RegisteredFactor] = {}
    for table, classification in classifications.items():
        for f in classification.factors or []:
            _register(registry, table, f.name, f.dimension, _levels(classification, f.name))
    result = FactorConsistency(registry)
    for table, classification in classifications.items():
        for f in classification.factors or []:
            if f.dimension != "treatment":
                continue
            readings: dict[str, Optional[LevelReading]] = {}
            for level in _levels(classification, f.name):
                if is_statistic_label(level) or SUMMARY_ROW_RE.match(level.strip()):
                    continue
                reading = read_level(level, f.name, registry)
                if reading is None and _TIME_LEVEL_RE.match(normalize(level).strip()):
                    reading = LevelReading("(calendar year)", "time", level.strip(),
                                           f"{level!r} is a calendar year: time, not a treatment level")
                readings[level] = reading
            non_treatment = {lv: r for lv, r in readings.items() if r and r.dimension != "treatment"}
            groups = sorted({r.factor for r in readings.values() if r} | ({f.name} if any(r is None for r in readings.values()) else set()))
            if non_treatment:
                result.withheld[table] = {
                    "kind": "mixed_factor" if len(groups) > 1 else "dimension_conflict",
                    "factor": f.name,
                    "groups": groups,
                    "non_treatment_levels": {lv: asdict(r) for lv, r in non_treatment.items()},
                    "reason": (f"treatment factor {f.name!r} holds level(s) the paper declares for a non-treatment factor "
                               f"({'; '.join(r.evidence for r in non_treatment.values())}); its Treatments and "
                               f"Observations are withheld rather than derived from a misread structure"),
                }
                break
            aliases = {lv: r.level for lv, r in readings.items() if r and r.dimension == "treatment"}
            if aliases:
                result.level_aliases.setdefault(table, {})[f.name] = aliases
    return result
