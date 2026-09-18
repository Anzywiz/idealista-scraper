"""
Orchestrator.

  python main.py --phase1                         # collect links only
  python main.py --phase2                         # scrape listings only (resumes)
  python main.py --phase2 --fresh                 # ignore saved progress
  python main.py --phase3                         # enrich from detail pages (resumes)
  python main.py --all                             # phase1 -> phase2 -> phase3
  python main.py --phase1 --section comprar --category casas
  python main.py --phase2 --section arrendar
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from utils import log_head, log_err, log_ok


def run_phase(script: str, args: argparse.Namespace) -> int:
    cmd = [sys.executable, script, "--config", args.config]
    if script in ("phase1_get_links.py", "phase2_scraper.py"):
        if args.section:
            cmd += ["--section", args.section]
        if args.category:
            cmd += ["--category", args.category]
    if script in ("phase2_scraper.py", "phase3_enrich.py") and args.fresh:
        cmd += ["--fresh"]

    log_head(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def main():
    ap = argparse.ArgumentParser(description="idealista.pt scraper orchestrator")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--phase1", action="store_true", help="run phase 1 (get_links)")
    ap.add_argument("--phase2", action="store_true", help="run phase 2 (scrape_listings)")
    ap.add_argument("--phase3", action="store_true", help="run phase 3 (enrich_listings)")
    ap.add_argument("--all", action="store_true", help="run phase 1 -> phase 2 -> phase 3")
    ap.add_argument("--section", default=None, help="comprar | arrendar | agencias")
    ap.add_argument("--category", default=None, help="casas | terrenos | escritorios | ...")
    ap.add_argument("--fresh", action="store_true", help="phase 2/3: ignore saved progress")
    args = ap.parse_args()

    if not (args.phase1 or args.phase2 or args.phase3 or args.all):
        ap.print_help()
        return

    phases = []
    if args.all:
        phases = ["phase1_get_links.py", "phase2_scraper.py", "phase3_enrich.py"]
    else:
        if args.phase1:
            phases.append("phase1_get_links.py")
        if args.phase2:
            phases.append("phase2_scraper.py")
        if args.phase3:
            phases.append("phase3_enrich.py")

    for script in phases:
        rc = run_phase(script, args)
        if rc != 0:
            log_err(f"{script} exited with code {rc} — stopping pipeline.")
            sys.exit(rc)

    log_ok("Pipeline finished.")


if __name__ == "__main__":
    main()
