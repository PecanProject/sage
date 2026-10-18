"""The table pass: one model call per table turns an upright image of the table into a grid of cell strings. The
grid is a transcription only; grounding in the text layer decides which cells are trusted."""

from dataclasses import dataclass

from pydantic import BaseModel, Field
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.exceptions import AgentRunError
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from sage.agents.models import explain, usage_of
from sage.events import Usage
from sage.parse.document import Table
from sage.parse.grounding import Grid

INSTRUCTIONS = """\
You transcribe one table from a scientific paper into a grid of cells.

- Copy every cell exactly as printed: the same digits, signs, letters, symbols, superscript markers and spacing \
of words. Never round, correct, translate or fill in a value.
- Give every row from top to bottom, header rows first. Every row has the same number of cells.
- A header that spans several columns is repeated in each column it spans. A row label that spans several rows \
is repeated in each row.
- An empty cell is "". A dash printed in a cell is that dash.
- A value and its ± part or bracketed part stay in one cell, with any letters after them: "4.1 ± 0.2 b".
- Footnotes and notes under the table go in `notes`, verbatim. The caption is not part of the table.
- If the image is not a table of values (it is a figure, or text), set is_table false and leave rows empty.

The words the PDF's text layer holds inside the table area are given as a hint, in rough reading order. Prefer \
their spelling where the image is unclear, but the image decides the layout.
"""


class TableGrid(BaseModel):
    is_table: bool = Field(description="False when the image is a figure or plain text rather than a table.")
    rows: list[list[str]] = Field(description="Every row top to bottom, header rows first, one string per cell.")
    header_rows: int = Field(ge=0, description="How many of the first rows are column headers.")
    notes: str | None = Field(default=None, description="Footnotes under the table, verbatim.")


@dataclass(frozen=True)
class TableJob:
    table: Table
    image: bytes
    """A PNG of the table region, turned so its text reads upright."""
    text_hint: str


@dataclass(frozen=True)
class TableResult:
    table_id: str
    grid: Grid | None
    usage: Usage
    error: str | None = None
    retry: bool = False
    """The call failed (the provider, or an output the model could not get right), so a later run tries again."""


def agent() -> Agent[None, TableGrid]:
    return Agent(output_type=TableGrid, instructions=INSTRUCTIONS, name="tables", retries=2)


async def transcribe(
    job: TableJob, *, runner: Agent[None, TableGrid], model: Model, settings: ModelSettings
) -> TableResult:
    t = job.table
    prompt = [
        f"{t.label}. Caption: {t.caption}",
        BinaryContent(data=job.image, media_type="image/png"),
        f"Text-layer words in the table area:\n{job.text_hint}",
    ]
    try:
        result = await runner.run(prompt, model=model, model_settings=settings)
    except AgentRunError as e:
        return TableResult(table_id=t.anchor, grid=None, usage=Usage(), error=explain(e), retry=True)
    out = result.output
    usage = usage_of(result.usage)
    if not out.is_table or not any(out.rows):
        return TableResult(table_id=t.anchor, grid=None, usage=usage, error="the model saw no table in the region")
    width = max(len(r) for r in out.rows)
    rows = [[*r, *([""] * (width - len(r)))] for r in out.rows]
    header_rows = min(out.header_rows, len(rows))
    return TableResult(table_id=t.anchor, grid=Grid(rows=rows, header_rows=header_rows, notes=out.notes), usage=usage)
