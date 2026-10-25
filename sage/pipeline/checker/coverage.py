"""Coverage level: nothing the paper reports is silently skipped."""

import re

from sage.domain.cell_values import parse_cell
from sage.domain.normalize import norm, numbers_in, read_number
from sage.ir.draft import Draft
from sage.ir.schema import Observation, Study, Treatment, Variable, fields_of
from sage.parse.document import Document
from sage.pipeline.checker.issues import Issue, blocking, warning

RESULTS = re.compile(r"^\s*(\d+(\.\d+)*\.?\s*)?results(\s+and\s+discussion)?\s*$", re.IGNORECASE)
AFTER_RESULTS = re.compile(
    r"^\s*(\d+(\.\d+)*\.?\s*)?(discussion|conclusions?|summary|acknowledg(e)?ments?)\s*$", re.IGNORECASE
)


def check_coverage(draft: Draft, doc: Document) -> list[Issue]:
    issues = [
        blocking(
            "coverage",
            table.anchor,
            f"{table.label} is neither mapped nor marked irrelevant; transcribe it into tables/{table.anchor}.csv and "
            f"map its values in records/observations/{table.anchor}.yaml, or mark it there with irrelevant: reason if "
            "it holds no experiment values",
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
                    "no Observation measures this Variable; map its values in records/observations/, or mark it in "
                    f"records/unresolved.yaml ({rid}: {{cause: figure_only, reason: …}} if its values are only in a "
                    "figure)",
                )
            )
    issues += _studies_without_treatments(draft)
    issues += _unmapped_rows(draft, doc)
    issues += _uncovered_results_numbers(draft, doc)
    return issues


def _studies_without_treatments(draft: Draft) -> list[Issue]:
    """Every experiment compares something, or runs one condition: either way it has a Treatment, and the export's
    treatments tab is built from them."""
    held = {r.study_id.value for r in draft.records.values() if isinstance(r, Treatment)}
    return [
        blocking(
            "coverage",
            rid,
            "no Treatment belongs to this Study; write each treatment it compares in records/treatments.yaml, with "
            "its factor_levels, or one Treatment for its single condition",
        )
        for rid, r in draft.records.items()
        if isinstance(r, Study) and rid not in held and draft.mark(rid) is None
    ]


def _unmapped_rows(draft: Draft, doc: Document) -> list[Issue]:
    """Rows of a mapped table that hold numbers but give no Observation and weren't skipped: values left out
    without a word."""
    issues: list[Issue] = []
    for decision in draft.tables.values():
        table = doc.table(decision.table_id)
        if decision.status != "expanded" or table is None or not table.rows:
            continue
        mapped = set(decision.rows)
        skipped = set(decision.skipped)
        left = [
            (i, row)
            for i, row in enumerate(table.rows)
            if i >= table.header_rows
            and i not in mapped
            and f"{table.anchor}.r{i}" not in skipped
            and any(parse_cell(c.text).numeric is not None for c in row)
        ]
        if not left:
            continue
        shown = ", ".join(f"r{i} '{_clip(next((c.text for c in row if c.text.strip()), ''))}'" for i, row in left[:6])
        more = f" and {len(left) - 6} more" if len(left) > 6 else ""
        skips = ", ".join(f"{table.anchor}.r{i}" for i, _ in left[:3])
        issues.append(
            blocking(
                "coverage",
                table.anchor,
                f"{table.label} rows hold numbers that no observation maps: {shown}{more}; map them under rows, or "
                f"add rows that hold no experiment values (an LSD or totals row) to skip: [{skips}]",
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
    r"|\[\s*\d+(?:\s*,\s*\d+)+\s*\]"
)
"""Numbers that point somewhere rather than report a value: a test ("P <0.05"), a figure or table, a sample size, a
time or a list of times ("77 and 100 DAP", "DAP -110, -32"), a unit's exponent ("m-2"), a citation list ("[24,29]")."""
_SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(])")
_WORD = re.compile(r"[A-Za-z]{2,}")
MIN_WORDS = 4
"""A block with fewer words is a label (an axis title, a tick label, a heading), not a sentence that reports a value."""


def _uncovered_results_numbers(draft: Draft, doc: Document) -> list[Issue]:
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
    shown = "; ".join(f"{a} '{_clip(s)}' ({', '.join(m)})" for a, s, m in found)
    return [
        warning(
            "coverage",
            "results",
            f"{total} numbers in {len(found)} Results sentences are cited by no record: {shown}. Record the ones "
            "that are experiment values; for the rest, add results: {cause, reason} to records/unresolved.yaml, with "
            "ambiguous when the paper doesn't tie them to one treatment, or schema_limitation when the IR can't hold "
            "them",
        )
    ]


def _clip(sentence: str, limit: int = 160) -> str:
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def _boring(n: str) -> bool:
    """Years, single digits and dates ("15.05.1999")."""
    read = read_number(n)
    if read is None:
        return True
    v = read[0]
    return v.is_integer() and (0 <= v < 10 or 1900 <= v <= 2100)
