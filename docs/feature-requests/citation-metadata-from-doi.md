# Feature request: fill Citation metadata from a DOI (or author + year) lookup, not from PDF text

**Status:** proposed (not started) · **Origin:** review-UI feedback from the project mentor · **Area:** extraction / Citation entity

## Problem

The Citation record (`author`, `year`, `title`, `persistent_identifier`) is currently extracted by the LLM reading the first
pages of the converted PDF. That is the wrong tool for bibliographic metadata:

- The metadata is often not on the page, or is garbled by conversion. The paper used as the example here,
  **Daren-1997-Canopy, has a DOI — `10.2134/agronj1997.00021962008900020018x` — but the DOI is not printed in the PDF**, so
  text extraction can only ever report it as unresolved.
- It is unrelated to the scientific content the review is about, yet it is the first thing a reviewer sees and the first
  extraction every other record depends on.
- It is a recurring source of pipeline failures. Every other entity needs a ready Citation (`citation_id` is a required
  reference), so a Citation that fails blocks the whole paper. In 3 of the last 5 live Felipe regression runs (2026-09-21) a
  Citation failure did exactly that, twice because the model wrote an explanation such as "No journal name visible…" where the
  contract requires literal source text.

## Proposal

Split the job into two steps and stop asking the LLM to produce bibliographic fields it cannot see:

1. **Identify** — the LLM extracts only what is actually printed and reliable: the DOI if present, otherwise first author +
   year (+ a title fragment).
2. **Resolve** — a deterministic tool queries a central citation database and returns the metadata.
   [habanero](https://github.com/sckott/habanero) (Python client for the Crossref API) is the suggested tool:
   - with a DOI: `Crossref().works(ids=doi)`;
   - without one: `Crossref().works(query_bibliographic="<author> <year> <title fragment>", limit=...)`, accepting a match
     only above a similarity threshold on title/author/year.

The Citation record is then filled from the response (title, authors, year, DOI, journal), with the identifying facts still
grounded in the paper text.

## Design notes / open questions

- **Provenance.** The IR labels every field `EXTRACTED` / `INFERRED` / `UNRESOLVED` and expects a source locator into the
  paper. Externally looked-up values need an honest label and a source (the DOI/URL of the record used); decide whether that is
  a new source kind or `INFERRED` with the lookup recorded. Do not label a looked-up value `EXTRACTED`.
- **Verification.** A lookup by author + year can return the wrong paper. Require agreement with what was read from the PDF
  (title similarity, year, first author) before accepting; otherwise leave the field `UNRESOLVED` with the candidates recorded.
- **Failure behaviour.** The lookup is optional enrichment: network errors, rate limits or no match must leave a valid Citation
  (fields `UNRESOLVED` with a reason), never a blocked run.
- **Reproducibility.** Cache the response per DOI in the run artifacts so a run can be replayed offline; record the query and
  the timestamp. Use Crossref's "polite" pool (a contact email) and respect rate limits.
- **Scope of the LLM step.** With the lookup in place the extractor's Citation contract can shrink to DOI + first author + year,
  removing the "absent field" facts that currently fail grounding.
- **Review UI.** Citation is already de-emphasised in the review page (collapsed under "Paper metadata"); a lookup makes it
  reasonable to show it as read-only metadata with its source link.

## Acceptance criteria

- A paper whose PDF has no printed DOI but is identifiable by author + year + title resolves to the correct DOI and metadata.
- A DOI printed in the PDF is used directly and its metadata matches Crossref's.
- A wrong or ambiguous match is rejected, not committed.
- With the network unavailable, the run still completes with a valid, partly unresolved Citation.
- Tests cover: DOI hit, author + year hit, ambiguous match rejected, lookup failure, and the provenance labelling.

## Dependencies

- `habanero` (add to `requirements.txt` when implemented).
- Network access to `api.crossref.org` at extraction time.

## Non-goals

- Fetching full text or abstracts.
- Replacing the LLM for any scientific field.
