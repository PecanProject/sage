"""
Evidence Index (Stage 3): deterministic, explainable retrieval over a paper's Document Map.

No embeddings and no blended score. Every hit carries the explicit reasons it was retrieved, and hits are ranked by
TIER (then by the caller's region preference, then by reading order):

    tier 1  an exact phrase occurs in the block (after notation normalisation -- the grounding validator's own
            `_normalize_typography`, so "Vcmax" finds "$V_{\\text{cmax}}$", "36°37′N" finds "36˚37´N");
    tier 2  every significant word of a phrase occurs in the block, in any order;
    tier 3  a typed detector fires (a coordinate, a DOI, a management verb, a measurement instrument, ...);
    tier 4  at least half of a phrase's significant words occur.

Typed detectors ("signals") are regexes over the normalised text -- each one exists because a real extraction needed
that kind of evidence and did not get it (coordinates written in degrees and minutes; management events such as the
Philippe thinning; instruments that tie a variable to its method; pooling statements).

Terminology: the paper's own definitions of abbreviations and symbols ("leaf area index (LAI)", "Abbreviations: DM,
dry matter; ...", "N a, leaf nitrogen concentration per unit area") -- the aliases later stages need so that "Na",
"$N_a$" and "leaf nitrogen concentration per unit area" are recognised as one thing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

from pipeline.document_map import DocBlock, DocumentMap

_WORD_RE = re.compile(r"[a-z0-9µμ°%]+(?:[-'][a-z0-9]+)*")
_STOPWORDS = frozenset(
    "the a an of and or in on at to for by with from as is was were be been are this that these those each per its "
    "their into over under between within than then also after before during all any both".split()
)

# Typed detectors. Case-insensitive, run on the normalised text.
SIGNALS: dict[str, re.Pattern] = {
    "coordinate": re.compile(
        r"(?<![\w.])-?\d{1,3}(?:\.\d+)?\s*°\s*(?:\d{1,2}(?:\.\d+)?\s*'\s*)?(?:\d{1,2}(?:\.\d+)?\s*\"\s*)?[nsew]\b"
        r"|\b(?:latitude|longitude)\b", re.I),
    "elevation": re.compile(r"\b\d[\d,.]*\s*m\s*(?:a\.?\s?s\.?\s?l\.?|above (?:mean )?sea level)|\b(?:elevation|altitude)\b", re.I),
    "date": re.compile(
        r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?"
        r"|nov(?:ember)?|dec(?:ember)?)\b\.?\s*\d{0,2}(?:,?\s*(?:19|20)\d{2})?|\b(?:19|20)\d{2}\b", re.I),
    "relative_time": re.compile(r"\b\d+\s*(?:dap|das|dat)\b|\bdays? (?:after|before) (?:planting|sowing|transplant\w*|emergence)\b"
                                r"|\b(?:before|after) (?:planting|harvest|sowing)\b", re.I),
    "doi": re.compile(r"\b10\.\d{4,9}/[^\s\])>,;\"']+", re.I),
    "measurement_verb": re.compile(r"\b(?:measured|determined|estimated|recorded|computed|calculated|sampled|analy[sz]ed"
                                   r"|quantified|assessed|derived|monitored|counted|weighed|scored|digiti[sz]ed)\b", re.I),
    "instrument": re.compile(r"\b[a-z]{1,6}-?\d{2,5}[a-z]?\b(?=[^.]{0,60}(?:analy[sz]er|meter|sensor|system|chamber|device))"
                             r"|\b(?:analy[sz]er|spectromet\w*|chromatograph\w*|solarimeter|durometer|refractometer|colorimeter"
                             r"|calliper|caliper|autosampler|lysimeter|probe|pointer|scanner|oven|meter)\b"
                             r"|\((?:[^()]*,\s*)?[a-z .]+,\s*[a-z]{2}\)", re.I),
    "management_verb": re.compile(
        r"\b(?:plant(?:ed|ing)|sown|sow(?:ed|ing)|seeded|transplant\w*|harvest\w*|thinn\w*|fertili[sz]\w*|irrigat\w*"
        r"|till(?:ed|age)|disk(?:ed|ing)|disc(?:ed|ing)|mow(?:n|ed|ing)|incorporat\w*|cut at|was cut|were cut|clipp\w*"
        r"|remov\w*|weed(?:ed|ing)|spray\w*|graz\w*|burn\w*|compost\w*|manure|lim(?:ed|ing)|prun\w*|subsoil\w*|spad\w*"
        r"|laser.level\w*|bed(?:s)? (?:were )?(?:prepared|formed|reshaped)|applied)\b", re.I),
    "amount": re.compile(r"\b\d[\d,.]*\s*(?:kg|mg|g|t|mg|l|ml|mm|cm|m|seeds?|plants?|stems?|trees?)\s*(?:[a-z]+\s*)?(?:ha|m|plant|tree)\s*[-−–]?\s*1\b"
                         r"|\b\d[\d,.]*\s*(?:kg|mg|t)\s+(?:n\s+)?per\s+ha\b|\bstems?\s*ha\b", re.I),
    "design_term": re.compile(r"\b(?:randomi[sz]ed|complete block|split[- ]plot|factorial|replicat\w*|treatments?|control"
                              r"|subplots?|main plots?|levels?|classes|gradient|experimental (?:design|unit))\b", re.I),
    "site_term": re.compile(r"\b(?:study (?:site|area)|site|located|situated|farm|station|experimental field|forest|stand"
                            r"|county|province|region|soil|climate|rainfall|precipitation|mean annual)\b", re.I),
    "pooling": re.compile(r"\b(?:averaged (?:across|over)|pooled|across (?:all|locations|populations|years|sites)"
                          r"|mean of|means? (?:of|over) (?:the )?\d+ years?|over (?:the )?\d+ years?)\b", re.I),
    "statistic": re.compile(r"\b(?:s\.?e\.?m?|s\.?d\.?|standard (?:error|deviation)|confidence (?:interval|limits?)|lsd|hsd"
                            r"|tukey|newman|duncan|anova|p\s*[<=>])\b|\bn\s*=\s*\d+|\(\d+,\s*\d+(?:,\s*\d+)*\)", re.I),
    "binomial": re.compile(r"\b[A-Z][a-z]{2,}\s+[a-z]{3,}\b(?:\s+(?:L\.|Mill\.|Lam\.|Walp\.|Koch|Moench|Czern\.))?"),
    "unit": re.compile(r"(?:\b(?:[µμmkn]?mol|[µμmk]?g|mg|kg|mm|cm|km|ha|%|°c|ppm|mg\s*l)\b[^.;]{0,12}?[-−–]\s*[123]\b)"
                       r"|\b(?:g|kg|mg|mm|cm|m)\s*(?:m|ha)\s*[-−–]?\s*[12]\b|°c\b|\bppm\b", re.I),
}
_AUTHORITY_RE = re.compile(r"\s+(?:L\.|Mill\.|Lam\.|Walp\.|Koch|Moench|Czern\.|Sacc\.|Schreb\.|Leyss\.|Maxim\.|Ehrh\.|Erhr\.|Liebl\.|\[L\.\])")
_NOT_EPITHETS = frozenset(
    "were was are is has have had and the for with from into online published received accepted using used measured "
    "during after before between within under over this that these those which their there then than also only both "
    "each other such more most less least same different total mean average annual daily field plots plot study data "
    "results values samples site sites area areas year years season seasons treatment treatments control effect "
    "effects state county university department analysis method methods".split()
)


_GENERIC_ORGANISM_WORDS = frozenset(
    "saplings sapling seedlings seedling plants plant trees tree crop crops species cultivar cultivars variety varieties "
    "study differences reported literature light size availability systems system ratio index conditions stress types "
    "demand matter erosion technique date yield architecture".split()
)


def _is_binomial(match: re.Match, text: str, document: str) -> bool:
    """A Capitalised + lowercase word pair is taken as a scientific name only with corroboration: an authority follows
    ("Fagus sylvatica L."), the genus is abbreviated elsewhere in the paper ("P. sylvestris"), or the pair sits
    mid-sentence (after a lowercase word, a comma or a bracket) -- so sentence openings such as "Published online" or
    "Measurements were" are not species."""
    genus, epithet = match.group(0).split()[:2]
    if epithet.lower() in _NOT_EPITHETS or genus.lower() in _NOT_EPITHETS:
        return False
    if _AUTHORITY_RE.match(text, match.end()) or re.search(rf"\b{genus[0]}\.\s*{re.escape(epithet)}\b", document):
        return True
    before = text[:match.start()].rstrip()
    return bool(before) and (before[-1] in "(,;[=" or before[-1].islower())


# Signals matched on the ORIGINAL (not casefolded) text because capitalisation carries the meaning.
_CASE_SENSITIVE_SIGNALS = frozenset({"binomial"})

_ABBREV_DEF_RE = re.compile(r"([A-Za-z][A-Za-z0-9 ,\-/]{2,70}?)\s*\(\s*\$?([A-Za-z][A-Za-z0-9_{}\\ ]{0,14}?)\$?\s*\)")
_ABBREV_LIST_RE = re.compile(r"abbreviations?\s*:\s*(.+)", re.I)


def normalize(text: str) -> str:
    from pipeline.validators import _normalize_typography

    return _normalize_typography(" ".join(str(text).split()).casefold())


def significant_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(normalize(text)) if w not in _STOPWORDS and (len(w) > 2 or any(c.isdigit() for c in w))]


@dataclass(frozen=True)
class Hit:
    anchor: str
    tier: int
    reasons: tuple[str, ...]


@dataclass
class EvidenceIndex:
    dmap: DocumentMap
    normalized: dict[str, str] = field(default_factory=dict)
    words: dict[str, frozenset[str]] = field(default_factory=dict)
    signals: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)   # anchor -> signal -> matches
    terminology: dict[str, set[str]] = field(default_factory=dict)                  # normalised term -> aliases

    def __post_init__(self) -> None:
        document = " ".join(b.text for b in self.dmap.blocks if b.region != "references")
        for block in self.dmap.blocks:
            norm = normalize(block.text)
            self.normalized[block.anchor] = norm
            self.words[block.anchor] = frozenset(_WORD_RE.findall(norm))
            found: dict[str, tuple[str, ...]] = {}
            for name, pattern in SIGNALS.items():
                source = block.text if name in _CASE_SENSITIVE_SIGNALS else norm
                found_matches = [m for m in pattern.finditer(source)]
                if name == "binomial":
                    found_matches = [m for m in found_matches if _is_binomial(m, source, document)]
                matches = tuple(dict.fromkeys(m.group(0).strip() for m in found_matches))
                if matches:
                    found[name] = matches
            self.signals[block.anchor] = found
        self._build_terminology()

    # --- terminology ------------------------------------------------------------------------------------------
    def _build_terminology(self) -> None:
        def add(term: str, alias: str) -> None:
            term_n, alias_n = normalize(term).strip(" ,.;:*[]"), normalize(alias).strip(" ,.;:*[]")
            if not term_n or not alias_n or term_n == alias_n or len(term_n) > 80:
                return
            # A short symbol rendered with a space ("N a" for $N_a$, "V cmax") is also keyed without it ("na").
            keys = {alias_n} | ({alias_n.replace(" ", "")} if len(alias_n) <= 8 else set())
            for key in keys:
                self.terminology.setdefault(term_n, set()).add(key)
                self.terminology.setdefault(key, set()).add(term_n)

        for block in self.dmap.blocks:
            if block.region == "references":
                continue
            text = block.text
            listed = _ABBREV_LIST_RE.search(text)
            if listed:
                # "Abbreviations: N a, leaf nitrogen concentration per unit area; V cmax, maximum ..." -- one pair per
                # ';', symbol first. Works for captions ("Abbreviations: BSD1 and BSD6, basal stem diameter ...").
                for part in listed.group(1).split(";"):
                    if "," in part:
                        symbol, meaning = part.split(",", 1)
                        meaning = re.split(r"\.\s|\.$|\*", meaning)[0]
                        for sym in re.split(r"\s+and\s+", symbol):
                            add(meaning, sym)
            for match in _ABBREV_DEF_RE.finditer(text):
                term, alias = match.group(1), match.group(2)
                words = term.split()
                # The expansion is the SHORTEST tail of the preceding words whose first word starts with the
                # abbreviation's first letter ("leaf area index (LAI)", "silhouette to total leaf area ratio (STAR)"),
                # at most 8 words; no such word -> no alias (never a guess).
                first = alias.strip("$\\ ")[:1].lower()
                for size in range(1, min(len(words), 8) + 1):
                    if words[-size][:1].lower() == first:
                        if size > 1 or len(alias.strip()) <= 2:
                            add(" ".join(words[-size:]), alias)
                        break

    def common_names(self) -> dict[str, set[str]]:
        """{"genus epithet" (lowercase): {common names the paper pairs with it}} from "common name (Genus epithet ...)"
        and "Genus epithet (common name)" -- e.g. "switchgrass ( Panicum virgatum L.)", "processing tomato
        (Lycopersicon esculentum Mill. = Solanum lycopersicum L.)". Only pairings the paper itself writes."""
        if hasattr(self, "_common_names"):
            return self._common_names
        pairs: dict[str, set[str]] = {}
        before_re = re.compile(r"([A-Za-z][A-Za-z\- ]{1,40}?)\s*\(([^()]{3,160})\)")
        binomial_re = re.compile(r"\b([A-Z][a-z]{2,})\s+([a-z]{3,})\b")
        for block in self.dmap.blocks:
            if block.region == "references":
                continue
            for match in before_re.finditer(block.text):
                words = [w for w in match.group(1).split() if w.lower() not in _STOPWORDS]
                inside = [b for b in binomial_re.finditer(match.group(2))
                          if b.group(2).lower() not in _NOT_EPITHETS and b.group(2).lower() not in _STOPWORDS
                          and not re.search(r"\d{4}", match.group(2)[b.end():b.end() + 12])]   # "(Smith and Lee 1996)"
                if not words or not inside or binomial_re.fullmatch(" ".join(words[-2:])):
                    continue   # no binomial, or "(Genus epithet)" after another binomial
                while words and words[-1].lower() in _GENERIC_ORGANISM_WORDS:
                    words = words[:-1]   # "beech saplings (Fagus sylvatica)" -> "beech"
                if not words:
                    continue
                last = words[-1].lower()
                if not last.isalpha() or last.endswith("ed"):
                    continue   # "potted", "affected": a verb form, not a name
                names = {last}
                if len(words) >= 2:
                    first = words[-2].lower()
                    if first.isalpha() and first not in _GENERIC_ORGANISM_WORDS and not first.endswith("ed"):
                        names.add(f"{first} {last}")
                for binomial in inside:
                    pairs.setdefault(f"{binomial.group(1)} {binomial.group(2)}".lower(), set()).update(names)
        self._common_names = pairs
        return pairs

    def aliases(self, term: str) -> set[str]:
        """The paper's own aliases of `term` (one hop, both directions), normalised."""
        return set(self.terminology.get(normalize(term).strip(), set()))

    # --- search -------------------------------------------------------------------------------------------------
    def search(
        self, phrases: Iterable[str] = (), signals: Iterable[str] = (), regions: Optional[Iterable[str]] = None,
        exclude_regions: Iterable[str] = ("references",), block_types: Optional[Iterable[str]] = None,
        anchors: Optional[Iterable[str]] = None, limit: Optional[int] = None,
    ) -> list[Hit]:
        """Blocks matching any phrase or signal, best tier first, then region preference (`regions` order), then
        reading order. Every hit says why."""
        phrases = [p for p in phrases if p and p.strip()]
        signals = list(signals)
        region_rank = {r: i for i, r in enumerate(regions or ())}
        excluded = set(exclude_regions or ())
        allowed_types = set(block_types) if block_types else None
        pool = set(anchors) if anchors is not None else None
        hits: list[tuple[tuple, Hit]] = []
        for block in self.dmap.blocks:
            if block.region in excluded or (allowed_types and block.block_type not in allowed_types):
                continue
            if pool is not None and block.anchor not in pool:
                continue
            tier, reasons = self._match(block, phrases, signals)
            if tier is None:
                continue
            rank = region_rank.get(block.region, len(region_rank))
            if regions is not None and block.region in region_rank:
                reasons = reasons + (f"region:{block.region}",)
            hits.append(((tier, rank, block.order), Hit(block.anchor, tier, reasons)))
        hits.sort(key=lambda item: item[0])
        out = [hit for _, hit in hits]
        return out[:limit] if limit else out

    def _match(self, block: DocBlock, phrases: list[str], signals: list[str]) -> tuple[Optional[int], tuple[str, ...]]:
        norm, words = self.normalized[block.anchor], self.words[block.anchor]
        best: Optional[int] = None
        reasons: list[str] = []
        for phrase in phrases:
            phrase_n = normalize(phrase)
            significant = significant_words(phrase)
            if phrase_n and (f" {phrase_n} " in f" {norm} " or (len(phrase_n) > 3 and phrase_n in norm)):
                tier, why = 1, f"phrase:{phrase!r}"
            elif significant and all(w in words for w in significant):
                tier, why = 2, f"all-words:{phrase!r}"
            elif len(significant) >= 2 and sum(w in words for w in significant) * 2 >= len(significant):
                tier, why = 4, f"partial-words:{phrase!r}"
            else:
                continue
            reasons.append(why)
            best = tier if best is None else min(best, tier)
        for name in signals:
            matched = self.signals[block.anchor].get(name)
            if matched:
                reasons.append(f"signal:{name}({matched[0][:40]})")
                best = 3 if best is None else min(best, 3)
        return best, tuple(reasons)

    def signal_hits(self, name: str, exclude_regions: Iterable[str] = ("references",)) -> list[str]:
        """Every block (reading order) where a typed detector fires -- the whole-document scan used to decide that a
        pattern-detectable fact is ABSENT, not merely not retrieved."""
        excluded = set(exclude_regions)
        return [b.anchor for b in self.dmap.blocks if b.region not in excluded and self.signals[b.anchor].get(name)]


_INDEX_CACHE: dict[tuple[str, str], EvidenceIndex] = {}


def build_evidence_index(dmap: DocumentMap) -> EvidenceIndex:
    """One index per (paper, content hash); rebuilt automatically when content.md changes."""
    key = (dmap.paper_id, dmap.document_hash)
    if key not in _INDEX_CACHE:
        if len(_INDEX_CACHE) > 32:
            _INDEX_CACHE.clear()
        _INDEX_CACHE[key] = EvidenceIndex(dmap)
    return _INDEX_CACHE[key]
