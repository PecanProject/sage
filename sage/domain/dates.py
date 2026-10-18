"""A reported date, date range or relative timing parsed into a DateRange. The text decides, never a guess."""

import calendar
import re
from collections.abc import Callable, Iterable
from datetime import date
from typing import NamedTuple

from sage.domain.normalize import norm
from sage.ir.schema import DateRange

type Span = tuple[date, date]

_MONTHS: dict[str, int] = {
    m: i
    for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)
}
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
    r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
)
_YEAR = r"(?:1[89]|20)\d{2}"
_TO = r"\s*(?:-|to|through|until)\s*"
_AND = r"\s*(?:and|&)\s*"
_SEASON = r"(?:spring|summer|autumn|fall|winter)"
_SIGN = r"(?:(?<![\w.])(?P<sign>[-+])\s*)?"
"""A sign before a count: "-32 DAP" is 32 days before planting. A dash after a number is a range, not a sign."""


def _month(name: str) -> int:
    return _MONTHS[name.lower().rstrip(".")[:3]]


def _month_end(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _day(n: str) -> str:
    return rf"(?P<{n}>\d{{1,2}})(?:st|nd|rd|th)?"


def _m(n: str) -> str:
    return rf"(?P<{n}>{_MONTH})"


def _y(n: str = "y", *, optional: bool = True) -> str:
    return rf"(?:,?[\s-]*(?:of\s+)?(?P<{n}>{_YEAR}))" + ("?" if optional else "")


def _same(d: date) -> Span:
    return d, d


def _whole_month(year: int, month: int) -> Span:
    return date(year, month, 1), date(year, month, _month_end(year, month))


_PART_OF_MONTH = {"early": (1, 10), "mid": (11, 20), "middle": (11, 20), "late": (21, 31), "end": (21, 31)}


def _part_of_month(part: str, year: int, month: int) -> Span:
    first, last = _PART_OF_MONTH[part.lower()]
    return date(year, month, first), date(year, month, min(last, _month_end(year, month)))


def _season(y1: str, y2: str) -> Span | None:
    end = int(y2) if len(y2) == 4 else int(y1[:2] + y2)
    return (date(int(y1), 1, 1), date(end, 12, 31)) if end > int(y1) else None


type Groups = dict[str, str]
type Builder = Callable[[Groups, int], Span | None]


def _iso(g: Groups) -> date:
    return date(int(g["y1"]), int(g["m1"]), int(g["d1"]))


class _Rule(NamedTuple):
    pattern: re.Pattern[str]
    build: Builder
    needs_year: bool
    season: bool = False
    """A season states a year but no dates: which months it covers depends on the hemisphere and the paper."""


def _rule(pattern: str, build: Builder, *, needs_year: bool, season: bool = False) -> _Rule:
    return _Rule(re.compile(pattern, re.IGNORECASE), build, needs_year, season)


# A rule whose year group "y" is optional needs_year: with no year written, it takes the year the quotes state.
_DATED: tuple[_Rule, ...] = (
    _rule(
        r"\b(?P<y1>\d{4})-(?P<m1>\d{2})-(?P<d1>\d{2})" + _TO + r"(?P<y2>\d{4})-(?P<m2>\d{2})-(?P<d2>\d{2})\b",
        lambda g, _: (_iso(g), date(int(g["y2"]), int(g["m2"]), int(g["d2"]))),
        needs_year=False,
    ),
    _rule(r"\b(?P<y1>\d{4})-(?P<m1>\d{2})-(?P<d1>\d{2})\b", lambda g, _: _same(_iso(g)), needs_year=False),
    _rule(
        rf"\b{_day('d1')}[\s-]+{_m('m1')}{_TO}{_day('d2')}[\s-]+{_m('m2')}{_y()}\b",
        lambda g, y: (date(y, _month(g["m1"]), int(g["d1"])), date(y, _month(g["m2"]), int(g["d2"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_m('m1')}\s+{_day('d1')}{_TO}{_m('m2')}\s+{_day('d2')}{_y()}\b",
        lambda g, y: (date(y, _month(g["m1"]), int(g["d1"])), date(y, _month(g["m2"]), int(g["d2"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_day('d1')}{_TO}{_day('d2')}[\s-]+{_m('m1')}{_y()}\b",
        lambda g, y: (date(y, _month(g["m1"]), int(g["d1"])), date(y, _month(g["m1"]), int(g["d2"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_m('m1')}\s+{_day('d1')}{_TO}{_day('d2')}{_y()}\b",
        lambda g, y: (date(y, _month(g["m1"]), int(g["d1"])), date(y, _month(g["m1"]), int(g["d2"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_day('d1')}{_AND}{_day('d2')}[\s-]+{_m('m1')}{_y()}\b",
        lambda g, y: (date(y, _month(g["m1"]), int(g["d1"])), date(y, _month(g["m1"]), int(g["d2"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_m('m1')}\s+{_day('d1')}{_AND}{_day('d2')}{_y()}\b",
        lambda g, y: (date(y, _month(g["m1"]), int(g["d1"])), date(y, _month(g["m1"]), int(g["d2"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_day('d1')}[\s-]+{_m('m1')}{_y()}\b",
        lambda g, y: _same(date(y, _month(g["m1"]), int(g["d1"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b{_m('m1')}\s+{_day('d1')}{_y()}\b",
        lambda g, y: _same(date(y, _month(g["m1"]), int(g["d1"]))),
        needs_year=True,
    ),
    _rule(
        rf"\b(?P<part>early|mid|middle|late|end)(?:\s+of)?[\s-]+{_m('m1')}{_y()}\b",
        lambda g, y: _part_of_month(g["part"], y, _month(g["m1"])),
        needs_year=True,
    ),
    _rule(
        rf"\b{_m('m1')}\s+(?P<y1>{_YEAR}){_TO}{_m('m2')}\s+(?P<y2>{_YEAR})\b",
        lambda g, _: (
            _whole_month(int(g["y1"]), _month(g["m1"]))[0],
            _whole_month(int(g["y2"]), _month(g["m2"]))[1],
        ),
        needs_year=False,
    ),
    _rule(
        rf"\b{_m('m1')}{_TO}{_m('m2')}{_y()}\b",
        lambda g, y: (_whole_month(y, _month(g["m1"]))[0], _whole_month(y, _month(g["m2"]))[1]),
        needs_year=True,
    ),
    _rule(rf"\b{_m('m1')}{_y()}(?![a-z])", lambda g, y: _whole_month(y, _month(g["m1"])), needs_year=True),
    _rule(
        rf"\b(?P<y1>{_YEAR})(?:\s*/\s*|{_TO})(?P<y2>{_YEAR}|\d{{2}})\b",
        lambda g, _: _season(g["y1"], g["y2"]),
        needs_year=False,
    ),
    _rule(
        rf"\b(?:(?:early|mid|late)[\s-]*)?{_SEASON}(?:,?\s*(?P<y1>{_YEAR}))?"
        rf"(?:\s*(?:/|-|and|to|through)\s*(?:(?:early|mid|late)[\s-]*)?{_SEASON})*{_y()}(?![a-z])",
        lambda _g, _y: None,
        needs_year=False,
        season=True,
    ),
)
"""Every way of writing a date that places a day, a month or a season in time. A year written in one of these is a
year the text states."""
_BARE_YEAR = _rule(rf"\b(?P<y>{_YEAR})\b", lambda _, y: (date(y, 1, 1), date(y, 12, 31)), needs_year=False)
_RULES: tuple[_Rule, ...] = (*_DATED, _BARE_YEAR)


_EVENT = (
    r"(?P<event>planting|sowing|seeding|emergence|transplanting|flowering|anthesis|silking|heading|harvest(?:ing)?"
    r"|bud ?break)"
)
_UNIT_DAYS = {"d": 1, "day": 1, "days": 1, "wk": 7, "week": 7, "weeks": 7}
_ABBREVIATIONS = {"p": "planting", "s": "sowing", "e": "emergence", "t": "transplanting", "h": "harvest"}
_RELATIVE_COUNT = re.compile(
    _SIGN + r"\b(?P<n>\d+)\s*(?P<unit>d|days?|wk|weeks?|months?)\s+(?P<dir>after|before|prior to|post)\s+"
    r"(?:the\s+)?" + _EVENT,
    re.IGNORECASE,
)
_RELATIVE_ABBREVIATION = re.compile(
    _SIGN + r"\b(?:(?P<n>\d+)\s*(?P<unit>[DW])A(?P<event>[PSETH])"
    r"|(?P<unit2>[DW])A(?P<event2>[PSETH])\s*(?P<sign2>[-+])?\s*(?P<n2>\d+))\b"
)
_RELATIVE_WORD = re.compile(r"\b(?P<dir>at|before|after|prior to|pre|post)[\s-]*(?:the\s+)?" + _EVENT, re.IGNORECASE)
_DIRECTIONS = {"at": "at", "before": "before", "prior to": "before", "pre": "before", "after": "after", "post": "after"}


def _event(name: str) -> str:
    name = name.lower().replace(" ", "")
    return "harvest" if name.startswith("harvest") else name


_DATE_LIKE = re.compile(rf"\d|\b{_MONTH}(?![a-z])", re.IGNORECASE)
_FLIP = {"after": "before", "before": "after"}


def _signed(direction: str, sign: str | None) -> str:
    return _FLIP.get(direction, direction) if sign == "-" else direction


def _relative(text: str, reported_text: str) -> tuple[DateRange, re.Match[str]] | None:
    if match := _RELATIVE_COUNT.search(text):
        direction = _signed(_DIRECTIONS[match["dir"].lower()], match["sign"])
        per = _UNIT_DAYS.get(match["unit"].lower())
        days = None if per is None else int(match["n"]) * per
        timing = f"{direction}_{_event(match['event'])}"
        return DateRange(reported_text=reported_text, relative_timing=timing, relative_timing_days=days), match
    if match := _RELATIVE_ABBREVIATION.search(text):
        unit = match["unit"] or match["unit2"]
        event = _ABBREVIATIONS[(match["event"] or match["event2"]).lower()]
        days = int(match["n"] or match["n2"]) * (7 if unit == "W" else 1)
        direction = _signed("after", match["sign"] or match["sign2"])
        timing = f"{direction}_{event}"
        return DateRange(reported_text=reported_text, relative_timing=timing, relative_timing_days=days), match
    if match := _RELATIVE_WORD.search(text):
        direction = _DIRECTIONS[match["dir"].lower()]
        relative = DateRange(
            reported_text=reported_text,
            relative_timing=f"{direction}_{_event(match['event'])}",
            relative_timing_days=0 if direction == "at" else None,
        )
        return relative, match
    return None


_LINK = r"(?:-|to|through|and|&|,|or)"
_JOINED_BEFORE = re.compile(rf"(?:\d|\b{_MONTH})\s*{_LINK}\s*$", re.IGNORECASE)
_JOINED_AFTER = re.compile(rf"^\s*{_LINK}\s*[-+]?(?:\d|{_MONTH}(?![a-z]))", re.IGNORECASE)
_OPEN_BEFORE = re.compile(
    r"\b(?:before|after|until|till|by|since|pre|post|prior to|up to|within|(?:no|not) (?:later|earlier) than)[\s-]*$",
    re.IGNORECASE,
)
_OPEN_AFTER = re.compile(
    r"^\s*(?:or (?:later|earlier|before|after)|onwards?|and (?:later|after|before))\b", re.IGNORECASE
)
"""Words that make a date one end of an open span ("before 2000", "since 1990", "15 May or later")."""


def _unread(text: str, match: re.Match[str]) -> str | None:
    """The text joined to the date code read by a range or list word, when it holds another number or month ("and 19"
    in "May 18 and 19" read as "May 18", "28-" in "28-32 DAP" or "28 - 32 DAP"), or that leaves the span open
    ("before" in "before 2000"): code then reads no date at all rather than part of one. A label beside the date,
    such as "Year 1 (2005)", is not part of it."""
    before, after = text[: match.start()], text[match.end() :]
    head = text[: match.end("sign")] if match.groupdict().get("sign") is not None else before
    joined = _JOINED_BEFORE.search(head) or _JOINED_AFTER.search(after)
    if joined or _OPEN_BEFORE.search(before) or _OPEN_AFTER.search(after):
        return f"{before} … {after}".strip(" …")
    return None


_YEAR_LIST = rf"{_YEAR}(?:\s*(?:,|and|or|&|to|through|-|/)\s*{_YEAR})*(?![.,]?\d)"
_YEAR_IN_WORDS = re.compile(
    rf"\b(?:in|during|since|until|between|years?|seasons?|(?:spring|summer|autumn|fall|winter)(?:\s+of)?)\s+"
    rf"(?P<lead>{_YEAR_LIST})|\b(?P<follow>{_YEAR_LIST})\s+(?:(?:growing|cropping|field|crop)\s+)?(?:seasons?|years?)\b",
    re.IGNORECASE,
)
"""A year the words around it make one: "in 2011", "during 2010 and 2011", "the 2011 growing season". A bare number
is not, so "2000 seeds per plot" states no year."""
_FOUR_DIGITS = re.compile(r"\d{4}")


def _expression(text: str) -> tuple[re.Match[str], _Rule] | None:
    """The one date expression in `text`; several separate dates are ambiguous and never merged."""
    found = sorted(
        ((m, rule) for rule in _RULES for m in rule.pattern.finditer(text)), key=lambda f: (f[0].start(), -f[0].end())
    )
    outermost: list[tuple[re.Match[str], _Rule]] = []
    for m, rule in found:
        if not any(m.start() >= o.start() and m.end() <= o.end() for o, _ in outermost):
            outermost.append((m, rule))
    return outermost[0] if len(outermost) == 1 else None


def _absolute(text: str, year: int | None) -> Span | None:
    found = _expression(text)
    if found is None or found[1].season or _unread(text, found[0]) is not None:
        return None
    match, rule = found
    groups = {k: v for k, v in match.groupdict().items() if v is not None}
    year = int(groups["y"]) if "y" in groups else year
    if year is None and rule.needs_year:
        return None
    try:
        span = rule.build(groups, 0 if year is None else year)
    except ValueError:
        return None
    if span is None or span[0] > span[1]:
        return None
    return span


def stated_years(quotes: Iterable[str]) -> list[int]:
    """The distinct years the quotes state, in order: those written in a date ("15 May 1999", "May 1999", "2005-06")
    or that the words around them make a year ("in 1999", "the 1999 season"). A bare number such as "2000 seeds" is
    not a year."""
    years: list[int] = []
    for q in quotes:
        text = norm(q)
        for rule in _DATED:
            for m in rule.pattern.finditer(text):
                years += _written_years(m)
        for m in _YEAR_IN_WORDS.finditer(text):
            years += [int(y) for y in _FOUR_DIGITS.findall(m["lead"] or m["follow"])]
    return list(dict.fromkeys(years))


def _written_years(match: re.Match[str]) -> list[int]:
    """The years a date match writes out. A two-digit end year ("2005-06") takes the start's century, or the next one
    when it is lower ("1999-00")."""
    written = [int(v) for k in ("y", "y1", "y2") if (v := match.groupdict().get(k)) is not None]
    if not written:
        return []
    start = written[0]
    return [y if y >= 100 else start - start % 100 + y + (100 if y < start % 100 else 0) for y in written]


def parse_date_text(text: str, *, year: int | None = None) -> DateRange | None:
    """`text` as a DateRange: relative timing ("30 days after planting", "45 DAS", "at harvest"), or the interval of
    the one date it states. A month spans the whole month, "mid-May" its middle third, a year or a "2005-06" season
    whole calendar years. A day and month with no year written take `year`; without it they do not parse."""
    cleaned = norm(text)
    if relative := _relative(cleaned, text):
        reading, match = relative
        return reading if _unread(cleaned, match) is None else None
    span = _absolute(cleaned, year)
    if span is None:
        return None
    return DateRange(reported_text=text, earliest=span[0], latest=span[1])


def recompute_date(d: DateRange, quotes: Iterable[str]) -> DateRange:
    """Code wins: the interval or timing comes from `reported_text`, and a day or month with no year written takes
    the one year the field's quotes state. Whatever code cannot derive is dropped, so a date the model worked out
    never stands; `date_problem` tells the agent why."""
    years = stated_years(quotes)
    parsed = parse_date_text(d.reported_text, year=years[0] if len(years) == 1 else None)
    return parsed or DateRange(reported_text=d.reported_text)


def _reading(text: str) -> tuple[re.Match[str], _Rule | None] | None:
    """The one expression code reads in `text`, relative or dated, and its rule (None for a relative timing)."""
    if relative := _relative(text, text):
        return relative[1], None
    return _expression(text)


def date_problem(d: DateRange, quotes: Iterable[str]) -> str | None:
    """Why code can't date `d`, whose text gives a number or a month: it has no year and its quotes don't state
    exactly one, it holds more than the one date code reads, or code can't read a date from it at all. Text with
    neither, such as "at harvest", needs no date, and a season is dated by `date_warning` instead."""
    years = stated_years(quotes)
    if parse_date_text(d.reported_text, year=years[0] if len(years) == 1 else None) is not None:
        return None
    cleaned = norm(d.reported_text)
    if (found := _reading(cleaned)) is not None:
        match, rule = found
        if (rest := _unread(cleaned, match)) is not None:
            return (
                f"code reads '{match[0]}' in '{d.reported_text}' but not '{rest}'; put one date, range or timing in "
                "reported_text (e.g. 'May 18-19, 2006' for two days), or set the value null with a note when the paper "
                "gives only one end of the span ('before 2000')"
            )
        if rule is not None and rule.season:
            return None
        if rule is not None and rule.needs_year and match.groupdict().get("y") is None and len(years) != 1:
            if not years:
                return (
                    f"'{d.reported_text}' has no year and no quote of this field states one. Papers usually state the "
                    "year once, elsewhere in the methods ('in 2006', 'the 2006 growing season', 'September of 2006'): "
                    "keep this text and also cite that sentence (a field can cite several anchors). Set the value null "
                    "only if no sentence in the paper states the year"
                )
            listed = ", ".join(str(y) for y in years)
            return (
                f"'{d.reported_text}' has no year and the quotes state several ({listed}); cite only the text for "
                "this date"
            )
    if not _DATE_LIKE.search(cleaned):
        return None
    return (
        f"code can't read a date from '{d.reported_text}'; put one date, range or timing in reported_text as the "
        "paper prints it (e.g. '15 May 1999', 'May-June 2000', '45 days after sowing'), or set the value null with "
        "a note if the paper doesn't date it"
    )


_SEASON_MONTHS = {"spring": (3, 5), "summer": (6, 8), "fall": (9, 11), "autumn": (9, 11), "winter": (12, 2)}
"""Meteorological seasons in the northern hemisphere, first and last month; the south is six months on."""


def season_window(text: str, quotes: Iterable[str], *, southern: bool) -> Span | None:
    """The months a season covers ("Summer/Fall, 2005" is June 1 to November 30 in the north), so a curator gets a
    bounded window instead of a blank one (calibration protocol §10.1). Early, mid and late keep the whole season.
    None when the text is not one season or a run of adjacent seasons, its year is not the one year stated, or a
    season crosses the new year, where the text doesn't say which year it starts in."""
    cleaned = norm(text)
    found = _expression(cleaned)
    if found is None or not found[1].season or _unread(cleaned, found[0]) is not None:
        return None
    match = found[0]
    years = list(dict.fromkeys(_written_years(match))) or stated_years(quotes)
    if len(years) != 1:
        return None
    months: list[tuple[int, int]] = []
    for name in re.findall(_SEASON, match[0], re.IGNORECASE):
        first, last = _SEASON_MONTHS[name.lower()]
        if southern:
            first, last = (first + 5) % 12 + 1, (last + 5) % 12 + 1
        if first > last or (months and first != months[-1][1] + 1):
            return None
        months.append((first, last))
    year = years[0]
    return date(year, months[0][0], 1), date(year, months[-1][1], _month_end(year, months[-1][1]))


def date_warning(d: DateRange) -> str | None:
    """A date code keeps as text only, though it parses: a season names a year but not which months."""
    cleaned = norm(d.reported_text)
    found = _expression(cleaned)
    if found is None or not found[1].season or _relative(cleaned, d.reported_text) is not None:
        return None
    return (
        f"'{d.reported_text}' names a season, so code sets no dates here; the export reads it as that season's months "
        "in the site's hemisphere. Check it against the paper"
    )
