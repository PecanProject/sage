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
_NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)*")


def norm(text: str) -> str:
    text = _SOFT_BREAK.sub("", text)
    text = unicodedata.normalize("NFKC", text).translate(_DASHES).translate(_QUOTES).translate(_INVISIBLE)
    return _SPACE.sub(" ", text).strip()


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


def numbers_in(text: str) -> list[str]:
    """Numeric tokens as written, with thousands commas dropped."""
    return [m.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", m) else m for m in _NUMBER.findall(norm(text))]


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
    """Whether `value` is written in `text` at the precision the text uses."""
    for token in (t.replace(",", ".") for t in numbers_in(text)):
        try:
            written = float(token)
        except ValueError:
            continue
        decimals = len(token.split(".")[1]) if "." in token else 0
        if abs(abs(written) - abs(value)) <= 0.5 * 10**-decimals + 1e-9:
            return True
    return False
