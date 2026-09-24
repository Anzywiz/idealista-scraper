# idealista.pt scraper

Two-phase scraper for idealista.pt (Portugal), configurable for `pt` or `en`
locale, DBQ `REAL-ESTATE-BASIC` output schema.

## Setup

```bash
pip install -r requirements.txt
seleniumbase install chromedriver   # first time only
```

## Config (`config.json`)

- `language`: `"pt"` or `"en"` → controls `base_url` (`idealista.pt` vs `idealista.pt/en`)
- `headed`: `true`/`false` → passed straight to `SB(headed=...)`
- `max_listing_cap`: 1800 (site's per-listing-page cap)
- `sections`: comprar (SALE) / arrendar (RENTAL) / agencias — each with its
  own category list, mirroring the site's second/third-level menus you provided
- `fixed_values`: DBQ constants (fill in real `contract_id` / `seller_id`)

## Run

```bash
python main.py --phase1                          # collect links -> output/links.json
python main.py --phase2                          # scrape listing cards -> output/listings.csv (resumable)
python main.py --phase3                          # enrich from detail pages -> output/listings_enriched.csv (resumable)
python main.py --all                              # phase1 -> phase2 -> phase3
python main.py --phase1 --section comprar --category casas   # narrow scope for testing
python main.py --phase2 --fresh                  # ignore saved progress, start clean
python main.py --phase3 --fresh                  # (phase3_enrich.py also takes --limit N for testing)
```

## How it works

**Phase 1** (`phase1_get_links.py`) fetches each category's landing page and
walks the region → district hierarchy. If a region's districts sum to
≤1800 listings, the region's own `/geo/...` link is used directly (covers
everything). If a region exceeds the cap (e.g. Alentejo), every district is
opened at its `.../concelhos-freguesias` page and each parish/municipality
link + count is collected individually — these always stay well under 1800,
so nothing is missed.

**Phase 2** (`phase2_scraper.py`) reads `links.json`, paginates each link
(`.../pagina-N`) up to `ceil(count / 30)` pages or `max_page` (60, whichever
first), parses every `article.item` card, and appends DBQ rows to
`output/listings.csv`. Progress is checkpointed per page URL so a rerun
skips completed pages.

`agencias` links are collected in phase 1 for completeness but skipped in
phase 2 — the DBQ `REAL-ESTATE-BASIC` schema's `listing_type` only supports
`SALE / RENTAL / AUCTION / NEW DEVELOPMENT`, and the agency directory isn't
a property listing.

## Cloudflare handling

`session_manager.CloudflareSession` tries `curl_cffi` (fast, TLS-fingerprinted)
first. If the response is blocked or shows the "Performing security
verification" interstitial, it escalates to **a single, process-wide
SeleniumBase(uc=True) CDP-mode browser** — shared and lock-protected across
every worker thread, so concurrent phase2/phase3 workers never spawn more
than one Chrome instance. Whichever thread hits the block first refreshes
Cloudflare clearance; the resulting cookies + user-agent are broadcast to
every thread's `curl_cffi` session so subsequent requests go back to being
fast HTTP calls. The browser is registered for cleanup via `atexit` and a
`SIGINT`/`SIGTERM` handler, so Ctrl+C closes it instead of leaving an
orphaned Chrome process running.

## Phase 3 — enrich_listings

`phase3_enrich.py` reads `output/listings.csv`, visits each row's `itemurl`
detail page, and fills in what the search-card selectors can't reach:

- `bathrooms`, `floor` (from "1º andar com elevador" / "rés-do-chão" style text)
- `listing_status` (condition, e.g. "Segunda mão/bom estado")
- `amenities_list` — merges the "Características específicas" list (lote
  size, balcony, garage...), the "Equipamento" list, and the energy
  certificate value, each labelled
- `location_street` / `locaiton_neighborhood` / `location_description` from
  the `#headerMap` breadcrumb
- a more reliable `price` (from the price panel rather than the card)
- `agent_id` — prefers the AMI licence number (e.g. `"AMI 25001"`) over a
  URL slug when present
- `listing_date` — parses `"Anúncio atualizado no dia 28 de Agosto"` into
  `YYYY-MM-DD` (site omits the year; assumes current year, rolling back one
  if that would land in the future)
- `property_type`, `area_value`, `bedrooms` — re-derived from the detail
  page and preferred over the phase-2 card values when present

Card-derived fields (`imageurl`, `listing_title`, etc.) are kept whenever
the detail page doesn't offer something better. Progress is checkpointed
per `listing_id` to `output/phase3_progress.json`.

`total_floors` and precise lat/lon weren't present in any of the HTML
segments you shared — left blank; wire them in if you find the selectors
on the live page.

## Not included (per your instructions)

`phase4_upload_to_dbq.py` was intentionally not written — reuse the
standard version from the web-scraper skill, pointing `ALL_COLUMNS` at the
list in `phase2_scraper.py`, and set the enriched CSV
(`output/listings_enriched.csv`) as its input once phase 3 is stable.

## Resuming

Both `phase2` and `phase3` checkpoint progress and can be safely re-run with
the same command after a crash or Ctrl+C — pages/listings already done are
skipped, not re-fetched or re-written. Phase 2 tracks completion two ways:
per-page (`output/phase2_progress.json`, keyed by the exact paginated URL)
*and* a whole-link "fully scraped" marker, so a restart filters out
already-finished links up front rather than re-walking each one's already-
done pages one at a time (which used to make the progress counter look like
it had reset back to 1 and produce a wall of `(+0 rows)` lines). Use
`--fresh` on either phase to ignore saved progress and start clean.

## Known site quirk: nested `concelhos-freguesias` pages

Not every entry on a district's `.../concelhos-freguesias` breakdown page is
a genuine leaf listing page — some concelhos (e.g. Avis) point to their own
`.../avis/concelhos-freguesias` page instead, one level deeper, and some
neighbouring concelhos' pages cross-link each other (a "nearby areas" style
widget) rather than showing their own freguesia breakdown at all. `phase1`
now resolves these recursively with a cycle guard: it never re-fetches a
URL it's already visited in the same drill, and if a concelho's own page
never yields a genuine leaf (either through max depth or a cross-linking
loop), it falls back to that concelho's plain listing page
(`.../moncao/concelhos-freguesias` → `.../moncao/`) instead of writing the
broken index URL into `links.json`. **If your `links.json` predates this
fix, re-run `--phase1`** to regenerate it — phase 2 can't fix already-broken
links on its own.

## Known duplication across categories/sections

`trespasse` (business transfers) has no separate buy/rent path on the
site — `/trespasse/` is the same URL under both the Comprar and Arrendar
menus. Phase 1 now fetches/drills each unique category URL only once and
reuses the result for every section that maps to it (so it's not scraped
twice), and logs a one-line summary of any URLs still shared across more
than one (section, category) pair so this is never a silent surprise.

## Throughput: the browser pool

On this site, curl_cffi ends up Cloudflare-blocked on almost every request
once the "Performing security verification" challenge is active — a real
browser's cookies don't reliably transfer to curl_cffi's TLS-impersonated
requests. Routing every blocked request through a single shared browser
(the earlier fix) meant throughput was capped at ~1 request per ~12–15s
*no matter how many worker threads were running*, since they all queued
behind the same browser.

`session_manager.py` now runs a **pool** of `browser_pool_size` (config,
default `3`) independent SeleniumBase browsers, each with its own lock —
so that many blocked requests resolve concurrently instead of serializing.
Throughput now scales with pool size instead of being capped at 1. On top
of that:

- `prime(cfg)` warms up every pool slot against the homepage *before* any
  real scraping starts, so the pool already holds valid cookies instead of
  every worker paying the clearance cost on its first real request.
- A circuit breaker tracks curl_cffi's recent success rate: after 5
  consecutive failures it's skipped entirely for the next 20 requests
  (going straight to the pool), instead of wasting an attempt+timeout on
  every single call once it's clearly not working for the current session.

**Tuning `browser_pool_size`:** each slot is a full (optionally headed)
Chrome instance, so this trades RAM/CPU for throughput. Start at `3`; if
your machine has headroom, raise it towards `phase2_workers`/
`phase3_workers` so every worker thread can get a browser slot without
queuing. Lower it if Chrome instances are competing for memory. Headless
(`"headed": false`) also helps scale the pool higher on the same hardware.

## Testing without hitting the live site

This sandbox can't reach `idealista.pt` (not in its network allowlist), so
the parsers were verified offline against the exact HTML excerpts you
pasted (region blocks, parish list, listing card) — all assertions passed.
Run `python main.py --phase1 --section comprar --category casas` locally
first to confirm selectors still match the live DOM before a full run.
