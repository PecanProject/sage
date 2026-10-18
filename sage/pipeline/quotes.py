"""The line boxes of a run's quotes, so the paper marks the quoted words and not the whole block. They are worked out
once from the PDF, when the run settles, and kept in `runs/<id>/quotes.json`. A run without that file gets them the
first time it is viewed."""

import hashlib
import json

from pydantic import BaseModel, Field

from sage.ir.schema import fields_of
from sage.ir.settled import Settled
from sage.parse.document import Document
from sage.parse.quotes import LOCATOR_VERSION, QuoteLines, locate
from sage.store import files
from sage.store.paths import Home


class QuoteBoxes(BaseModel):
    key: str = Field(description="What the boxes were worked out from: the locator, the document and the quotes.")
    lines: QuoteLines


def cites(settled: Settled) -> set[tuple[str, str]]:
    out = {(ev.anchor, ev.quote) for r in settled.records for f in fields_of(r.record).values() for ev in f.evidence}
    for t in settled.tables:
        if t.roles is not None:
            out |= {(ev.anchor, ev.quote) for ev in t.roles.scope_evidence}
    return out


def quote_lines(home: Home, paper_id: str, run_id: str, settled: Settled, doc: Document) -> QuoteLines:
    wanted = cites(settled)
    key = _key(wanted, doc)
    path = home.run(paper_id, run_id).quotes
    try:
        cached = files.read_model_or_none(path, QuoteBoxes)
    except files.UnreadableError:
        cached = None  # A derived file: one that won't parse is worked out again, and nothing is lost.
    if cached is not None and cached.key == key:
        return cached.lines
    lines = locate(files.read_bytes(home.pdf(paper_id)), doc, wanted)
    files.write_model(path, QuoteBoxes(key=key, lines=lines))
    return lines


def _key(wanted: set[tuple[str, str]], doc: Document) -> str:
    body = json.dumps([LOCATOR_VERSION, doc.sha256, doc.parser_version, sorted(wanted)], ensure_ascii=False)
    return hashlib.sha256(body.encode()).hexdigest()
