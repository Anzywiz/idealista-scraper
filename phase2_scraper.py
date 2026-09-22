"""
Phase 2 — scrape_listings

Reads output/links.json (from phase 1) and, for every link, paginates
".../pagina-N" until either the listing's expected count is covered, no
more <article class="item ..."> cards are found, or max_page is hit.

Each card is mapped to a DBQ REAL-ESTATE-BASIC row and appended to
output/listings.csv. Progress (which "<link_url>|page=N" keys are done)
is checkpointed to output/phase2_progress.json so a crashed/interrupted
run can resume with --resume (default) instead of --fresh.

NOTE on field coverage: the listing *card* (search-results page) does not
expose floor, total_floors, bathrooms, zip, street, lat/lon — those only
appear on the individual listing detail page. This phase scrapes cards
only (per the provided HTML sample), so those columns are left blank.
Visiting every detail page would be a straightforward phase2b extension
using the same CloudflareSession + article a.item-link -> itemurl.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from session_manager import CloudflareSession, close_all_browsers, prime
from utils import (
    load_config, log_head, log_info, log_ok, log_warn, log_err, log_step,
    parse_price, jitter_sleep, load_progress, save_progress, today_str, epoch_now,
)

ALL_COLUMNS = [
    "dbq_prd_type", "website_name", "competence_date",
    "listing_id", "listing_title", "listing_description",
    "property_type", "listing_type", "reference_market", "country_code",
    "location_description", "location_region", "location_province", "location_city",
    "location_zip", "locaiton_neighborhood", "location_street", "location_street_n",
    "location_lon", "location_lat",
    "area_unit", "area_value", "bedrooms", "bathrooms", "floor", "total_floors",
    "amenities_list", "listing_date", "listing_status",
    "agent_id", "agent_url",
    "currency_code", "price", "imageurl", "itemurl",
    "contract_id", "seller_id", "delivery_id",
]

BEDROOM_RE = re.compile(r"\bT(\d+)\b", re.IGNORECASE)
AREA_RE = re.compile(r"([\d.,]+)\s*m²")

_thread_local = threading.local()


def get_thread_session(cfg: dict) -> CloudflareSession:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = CloudflareSession(cfg)
    return _thread_local.session


# ───────────────────────── CSV ─────────────────────────

def write_rows(rows: list[dict], path: str):
    p = Path(path)
    write_header = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerows(rows)


# ───────────────────────── parsing ─────────────────────────

def parse_listing_cards(html: str, base_url: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    articles = soup.select("article.item")
    cards = []
    for art in articles:
        a_link = art.select_one("a.item-link")
        if not a_link or not a_link.get("href"):
            continue

        listing_id = art.get("data-element-id", "")
        title = a_link.get_text(strip=True) or a_link.get("title", "")
        itemurl = urljoin(base_url, a_link["href"])

        price_span = art.select_one(".item-price")
        price = parse_price(price_span.get_text() if price_span else "")

        detail_spans = [d.get_text(strip=True) for d in art.select(".item-detail-char .item-detail")]
        bedrooms, area_value, amenities = None, None, []
        for d in detail_spans:
            bd = BEDROOM_RE.search(d)
            ar = AREA_RE.search(d)
            if bd:
                bedrooms = bd.group(1)
            elif ar:
                area_value = ar.group(1).replace(".", "").replace(",", ".")
            else:
                amenities.append(d)

        desc_el = art.select_one(".item-description p")
        description = desc_el.get_text(strip=True) if desc_el else ""

        # Image markup varies (image-gallery slides vs. lazy mask/placeholder
        # slider), but the actual <picture><img> always lives inside
        # .item-gallery. Skip the agency logo picture (.logo-branding).
        img = art.select_one(".item-gallery img")
        imageurl = ""
        if img:
            imageurl = img.get("src") or img.get("data-src") or ""
            if not imageurl:
                # some lazy variants only populate <source srcset="...">
                source = img.find_previous_sibling("source") or (
                    img.parent.select_one("source") if img.parent else None
                )
                if source and source.get("srcset"):
                    imageurl = source["srcset"].split(",")[0].strip().split(" ")[0]

        agent_a = art.select_one("picture.logo-branding a")
        agent_url = urljoin(base_url, agent_a["href"]) if agent_a and agent_a.get("href") else ""
        agent_id = agent_a["href"].strip("/").split("/")[-1] if agent_a and agent_a.get("href") else ""

        ribbon = art.select_one(".item-ribbon-container")
        listing_status = ribbon.get_text(strip=True) if ribbon and ribbon.get_text(strip=True) else ""

        cards.append({
            "listing_id": listing_id,
            "listing_title": title,
            "listing_description": description,
            "itemurl": itemurl,
            "price": price,
            "bedrooms": bedrooms,
            "area_value": area_value,
            "amenities_list": " | ".join(amenities) if amenities else "",
            "imageurl": imageurl,
            "agent_id": agent_id,
            "agent_url": agent_url,
            "listing_status": listing_status,
        })
    return cards


def build_row(card: dict, link: dict, section_key: str, listing_type: str,
              category: dict, cfg: dict) -> dict:
    fv = cfg["fixed_values"]
    row = {
        "dbq_prd_type": fv["dbq_prd_type"],
        "website_name": fv["website_name"],
        "competence_date": today_str(),
        "listing_id": card["listing_id"],
        "listing_title": card["listing_title"],
        "listing_description": card["listing_description"],
        "property_type": category.get("property_type", category.get("slug", "")),
        "listing_type": listing_type or "",
        "reference_market": fv["reference_market"],
        "country_code": fv["country_code"],
        "location_description": ", ".join(filter(None, [
            link.get("parish"), link.get("district"), link.get("region"),
        ])),
        "location_region": link.get("region", ""),
        "location_province": link.get("district", ""),
        "location_city": link.get("parish", link.get("district", "")),
        "location_zip": "",
        "locaiton_neighborhood": "",
        "location_street": "",
        "location_street_n": "",
        "location_lon": "",
        "location_lat": "",
        "area_unit": fv["area_unit"],
        "area_value": card.get("area_value") or "",
        "bedrooms": card.get("bedrooms") or "",
        "bathrooms": "",
        "floor": "",
        "total_floors": "",
        "amenities_list": card.get("amenities_list", ""),
        "listing_date": "",
        "listing_status": card.get("listing_status", ""),
        "agent_id": card.get("agent_id", ""),
        "agent_url": card.get("agent_url", ""),
        "currency_code": fv["currency_code"],
        "price": card.get("price") if card.get("price") is not None else "",
        "imageurl": card.get("imageurl", ""),
        "itemurl": card.get("itemurl", ""),
        "contract_id": fv["contract_id"],
        "seller_id": fv["seller_id"],
        "delivery_id": f"{epoch_now():.7f}",
    }
    return row


# ───────────────────────── pagination ─────────────────────────

def paginated_url(base_link_url: str, page: int) -> str:
    if page <= 1:
        return base_link_url
    return f"{base_link_url.rstrip('/')}/pagina-{page}"


def link_complete_key(link: dict) -> str:
    """Distinct from any per-page progress key (page 1's key IS link['url']
    itself), so a whole-link 'nothing more to do here' marker can't collide
    with a real page entry."""
    return f"{link['url']}::complete"


def scrape_link(cfg: dict, link: dict, section_key: str, listing_type: str,
                 category: dict, progress: dict, progress_path: str, csv_path: str) -> int:
    complete_key = link_complete_key(link)
    if progress.get(complete_key) == "done":
        return 0  # fully scraped in a previous run — nothing to do

    session = get_thread_session(cfg)
    base_url = cfg["base_url"]
    page_size = cfg.get("listing_page_size", 30)
    max_page = cfg.get("max_page", 60)
    expected_pages = min(max_page, max(1, -(-max(link.get("count", 1), 1) // page_size)))

    total_rows = 0
    for page in range(1, expected_pages + 1):
        url = paginated_url(link["url"], page)
        if progress.get(url) == "done":
            continue

        html = session.get_html(url)
        if html is None:
            log_warn(f"No HTML for {url} — skipping page")
            continue

        cards = parse_listing_cards(html, base_url)
        if not cards:
            progress[url] = "done"
            break  # no more listings on this link — every page beyond this
                   # one is implicitly covered by the completion marker below,
                   # so resuming won't re-check pages we deliberately never
                   # visited

        rows = [build_row(c, link, section_key, listing_type, category, cfg) for c in cards]
        write_rows(rows, csv_path)
        total_rows += len(rows)

        progress[url] = "done"
        save_progress(progress_path, progress)

        jitter_sleep(cfg.get("phase2_delay_seconds", [1.5, 3.5]))

    progress[complete_key] = "done"
    save_progress(progress_path, progress)
    return total_rows


# ───────────────────────── orchestration ─────────────────────────

def flatten_tasks(links_data: dict, cfg: dict, only_section=None, only_category=None):
    tasks = []
    section_cfg_by_key = {s["key"]: s for s in cfg["sections"]}
    for section_key, cats in links_data.items():
        if only_section and section_key != only_section:
            continue
        section_cfg = section_cfg_by_key.get(section_key, {})
        listing_type = section_cfg.get("listing_type")
        cat_cfg_by_slug = {c["slug"]: c for c in section_cfg.get("categories", [])}
        for cat_key, link_list in cats.items():
            if only_category and cat_key != only_category:
                continue
            category = cat_cfg_by_slug.get(cat_key, {"slug": cat_key, "property_type": cat_key})
            for link in link_list:
                tasks.append((section_key, listing_type, category, link))
    return tasks


def run(cfg: dict, only_section=None, only_category=None, fresh=False):
    links_path = cfg["phase1_links_file"]
    if not Path(links_path).exists():
        log_err(f"{links_path} not found — run phase 1 first.")
        return

    links_data = json.loads(Path(links_path).read_text(encoding="utf-8"))
    tasks = flatten_tasks(links_data, cfg, only_section, only_category)
    if not tasks:
        log_warn("No tasks matched the given --section/--category filters.")
        return

    progress_path = cfg["phase2_progress_file"]
    progress = {} if fresh else load_progress(progress_path)
    csv_path = cfg["phase2_listings_csv"]

    # skip agencias section by default: no listing_type maps to the
    # REAL-ESTATE-BASIC schema's SALE/RENTAL/AUCTION/NEW DEVELOPMENT values.
    tasks = [t for t in tasks if t[1] is not None]

    # Resume support: drop links already fully scraped in a previous run
    # BEFORE submitting anything to the pool. Without this, a restart still
    # "resumes" correctly at the page level (each already-done page is
    # skipped fast, no network call, no duplicate rows) — but it does so by
    # re-submitting and re-walking every one of the already-finished links
    # one at a time, which is why the progress counter looked like it had
    # gone back to 1 and produced a wall of "(+0 rows)" lines instead of
    # picking up where it left off.
    if not fresh:
        before = len(tasks)
        tasks = [t for t in tasks if progress.get(link_complete_key(t[3])) != "done"]
        skipped = before - len(tasks)
        if skipped:
            log_info(f"Skipping {skipped} link(s) already fully scraped in a previous run "
                     f"— {len(tasks)} remaining")

    log_info(f"{len(tasks)} link(s) queued for scraping "
             f"(agencias skipped — not a property listing_type)")

    workers = cfg.get("phase2_workers", 4)
    total_rows = 0
    done_count = 0

    prime(cfg)  # register SIGINT/SIGTERM cleanup from the main thread, before
                # any worker thread gets a chance to create the shared browser
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {
            pool.submit(scrape_link, cfg, link, section_key, listing_type, category,
                        progress, progress_path, csv_path): (section_key, category, link)
            for section_key, listing_type, category, link in tasks
        }
        for fut in as_completed(futures):
            section_key, category, link = futures[fut]
            done_count += 1
            try:
                rows = fut.result() or 0
                total_rows += rows
                log_step(done_count, len(tasks),
                          f"{section_key}/{category.get('slug')} "
                          f"{link.get('parish') or link.get('district') or link.get('region')} "
                          f"(+{rows} rows)")
            except Exception as e:
                log_err(f"Task failed {section_key}/{category.get('slug')} {link.get('url')}: {e}")
    except KeyboardInterrupt:
        pending = sum(1 for f in futures if not f.done())
        log_warn(f"Interrupted — cancelling {pending} not-yet-started task(s); "
                 f"any already in flight will finish in a moment…")
        raise
    finally:
        # cancel_futures=True is the key fix: the default `with
        # ThreadPoolExecutor() as pool:` context manager calls
        # shutdown(wait=True) with NO cancellation, which drains the
        # *entire* remaining task queue (potentially thousands of links)
        # before returning — so Ctrl+C appeared to "do nothing" for a very
        # long time, and if the terminal was killed impatiently instead,
        # the shared browser never got a chance to close. Cancelling
        # unstarted futures here means only the handful of tasks already
        # mid-flight (<= workers) need to finish before we can clean up.
        pool.shutdown(wait=True, cancel_futures=True)
        close_all_browsers()

    log_ok(f"Phase 2 complete — {total_rows} rows written to {csv_path}")


def main():
    ap = argparse.ArgumentParser(description="Phase 2 — scrape idealista.pt listings")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--section", default=None)
    ap.add_argument("--category", default=None)
    ap.add_argument("--fresh", action="store_true", help="ignore saved progress")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_head(f"PHASE 2 — scrape_listings (workers={cfg.get('phase2_workers', 4)})")
    try:
        run(cfg, only_section=args.section, only_category=args.category, fresh=args.fresh)
    except KeyboardInterrupt:
        log_warn("Stopped by user — progress up to the last completed page was saved; "
                 "re-run the same command to resume.")
        sys.exit(130)


if __name__ == "__main__":
    main()
