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
FUSED_RESULTS = re.compile(r"^\s*(\d+(\.\d+)*\.?\s*)?RESULTS(\s+AND\s+DISCUSSION)?\s+\S")
FUSED_AFTER = re.compile(r"^\s*(\d+(\.\d+)*\.?\s*)?(DISCUSSION|CONCLUSIONS?|SUMMARY|ACKNOWLEDG(E)?MENTS?)\s+\S")
"""A heading set in capitals that the layout ran into its first paragraph ("RESULTS Succession by site type …")."""


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
    uncovered = _uncovered_results_numbers(draft, doc)
    issues += uncovered
    if uncovered and not observed:
        first = uncovered[0].target
        issues.append(
            blocking(
                "coverage",
                first,
                "the Results report numbers and no record holds an Observation; record the experiment values they "
                f"report (the warnings list them, from {first})",
            )
        )
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
    r"|\b0?\.\d+(?:\s*(?:,|and|or)\s*0?\.\d+)*\s+(?:probability\s+|significance\s+)?levels?\b"
    r"|\b(?:Figs?|Figures?|Tables?)\.?\s*\d+[a-z]?"
    r"|\b[nN]\s*=\s*\d+"
    rf"|{_COUNTS}\s*(?:DA[PSETH]\b|d(?:ays?)?\s+(?:after|before)\b|w(?:ee)?ks?\s+(?:after|before)\b)"
    rf"|\bDA[PSETH]\s*{_COUNTS}"
    r"|(?<=[A-Za-z])\s*-\s*[1-4]\b"
    r"|\b\d+\s*(?:-|to)\s*\d+[\s-]*(?:cm|mm|m|yrs?|years?|wks?|weeks?|d|days?)\b"
    r"|\b(?:doi\.org/|doi:\s*)?10\.\d{4,}/\S+"
    r"|\[\s*\d+(?:\s*[,-]\s*\d+)*\s*\]"
)
"""Numbers that point somewhere rather than report a value: a test ("P <0.05", "the 0.05 and 0.01 levels"), a
figure or table, a sample size, a time or a list of times ("77 and 100 DAP", "DAP -110, -32"), a unit's exponent
("m-2"), a depth layer or span of whole numbers ("0-15-cm", "0 to 15 cm", "3-5 yrs", but not "1.2 to 1.5 m", a
reported value), a citation ("[21]", "[24,29]") or a DOI. The text is normalised first, so every dash is "-"."""
_SENTENCE = re.compile(r"(?<=[.;!?])\s+(?=[A-Z(])")
_WORD = re.compile(r"[A-Za-z]{2,}")
MIN_WORDS = 4
"""A block with fewer words is a label (an axis title, a tick label, a heading), not a sentence that reports a value."""


def _uncovered_results_numbers(draft: Draft, doc: Document) -> list[Issue]:
    """Numbers in the Results section that no record quotes from their own block (or from a table it cites), one
    warning per block, which a mark on that block answers. A warning only: many are tests and percentages."""
    by_anchor: dict[str, set[str]] = {}
    tables: set[str] = set()
    for r in draft.records.values():
        for f in fields_of(r).values():
            for ev in f.evidence:
                by_anchor.setdefault(ev.anchor, set()).update(numbers_in(ev.quote))
                resolved = doc.resolve(ev.anchor)
                if resolved is not None and ".t" in ev.anchor:
                    tables.update(numbers_in(resolved.text))
    issues: list[Issue] = []
    in_results = False
    for b in doc.blocks:
        if b.kind in ("furniture", "references", "table", "caption"):
            continue
        text = norm(b.text)
        if RESULTS.match(text):
            in_results = True
            continue
        if AFTER_RESULTS.match(text) or FUSED_AFTER.match(text):
            in_results = False
        if FUSED_RESULTS.match(text):
            in_results = True
        if not in_results or len(_WORD.findall(text)) < MIN_WORDS:
            continue
        cited = by_anchor.get(b.anchor, set()) | tables
        found: list[tuple[str, list[str]]] = []
        for sentence in _SENTENCE.split(text):
            missing = [n for n in numbers_in(_REFERENCES.sub(" ", sentence)) if n not in cited and not _boring(n)]
            if missing:
                found.append((sentence, list(dict.fromkeys(missing))))
        if not found:
            continue
        shown = "; ".join(f"'{_clip(s)}' ({', '.join(m)})" for s, m in found)
        issues.append(
            warning(
                "coverage",
                b.anchor,
                f"Results numbers no record cites: {shown}. Record the experiment values in "
                f"records/observations/text.yaml; mark {b.anchor} in records/unresolved.yaml only for what isn't one, "
                "with ambiguous when the paper doesn't tie it to one treatment, or schema_limitation when the records "
                "can't hold it",
            )
        )
    return issues


def _clip(sentence: str, limit: int = 160) -> str:
    return sentence if len(sentence) <= limit else sentence[: limit - 1] + "…"


def _boring(n: str) -> bool:
    """Years, single digits and dates ("15.05.1999")."""
    read = read_number(n)
    if read is None:
        return True
    v = read[0]
    return v.is_integer() and (0 <= v < 10 or 1900 <= v <= 2100)
