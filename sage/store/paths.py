"""Where everything lives under SAGE_HOME."""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from sage.store.workspace import AGENT_FOLDERS

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
"""Paper and run ids. They name folders, so one from outside is checked against this before it reaches a path."""

ChangeKind = Literal["library", "paper", "files", "reviews", "run", "log"]
"""library: a paper's folder came or went. paper: its PDF, document, paper.md or images. files: what the agent writes,
records/, tables/*.csv and pages/*.md. reviews: the scientist's reviews. run: a run's own files. log: a run's events,
which the run log streams on its own."""


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

    @property
    def ui_lock(self) -> Path:
        """Held by `sage ui` for as long as it serves, so a second one points to it instead of serving the same data."""
        return self.root / "ui.lock"

    @property
    def ui_info(self) -> Path:
        """Where the live `sage ui` serves, for a second one to point to."""
        return self.root / "ui.json"

    @property
    def runner_lock(self) -> Path:
        """Held by the one runner process for as long as it lives, so there is never a second."""
        return self.root / "runner.lock"

    @property
    def runner_pid(self) -> Path:
        """The live runner's pid, which tells a reader whether a queued run has a runner to take it on."""
        return self.root / "runner.pid"

    @property
    def runner_log(self) -> Path:
        """The runner's stdout and stderr."""
        return self.root / "runner.log"

    def paper(self, paper_id: str) -> Path:
        """The paper's folder. Agents read and write the files at its top; SAGE keeps its own under `.sage/`."""
        return self.papers / paper_id

    def pdf(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "paper.pdf"

    def paper_md(self, paper_id: str) -> Path:
        """The anchored paper view, written again whenever the effective document changes."""
        return self.paper(paper_id) / "paper.md"

    def pages_dir(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "pages"

    def page_png(self, paper_id: str, page: int) -> Path:
        return self.pages_dir(paper_id) / f"p{page}.png"

    def tables_dir(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "tables"

    def table_png(self, paper_id: str, anchor: str) -> Path:
        return self.tables_dir(paper_id) / f"{anchor}.png"

    def table_csv(self, paper_id: str, anchor: str) -> Path:
        """An agent's transcription of a table's grid, which SAGE grounds in the text layer."""
        return self.tables_dir(paper_id) / f"{anchor}.csv"

    def figures_dir(self, paper_id: str) -> Path:
        return self.paper(paper_id) / "figures"

    def figure_png(self, paper_id: str, anchor: str) -> Path:
        return self.figures_dir(paper_id) / f"{anchor}.png"

    def records_dir(self, paper_id: str) -> Path:
        """The agent's records, as YAML."""
        return self.paper(paper_id) / "records"

    def sage_dir(self, paper_id: str) -> Path:
        """What SAGE alone writes for the paper."""
        return self.paper(paper_id) / ".sage"

    def paper_json(self, paper_id: str) -> Path:
        return self.sage_dir(paper_id) / "paper.json"

    def document(self, paper_id: str) -> Path:
        return self.sage_dir(paper_id) / "document.json"

    def reviews(self, paper_id: str) -> Path:
        return self.sage_dir(paper_id) / "reviews.jsonl"

    def runs(self, paper_id: str) -> Path:
        return self.sage_dir(paper_id) / "runs"

    def run_lock(self, paper_id: str) -> Path:
        """Held while a run is checked against the paper's live runs and queued, so two can't both start."""
        return self.sage_dir(paper_id) / "run.lock"

    def snapshots(self, paper_id: str) -> Path:
        """Numbered copies of records/ and the table CSVs, one per change that compiled."""
        return self.sage_dir(paper_id) / "snapshots"

    def table_cache(self, paper_id: str, anchor: str) -> Path:
        """A table grounded from its CSV, kept until the CSV or the grounding changes."""
        return self.sage_dir(paper_id) / "tables" / f"{anchor}.json"

    def rendered(self, paper_id: str) -> Path:
        """What the page, table and figure images were rendered from, so they are rendered again when it changes."""
        return self.sage_dir(paper_id) / "rendered.json"

    def run(self, paper_id: str, run_id: str) -> "RunPaths":
        return RunPaths(self.runs(paper_id) / run_id)

    def change(self, rel: Path) -> tuple[str, ChangeKind, str | None] | None:
        """What a path under papers/, relative to it, is to a screen: its paper, its kind, and for `run` and `log` its
        run. SAGE's caches, snapshots and locks, and anything else in the folder, are nothing to it."""
        if not rel.parts or ID.fullmatch(rel.parts[0]) is None:
            return None
        pid, path = rel.parts[0], self.papers / rel
        runs = self.runs(pid)
        if path == self.paper(pid):
            return pid, "library", None
        if path in (self.paper_json(pid), self.document(pid), self.paper_md(pid), self.pdf(pid)):
            return pid, "paper", None
        if path == self.reviews(pid):
            return pid, "reviews", None
        if path != runs and path.is_relative_to(runs):
            run_id = path.relative_to(runs).parts[0]
            if path == self.run(pid, run_id).events:
                return pid, "log", run_id
            return (pid, "run", run_id) if ID.fullmatch(run_id) is not None else None
        images = (self.pages_dir(pid), self.tables_dir(pid), self.figures_dir(pid))
        if path.parent in images and path.name.endswith(".png"):
            return pid, "paper", None
        if len(rel.parts) > 2 and path.name.endswith(AGENT_FOLDERS.get(rel.parts[1], ())):
            return pid, "files", None
        return None


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
    def stop(self) -> Path:
        """Asks the runner to stop the run. It goes once the runner has stopped it, or when the run starts again."""
        return self.dir / "stop"

    @property
    def document(self) -> Path:
        """The document the run was extracted against, so a later reparse of the paper never moves its evidence."""
        return self.dir / "document.json"

    @property
    def strikes(self) -> Path:
        """How often each open blocking issue has come back, per target and message."""
        return self.dir / "strikes.json"

    @property
    def marks(self) -> Path:
        """Unresolved marks code set, such as `repeated_error`, merged into the draft at every check."""
        return self.dir / "marks.json"

    @property
    def permission(self) -> Path:
        """What an agent SAGE started asks you to allow, and your answer once you give it."""
        return self.dir / "permission.json"
