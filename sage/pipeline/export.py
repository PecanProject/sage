"""Exports. JSON is records.json as it is. The workbook is the PEcAn calibration-validation curation workbook
(docs/pecan/workbook.md): one tab per table, the curators' columns first, values in the units the paper prints,
statistics as published, and every row with its SAGE status and review. The Data screen shows the same tabs, so what a
scientist reviews is what the workbook holds."""

import csv
import io
import json
import re
import zipfile
from collections import defaultdict
from datetime import date

import xlsxwriter
from pydantic import BaseModel

from sage.domain.dates import season_window
from sage.domain.normalize import NUMBER, read_number
from sage.domain.stats import statname
from sage.domain.units import ascii_unit
from sage.ir.schema import (
    Citation,
    Coverage,
    Crop,
    DateRange,
    Entity,
    Field,
    Management,
    Method,
    Observation,
    QuantityValue,
    Site,
    Species,
    StatisticalSummary,
    Study,
    Treatment,
    TreatmentPair,
    Variable,
)
from sage.ir.settled import Settled, SettledRecord
from sage.parse.document import Document
from sage.pipeline.review import comments, latest_reviews, reason
from sage.store.models import Review, RunInfo

type Value = str | int | float | None

_STOPWORDS = frozenset(
    ("a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "under", "with")
)
_INITIALS = re.compile(r"^(?:[A-Z]\.?-?)+$")
"""An initial or a run of them ("E.", "KE", "C.M."), so the surname is the last other word, whether the byline is
"White KE" or "Nicole E. Tautges"."""
FILL_STATUS = {
    "FILLED_PUBLICATION_TABLE": "Read from a table cell of the paper; source_table names the table.",
    "FILLED_PUBLICATION_TEXT": "Read from the paper's text; source names the page.",
}
DATA_ORIGIN = {"FILLED_PUBLICATION_TABLE": "publication_table", "FILLED_PUBLICATION_TEXT": "publication_text"}
STATUS = {
    "verified": "Every check passed on quoted or calculated evidence.",
    "flagged": "Passed, but a person should look: inferred, an approximate match or a checker warning.",
    "unresolved": "Not settled; status_reason says why.",
}


class Tab(BaseModel):
    """One sheet of the workbook. `fields` names, per column, the record field the column reads, so a screen can show
    that field's status and evidence; `review` is the columns a reviewer reads first, in reading order; `ids` is the
    record each row shows (a Management applied to several treatments is a row per treatment, one record)."""

    name: str
    columns: list[str]
    rows: list[list[Value]]
    fields: list[str | None] = []
    review: list[str] = []
    ids: list[str | None] = []


def workbook(
    settled: Settled, doc: Document, reviews: list[Review], run: RunInfo, *, keep_rejected: bool = False
) -> list[Tab]:
    """The workbook's tabs. A record a scientist rejected is left out, and the README counts them, unless
    `keep_rejected` asks for every record, as the review screen does."""
    return _Book(settled, doc, reviews, run, keep_rejected=keep_rejected).tabs()


def xlsx(tabs: list[Tab]) -> bytes:
    buffer = io.BytesIO()
    # Paper text and model text are data: a quote that starts with "=" must not become a formula.
    options = {"in_memory": True, "strings_to_numbers": False, "strings_to_urls": False, "strings_to_formulas": False}
    book = xlsxwriter.Workbook(buffer, options)
    bold = book.add_format({"bold": True})
    for tab in tabs:
        sheet = book.add_worksheet(tab.name)
        sheet.write_row(0, 0, tab.columns, bold)
        for i, row in enumerate(tab.rows, start=1):
            sheet.write_row(i, 0, row)
        sheet.freeze_panes(1, 0)
        sheet.set_column(0, len(tab.columns) - 1, 18)
        if tab.rows:
            sheet.autofilter(0, 0, len(tab.rows), len(tab.columns) - 1)
    book.close()
    return buffer.getvalue()


def csv_zip(tabs: list[Tab]) -> bytes:
    """One CSV per tab, for version control (protocol §3)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for tab in tabs:
            text = io.StringIO()
            writer = csv.writer(text, lineterminator="\n")
            writer.writerow(tab.columns)
            writer.writerows(tab.rows)
            archive.writestr(f"{tab.name}.csv", text.getvalue())
    return buffer.getvalue()


def citation_key(c: Citation) -> str | None:
    """authorYYYYabc: the first author's surname, the year, and the first letters of the title's first three
    informative words (protocol §6.1)."""
    author, year, title = c.author.value, c.year.value, c.title.value
    if author is None or year is None:
        return None
    first = str(re.split(r"[,;&]| and ", author.strip())[0])
    names = [n for n in first.split() if not _INITIALS.match(n)]
    if not names:
        return None
    words = [str(w) for w in re.findall(r"[A-Za-z]+", title or "") if str(w).lower() not in _STOPWORDS][:3]
    return re.sub(r"\W", "", names[-1]).lower() + str(year) + "".join(w[0].lower() for w in words)


class _Book:
    def __init__(
        self, settled: Settled, doc: Document, reviews: list[Review], run: RunInfo, *, keep_rejected: bool
    ) -> None:
        self.settled = settled
        self.doc = doc
        self.run = run
        latest = latest_reviews(reviews)
        said = comments(reviews)
        self.rejected = {rid for rid, r in latest.items() if r.action == "reject"}
        self.review = {
            rid: text
            for rid in {*latest, *said}
            if (text := _review_text(rid, latest.get(rid), reviews, said.get(rid, []))) is not None
        }
        self.kept = [r for r in settled.records if keep_rejected or r.record.id not in self.rejected]
        self.by_id: dict[str, Entity] = {r.record.id: r.record for r in settled.records}
        self.citation = next(iter(self.of(Citation)), None)
        key = citation_key(self.citation) if self.citation is not None else None
        self.dataset_id = key or settled.paper_id
        self.checks: list[str] = []

    def of[E: Entity](self, kind: type[E]) -> list[E]:
        return [r.record for r in self.kept if isinstance(r.record, kind)]

    def settled_of[E: Entity](self, kind: type[E]) -> list[tuple[SettledRecord, E]]:
        return [(r, r.record) for r in self.kept if isinstance(r.record, kind)]

    def tail(self, r: SettledRecord) -> list[Value]:
        return [r.record.id, r.status, self.review.get(r.record.id)]

    def cite(self, citation_id: str) -> str:
        c = self.by_id.get(citation_id)
        if isinstance(c, Citation):
            return c.persistent_identifier.value or citation_key(c) or c.id
        return citation_id

    def citation_id(self, citation_id: str) -> str:
        c = self.by_id.get(citation_id)
        return (citation_key(c) if isinstance(c, Citation) else None) or citation_id

    def tabs(self) -> list[Tab]:
        tabs = [
            self.citations(),
            self.sites(),
            self.treatments(),
            self.methods(),
            self.observations(),
            self.managements(),
            self.variables(),
            self.crops(),
            self.treatment_pairs(),
        ]
        if self.of(Coverage):
            tabs.append(self.coverage())
        return [self.readme(), *(_described(t) for t in tabs)]

    def citations(self) -> Tab:
        rows: list[list[Value]] = [
            [
                citation_key(c) or c.id,
                c.author.value,
                c.year.value,
                c.title.value,
                c.persistent_identifier.value,
                *self.tail(r),
            ]
            for r, c in self.settled_of(Citation)
        ]
        return Tab(name="citations", columns=_columns("citation_id, author, year, title, doi"), rows=rows)

    def sites(self) -> Tab:
        rows: list[list[Value]] = []
        for r, s in self.settled_of(Site):
            elevation = _value(s.elevation)
            rows.append(
                [
                    s.id,
                    _degrees(s.latitude),
                    _degrees(s.longitude),
                    _number(elevation),
                    _units(elevation),
                    _value(s.name),
                    _value(s.country),
                    _value(s.state_or_region),
                    _value(s.nearest_city),
                    _value(s.soil_context),
                    _value(s.description),
                    *self.tail(r),
                ]
            )
        columns = _columns(
            "name, latitude, longitude, elevation, elevation_units, site_name, country, state, city, soil, notes"
        )
        return Tab(name="sites", columns=columns, rows=rows)

    def treatments(self) -> Tab:
        rows: list[list[Value]] = []
        for r, t in self.settled_of(Treatment):
            study = self.by_id.get(t.study_id.value or "")
            period = _value(study.period) if isinstance(study, Study) else None
            control = _value(t.control_status)
            rows.append(
                [
                    t.id,
                    _value(t.definition),
                    _iso(period.earliest) if period is not None else None,
                    _iso(period.latest) if period is not None else None,
                    None if control is None else ("yes" if control else "no"),
                    _value(t.name),
                    "; ".join(f"{k}={v}" for k, v in t.factor_levels.items()) or None,
                    t.site_id,
                    t.study_id.value,
                    self.cite(t.citation_id),
                    *self.tail(r),
                ]
            )
        columns = _columns(
            "name, definition, start_date, end_date, control, label, factor_levels, sitename, study, citation"
        )
        return Tab(name="treatments", columns=columns, rows=rows)

    def methods(self) -> Tab:
        rows: list[list[Value]] = [
            [m.id, _value(m.description), self.cite(m.citation_id), _value(m.name), *self.tail(r)]
            for r, m in self.settled_of(Method)
        ]
        return Tab(name="methods", columns=_columns("method name, description, citation_id, label"), rows=rows)

    def variables(self) -> Tab:
        rows: list[list[Value]] = [
            [
                v.id,
                _value(v.description) or _value(v.name),
                ascii_unit(units) if (units := _value(v.units)) is not None else None,
                _value(v.notes),
                _value(v.name),
                *self.tail(r),
            ]
            for r, v in self.settled_of(Variable)
        ]
        return Tab(name="variables", columns=_columns("name, description, units, notes, label"), rows=rows)

    def crops(self) -> Tab:
        rows: list[list[Value]] = []
        grown: set[str] = set()
        for r, c in self.settled_of(Crop):
            grown.add(c.species_id)
            species = self.by_id.get(c.species_id)
            rows.append(
                [
                    _value(species.scientific_name) if isinstance(species, Species) else None,
                    _value(c.common_name) or (_value(species.common_name) if isinstance(species, Species) else None),
                    _value(c.notes),
                    _value(c.cultivar),
                    _value(c.cover_crop),
                    *self.tail(r),
                ]
            )
        for r, s in self.settled_of(Species):
            if s.id not in grown:
                rows.append([_value(s.scientific_name), _value(s.common_name), None, None, None, *self.tail(r)])
        return Tab(
            name="crops", columns=_columns("scientific name, common name, notes, variety, cover_crop"), rows=rows
        )

    def treatment_pairs(self) -> Tab:
        rows: list[list[Value]] = [
            [
                p.id,
                p.treatment_id_1,
                p.treatment_id_2,
                _value(p.comparison_factor),
                _value(p.comparison_label),
                _value(p.use_for_validation),
                p.notes,
                *self.tail(r),
            ]
            for r, p in self.settled_of(TreatmentPair)
        ]
        columns = _columns(
            "pair_id, treatment_id_1, treatment_id_2, comparison_factor, comparison_label, use_for_validation, notes"
        )
        return Tab(name="treatment_pairs", columns=columns, rows=rows)

    def coverage(self) -> Tab:
        rows: list[list[Value]] = [
            [
                c.system,
                c.treatment_contrast,
                c.site_id,
                c.variable_id,
                c.daily_rows,
                c.annual_rows,
                c.seasonal_rows,
                c.static_observation_rows,
                c.weather_rows,
                c.initial_data_sources,
                c.notes,
                *self.tail(r),
            ]
            for r, c in self.settled_of(Coverage)
        ]
        columns = _columns(
            "System, Treatment contrast, sitename, variable, daily_rows, annual_rows, seasonal_rows, "
            "static_observation_rows, weather_rows, Initial data sources, Notes"
        )
        return Tab(name="coverage", columns=columns, rows=rows)

    def observations(self) -> Tab:
        rows = [self._observation(r, o) for r, o in self.settled_of(Observation)]
        return Tab(name="observations", columns=list(OBSERVATION_COLUMNS), rows=rows)

    def _observation(self, r: SettledRecord, o: Observation) -> list[Value]:
        value = o.value.value
        stat = _value(o.statistical_encoding)
        code = statname(stat.statistic_name) if stat is not None else None
        if stat is not None and code is None:
            self.checks.append(
                f"{o.id}: statistic '{stat.statistic_name}' is not one the protocol names; kept as printed"
            )
        stat_value, interval = _statistic(stat)
        when = _value(o.temporal_info)
        start, end, date_note = self._window(o.temporal_info, o.site_id)
        depth = _value(o.depth)
        crop, variety = self._crop(o.crop_id, o.species_id)
        letters = _value(o.letters)
        ev = o.value.evidence[0] if o.value.evidence else None
        fill = self._fill_status(ev.anchor) if ev is not None else None
        numeric = value.reported_numeric_value if value is not None else None
        attributes = {
            k: v
            for k, v in {
                "statistic_as_reported": stat.statistic_name
                if stat is not None and code != stat.statistic_name
                else None,
                "ci_lower": interval[0] if interval is not None else None,
                "ci_upper": interval[1] if interval is not None else None,
                "letters": letters,
                "letters_compare": _letters_compare(o.letters),
                "reported_units_printed": value.reported_units
                if value is not None and value.reported_units != _units(value)
                else None,
                "converted_value": value.converted_value if value is not None else None,
                "converted_units": value.converted_units if value is not None else None,
                "conversion_formula": value.conversion_formula if value is not None else None,
            }.items()
            if v is not None
        }
        scope = o.reported_effect_scope.value
        raw = o.is_raw_replicate_level.value
        printed = f"reported as '{value.reported_text}'" if value is not None and numeric is None else None
        notes = [n for n in (_value(o.notes), printed, date_note) if n is not None]
        return [
            start,
            end,
            crop,
            variety,
            o.site_id,
            o.variable_id,
            o.method_id,
            numeric,
            _value(o.n),
            code or (stat.statistic_name if stat is not None else None),
            stat_value,
            o.treatment_id,
            _value(o.replicate_id),
            None,
            depth.top_cm if depth is not None else None,
            depth.bottom_cm if depth is not None else None,
            self.cite(o.citation_id),
            fill,
            "; ".join(notes) or None,
            self.dataset_id,
            _units(value),
            "replicate" if raw is True else scope,
            self.citation_id(o.citation_id),
            DATA_ORIGIN.get(fill or ""),
            "SOURCE_REPORTED" if ev is not None else None,
            json.dumps(attributes, ensure_ascii=False) if attributes else None,
            self._table_label(ev.anchor) if ev is not None else None,
            letters,
            scope,
            ", ".join(_value(o.aggregated_over_factors) or []) or None,
            when.relative_timing if when is not None else None,
            when.relative_timing_days if when is not None else None,
            when.reported_text if when is not None else None,
            depth.reported_text if depth is not None else None,
            _value(o.variable_name),
            value.reported_text if value is not None else None,
            self._source(ev.anchor) if ev is not None else None,
            ev.quote if ev is not None else None,
            *self.tail(r),
            reason(r),
        ]

    def _crop(self, crop_id: str | None, species_id: str | None) -> tuple[str | None, str | None]:
        """The crop's name (its common name, else its species') and its cultivar."""
        crop = self.by_id.get(crop_id or "")
        species = self.by_id.get(crop.species_id if isinstance(crop, Crop) else species_id or "")
        named = None
        if isinstance(species, Species):
            named = _value(species.common_name) or _value(species.scientific_name)
        if isinstance(crop, Crop):
            return _value(crop.common_name) or named, _value(crop.cultivar)
        return named, None

    def _fill_status(self, anchor: str) -> str:
        table = self.doc.table(_table_of(anchor)) is not None
        return "FILLED_PUBLICATION_TABLE" if table else "FILLED_PUBLICATION_TEXT"

    def managements(self) -> Tab:
        treatments = {t.id: t for t in self.of(Treatment)}
        sites = {t.site_id for t in treatments.values()}
        only_site = next(iter(sites)) if len(sites) == 1 else None
        rows: list[list[Value]] = []
        for r, m in self.settled_of(Management):
            ids: list[str | None] = list(_value(m.treatment_ids) or []) or [None]
            amount = _value(m.amount)
            crop = self.by_id.get(m.crop_id or "")
            cover = _value(crop.cover_crop) if isinstance(crop, Crop) else None
            crop_name, cultivar = self._crop(m.crop_id, None)
            attributes = "; ".join(
                f"{k}={v}" for k, v in (("material", _value(m.material)), ("cultivar", cultivar)) if v is not None
            )
            when = _value(m.date)
            for tid in ids:
                site = treatments[tid].site_id if tid in treatments else only_site
                start, end, date_note = self._window(m.date, site)
                every = "applies to every treatment" if tid is None else None
                notes = [n for n in (_value(m.notes), date_note, every) if n is not None]
                rows.append(
                    [
                        m.id if len(ids) <= 1 else f"{m.id}_{tid}",
                        site,
                        tid,
                        start,
                        end,
                        _value(m.event_type),
                        cover,
                        crop_name,
                        _number(amount),
                        _units(amount),
                        "; ".join(notes) or None,
                        attributes or None,
                        self.cite(m.citation_id),
                        when.relative_timing if when is not None else None,
                        when.relative_timing_days if when is not None else None,
                        when.reported_text if when is not None else None,
                        amount.reported_text if amount is not None else None,
                        *self.tail(r),
                    ]
                )
        return Tab(name="managements", columns=list(MANAGEMENT_COLUMNS), rows=rows)

    def _window(self, f: Field[DateRange] | None, site_id: str | None) -> tuple[str | None, str | None, str | None]:
        """min_date and max_date, and a note when the paper gives no exact window: a season is read as its months
        in the site's hemisphere (protocol §10.1), a relative timing and a missing date stay blank."""
        d = _value(f)
        if f is None or d is None:
            return None, None, (f.note if f is not None else None)
        if d.earliest is not None:
            return _iso(d.earliest), _iso(d.latest), None
        if d.relative_timing is not None:
            return None, None, None
        site = self.by_id.get(site_id or "")
        latitude = _degrees(site.latitude) if isinstance(site, Site) else None
        if latitude is None:
            return None, None, f"'{d.reported_text}' has no dates, and the site's hemisphere is unknown"
        window = season_window(d.reported_text, [e.quote for e in f.evidence], southern=latitude < 0)
        if window is None:
            return None, None, f"'{d.reported_text}' gives no date window"
        hemisphere = "southern" if latitude < 0 else "northern"
        return (
            _iso(window[0]),
            _iso(window[1]),
            f"'{d.reported_text}' read as the meteorological season(s) of the {hemisphere} hemisphere",
        )

    def _source(self, anchor: str) -> str:
        table = self.doc.table(_table_of(anchor))
        page = anchor.split(".", maxsplit=1)[0].removeprefix("p")
        return f"{table.label}, p. {page}" if table is not None else f"p. {page}"

    def _table_label(self, anchor: str) -> str | None:
        table = self.doc.table(_table_of(anchor))
        return table.label if table is not None else None

    def readme(self) -> Tab:
        c = self.citation
        rows: list[list[Value]] = [
            ["dataset_id", self.dataset_id],
            ["title", _value(c.title) if c is not None else None],
            ["citation", _reference(c) if c is not None else None],
            ["doi", _value(c.persistent_identifier) if c is not None else None],
            ["extracted by", f"SAGE run {self.run.id} ({self.run.extraction_model}), {self.run.created_at:%Y-%m-%d}"],
            ["run outcome", f"{self.settled.outcome}: {self.settled.summary or ''}".strip(": ")],
        ]
        for s in self.of(Study):
            period = _value(s.period)
            rows.append([f"design ({s.id})", _value(s.design)])
            rows.append([f"period ({s.id})", period.reported_text if period is not None else None])
        levels: dict[str, list[str]] = defaultdict(list)
        for t in self.of(Treatment):
            for factor, level in t.factor_levels.items():
                if level not in levels[factor]:
                    levels[factor].append(level)
        rows += [[f"factor: {factor}", ", ".join(values)] for factor, values in levels.items()]
        rows += [
            [
                "units",
                (
                    "Values are in the units the paper prints (reported_units), written in plain ASCII "
                    "(g m-2, ug C g-1, degC); nothing is converted (protocol §11)."
                ),
            ],
            [
                "statistics",
                (
                    "value and stat are numbers. statname uses the protocol's names (SE, SD, MSE, CI, LSD, HSD, MSD, "
                    "P); a confidence interval's bounds are in attributes_json as ci_lower and ci_upper, and "
                    "mean-separation letters as letters, with letters_compare saying what they compare."
                ),
            ],
            ["dates", "min_date and max_date bound what the paper states; a season is read by the site's hemisphere."],
            [
                "keys",
                (
                    "Every key is the SAGE record id, also in sage_id, except on the citations tab, whose key is the "
                    "citation key (authorYYYYabc), which observations also carry as citation_id. A row's citation is "
                    "the paper's DOI, else that citation key."
                ),
            ],
        ]
        rows += [[f"fill_status: {code}", meaning] for code, meaning in FILL_STATUS.items()]
        rows += [[f"status: {code}", meaning] for code, meaning in STATUS.items()]
        rows += [
            [f"not extracted: {self._table_name(t.table_id)}", t.reason]
            for t in self.settled.tables
            if t.status == "irrelevant"
        ]
        rows += [[f"unresolved: {u.target}", f"{u.cause}: {u.reason}"] for u in self.settled.unresolved]
        if self.rejected:
            rows.append(["rejected", f"{len(self.rejected)} records a scientist rejected are left out"])
        rows += [["check", line] for line in self.checks]
        return Tab(name="README", columns=["field", "value"], rows=rows)

    def _table_name(self, anchor: str) -> str:
        table = self.doc.table(anchor)
        return f"{table.label} ({anchor})" if table is not None else anchor


def _columns(names: str) -> list[str]:
    """A tab's columns, written comma-separated, then the SAGE id, status and review every tab ends with."""
    return [*names.split(", "), *_TAIL]


_TAIL = ("sage_id", "status", "review")
OBSERVATION_COLUMNS = (
    *_columns(
        "min_date, max_date, crop, variety, sitename, variable, method, value, n, statname, stat, treatment_id, "
        "replicate_id, study_year, min_depth, max_depth, citation, fill_status, notes, dataset_id, reported_units, "
        "observation_level, citation_id, data_origin, flags, attributes_json, source_table, letters, "
        "reported_effect_scope, aggregated_over_factors, relative_timing, relative_timing_days, reported_time, "
        "reported_depth, reported_variable, reported_value, source, quote"
    ),
    "status_reason",
)
"""The curators' observation columns, in the order ccmmf/cal-val-data's datapackage.json declares them (through
source_table), then what SAGE adds."""
MANAGEMENT_COLUMNS = _columns(
    "event_id, sites.name, treatments.name, min_date, max_date, mgmttype, cover_crop, crop_name, level, units, notes, "
    "attributes_keyvalue, citation, relative_timing, relative_timing_days, reported_date, reported_amount"
)

_TAB_FIELDS: dict[str, dict[str, str]] = {
    "citations": {"author": "author", "year": "year", "title": "title", "doi": "persistent_identifier"},
    "sites": {
        "latitude": "latitude",
        "longitude": "longitude",
        "elevation": "elevation",
        "elevation_units": "elevation",
        "site_name": "name",
        "country": "country",
        "state": "state_or_region",
        "city": "nearest_city",
        "soil": "soil_context",
        "notes": "description",
    },
    "treatments": {
        "definition": "definition",
        "control": "control_status",
        "label": "name",
        "factor_levels": "factor_levels",
        "sitename": "site_id",
        "study": "study_id",
        "citation": "citation_id",
    },
    "methods": {"description": "description", "citation_id": "citation_id", "label": "name"},
    "observations": {
        "min_date": "temporal_info",
        "max_date": "temporal_info",
        "crop": "crop_id",
        "variety": "crop_id",
        "sitename": "site_id",
        "variable": "variable_id",
        "method": "method_id",
        "value": "value",
        "n": "n",
        "statname": "statistical_encoding",
        "stat": "statistical_encoding",
        "treatment_id": "treatment_id",
        "replicate_id": "replicate_id",
        "min_depth": "depth",
        "max_depth": "depth",
        "citation": "citation_id",
        "fill_status": "value",
        "notes": "notes",
        "reported_units": "value",
        "observation_level": "is_raw_replicate_level",
        "citation_id": "citation_id",
        "data_origin": "value",
        "source_table": "value",
        "letters": "letters",
        "reported_effect_scope": "reported_effect_scope",
        "aggregated_over_factors": "aggregated_over_factors",
        "relative_timing": "temporal_info",
        "relative_timing_days": "temporal_info",
        "reported_time": "temporal_info",
        "reported_depth": "depth",
        "reported_variable": "variable_name",
        "reported_value": "value",
        "source": "value",
        "quote": "value",
    },
    "managements": {
        "treatments.name": "treatment_ids",
        "min_date": "date",
        "max_date": "date",
        "mgmttype": "event_type",
        "cover_crop": "crop_id",
        "crop_name": "crop_id",
        "level": "amount",
        "units": "amount",
        "notes": "notes",
        "attributes_keyvalue": "material",
        "citation": "citation_id",
        "relative_timing": "date",
        "relative_timing_days": "date",
        "reported_date": "date",
        "reported_amount": "amount",
    },
    "variables": {"description": "description", "units": "units", "notes": "notes", "label": "name"},
    "crops": {
        "scientific name": "scientific_name",
        "common name": "common_name",
        "notes": "notes",
        "variety": "cultivar",
        "cover_crop": "cover_crop",
    },
    "treatment_pairs": {
        "treatment_id_1": "treatment_id_1",
        "treatment_id_2": "treatment_id_2",
        "comparison_factor": "comparison_factor",
        "comparison_label": "comparison_label",
        "use_for_validation": "use_for_validation",
        "notes": "notes",
    },
}
"""Per tab, the record field each column reads. A column left out reads no one field (a key, the SAGE tail)."""
_REVIEW_COLUMNS: dict[str, tuple[str, ...]] = {
    "observations": (
        "treatment_id",
        "crop",
        "variety",
        "variable",
        "value",
        "reported_units",
        "statname",
        "stat",
        "n",
        "letters",
        "reported_time",
        "min_date",
        "max_date",
        "reported_depth",
        "min_depth",
        "max_depth",
        "source",
    ),
    "managements": ("treatments.name", "mgmttype", "crop_name", "min_date", "max_date", "level", "units", "notes"),
    "treatments": ("label", "factor_levels", "control", "definition", "name", "sitename"),
}
"""The columns a reviewer reads first, in reading order: the protocol's order keeps an observation's units 13 columns
from its value. A tab left out reads its curators' columns in order."""


def _described(tab: Tab) -> Tab:
    """The tab with the record each row shows, the field each column reads, and the columns a reviewer reads first."""
    fields = _TAB_FIELDS.get(tab.name, {})
    at = tab.columns.index("sage_id")
    review = list(_REVIEW_COLUMNS.get(tab.name, tab.columns[:at]))
    ids: list[str | None] = [rid if isinstance(rid := row[at], str) else None for row in tab.rows]
    return tab.model_copy(update={"fields": [fields.get(c) for c in tab.columns], "review": review, "ids": ids})


def _value[T](f: Field[T] | None) -> T | None:
    return f.value if f is not None else None


def _number(q: QuantityValue | None) -> float | None:
    """The number the paper prints; none when the text isn't one number ("<0.1", "trace"), whose text the reported
    columns and the notes keep."""
    return q.reported_numeric_value if q is not None else None


def _units(q: QuantityValue | None) -> str | None:
    return ascii_unit(q.reported_units) if q is not None and q.reported_units else None


def _statistic(stat: StatisticalSummary | None) -> tuple[float | None, tuple[float, float] | None]:
    """The statistic as a number, and a confidence interval's bounds when it is an interval ("[9.3, 9.8]")."""
    if stat is None:
        return None, None
    v = stat.statistic_value
    if isinstance(v, float | int):
        return float(v), None
    numbers = [read for token in re.findall(NUMBER, v) if (read := read_number(token)) is not None]
    if len(numbers) == 2:
        return None, (numbers[0][0], numbers[1][0])
    if len(numbers) == 1:
        return numbers[0][0], None
    return None, None


def _letters_compare(f: Field[str] | None) -> str | None:
    """What the letters compare: the sentence that says so, or the observations file's word for it."""
    if f is None or f.value is None:
        return None
    said = [e.quote for e in f.evidence[1:]]
    return said[0] if said else f.note


def _degrees(f: Field[QuantityValue] | None) -> float | None:
    q = _value(f)
    if q is None:
        return None
    return q.converted_value if q.converted_value is not None else q.reported_numeric_value


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d is not None else None


def _table_of(anchor: str) -> str:
    """The table anchor a cell or row anchor belongs to: "p6.t1" from "p6.t1.r2.c3"."""
    return ".".join(anchor.split(".")[:2])


def _reference(c: Citation) -> str:
    parts = [_value(c.author), f"({c.year.value})" if c.year.value is not None else None, _value(c.title)]
    return " ".join(p for p in parts if p is not None)


def _review_text(record_id: str, latest: Review | None, mine: list[Review], said: list[Review]) -> str | None:
    """The review column: the latest decision, with an edit's values or a rejection's reason, then the comments."""
    parts: list[str] = []
    if latest is not None:
        words = {"accept": "accepted", "edit": "edited", "reject": "rejected", "comment": "commented"}[latest.action]
        edits = {
            r.field: r.value for r in mine if r.record_id == record_id and r.action == "edit" and r.field is not None
        }
        if latest.action == "edit" and edits:
            words += ": " + "; ".join(f"{name} -> {value}" for name, value in edits.items())
        elif latest.note is not None:
            words += f": {latest.note}"
        parts.append(words)
    parts += [f"comment{'' if c.field is None else f' on {c.field}'}: {c.note}" for c in said if c.note is not None]
    return " | ".join(parts) or None
