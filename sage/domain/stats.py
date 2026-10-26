"""What a table cell's dispersion, letter groups and scope are, from the header, caption or note that states it."""

import re
from collections.abc import Iterable
from typing import NamedTuple

from sage.domain.cell_values import CellValue, parse_cell
from sage.domain.normalize import contains_loose, norm
from sage.ir.schema import EffectScope, Evidence, Observation, QuantityValue

_DISPERSION_NAMES = (
    ("SE", re.compile(r"±\s*s\.?e\.?(?:m\.?)?\b|\bs\.?e\.?m?\b|standard errors?", re.IGNORECASE)),
    ("SD", re.compile(r"±\s*s\.?d\.?\b|\bs\.?d\.?\b|standard deviations?", re.IGNORECASE)),
    ("95% CI", re.compile(r"95\s*%\s*(?:confidence (?:intervals?|limits?)|c\.?i\.?)", re.IGNORECASE)),
    ("CI", re.compile(r"confidence (?:intervals?|limits?)", re.IGNORECASE)),
)
_LETTERS_MEANING = re.compile(r"(?:different|same) letters?[^.]{0,120}(?:significant|differ)\w*", re.IGNORECASE)
_MEANS = re.compile(r"\b(?:means?|averages?|averaged)\b", re.IGNORECASE)
_COUNT = r"(?:all|both|each|the|two|three|four|five|six|seven|eight|nine|ten|\d+)"
_ACROSS = rf"(?:over|across|regardless of|irrespective of|(?:for|of) (?:all|both|the {_COUNT}))"
_NUMBER_WORD = r"(?:\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_NOT_A_FACTOR = (
    rf"(?!(?:\s+{_COUNT})*\s+(?:replicat\w*|reps?|blocks?|plots?|samples?|subsamples?|plants?|measurements?"
    r"|observations?|readings?|treatments?)\b)"
)
_SPAN = rf"(?!\s+(?:the\s+)?{_NUMBER_WORD}[\s-]*(?:years?|yrs?|seasons?|months?|weeks?|days?)\b)"
_POOLING = rf"\b(?:means?|averages?|averaged|pooled|combined)\b[^.;]{{0,60}}?\b{_ACROSS}\b{_NOT_A_FACTOR}"
_BARE_POOLED = r"\bpooled\b(?!\s+(?:s\.?e|standard|error|variance|s\.?d|lsd|cv)\b)"
_POOLED = re.compile(f"{_POOLING}|{_BARE_POOLED}", re.IGNORECASE)
"""Values pooled over a factor, as papers word it: "means over two cultivars", "averaged across years", "the mean ±
SE for all cultivar mixture treatments", "data were combined over locations", "means for both years", "pooled".
Averaging over replicates, blocks, plots or samples is how any treatment mean is made, so it pools nothing; "means
for all treatments" lists every treatment's mean; and a "pooled SE" names a statistic, not a pooling of the values."""
_POOLED_FACTOR = re.compile(f"{_POOLING}{_SPAN}|{_BARE_POOLED}", re.IGNORECASE)
"""Pooling that rules out a treatment mean. A counted span ("mean inputs over eight years") may only be the period
the value covers, which its time states, so it still supports a mean over years but does not rule out a treatment
mean."""
_RAW = re.compile(
    r"\b(?:individual|single)\s+(?:plot|replicate|rep|sample|plant)s?\b|\braw\s+(?:data|values)\b", re.IGNORECASE
)
"""Replicate values, as papers word them. "Per plot" and "each plot" are not: they name a unit basis ("seeds per
plot") or the layout ("each plot was 3 m wide")."""
_SPREAD_SIGN = re.compile(r"±|\+/-|\+-")
_TEST = re.compile(r"(?<![A-Za-z])[Pp]\s*[<>=≤≥]\s*0?[.,]\d")
"""A significance test ("P <0.05", "P = 0.06"): what it compares are means of replicates, not replicate values."""
_SCOPE_WORDS = {
    "treatment_mean": "'mean' or 'average' with no pooling word",
    "aggregated_mean": "'over', 'across', 'for all', 'of all', 'averaged', 'pooled' or 'combined'",
}
"""What code looks for in the cited text, per scope, so a reason can say it."""


_STATNAMES = (
    ("SE", re.compile(r"^(?:s\.?e\.?(?:m\.?)?|standard errors?(?: of the mean)?)$", re.IGNORECASE)),
    ("SD", re.compile(r"^(?:s\.?d\.?|standard deviations?)$", re.IGNORECASE)),
    ("MSE", re.compile(r"^(?:mse|mean squared? errors?)$", re.IGNORECASE)),
    ("CI", re.compile(r"\bc\.?i\.?\b|confidence (?:intervals?|limits?)", re.IGNORECASE)),
    ("LSD", re.compile(r"^(?:lsd|least significant difference)(?![a-z])", re.IGNORECASE)),
    ("HSD", re.compile(r"\bhsd\b|^tukey", re.IGNORECASE)),
    ("MSD", re.compile(r"^(?:msd|minimum significant difference)(?![a-z])", re.IGNORECASE)),
    ("P", re.compile(r"^p(?:[\s-]*values?)?$", re.IGNORECASE)),
)


def statname(name: str) -> str | None:
    """The calibration protocol's statname (SE, SD, MSE, CI, LSD, HSD, MSD or P) for a statistic as the paper names
    it; None for one it doesn't list, such as a letter group."""
    name = norm(name)
    return next((code for code, pattern in _STATNAMES if pattern.search(name)), None)


def dispersion(cell: CellValue) -> float | str | None:
    if cell.interval_text is not None:
        return cell.interval_text
    return cell.plus_minus if cell.plus_minus is not None else cell.parenthetical


def dispersion_statistic(cell: CellValue, sources: Iterable[Evidence]) -> tuple[str, Evidence] | None:
    """What the cell's "±" or bracketed value is, from the first header, caption or note that states it. The returned
    Evidence quotes just the statement within its source. An interval pairs only with a CI name and a "±" or
    parenthetical only with SE or SD; otherwise the dispersion stays unnamed."""
    if cell.interval is not None:
        wanted = ("95% CI", "CI")
    elif cell.plus_minus is not None or cell.parenthetical is not None:
        wanted = ("SE", "SD")
    else:
        return None
    sources = list(sources)
    for name, pattern in _DISPERSION_NAMES:
        if name not in wanted:
            continue
        for source in sources:
            if match := pattern.search(source.quote):
                return name, Evidence(anchor=source.anchor, quote=match[0])
    return None


def letters_meaning(sources: Iterable[Evidence]) -> Evidence | None:
    """The note that says what the mean-separation letters mean ("means followed by different letters differ ...")."""
    for source in sources:
        if match := _LETTERS_MEANING.search(source.quote):
            return Evidence(anchor=source.anchor, quote=match[0])
    return None


def scope_statement(scope: EffectScope, factors: Iterable[str], sources: Iterable[Evidence]) -> Evidence | None:
    """The cited text that says what the values are means of. Treatment means need a quote that says "means" while no
    quote pools them; means over factors need a quote that pools them and names every factor."""
    factors, sources = list(factors), list(sources)
    if scope == "treatment_mean":
        pooled = any(_says(_POOLED_FACTOR, s) for s in sources)
        return None if pooled else next((s for s in sources if _says(_MEANS, s)), None)
    pooled = [s for s in sources if _says(_POOLED, s)]
    return next((s for s in pooled if all(_names(s.quote, f) for f in factors)), None)


PLACES = frozenset({"site", "sites", "location", "locations", "country", "countries", "environment", "environments"})
"""Words for pooling over places. An Observation has one site, so a mean over several says so in its factor list,
under any of these names, and a quote may use any of them ("averaged across two environments" for `site`)."""


def _names(quote: str, factor: str) -> bool:
    if factor.lower() in PLACES:
        return any(contains_loose(quote, place) for place in PLACES)
    return contains_loose(quote, factor)


def scope_reason(scope: EffectScope, factors: Iterable[str], sources: Iterable[Evidence]) -> str:
    """Why `scope_statement` found nothing: what code looked for, and the start of what the cited text says."""
    named = list(factors)
    wanted = _SCOPE_WORDS[scope] + (f" naming {', '.join(repr(f) for f in named)}" if named else "")
    said = "; ".join(f"{s.anchor} '{_clip(s.quote)}'" for s in sources) or "nothing"
    return f"code looked for {wanted} and the cited text is {said}"


def _clip(text: str, limit: int = 100) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ReplicateLevel(NamedTuple):
    raw: bool
    evidence: list[Evidence]


def replicate_level(
    value: QuantityValue, value_evidence: list[Evidence], cited: list[Evidence], replicate: list[Evidence]
) -> ReplicateLevel | None:
    """Whether the paper says a value is a replicate value or a mean, from its own text and the quotes its record
    cites: a "±" or bracketed dispersion or a letter group in the value, the replicate it comes from (`replicate`, the
    evidence of its replicate_id: a plot's mean of subsamples is still that replicate's value), "individual plot"
    wording, "mean" or "average" wording, or a significance test such as the P value cited as its statistic. None when
    none of these is there, or when the quotes say both ("means of individual plot values")."""
    cell = parse_cell(value.reported_text)
    letters = cell.letters is not None and cell.letters.lower() != value.reported_units.strip().lower()
    if dispersion(cell) is not None or _SPREAD_SIGN.search(value.reported_text) or letters:
        return ReplicateLevel(raw=False, evidence=value_evidence)
    if replicate:
        return ReplicateLevel(raw=True, evidence=replicate)
    quotes = [*value_evidence, *cited]
    raw = next((e for e in quotes if _says(_RAW, e)), None)
    mean = next((e for e in quotes if _says(_MEANS, e)), None) or next((e for e in quotes if _says(_TEST, e)), None)
    if raw is not None and mean is not None:
        return None
    if raw is not None:
        return ReplicateLevel(raw=True, evidence=[raw])
    if mean is not None:
        return ReplicateLevel(raw=False, evidence=[mean])
    return None


def named_replicate(o: Observation) -> list[Evidence]:
    """The evidence of the replicate an Observation names, if it names one."""
    rep = o.replicate_id
    return rep.evidence if rep is not None and rep.value is not None else []


def has_dispersion(value: QuantityValue, evidence: Iterable[Evidence]) -> bool:
    """Whether the value or the text it was read from shows a dispersion, so a statistic is there to name."""
    if dispersion(parse_cell(value.reported_text)) is not None:
        return True
    return any(_SPREAD_SIGN.search(text) for text in (value.reported_text, *(e.quote for e in evidence)))


def replicate_statement(raw: bool, sources: Iterable[Evidence]) -> Evidence | None:
    """The cited text that says the values are individual plot or replicate values (`raw`), or means."""
    pattern = _RAW if raw else _MEANS
    return next((source for source in sources if _says(pattern, source)), None)


def _says(pattern: re.Pattern[str], source: Evidence) -> bool:
    return pattern.search(norm(source.quote)) is not None
