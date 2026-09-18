"""
Phase 1 — get_links

For every (section, category) pair — e.g. (comprar, casas), (arrendar, terrenos) —
fetch the category landing page and walk its region -> district -> parish
hierarchy, producing a flat list of scrape-ready links for phase 2.

Rule (per site behaviour):
  * Each top-level region (Açores, Alentejo, Norte, ...) is rendered as its
    own <ul class="locations-list__links"> block. The first <li> is the
    region title/link; the remaining <li> siblings are its districts, each
    with a <p>count</p> and an <a class="subregion"> link.
  * If the SUM of a region's district counts is <= max_listing_cap (1800),
    the region's own link (e.g. /geo/comprar-casas/acores/) already covers
    every listing — no need to drill further.
  * Otherwise, each district must be opened at its
    ".../concelhos-freguesias" page, which lists every parish/municipality
    (grouped by starting letter) with its own count. Those parish-level
    links are what phase 2 actually scrapes, since they stay comfortably
    under the 1800 cap.

Output: output/links.json
{
  "comprar": {
    "casas": [
       {"url": "...", "count": 1308, "granularity": "region", "region": "Açores"},
       {"url": "...", "count": 13, "granularity": "parish", "region": "Alentejo",
        "district": "Beja", "parish": "Albernoa e Trindade"},
       ...
    ],
    ...
  },
  "arrendar": {...},
  "agencias": {...}
}
"""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from session_manager import CloudflareSession, close_all_browsers
from utils import (
    load_config, log_head, log_info, log_ok, log_warn, log_err, log_step,
    parse_pt_number, jitter_sleep,
)


# ───────────────────────── parsing ─────────────────────────

def parse_region_blocks(html: str, root_url: str) -> list[dict]:
    """Return [{region, region_url, districts: [{name, url, count}]}]"""
    soup = BeautifulSoup(html, "lxml")
    blocks = soup.find_all("ul", class_="locations-list__links")
    regions = []

    for ul in blocks:
        lis = ul.find_all("li", recursive=False)
        if not lis:
            continue

        title_li = lis[0]
        title_a = title_li.find("a")
        if not title_a:
            continue
        region_name = title_a.get_text(strip=True)
        region_url = urljoin(root_url, title_a.get("href", ""))

        districts = []
        for li in lis[1:]:
            sub_a = li.find("a", class_="subregion")
            if not sub_a:
                continue
            count_p = li.find("p")
            count = parse_pt_number(count_p.get_text() if count_p else "")
            name_span = sub_a.find("span", class_="subregion-name")
            name = name_span.get_text(strip=True) if name_span else sub_a.get_text(strip=True)
            districts.append({
                "name": name,
                "url": urljoin(root_url, sub_a.get("href", "")),
                "count": count,
            })

        regions.append({"region": region_name, "region_url": region_url, "districts": districts})

    return regions


def parse_parish_list(html: str, root_url: str) -> list[dict]:
    """Parse a '.../concelhos-freguesias' page into leaf parish/municipality
    links. Matches <li><span>N</span><a href="...">Name</a></li> pattern,
    grouped under <li><strong class="location_letter">X</strong><ul>...</ul></li>
    letter headers (letter headers are skipped automatically since they have
    no direct <span>/<a> children)."""
    soup = BeautifulSoup(html, "lxml")
    leaves = []
    for li in soup.find_all("li"):
        span = li.find("span", recursive=False)
        a = li.find("a", recursive=False)
        if not span or not a:
            continue
        href = a.get("href")
        if not href:
            continue
        leaves.append({
            "name": a.get_text(strip=True),
            "url": urljoin(root_url, href),
            "count": parse_pt_number(span.get_text()),
        })
    return leaves


# ───────────────────────── crawl logic ─────────────────────────

def build_links_for_category(session: CloudflareSession, cfg: dict, root_url: str,
                              category_url: str, cap: int) -> list[dict]:
    html = session.get_html(category_url)
    if not html:
        log_err(f"Could not fetch category page: {category_url}")
        return []

    regions = parse_region_blocks(html, root_url)
    if not regions:
        log_warn(f"No region blocks found on {category_url} — page structure may differ "
                  f"(this happens e.g. on the Agências directory).")
        return []

    results: list[dict] = []
    drill_jobs = []  # (region_name, district) pairs needing a concelhos-freguesias fetch

    for region in regions:
        total = sum(d["count"] for d in region["districts"])
        if not region["districts"] or total <= cap:
            results.append({
                "url": region["region_url"],
                "count": total,
                "granularity": "region",
                "region": region["region"],
            })
        else:
            for d in region["districts"]:
                drill_jobs.append((region["region"], d))

    if not drill_jobs:
        return results

    log_info(f"{len(drill_jobs)} district(s) exceed the {cap}-listing cap — "
              f"drilling into concelhos-freguesias pages…")

    def _drill(job):
        region_name, district = job
        d_html = session.get_html(district["url"])
        jitter_sleep(cfg.get("phase1_delay_seconds", [1.0, 2.0]))
        if not d_html:
            log_warn(f"Failed to fetch district page for {district['name']} ({district['url']})")
            return []
        parishes = parse_parish_list(d_html, root_url)
        if not parishes:
            log_warn(f"No parish links parsed for {district['name']} — falling back to district link")
            return [{
                "url": district["url"], "count": district["count"],
                "granularity": "district", "region": region_name, "district": district["name"],
            }]
        out = []
        for p in parishes:
            out.append({
                "url": p["url"], "count": p["count"], "granularity": "parish",
                "region": region_name, "district": district["name"], "parish": p["name"],
            })
        return out

    workers = cfg.get("phase1_concurrency", 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_drill, job) for job in drill_jobs]
        done = 0
        for fut in as_completed(futures):
            done += 1
            log_step(done, len(drill_jobs), "district pages drilled")
            results.extend(fut.result() or [])

    return results


def run(cfg: dict, only_section: str | None = None, only_category: str | None = None) -> dict:
    root_url = cfg["base_urls"]["pt"].rstrip("/")
    base_url = cfg["base_url"]
    cap = cfg.get("max_listing_cap", 1800)

    session = CloudflareSession(cfg)
    all_links: dict[str, dict[str, list[dict]]] = {}

    try:
        for section in cfg["sections"]:
            skey = section["key"]
            if only_section and skey != only_section:
                continue
            all_links.setdefault(skey, {})

            for cat in section["categories"]:
                ckey = cat["slug"]
                if only_category and ckey != only_category:
                    continue

                category_url = f"{base_url}/{cat['path']}/"
                log_head(f"{skey} / {ckey} — {category_url}")

                links = build_links_for_category(session, cfg, root_url, category_url, cap)
                all_links[skey][ckey] = links
                log_ok(f"{skey}/{ckey}: {len(links)} scrape-ready links "
                       f"({sum(l['count'] for l in links)} listings)")

                jitter_sleep(cfg.get("phase1_delay_seconds", [1.0, 2.0]))
    finally:
        session.close()
        close_all_browsers()

    return all_links


def main():
    ap = argparse.ArgumentParser(description="Phase 1 — collect idealista.pt scrape links")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--section", default=None, help="only this section key (comprar/arrendar/agencias)")
    ap.add_argument("--category", default=None, help="only this category slug (casas, terrenos, ...)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    log_head(f"PHASE 1 — get_links  (language={cfg['language']}, base={cfg['base_url']})")

    links = run(cfg, only_section=args.section, only_category=args.category)

    out_path = cfg["phase1_links_file"]
    # merge with any existing file when running a single --section/--category
    if (args.section or args.category):
        try:
            existing = json.loads(open(out_path, encoding="utf-8").read())
        except Exception:
            existing = {}
        for skey, cats in links.items():
            existing.setdefault(skey, {})
            existing[skey].update(cats)
        links = existing

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(links, f, indent=2, ensure_ascii=False)

    total_links = sum(len(v) for cats in links.values() for v in cats.values())
    log_ok(f"Wrote {out_path} ({total_links} total links)")


if __name__ == "__main__":
    main()
