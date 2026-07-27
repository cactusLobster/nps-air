# NPS Artist-in-Residence Directory

A self-maintaining index of Artist-in-Residence programs across all ~470
National Park Service units, built to answer one question fast: **what can I
apply to right now, and when is it due?**

The NPS publishes no central list. Each park buries its program somewhere
under Get Involved, deadlines live in freeform prose, and half the calls
route through CaFE, Submittable, or partner nonprofits. This repo crawls all
of it weekly.

## Pipeline (`validate.py`)

1. **discover** — enumerates every NPS unit via the official Data API, then
   scans each unit's `siteindex.htm` (a page listing every page on that
   unit's site) for AIR links. Catches nonstandard URLs that guessing
   `/getinvolved/artist-in-residence.htm` would miss.
2. **check** — re-fetches every program page, classifies it
   verified/inactive/broken, then sends the page text to the Claude API
   (Haiku) to extract structured application data: accepting now?, open
   date, deadline, direct apply URL, expected reopening. If the park page
   defers to CaFE/Submittable/a partner, it follows that link one hop and
   extracts the deadline there. Every extraction stores the supporting
   quote so the site can show its evidence.
3. **partners** — scans the National Parks Arts Foundation and AIRIE sites
   to fill deadlines for programs the park's own page doesn't cover.

Without `ANTHROPIC_API_KEY`, a regex fallback still captures open/closed
signals and date sentences — just less reliably.

## Site (`index.html`)

Static, GitHub Pages ready. Open calls sort to the top by deadline
(red when under two weeks out), with an "Open now" filter, Apply buttons,
and the extraction evidence quote on every record. Reads `data.json`;
falls back to an embedded copy when opened locally.

## Setup

1. Push this repo to GitHub.
2. Repo secrets (Settings → Secrets and variables → Actions):
   - `NPS_API_KEY` — free: https://www.nps.gov/subjects/developer/get-started.htm
   - `ANTHROPIC_API_KEY` — https://console.anthropic.com (Haiku extraction
     over ~100 pages costs cents per weekly run)
3. Settings → Pages → deploy from branch (root).
4. Actions → run "Validate AIR directory" once manually for the first full
   crawl (~470 site indexes + extraction; expect 15–30 min with the
   politeness delay).

## Honest limits

- Extraction is machine-read; the UI shows the source quote and links so
  you confirm before applying. Treat "Due" dates as leads, not gospel.
- CaFE/Submittable calls are only found via links on park/partner pages,
  not by crawling those platforms' full catalogs.
- `EXTRACT_MODEL` env var overrides the default model string when it ages.
