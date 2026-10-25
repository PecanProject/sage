"""Text normalisation shared by grounding and quote checks, so both agree on what "verbatim" means."""

import re
import unicodedata

_DASHES = dict.fromkeys(map(ord, "\u2010\u2011\u2012\u2013\u2014\u2015\u2212\ufe58\ufe63\uff0d"), "-")
_QUOTES = {ord("\u2018"): "'", ord("\u2019"): "'", ord("\u201c"): '"', ord("\u201d"): '"', ord("\u00b4"): "'"}
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff"), None)
_SPACE = re.compile(r"\s+")
_SOFT_BREAK = re.compile(r"[\u0002\u00ad]\s*")
"""A soft hyphen marks a word broken across lines, so the line break after it goes too: "pro\u00ad cessing". pdftext
writes U+0002 for it, and also for an exponent's minus. The parser tells the two apart and writes U+00AD or U+2212, so
only a document parsed before parser sage.5 still holds U+0002, and there it is read as the soft hyphen it mostly is."""
_HYPHEN_BREAK = re.compile(r"(\w)- (\w)")
_WORD_HYPHEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")
_TOKEN = re.compile(r"[-+]?\d+(?:[.,]\d+)*")
"""Digits joined by points and commas, as one token, so a date ("15.05.1999") is never read as two numbers."""
_THOUSANDS = r"[-+]?[1-9]\d{0,2}(?:,\d{3})+(?:\.\d+)?"
NUMBER = rf"{_THOUSANDS}|[-+]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
"""One number as printed: with thousands commas ("1,234.5"), a decimal comma ("12,5") or a decimal point."""


def norm(text: str) -> str:
    return _SPACE.sub(" ", _fold(_SOFT_BREAK.sub("", text))).strip()


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES).translate(_INVISIBLE)


def squash(text: str) -> str:
    """Two texts that squash equal are the same words as printed, whatever the text layer did with spacing."""
    return "".join(_HYPHEN_BREAK.sub(r"\1\2", norm(text)).split())


def needles(needle: str) -> tuple[str, ...]:
    """The needle squashed, and also without the hyphens inside words: a compound split at a line break ("mid-
    September") reaches the text layer as a soft hyphen, which squashing joins to "midSeptember"."""
    squashed = squash(needle)
    joined = _WORD_HYPHEN.sub("", squashed)
    return (squashed,) if joined == squashed else (squashed, joined)


def contains(haystack: str, needle: str) -> bool:
    text = squash(haystack)
    return any(n in text for n in needles(needle))


def contains_loose(haystack: str, needle: str) -> bool:
    """Case-insensitive `contains`, for stated names and labels."""
    text = squash(haystack).lower()
    return any(n.lower() in text for n in needles(needle))


def span(haystack: str, needle: str) -> str | None:
    """The stretch of `haystack`, verbatim, that `contains_loose` finds `needle` in, or None. It lets code quote the
    words a value sits in instead of asking the agent to copy them."""
    text, at = _squashed_with_positions(haystack)
    for n in needles(needle):
        for hay, want in ((text, n), (text.lower(), n.lower())):
            start = hay.find(want) if want else -1
            if start >= 0:
                found = haystack[at[start] : at[start + len(want) - 1] + 1]
                if contains_loose(found, needle):
                    return found
    return None


def _squashed_with_positions(text: str) -> tuple[str, list[int]]:
    """`squash(text)` with, for each of its characters, the index in `text` it came from."""
    out: list[str] = []
    at: list[int] = []
    gap = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "\u0002\u00ad":
            i += 1
            while i < len(text) and text[i].isspace():
                i += 1
            continue
        if ch.isspace():
            gap = True
            i += 1
            continue
        piece = "".join(_fold(ch).split())
        if gap and piece[:1].isalnum() and len(out) >= 2 and out[-1] == "-" and out[-2].isalnum():
            out.pop()
            at.pop()
        gap = False
        for c in piece:
            out.append(c)
            at.append(i)
        i += 1
    return "".join(out), at


def numbers_in(text: str) -> list[str]:
    """Numeric tokens as written, with thousands commas dropped. A token that is no single number, such as a date
    ("15.05.1999"), stays as it is."""
    return [m.replace(",", "") if re.fullmatch(_THOUSANDS, m) else m for m in _TOKEN.findall(norm(text))]


def read_number(token: str) -> tuple[float, int] | None:
    """A token's value and the decimal places it is printed with, or None when it is no single number: a date
    ("15.05.1999") or a list written without spaces ("0,50,100")."""
    if re.fullmatch(_THOUSANDS, token):
        digits = token.replace(",", "")
    elif re.fullmatch(NUMBER, token):
        digits = token.replace(",", ".")
    else:
        return None
    return float(digits), len(digits.partition(".")[2])


_COUNT_WORDS = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
    "twenty",
)


def count_in(text: str, n: int) -> bool:
    """Whether the count `n` is written in `text`, as digits or as a word ("four replicates")."""
    if number_in(text, n):
        return True
    return 0 <= n < len(_COUNT_WORDS) and re.search(rf"\b{_COUNT_WORDS[n]}\b", norm(text), re.IGNORECASE) is not None


def number_in(text: str, value: float) -> bool:
    """Whether `value` is written in `text` at the precision the text uses. A date states no number."""
    for token in numbers_in(text):
        read = read_number(token)
        if read is None:
            continue
        written, decimals = read
        if abs(abs(written) - abs(value)) <= 0.5 * 10**-decimals + 1e-9:
            return True
    return False
