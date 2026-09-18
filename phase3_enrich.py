"""
Phase 3 — enrich_listings

Reads output/listings.csv (phase 2 output) and, for every row, visits its
`itemurl` detail page to fill in fields the search-card doesn't expose:
bathrooms, floor, condition/listing_status, listing_date, a fuller
amenities_list (equipment + energy certificate), location_street /
locaiton_neighborhood, and a more reliable price. Card-derived values
(bedrooms, area, listing_title, imageurl, agent_url...) are kept as a
fallback whenever the detail page doesn't have something better.

Output: output/listings_enriched.csv (same ALL_COLUMNS as phase 2).
Progress is checkpointed per listing_id to output/phase3_progress.json so
a crashed/interrupted run can resume.
"""

from __future__ import annotations

import argparse
import csv
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from bs4 import BeautifulSoup

from session_manager import CloudflareSession, close_all_browsers
from phase2_scraper import ALL_COLUMNS
from utils import (
    load_config, log_head, log_info, log_ok, log_warn, log_err, log_step,
    parse_price, jitter_sleep, load_progress, save_progress, parse_pt_date,
)

AREA_RE = re.compile(r"([\d.,]+)\s*m²\s*área bruta(?:,\s*([\d.,]+)\s*m²\s*úteis?)?", re.IGNORECASE)
BEDROOM_RE = re.compile(r"^T(\d+)$", re.IGNORECASE)
BATHROOM_RE = re.compile(r"(\d+)\s*casas?\s*de banho", re.IGNORECASE)
LOTE_RE = re.compile(r"Lote de\s*([\d.,]+)\s*m²", re.IGNORECASE)
FLOOR_RE = re.compile(r"(\d+)[ºª°]?\s*andar", re.IGNORECASE)
GROUND_FLOOR_RE = re.compile(r"r[ée]s[- ]do[- ]ch[ãa]o", re.IGNORECASE)
CONDITION_PREFIXES = ("segunda mão", "novo", "para reformar", "em construção", "bom estado")
PROPERTY_TYPE_RE = re.compile(r"^(.*?)\s+(?:à venda|para arrendar|para venda)", re.IGNORECASE)

_thread_local = threading.local()


def get_thread_session(cfg: dict) -> CloudflareSession:
    if not hasattr(_thread_local, "session"):
        _thread_local.session = CloudflareSession(cfg)
    return _thread_local.session


# ───────────────────────── parsing ─────────────────────────

def parse_specific_features(soup: BeautifulSoup) -> dict:
    out = {
        "raw_property_type": "", "area_value": "", "bedrooms": "", "bathrooms": "",
        "floor": "", "listing_status": "", "amenities": [],
    }
    container = soup.select_one(".details-property-feature-one")
    if not container:
        return out

    items = [li.get_text(strip=True) for li in container.select("li")]
    for i, text in enumerate(items):
        area_m = AREA_RE.search(text)
        bed_m = BEDROOM_RE.match(text)
        bath_m = BATHROOM_RE.search(text)
        lote_m = LOTE_RE.search(text)
        floor_m = FLOOR_RE.search(text)
        low = text.lower()

        if area_m:
            out["area_value"] = area_m.group(1).replace(".", "").replace(",", ".")
            if area_m.group(2):
                out["amenities"].append(f"Área útil: {area_m.group(2)} m²")
        elif bed_m:
            out["bedrooms"] = bed_m.group(1)
        elif bath_m:
            out["bathrooms"] = bath_m.group(1)
        elif lote_m:
            out["amenities"].append(f"Lote: {lote_m.group(1)} m²")
        elif GROUND_FLOOR_RE.search(text):
            out["floor"] = "0"
            out["amenities"].append(text)
        elif floor_m:
            out["floor"] = floor_m.group(1)
            out["amenities"].append(text)
        elif any(low.startswith(p) for p in CONDITION_PREFIXES):
            out["listing_status"] = text
        elif i == 0 and not any([area_m, bed_m, bath_m, lote_m, floor_m]):
            out["raw_property_type"] = text
        else:
            out["amenities"].append(text)

    return out


def parse_equipment(soup: BeautifulSoup) -> list[str]:
    """Feature-two block: equipment list + energy-certificate value,
    labelled by their preceding <h2> where possible."""
    container = soup.select_one(".details-property-feature-two")
    if not container:
        return []

    # The source HTML has a malformed nested <ul> around the energy
    # certificate block: an empty <h2> and the later "Certificado
    # energético" <h2> both resolve (via find_next) to the same feature
    # div. Group by feat_div identity first so the more specific label
    # wins instead of emitting the block twice.
    blocks: dict[int, dict] = {}
    order: list[int] = []
    for h2 in container.select("h2.details-property-h2"):
        label = h2.get_text(strip=True)
        feat_div = h2.find_next("div", class_="details-property_features")
        if not feat_div:
            continue
        key = id(feat_div)
        if key not in blocks:
            blocks[key] = {"label": label, "texts": [li.get_text(strip=True)
                           for li in feat_div.select("li") if li.get_text(strip=True)]}
            order.append(key)
        elif label and not blocks[key]["label"]:
            blocks[key]["label"] = label  # prefer the specific label over the empty one

    amenities = []
    for key in order:
        block = blocks[key]
        for text in block["texts"]:
            amenities.append(f"{block['label']}: {text}" if block["label"] else text)

    # Nested (malformed) blocks can still yield the same underlying value
    # twice at different label depths — keep the LAST (most specific) one.
    by_raw_text: dict[str, str] = {}
    order2 = []
    for entry in amenities:
        raw = entry.split(": ", 1)[-1]
        if raw not in by_raw_text:
            order2.append(raw)
        by_raw_text[raw] = entry
    return [by_raw_text[raw] for raw in order2]


def parse_location(soup: BeautifulSoup) -> dict:
    out = {"location_street": "", "locaiton_neighborhood": "", "location_description": ""}
    header_map = soup.select_one("#headerMap")
    if not header_map:
        return out
    items = [li.get_text(strip=True) for li in header_map.select("li.header-map-list")]
    if items:
        out["location_street"] = items[0]
    if len(items) > 1:
        out["locaiton_neighborhood"] = items[1]
    if items:
        out["location_description"] = items[-1]
    return out


def parse_price_block(soup: BeautifulSoup):
    strong = soup.select_one(".price-features__container strong.flex-feature-details")
    if strong:
        p = parse_price(strong.get_text())
        if p is not None:
            return p
    alt = soup.select_one(".info-data .info-data-price")
    if alt:
        return parse_price(alt.get_text())
    return None


def parse_agent(soup: BeautifulSoup) -> dict:
    out = {"agent_id": "", "agent_url": ""}
    a = soup.select_one(".advertiser-name-container a.about-advertiser-name")
    if a:
        out["agent_url"] = a.get("href", "")
        # spans right after the name often hold the AMI licence number,
        # which is a more useful agent identifier than a URL slug
        container = a.find_parent(class_="advertiser-name-container")
        if container:
            for span in container.find_all("span"):
                txt = span.get_text(strip=True)
                if txt.upper().startswith("AMI"):
                    out["agent_id"] = txt
                    break
    return out


def parse_listing_date(soup: BeautifulSoup) -> str:
    el = soup.select_one("#stats .stats-text")
    if not el:
        return ""
    return parse_pt_date(el.get_text(strip=True))


def parse_property_type_title(soup: BeautifulSoup) -> str:
    el = soup.select_one(".main-info__title-main")
    if not el:
        return ""
    m = PROPERTY_TYPE_RE.match(el.get_text(strip=True))
    return m.group(1).strip() if m else ""


def parse_detail_page(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")

    feats = parse_specific_features(soup)
    equipment = parse_equipment(soup)
    location = parse_location(soup)
    agent = parse_agent(soup)

    amenities = feats["amenities"] + equipment
    property_type = feats["raw_property_type"] or parse_property_type_title(soup)

    return {
        "property_type": property_type,
        "area_value": feats["area_value"],
        "bedrooms": feats["bedrooms"],
        "bathrooms": feats["bathrooms"],
        "floor": feats["floor"],
        "listing_status": feats["listing_status"],
        "amenities_list": " | ".join(a for a in amenities if a),
        "location_street": location["location_street"],
        "locaiton_neighborhood": location["locaiton_neighborhood"],
        "location_description": location["location_description"],
        "price": parse_price_block(soup),
        "agent_id": agent["agent_id"],
        "agent_url": agent["agent_url"],
        "listing_date": parse_listing_date(soup),
    }


def merge_row(row: dict, enrichment: dict) -> dict:
    """Prefer enrichment values (detail page is more authoritative);
    fall back to whatever phase 2 already captured from the card."""
    merged = dict(row)
    for key, value in enrichment.items():
        if key == "amenities_list":
            existing = row.get("amenities_list", "")
            combined = " | ".join(x for x in [existing, value] if x)
            merged[key] = combined
        elif value not in (None, "", []):
            merged[key] = value
    return merged


# ───────────────────────── CSV I/O ─────────────────────────

def write_row(row: dict, path: str):
    p = Path(path)
    write_header = not p.exists()
    with open(p, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ALL_COLUMNS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


def enrich_one(cfg: dict, row: dict, progress: dict, progress_path: str, out_path: str) -> bool:
    listing_id = row.get("listing_id") or row.get("itemurl")
    if not listing_id:
        return False
    if progress.get(listing_id) == "done":
        return False

    itemurl = row.get("itemurl")
    if not itemurl:
        progress[listing_id] = "done"
        return False

    session = get_thread_session(cfg)
    html = session.get_html(itemurl)
    if not html:
        log_warn(f"No HTML for {itemurl} — leaving row un-enriched")
        write_row(row, out_path)
        progress[listing_id] = "done"
        save_progress(progress_path, progress)
        return True

    try:
        enrichment = parse_detail_page(html)
    except Exception as e:
        log_warn(f"Parse failed for {itemurl}: {e} — leaving row un-enriched")
        enrichment = {}

    merged = merge_row(row, enrichment)
    write_row(merged, out_path)
    progress[listing_id] = "done"
    save_progress(progress_path, progress)

    jitter_sleep(cfg.get("phase3_delay_seconds", [1.5, 3.5]))
    return True


# ───────────────────────── orchestration ─────────────────────────

def run(cfg: dict, fresh: bool = False, limit: int | None = None):
    in_path = cfg["phase2_listings_csv"]
    if not Path(in_path).exists():
        log_err(f"{in_path} not found — run phase 2 first.")
        return

    with open(in_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if limit:
        rows = rows[:limit]

    out_path = cfg.get("phase3_enriched_csv", "output/listings_enriched.csv")
    progress_path = cfg.get("phase3_progress_file", "output/phase3_progress.json")
    progress = {} if fresh else load_progress(progress_path)

    if fresh and Path(out_path).exists():
        Path(out_path).unlink()

    pending = [r for r in rows if (r.get("listing_id") or r.get("itemurl")) not in progress
               or progress.get(r.get("listing_id") or r.get("itemurl")) != "done"]
    log_info(f"{len(pending)} of {len(rows)} listing(s) need enrichment")

    workers = cfg.get("phase3_workers", 4)
    done_count = 0
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(enrich_one, cfg, row, progress, progress_path, out_path): row
                       for row in pending}
            for fut in as_completed(futures):
                row = futures[fut]
                done_count += 1
                try:
                    fut.result()
                    log_step(done_count, len(pending), f"listing {row.get('listing_id')}")
                except Exception as e:
                    log_err(f"Enrichment failed for listing {row.get('listing_id')}: {e}")
    finally:
        close_all_browsers()

    log_ok(f"Phase 3 complete — enriched CSV at {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Phase 3 — enrich idealista.pt listings from detail pages")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--fresh", action="store_true", help="ignore saved progress, start clean")
    ap.add_argument("--limit", type=int, default=None, help="only enrich the first N rows (testing)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_head(f"PHASE 3 — enrich_listings (workers={cfg.get('phase3_workers', 4)})")
    run(cfg, fresh=args.fresh, limit=args.limit)


if __name__ == "__main__":
    main()
