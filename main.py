"""
Orchestrator.

  python main.py --phase1                         # collect links only
  python main.py --phase2                         # scrape listings only (resumes)
  python main.py --phase2 --fresh                 # ignore saved progress
  python main.py --phase3                         # enrich from detail pages (resumes)
  python main.py --phase4                         # convert to data_file.txt
  python main.py --phase5                         # upload to DBQ
  python main.py --phase5 --status                # check upload result (no re-upload)
  python main.py --all                             # phase1 -> phase2 -> phase3 -> phase4 -> phase5
  python main.py --phase1 --section comprar --category casas
  python main.py --phase2 --section arrendar
"""

from __future__ import annotations

import argparse
import subprocess
import sys

from utils import log_head, log_err, log_ok

ALL_PHASES = [
    "phase1_get_links.py",
    "phase2_scraper.py",
    "phase3_enrich.py",
    "phase4_convert.py",
    "phase5_upload.py",
]

# phases that accept --section/--category (link collection & scraping only)
SCOPED_PHASES = {"phase1_get_links.py", "phase2_scraper.py"}
# phases that checkpoint progress and accept --fresh to ignore it
RESUMABLE_PHASES = {"phase2_scraper.py", "phase3_enrich.py"}
# the upload phase can be polled for its result without re-uploading
STATUS_PHASES = {"phase5_upload.py"}


def run_phase(script: str, args: argparse.Namespace) -> int:
    cmd = [sys.executable, script, "--config", args.config]
    if script in SCOPED_PHASES:
        if args.section:
            cmd += ["--section", args.section]
        if args.category:
            cmd += ["--category", args.category]
    if script in RESUMABLE_PHASES and args.fresh:
        cmd += ["--fresh"]
    if script in STATUS_PHASES and args.status:
        cmd += ["--status"]

    log_head(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    return result.returncode


def main():
    ap = argparse.ArgumentParser(description="idealista.pt scraper orchestrator")
    ap.add_argument("--config", default="config.json")
    ap.add_argument("--phase1", action="store_true", help="run phase 1 (get_links)")
    ap.add_argument("--phase2", action="store_true", help="run phase 2 (scrape_listings)")
    ap.add_argument("--phase3", action="store_true", help="run phase 3 (enrich_listings)")
    ap.add_argument("--phase4", action="store_true", help="run phase 4 (convert to data_file.txt)")
    ap.add_argument("--phase5", action="store_true", help="run phase 5 (upload to DBQ)")
    ap.add_argument("--all", action="store_true", help="run phase 1 -> phase 2 -> phase 3 -> phase 4 -> phase 5")
    ap.add_argument("--section", default=None, help="comprar | arrendar | agencias")
    ap.add_argument("--category", default=None, help="casas | terrenos | escritorios | ...")
    ap.add_argument("--fresh", action="store_true", help="phase 2/3: ignore saved progress")
    ap.add_argument("--status", action="store_true", help="phase 5: check upload result without re-uploading")
    args = ap.parse_args()

    if not (args.phase1 or args.phase2 or args.phase3 or args.phase4 or args.phase5 or args.all):
        ap.print_help()
        return

    if args.all:
        phases = list(ALL_PHASES)
    else:
        flags = [args.phase1, args.phase2, args.phase3, args.phase4, args.phase5]
        phases = [script for script, flag in zip(ALL_PHASES, flags) if flag]

    for script in phases:
        rc = run_phase(script, args)
        if rc != 0:
            log_err(f"{script} exited with code {rc} — stopping pipeline.")
            sys.exit(rc)

    log_ok("Pipeline finished.")


if __name__ == "__main__":
    main()