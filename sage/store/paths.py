"""Where everything lives under SAGE_HOME."""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Home:
    root: Path

    @property
    def papers(self) -> Path:
        return self.root / "papers"

    @property
    def add_lock(self) -> Path:
        """Held while a new paper is checked against the library and written, so one file is never added twice."""
        return self.root / "add.lock"

    def paper(self, paper_id: str) -> Path:
        return self.papers / paper_id

    def pdf(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "paper.pdf"

    def paper_json(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "paper.json"

    def document(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "document.json"

    def reviews(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "reviews.jsonl"

    def runs(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "runs"

    def run_lock(self, paper_id: str) -> Path:
        """Held while a run is checked against the paper's live runs and queued, so two can't both start."""
        return self.paper(paper_id) / "run.lock"

    def run(self, paper_id: str, run_id: str) -> "RunPaths":
        return RunPaths(self.runs(paper_id) / run_id)


@dataclass(frozen=True)
class RunPaths:
    dir: Path

    @property
    def info(self) -> Path:
        return self.dir / "run.json"

    @property
    def events(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def draft(self) -> Path:
        return self.dir / "draft.json"

    @property
    def records(self) -> Path:
        return self.dir / "records.json"

    @property
    def quotes(self) -> Path:
        return self.dir / "quotes.json"

    @property
    def document(self) -> Path:
        """The document the run was extracted against, so a later reparse of the paper never moves its evidence."""
        return self.dir / "document.json"
