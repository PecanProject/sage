"""Coverage level: nothing the paper reports is silently skipped."""

import re

from sage.domain.normalize import norm, numbers_in, squash
from sage.ir.draft import Draft
from sage.ir.schema import Observation, Treatment, Variable, fields_of
from sage.parse.document import Document
from sage.pipeline.checker.issues import Issue, blocking, warning

RESULTS = re.compile(r"^\s*(\d+(\.\d+)*\.?\s*)?results(\s+and\s+discussion)?\s*$", re.IGNORECASE)
AFTER_RESULTS = re.compile(
    r"^\s*(\d+(\.\d+)*\.?\s*)?(discussion|conclusions?|summary|acknowledg(e)?ments?)\s*$", re.IGNORECASE
)


def check_coverage(draft: Draft, doc: Document, *, cap: int) -> list[Issue]:
    issues = [
        blocking(
            "coverage",
            table.anchor,
            f"{table.label} is neither expanded nor marked irrelevant; call expand_table, or "
            "expand_table with irrelevant_reason if it holds no experiment values",
        )
        for table in doc.tables
        if table.anchor not in draft.tables
    ]
    observed = {r.variable_id for r in draft.records.values() if isinstance(r, Observation)}
    for rid, r in draft.records.items():
        if isinstance(r, Variable) and rid not in observed and draft.mark(rid) is None:
            issues.append(
                blocking(
                    "coverage",
                    rid,
                    "no Observation measures this Variable; write its observations, or mark it unresolved "
                    "(figure_only if the values are only in a figure)",
                )
            )
    issues += _factor_levels(draft, doc)
    issues += _uncovered_results_numbers(draft, doc, cap=cap)
    return issues


def _factor_levels(draft: Draft, doc: Document) -> list[Issue]:
    levels: dict[str, set[str]] = {}
    for r in draft.records.values():
        if isinstance(r, Treatment):
            for factor, level in r.factor_levels.items():
                levels.setdefault(squash(factor).lower(), set()).add(squash(level).lower())
    issues: list[Issue] = []
    for decision in draft.tables.values():
        table = doc.table(decision.table_id)
        if decision.roles is None or table is None:
            continue
        header = decision.roles.header_rows if decision.roles.header_rows is not None else table.header_rows
        for col in decision.roles.columns:
            if col.role != "factor" or col.factor is None:
                continue
            known = levels.get(squash(col.factor).lower(), set())
            for i, row in enumerate(table.rows[header:], start=header):
                if i in decision.roles.skip_rows or col.col >= len(row):
                    continue
                level = row[col.col].text
                if level.strip() and squash(level).lower() not in known:
                    issues.append(
                        blocking(
                            "coverage",
                            decision.table_id,
                            f"factor '{col.factor}' level '{level}' ({row[col.col].anchor}) has no Treatment; write "
                            f"one with factor_levels {{'{col.factor}': '{level}'}}",
                        )
                    )
    return issues


_COUNTS = r"[-+]?\s*\d+(?:\s*(?:,\s*(?:and\s+)?|and\s+|&\s*)[-+]?\s*\d+)*"
_REFERENCES = re.compile(
    r"(?<![A-Za-z])[Pp]\s*[<>=≤≥]\s*[-+]?\d*[.,]?\d+"
    r"|\b(?:Figs?|Figures?|Tables?)\.?\s*\d+[a-z]?"
    r"|\b[nN]\s*=\s*\d+"
    rf"|{_COUNTS}\s*(?:DA[PSETH]\b|d(?:ays?)?\s+(?:after|before)\b|w(?:ee)?ks?\s+(?:after|before)\b)"
    rf"|\bDA[PSETH]\s*{_COUNTS}"
    r"|(?<=[A-Za-z])\s*-\s*[1-4]\b"
)
"""Numbers that point somewhere rather than report a value: a test ("P <0.05"), a figure or table, a sample size, a
time or a list of times ("77 and 100 DAP", "DAP -110, -32"), a unit's exponent ("m-2")."""
_SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(])")
_WORD = re.compile(r"[A-Za-z]{2,}")
MIN_WORDS = 4
"""A block with fewer words is a label (an axis title, a tick label, a heading), not a sentence that reports a value."""


def _uncovered_results_numbers(draft: Draft, doc: Document, *, cap: int) -> list[Issue]:
    """Numbers in the Results section that no record quotes from their own block (or from a table it cites), shown in
    their sentence. A warning only: many are tests and percentages."""
    by_anchor: dict[str, set[str]] = {}
    tables: set[str] = set()
    for r in draft.records.values():
        for f in fields_of(r).values():
            for ev in f.evidence:
                by_anchor.setdefault(ev.anchor, set()).update(numbers_in(ev.quote))
                resolved = doc.resolve(ev.anchor)
                if resolved is not None and ".t" in ev.anchor:
                    tables.update(numbers_in(resolved.text))
    found: list[tuple[str, str, list[str]]] = []
    in_results = False
    for b in doc.blocks:
        if b.kind in ("furniture", "references", "table", "caption"):
            continue
        text = norm(b.text)
        if RESULTS.match(text):
            in_results = True
            continue
        if AFTER_RESULTS.match(text):
            in_results = False
        if not in_results or len(_WORD.findall(text)) < MIN_WORDS:
            continue
        cited = by_anchor.get(b.anchor, set()) | tables
        for sentence in _SENTENCE.split(text):
            missing = [n for n in numbers_in(_REFERENCES.sub(" ", sentence)) if n not in cited and not _boring(n)]
            if missing:
                found.append((b.anchor, sentence, list(dict.fromkeys(missing))))
    if not found:
        return []
    total = sum(len(m) for _, _, m in found)
    shown = "; ".join(f"{a} '{_clip(s)}' ({', '.join(m)})" for a, s, m in found[:cap])
    more = f"; and {len(found) - cap} more sentences" if len(found) > cap else ""
    return [
        warning(
            "coverage",
            "results",
            f"{total} numbers in {len(found)} Results sentences are in no record's evidence: {shown}{more}. Record the "
            "ones that are experiment values; mark one the paper doesn't tie to a single treatment ambiguous, or one "
            "the IR can't hold schema_limitation",
        )
    ]


def _clip(sentence: str, limit: int = 160) -> str:
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def _boring(n: str) -> bool:
    """Years and single digits."""
    try:
        v = float(n)
    except ValueError:
        return True
    return v.is_integer() and (0 <= v < 10 or 1900 <= v <= 2100)
