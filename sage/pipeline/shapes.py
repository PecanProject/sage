"""The five field shapes the agent writes, read into the `Field` the checker knows. The agent points at the paper and
code reads the words, the number and the basis from where it points:

    [value, p3.b4]                       stated: the value is in the text at its anchors
    {at: p7.t1.r0.c1}                    stated: the value is the text at the anchor
    [value, p3.b4, note: how]            derived: composed from the cited text
    [value, note: why]                   inferred: a judgement, said why
    [null, note: what was looked for]    absent

An anchor can pin its quote, `{p3.b4: "the second 120 kg"}`, when the words occur twice. YAML is composed with
`BaseLoader`, so every scalar is a string and every node knows its line."""

import re
import types
from dataclasses import dataclass, field
from typing import Literal, Union, get_args, get_origin

from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from sage.domain.normalize import norm, read_number, span
from sage.ir.files import FileIssue
from sage.ir.schema import DateRange, DepthRange, Field, Model, QuantityValue, StatisticalSummary
from sage.parse.document import ANCHOR, Document

NULL = frozenset({"null", "~", "", "Null", "NULL"})
TRUE = frozenset({"true", "yes", "y", "on", "1"})
FALSE = frozenset({"false", "no", "n", "off", "0"})
SHAPES = (
    "write it as [value, anchor] (the paper says it there), [value, anchor, note: how] (you composed it from there), "
    "[value, note: why] (your judgement) or [null, note: what you looked for]"
)
_LEAD = re.compile(
    r"^\s*(?:[<>≤≥~≈]=?\s*)?[-+\u2212]?(?:\d[\d.,]*|[.,]\d+)"
    r"(?:\s*(?:-|\u2013|\u2014|to)\s*[-+\u2212]?\d[\d.,]*)?"
    r"(?:\s*(?:±|\+/-|\+-)\s*\d[\d.,]*)?"
    r"(?:\s*\(\s*[-+\u2212]?[\d.,]+\s*\))?"
)
"""The number a quantity's text starts with: a qualifier, the number or a range, then a ± or bracketed dispersion. What
follows is the unit."""


@dataclass
class Located:
    """A node's place in its file, for messages and for mapping checker issues back to `file:line`."""

    path: str
    line: int


@dataclass
class Reader:
    """Reads one file's nodes, collecting the problems with what the agent wrote."""

    path: str
    doc: Document
    issues: list[FileIssue] = field(default_factory=list)

    def at(self, node: Node) -> Located:
        return Located(self.path, node.start_mark.line + 1)

    def issue(self, node: Node | None, message: str) -> None:
        line = node.start_mark.line + 1 if node is not None else 1
        self.issues.append(FileIssue(path=self.path, line=line, message=message))

    def issue_at(self, at: Located, message: str) -> None:
        self.issues.append(FileIssue(path=at.path, line=at.line, message=message))

    def entries(self, node: Node, what: str) -> list[tuple[str, Node, Node]]:
        """A mapping's (key, key node, value node), each key once. A repeated key is a problem, not a silent
        overwrite."""
        if not isinstance(node, MappingNode):
            self.issue(node, f"{what} must be a mapping of key: value")
            return []
        seen: set[str] = set()
        out: list[tuple[str, Node, Node]] = []
        for k, v in node.value:
            key = k.value if isinstance(k, ScalarNode) else ""
            if not key:
                self.issue(k, f"{what}: a key must be a plain word")
                continue
            if key in seen:
                self.issue(k, f"'{key}' appears twice in {what}; keep one")
                continue
            seen.add(key)
            out.append((key, k, v))
        return out

    def field(self, node: Node, kind: object, *, where: str, coordinate: bool = False) -> dict[str, object] | None:
        """One of the five shapes as a `Field` dict, or None after reporting why it isn't one."""
        if isinstance(node, MappingNode) and _keys(node) == ["at"]:
            return self._at(node.value[0][1], kind, where=where)
        if where.endswith(".notes") and isinstance(node, ScalarNode) and not is_null(node):
            # A note is commentary, not a value the paper reports: written plainly, it is the agent's own words.
            return {"value": scalar(node).strip(), "basis": "inferred", "evidence": [], "note": "the agent's note"}
        if not isinstance(node, SequenceNode) or not node.value:
            self.issue(node, f"{where}: {SHAPES}")
            return None
        head, *rest = node.value
        anchors: list[tuple[str, str | None, Node]] = []
        note: str | None = None
        for item in rest:
            if isinstance(item, ScalarNode) and note is not None:
                # The note comes last, so a flow list split at a comma in it ([V, note: a, b]) only continues it.
                if ANCHOR.fullmatch(scalar(item).strip()):
                    self.issue(item, f"{where}: the note comes last, after the anchors: {SHAPES}")
                    return None
                note = f"{note}, {scalar(item).strip()}"
            elif isinstance(item, ScalarNode):
                text = scalar(item).strip()
                if not ANCHOR.fullmatch(text):
                    self.issue(
                        item,
                        f"{where}: '{text}' isn't an anchor. A value with a comma in it must be quoted, "
                        f'["12,5 g", p3.b4]; otherwise {SHAPES}',
                    )
                    return None
                anchors.append((text, None, item))
            elif isinstance(item, MappingNode) and len(item.value) == 1:
                key, value = item.value[0]
                name = scalar(key)
                text = scalar(value)
                if name == "note":
                    note = text.strip() or None
                elif ANCHOR.match(name):
                    anchors.append((name, text, item))
                else:
                    self.issue(item, f"{where}: '{name}' is neither an anchor nor note; {SHAPES}")
                    return None
            else:
                self.issue(item, f"{where}: after the value come anchors and an optional note: {SHAPES}")
                return None
        if is_null(head):
            if anchors:
                self.issue(node, f"{where}: a null value cites nothing; give the value, or [null, note: why]")
                return None
            return {"value": None, "basis": "stated", "evidence": [], "note": note}
        read = self.typed(head, kind, where=where, coordinate=coordinate)
        if read is None:
            return None
        value, text = read
        if not anchors and note is None:
            self.issue(
                node,
                f"{where}: say where the paper gives it, [value, anchor], or why it is your judgement, "
                "[value, note: why]",
            )
            return None
        basis = "stated" if anchors and note is None else "derived" if anchors else "inferred"
        evidence = [self._evidence(anchor, pinned, text) for anchor, pinned, _ in anchors]
        return {"value": value, "basis": basis, "evidence": evidence, "note": note}

    def _at(self, node: Node, kind: object, *, where: str) -> dict[str, object] | None:
        anchors = [n for n in (node.value if isinstance(node, SequenceNode) else [node]) if isinstance(n, ScalarNode)]
        if not anchors:
            self.issue(node, f"{where}: at takes an anchor or a list of anchors")
            return None
        texts: list[str] = []
        for a in anchors:
            resolved = self.doc.resolve(a.value.strip())
            if resolved is None:
                self.issue(a, f"{where}: no anchor '{a.value}' in this paper; copy one from paper.md")
                return None
            texts.append(resolved.text)
        text = " ".join(t.strip() for t in texts)
        value = coerce(text, kind, coordinate=False)
        if value is None:
            self.issue(
                node, f"{where}: the text at {', '.join(a.value for a in anchors)} ('{text}') isn't {_what(kind)}"
            )
            return None
        evidence = [{"anchor": a.value.strip(), "quote": t} for a, t in zip(anchors, texts, strict=True)]
        return {"value": value, "basis": "stated", "evidence": evidence, "note": None}

    def typed(
        self, node: Node, kind: object, *, where: str, coordinate: bool = False
    ) -> tuple[object, str | None] | None:
        """The value as the model's type wants it, and the text to find it by in the cited anchors."""
        if kind is QuantityValue and isinstance(node, MappingNode):
            body = {k: scalar(v) for k, _, v in self.entries(node, where) if isinstance(v, ScalarNode)}
            text = body.pop("text", None)
            if text is None:
                self.issue(node, f"{where}: a quantity mapping needs text (and units)")
                return None
            body = {("reported_units" if k == "units" else k): v for k, v in body.items()}
            return {"reported_text": text, "reported_units": body.pop("reported_units", ""), **body}, text
        if kind is StatisticalSummary:
            if not isinstance(node, MappingNode):
                self.issue(node, f"{where}: a statistic is {{statistic: SE, value: '0.2'}}")
                return None
            body = {k: scalar(v) for k, _, v in self.entries(node, where) if isinstance(v, ScalarNode)}
            if "statistic" not in body or "value" not in body:
                self.issue(node, f"{where}: a statistic is {{statistic: SE, value: '0.2'}}")
                return None
            raw = body["value"]
            return {"statistic_name": body["statistic"], "statistic_value": _number_or_text(raw)}, raw
        if get_origin(kind) is list:
            if not isinstance(node, SequenceNode) or not all(isinstance(n, ScalarNode) for n in node.value):
                self.issue(node, f"{where}: this value is a list, written [[a, b], anchor]")
                return None
            return [scalar(n) for n in node.value if isinstance(n, ScalarNode)], None
        if not isinstance(node, ScalarNode):
            self.issue(node, f"{where}: the value must be plain text here")
            return None
        text = scalar(node)
        value = coerce(text, kind, coordinate=coordinate)
        if value is None:
            self.issue(node, f"{where}: '{text}' isn't {_what(kind)}")
            return None
        return value, None if kind is bool else text

    def _evidence(self, anchor: str, pinned: str | None, text: str | None) -> dict[str, str]:
        if pinned is not None:
            return {"anchor": anchor, "quote": pinned}
        resolved = self.doc.resolve(anchor)
        if resolved is None:
            # The checker says the anchor doesn't exist; the quote only has to be non-empty.
            return {"anchor": anchor, "quote": text if text is not None and text.strip() else anchor}
        found = span(resolved.text, text) if text is not None and text.strip() else None
        return {"anchor": anchor, "quote": found or resolved.text}


def coerce(text: str, kind: object, *, coordinate: bool) -> object | None:
    """`text` as the value a `Field[kind]` holds, or None when it can't be one."""
    text = text.strip()
    if kind is str or get_origin(kind) is Literal:
        if get_origin(kind) is Literal and text not in get_args(kind):
            return None
        return text
    if kind is bool:
        low = text.lower()
        return True if low in TRUE else False if low in FALSE else None
    if kind is int:
        n = _number_or_text(text)
        return int(n) if isinstance(n, float) and n.is_integer() else None
    if kind is float:
        n = _number_or_text(text)
        return n if isinstance(n, float) else None
    if kind is QuantityValue:
        return {"reported_text": text, "reported_units": "" if coordinate else quantity_units(text)}
    if kind in (DateRange, DepthRange):
        return {"reported_text": text}
    return None


def quantity_units(text: str) -> str:
    """The unit a quantity's text ends with: whatever follows its number ("12.4 g m-2" gives "g m-2")."""
    m = _LEAD.match(text)
    return text[m.end() :].strip() if m else ""


def field_kind(model: type[Model], attr: str) -> object | None:
    """T for an attribute declared `Field[T]` (or `Field[T] | None`), else None."""
    info = model.model_fields.get(attr)
    if info is None:
        return None
    annotation = info.annotation
    options = get_args(annotation) if get_origin(annotation) in (Union, types.UnionType) else (annotation,)
    for t in options:
        if isinstance(t, type) and issubclass(t, Field):
            return t.__pydantic_generic_metadata__["args"][0]
    return None


def plain(node: Node) -> object:
    """A node as plain data: strings, lists and dicts, with an unquoted null read as None."""
    if isinstance(node, ScalarNode):
        return None if node.style is None and node.value in NULL else node.value
    if isinstance(node, SequenceNode):
        return [plain(n) for n in node.value]
    if isinstance(node, MappingNode):
        return {k.value: plain(v) for k, v in node.value if isinstance(k, ScalarNode)}
    return None


def scalar(node: Node) -> str:
    """A scalar node's text; "" for anything else."""
    return str(node.value) if isinstance(node, ScalarNode) else ""


def is_null(node: Node) -> bool:
    return isinstance(node, ScalarNode) and node.style is None and node.value in NULL


def _keys(node: MappingNode) -> list[str]:
    return [k.value for k, _ in node.value if isinstance(k, ScalarNode)]


def _number_or_text(text: str) -> float | str:
    """The number `text` prints, read as a table cell is ("1,234.5", "12,5"), or the text when it is no single
    number."""
    read = read_number(norm(text))
    return read[0] if read is not None else text


def _what(kind: object) -> str:
    if get_origin(kind) is Literal:
        return "one of " + ", ".join(str(a) for a in get_args(kind))
    names = {str: "text", int: "a whole number", float: "a number", bool: "true or false"}
    return names.get(kind, "a value of this kind") if isinstance(kind, type) else "a value of this kind"
