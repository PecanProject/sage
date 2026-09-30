"""
Variable -> Method map (Phase B / Stage 5): which Method measured which Variable, decided ONCE per paper and inherited
by every table cell, instead of each cell guessing on its own.

Why: every Observation of Philippe (118/118), Kathryn (12/12), Felipe (23/24) and Smukler (198/198) was left unresolved
because `method_id` was ambiguous. Methods are named by their instrument or procedure ("LI-6400 gas-exchange analyzer"),
cells by their variable ("Vcmax"); nothing recorded which method measures which variable, so the per-cell matcher --
correctly -- refused to guess.

Tiers, in order; a lower tier never overrides a higher one, and nothing is ever guessed:
  1. hint      the table's own method hint resolves to exactly one Method (the existing matcher);
  2. evidence  exactly one Method's OWN cited evidence blocks mention the variable (its label, its name, or one of the
               paper's aliases for it);
  3. model     for what is still unresolved: one bounded, batched model call over the Methods evidence packet, asked to
               answer linked / ambiguous / none with the block that says so -- and every link it proposes is VERIFIED:
               the cited block must mention the variable AND be one of that Method's own evidence blocks or name the
               Method. An unverified link is recorded as ambiguous, never used.
The result is a run artifact; an unresolved variable stays unresolved (its cells keep the existing refusal).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Optional

from pipeline.evidence_index import EvidenceIndex, normalize, significant_words

LINKED = "linked"
AMBIGUOUS = "ambiguous"
NONE = "none"
MAX_MAP_ATTEMPTS = 2


@dataclass
class VariableEntry:
    key: str                              # normalised lookup key (label without units)
    label: str                            # as the table writes it, e.g. "Vcmax (µmol m–2 s–1)"
    name: Optional[str] = None            # the classification's variable_name, e.g. "maximum carboxylation rate"
    hints: list[str] = field(default_factory=list)
    tables: list[str] = field(default_factory=list)


@dataclass
class MethodLink:
    status: str                           # linked | ambiguous | none
    tier: Optional[str] = None            # hint | evidence | model
    method_slug: Optional[str] = None
    method_record_id: Optional[str] = None
    anchors: list[str] = field(default_factory=list)
    reason: str = ""
    candidates: list[str] = field(default_factory=list)


_UNITS_TAIL_RE = re.compile(r"\s*[\(\[][^()\[\]]*[\)\]]\s*$")


def variable_key(label: str) -> str:
    """Lookup key of a variable label: notation-normalised, trailing "(units)" removed ("Vcmax (µmol m–2 s–1)" -> "vcmax")."""
    text = _UNITS_TAIL_RE.sub("", label or "").strip()
    return normalize(text or label or "")


def collect_variables(classifications: Iterable[Any]) -> list[VariableEntry]:
    """Every distinct variable the reconstructed tables report, with its name and any method hint."""
    entries: dict[str, VariableEntry] = {}

    def add(label: Optional[str], name: Optional[str], hint: Optional[str], table: str) -> None:
        if not label:
            return
        key = variable_key(label)
        if not key:
            return
        entry = entries.setdefault(key, VariableEntry(key=key, label=label))
        entry.name = entry.name or name
        if hint and hint not in entry.hints:
            entry.hints.append(hint)
        if table not in entry.tables:
            entry.tables.append(table)

    for classification in classifications:
        table = (classification.table_anchors or ["?"])[0]
        for variable in classification.variables or []:
            add(variable.label, variable.variable_name, variable.method_hint, table)
        for column in classification.value_columns or []:
            add(column.variable or column.variable_name_hint, column.variable_name_hint, column.method_hint, table)
    return list(entries.values())


def _payload_anchors(payload: Any) -> set[str]:
    anchors: set[str] = set()
    if isinstance(payload, dict):
        source = payload.get("source")
        if isinstance(source, dict):
            anchors |= {l.get("block_anchor") for l in source.get("locators") or [] if isinstance(l, dict) and l.get("block_anchor")}
        for value in payload.values():
            anchors |= _payload_anchors(value)
    elif isinstance(payload, list):
        for value in payload:
            anchors |= _payload_anchors(value)
    return anchors


def method_evidence(method_records: list[dict]) -> dict[str, dict[str, Any]]:
    """{record_id: {slug, name, description, anchors}} of every referenceable Method record."""
    out = {}
    for record in method_records:
        payload = ((record.get("detail") or {}).get("payload")) or {}
        value = lambda f: payload.get(f, {}).get("value") if isinstance(payload.get(f), dict) else None
        out[record["record_id"]] = {
            "slug": record.get("slug") or record["record_id"], "name": value("name"), "description": value("description"),
            "anchors": sorted(_payload_anchors(payload)),
        }
    return out


def _variable_phrases(index: EvidenceIndex, entry: VariableEntry) -> list[str]:
    phrases = [p for p in [_UNITS_TAIL_RE.sub("", entry.label).strip(), entry.name] if p]
    for phrase in list(phrases):
        phrases += sorted(index.aliases(phrase))[:4]
    return [normalize(p) for p in dict.fromkeys(phrases) if significant_words(p)]


def _mentions_variable(index: EvidenceIndex, anchor: str, entry: VariableEntry) -> bool:
    block = index.dmap.block(anchor)
    if block is None:
        return False
    tier, _ = index._match(block, _variable_phrases(index, entry), [])
    return tier is not None and tier <= 2


def _names_method(index: EvidenceIndex, anchor: str, method: dict[str, Any]) -> bool:
    """Does the block name the Method (at least half of its name's significant words)?"""
    block = index.dmap.block(anchor)
    if block is None or not method.get("name"):
        return False
    words = set(significant_words(method["name"]))
    return bool(words) and len(words & set(index.words[block.anchor])) * 2 >= len(words)


def lookup(links: dict[str, "MethodLink"], *names: Optional[str]) -> Optional["MethodLink"]:
    """The map entry for a cell's variable, by any of its spellings (table label, classification name)."""
    for name in names:
        if name:
            link = links.get(variable_key(name))
            if link is not None:
                return link
    return None


_SENTENCE_RE = re.compile(r"(?<=[.;])\s+(?=[A-Z0-9(\[$])")
_MEASURE_RE = re.compile(r"\b(?:measured|determined|estimated|recorded|computed|calculated|derived|quantified|analy[sz]ed"
                         r"|assessed|monitored|counted|weighed|scored|obtained|sampled|express(?:ed)?|digiti[sz]ed)\b", re.I)


def measured_in(index: EvidenceIndex, anchor: str, entry: VariableEntry) -> Optional[str]:
    """The sentence of block `anchor` that states the variable was measured/derived (the variable phrase and a
    measurement verb in the SAME sentence) -- a mere mention of a word is not evidence of how it was measured."""
    block = index.dmap.block(anchor)
    if block is None:
        return None
    phrases = _variable_phrases(index, entry)
    for sentence in _SENTENCE_RE.split(block.text):
        norm = f" {normalize(sentence)} "
        verbs = [m.start() for m in _MEASURE_RE.finditer(norm)]
        if not verbs:
            continue
        for phrase in phrases:
            for match in re.finditer(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", norm):
                for verb in verbs:
                    # The variable is what the verb applies to: "X ... was determined" (X within 12 words before the
                    # verb) or "measured ... X" (X within 3 words after it) -- not a word that merely occurs in the
                    # sentence ("determined using rings of 345 cm3 volume", Smukler b:0061).
                    if match.end() <= verb and len(norm[match.end():verb].split()) <= 12:
                        return sentence
                    if verb < match.start() and len(norm[verb:match.start()].split()) <= 3:
                        return sentence
    return None


_GENERIC_METHOD_WORDS = frozenset(
    "analyzer analyser analysis meter system device instrument method methods measured measurement measurements using "
    "with portable determined estimated sensor sensors model software technique procedure".split()
)


_SPELLING = ((re.compile(r"(?<=[a-z])is(ing|ation|ed|e|es)$"), r"iz\1"), (re.compile(r"(?<=[a-z])ys(e|ed|es|ing)$"), r"yz\1"))
_HINT_ANCHOR_RE = re.compile(r"\bb:\d{3,5}\b")
COMMON_WORD_BLOCK_SHARE = 0.10


def _spelling(word: str) -> str:
    """British/American spellings are one word ("digitising" = "digitizing", "analyse" = "analyze")."""
    for pattern, replacement in _SPELLING:
        word = pattern.sub(replacement, word)
    return word


def _distinctive(text: str, common: frozenset[str] = frozenset()) -> set[str]:
    text = re.sub(r"\b(\d)\s*[-‐‑–]\s*([a-z])\b", r"\1\2", normalize(text or "").lower())   # "3-D" = "3D"
    text = _HINT_ANCHOR_RE.sub(" ", text)
    tokens = significant_words(text)
    tokens += [part for w in tokens if "-" in w for part in w.split("-")]    # "3d-digitizing" also counts as its parts
    words = {_spelling(w) for w in tokens if w not in _GENERIC_METHOD_WORDS and len(w) > 2}
    return {w for w in words if w not in common and re.search(r"[a-z]", w)}


def common_words(index: Optional[EvidenceIndex]) -> frozenset[str]:
    """Words in more than COMMON_WORD_BLOCK_SHARE of the paper's blocks (the study organism, "sapling", "leaf"): they
    name no Method. Real case (Philippe run 20260926T190955_435c16fa): the hint "counted after 3-D digitising of each
    sapling" was linked to gap-fraction photography by the single shared word "sapling" (44 of 160 blocks)."""
    if index is None or not index.dmap.blocks:
        return frozenset()
    counts: dict[str, int] = {}
    for block in index.dmap.blocks:
        for word in _distinctive(block.text):
            counts[word] = counts.get(word, 0) + 1
    limit = COMMON_WORD_BLOCK_SHARE * len(index.dmap.blocks)
    return frozenset(w for w, n in counts.items() if n > limit)


def hint_tier(entry: VariableEntry, methods: dict[str, dict[str, Any]],
              index: Optional[EvidenceIndex] = None) -> MethodLink:
    """Tier 1: a table method hint names a Method by its DISTINCTIVE words (TruSpec, AccuPAR, VegeSTAR, Pol95, ...),
    never by generic ones ("analyzer", "meter") or by words common across the paper ("sapling"): the unique Method
    whose name/description carries the most of them. A hint that cites a block ("... (Methods b:0038)") is matched only
    against the Methods whose own evidence is that block -- and names the one Method there, if only one is."""
    common = common_words(index)
    for hint in entry.hints:
        pool = methods
        cited = set(_HINT_ANCHOR_RE.findall(hint or ""))
        if cited:
            anchored = {rid: m for rid, m in methods.items() if cited & set(m.get("anchors") or [])}
            if anchored:
                pool = anchored
        words = _distinctive(hint, common)
        scores = {rid: len(words & _distinctive(f"{m['name'] or ''} {m['description'] or ''}", common))
                  for rid, m in pool.items()}
        best = max(scores.values(), default=0)
        winners = [rid for rid, score in scores.items() if score == best]
        if best > 0 and len(winners) == 1:
            rid = winners[0]
            shared = sorted(words & _distinctive(f"{pool[rid]['name'] or ''} {pool[rid]['description'] or ''}", common))
            where = f" among the Methods of {sorted(cited)}" if pool is not methods else ""
            return MethodLink(LINKED, "hint", pool[rid]["slug"], rid, [],
                              f"table method hint {hint!r} names {pool[rid]['slug']}{where} by {shared}")
        if best == 0 and pool is not methods and len(pool) == 1:
            rid = next(iter(pool))
            return MethodLink(LINKED, "hint", pool[rid]["slug"], rid, sorted(cited),
                              f"table method hint {hint!r} cites {sorted(cited)}, the evidence of {pool[rid]['slug']} only")
        if best > 0 or (pool is not methods and len(pool) > 1):
            return MethodLink(AMBIGUOUS, reason=f"hint {hint!r} fits {len(winners) if best else len(pool)} Methods equally",
                              candidates=sorted(pool[r]["slug"] for r in (winners if best else pool)))
    return MethodLink(NONE, reason="no distinctive table method hint")


def evidence_tier(entry: VariableEntry, methods: dict[str, dict[str, Any]], index: EvidenceIndex) -> MethodLink:
    """Tier 2: the Methods whose own evidence has a sentence stating the variable was measured/derived."""
    hits: dict[str, list[str]] = {}
    for rid, method in methods.items():
        anchors = [a for a in method["anchors"] if measured_in(index, a, entry)]
        if anchors:
            hits[rid] = anchors
    if len(hits) == 1:
        rid, anchors = next(iter(hits.items()))
        sentence = measured_in(index, anchors[0], entry) or ""
        return MethodLink(LINKED, "evidence", methods[rid]["slug"], rid, anchors,
                          f"its own evidence {anchors[0]} states: {sentence[:160]!r}")
    if len(hits) > 1:
        # No positional tie-break: tried and measured wrong (Felipe "Total fruit" -> nitrogen analysis; Kathryn SOC ->
        # titration alone, though SOC is total minus inorganic C). Shared-block cases go to the verified model tier.
        return MethodLink(AMBIGUOUS, reason=f"{len(hits)} Methods' evidence states how {entry.label!r} was measured",
                          candidates=sorted(methods[r]["slug"] for r in hits))
    return MethodLink(NONE, reason=f"no Method's evidence states how {entry.label!r} was measured")


def is_variable_label(label: str) -> bool:
    """A year, a number or a treatment level misread as a column variable ("2005", "2006 – Fallow") is not one."""
    text = _UNITS_TAIL_RE.sub("", label or "").strip()
    return bool(re.search(r"[A-Za-zµμ]{2,}", text)) and not re.match(r"^\s*(?:19|20)\d{2}\b", text)


def map_prompt(entries: list[VariableEntry], methods: dict[str, dict[str, Any]], packet: Optional[str],
               index: Optional[EvidenceIndex] = None) -> str:
    def names(e: VariableEntry) -> list[str]:
        out = [e.name] if e.name else []
        if index is not None:
            out += [a for a in _variable_phrases(index, e) if a != normalize(_UNITS_TAIL_RE.sub("", e.label).strip())]
        return list(dict.fromkeys(n for n in out if n))

    variables = [{"variable": e.label, **({"also_called": names(e)} if names(e) else {})} for e in entries]
    method_list = [{"slug": m["slug"], "name": m["name"], "description": (m["description"] or "")[:300],
                    "evidence_blocks": m["anchors"]} for m in methods.values()]
    return (
        "For each VARIABLE below, which of the paper's already-extracted METHODS measured or derived it? Decide from "
        "what the paper STATES (the methods text, table captions and notes), never from what is usual.\n\n"
        f"VARIABLES:\n{json.dumps(variables, ensure_ascii=False, indent=1)}\n\n"
        f"METHODS:\n{json.dumps(method_list, ensure_ascii=False, indent=1)}\n\n"
        "Output ONLY a JSON object {\"links\": [{\"variable\": <exactly as listed>, \"status\": \"linked\"|\"ambiguous\"|"
        "\"none\", \"method\": <slug, only when linked>, \"anchor\": <the content.md block that states it, only when "
        "linked>, \"reason\": <one sentence>}]} with one entry per variable. \"linked\" only when the cited block itself "
        "ties this variable to this method (the variable is named in it, and it is that method's text); \"ambiguous\" when "
        "more than one method could apply and the paper does not say which; \"none\" when the paper does not say how it "
        "was measured. Never link by elimination or plausibility.\n\n" + (packet or "")
    )


def verify_model_link(entry: VariableEntry, proposal: dict, methods: dict[str, dict[str, Any]], index: EvidenceIndex) -> MethodLink:
    slug, anchor = proposal.get("method"), str(proposal.get("anchor") or "").strip("[]⟦⟧ ")
    method = next(((rid, m) for rid, m in methods.items() if m["slug"] == slug), None)
    if method is None:
        return MethodLink(AMBIGUOUS, reason=f"model named an unknown method {slug!r}")
    rid, data = method
    if not anchor or index.dmap.block(anchor) is None:
        return MethodLink(AMBIGUOUS, reason=f"model cited no real block for {entry.label!r} -> {slug}")
    if not _mentions_variable(index, anchor, entry):
        return MethodLink(AMBIGUOUS, reason=f"cited block {anchor} does not mention {entry.label!r}")
    if anchor not in data["anchors"] and not _names_method(index, anchor, data):
        return MethodLink(AMBIGUOUS, reason=f"cited block {anchor} is neither {slug}'s evidence nor names it")
    return MethodLink(LINKED, "model", slug, rid, [anchor], f"model link verified: {proposal.get('reason') or ''}"[:300])


def build_map(
    entries: list[VariableEntry], methods: dict[str, dict[str, Any]], index: EvidenceIndex,
    hint_resolver: Optional[Callable[[VariableEntry], Optional[str]]],
    ask_model: Optional[Callable[[str], Optional[dict]]] = None, packet: Optional[str] = None,
) -> dict[str, MethodLink]:
    """The map for every variable. `hint_resolver` is tier 1 (returns a Method slug or None); `ask_model(prompt)`
    returns the parsed JSON answer or None (provider failure); omitted -> no model tier."""
    result: dict[str, MethodLink] = {}
    slug_to_rid = {m["slug"]: rid for rid, m in methods.items()}
    for entry in entries:
        if not is_variable_label(entry.label):
            result[entry.key] = MethodLink(NONE, reason=f"{entry.label!r} is not a variable label")
            continue
        link = hint_tier(entry, methods, index)
        if link.status != LINKED and hint_resolver is not None:
            slug = hint_resolver(entry)
            if slug and slug in slug_to_rid and not link.candidates:
                link = MethodLink(LINKED, "hint", slug, slug_to_rid[slug], [], f"table method hint resolves to {slug}")
        if link.status == LINKED:
            result[entry.key] = link
            continue
        evidence = evidence_tier(entry, methods, index)
        result[entry.key] = evidence if evidence.status != NONE or not link.candidates else link
    pending = [e for e in entries if result[e.key].status != LINKED and is_variable_label(e.label)]
    if pending and ask_model is not None and len(methods) > 1:
        for _attempt in range(MAX_MAP_ATTEMPTS):
            answer = ask_model(map_prompt(pending, methods, packet, index))
            links = (answer or {}).get("links") if isinstance(answer, dict) else None
            if not isinstance(links, list):
                continue
            by_label = {variable_key(str(l.get("variable") or "")): l for l in links if isinstance(l, dict)}
            for entry in pending:
                proposal = by_label.get(entry.key)
                if proposal is None:
                    continue
                if proposal.get("status") == LINKED:
                    result[entry.key] = verify_model_link(entry, proposal, methods, index)
                elif proposal.get("status") in (AMBIGUOUS, NONE):
                    previous = result[entry.key]
                    result[entry.key] = MethodLink(proposal["status"], "model", reason=str(proposal.get("reason") or "")[:300],
                                                   candidates=previous.candidates)
            break
    return result


def to_artifact(entries: list[VariableEntry], links: dict[str, MethodLink]) -> dict:
    return {"variables": [asdict(e) for e in entries], "links": {k: asdict(v) for k, v in links.items()},
            "counts": {s: sum(1 for v in links.values() if v.status == s) for s in (LINKED, AMBIGUOUS, NONE)}}
