"""Field events by kind: the controlled `Management.event_type` names and the words papers use for each. The first
five are PEcAn's `events_schema` types; `compost_application` is the curation workbook's (docs/pecan/workbook.md)."""

import re
from collections.abc import Iterable
from typing import NamedTuple

from sage.domain.normalize import norm
from sage.ir.schema import Evidence


def _words(pattern: str) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{pattern})\b", re.IGNORECASE)


EVENT_TYPES: dict[str, re.Pattern[str]] = {
    "planting": _words(r"plant(?:ed|ing)|sow(?:n|ed|ing)?|seed(?:ed|ing)|transplant(?:ed|ing|s)?|drill(?:ed|ing)"),
    "harvest": _words(r"harvest(?:ed|ing|s)?"),
    "irrigation": _words(r"irrigat\w*"),
    "fertilization": _words(r"fertili[sz]\w*|urea|top-?dress\w*|side-?dress\w*"),
    "tillage": _words(r"till(?:ed|age|ing)?|dis[ck](?:ed|ing)|plou?gh(?:ed|ing)?|harrow\w*|incorporat\w*|cultivat\w*"),
    "compost_application": _words(r"compost(?:ed|ing|s)?|manure[ds]?"),
    "mowing": _words(r"mow(?:ed|n|ing)?"),
    "herbicide": _words(r"herbicid\w*"),
    "pesticide": _words(r"pesticid\w*|insecticid\w*|fungicid\w*|nematicid\w*"),
    "weeding": _words(r"weed(?:ed|ing)"),
    "liming": _words(r"lim(?:e|ed|ing)"),
    "burning": _words(r"burn(?:ed|t|ing)?"),
    "grazing": _words(r"graz(?:e|ed|ing)"),
    "thinning": _words(r"thinn(?:ed|ing)"),
}
"""Each event type and the words that name it: "transplanted" and "sown" are planting, "urea was side-dressed" is
fertilization, "compost applied" or "manure spread" is compost_application, "disked" or "residue was incorporated" is
tillage."""


class EventType(NamedTuple):
    name: str
    evidence: Evidence


def event_type(value: str, quotes: Iterable[Evidence]) -> EventType | None:
    """The one event type the agent's `value` names, when a quote holds a word for it too. None when the value names
    no type or several, or no quote says it; the value then stays as written."""
    named = [name for name, words in EVENT_TYPES.items() if name == value.strip().lower() or words.search(value)]
    if len(named) != 1:
        return None
    words = EVENT_TYPES[named[0]]
    said = next((e for e in quotes if words.search(norm(e.quote))), None)
    return EventType(named[0], said) if said is not None else None
