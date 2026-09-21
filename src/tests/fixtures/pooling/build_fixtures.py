"""Rebuild the real Marker-derived fixtures for tests/test_pooling_evidence.py.

    python tests/fixtures/pooling/build_fixtures.py        (run from src/)

For each paper, every Table block plus a few named prose/caption blocks is kept
together with a margin of rendered blocks around it (3 before, 6 after), as
CONTIGUOUS rendered-block segments (overlaps merged). Contiguity matters: the
pooling windows depend on exactly which blocks follow a table, and the 6-block
margin keeps any table's whole window inside its own segment, so no window can
run across a seam into an unrelated segment. Block text and anchors are copied
UNMODIFIED from src/paper/<paper>/content.md; provenance keeps block_type,
page_id, section_path and the rendered flag.
"""
import json
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(SRC))
from pipeline import content_reader as cr  # noqa: E402

PAPERS = SRC / "paper"
OUT = Path(__file__).resolve().parent
BEFORE, AFTER = 3, 6
EXTRA = {
    "Felipe-2010-Cultivar": [],
    "Daren-1997-Canopy": ["b:0452", "b:0462", "b:0838", "b:0850"],
    "Kathryn-2020-Winter": ["b:0093", "b:0131"],
    "Berntson-1997-Regenerating": ["b:0058"],
    "Paul-1998-Foliar": ["b:0006", "b:0422", "b:0470"],
}

for paper, extra in EXTRA.items():
    prov = cr._load_provenance(paper, PAPERS)
    texts = cr._rendered_block_texts(paper, PAPERS)
    rendered = sorted((a for a in prov if a in texts), key=cr._anchor_sort_key)
    index = {a: i for i, a in enumerate(rendered)}
    keep: set[int] = set()
    for a in [x for x in rendered if prov[x].get("block_type") == "Table"] + extra:
        i = index[a]
        keep.update(range(max(0, i - BEFORE), min(len(rendered), i + AFTER + 1)))
    chosen = [rendered[i] for i in sorted(keep)]
    (OUT / paper).mkdir(exist_ok=True)
    (OUT / paper / "content.md").write_text("".join(f"{texts[a]}\n⟦{a}⟧\n\n" for a in chosen), encoding="utf-8")
    (OUT / paper / "provenance.json").write_text(json.dumps({
        a: {"block_type": prov[a].get("block_type"), "page_id": prov[a].get("page_id"),
            "section_path": prov[a].get("section_path"), "rendered_in_content_md": True}
        for a in chosen
    }, indent=1), encoding="utf-8")
    print(paper, len(chosen), "blocks")
