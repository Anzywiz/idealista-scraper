"""
Shared helpers for the idealista.pt scraper.

- colour logging
- config loading
- Portuguese-locale number parsing ("1.769" -> 1769)
- a CloudflareSession wrapper: tries curl_cffi first, escalates to
  SeleniumBase (undetected-chrome / CDP mode) when Cloudflare blocks it,
  then hands the resulting cookies back to curl_cffi for speed.
"""

from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ───────────────────────── colour logging ─────────────────────────

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log_head(msg: str) -> None:
    print(f"\n{C.BOLD}{C.MAGENTA}=== {msg} ==={C.RESET}")


def log_info(msg: str) -> None:
    print(f"{C.CYAN}[{_ts()}] ℹ {msg}{C.RESET}")


def log_ok(msg: str) -> None:
    print(f"{C.GREEN}[{_ts()}] ✔ {msg}{C.RESET}")


def log_warn(msg: str) -> None:
    print(f"{C.YELLOW}[{_ts()}] ⚠ {msg}{C.RESET}")


def log_err(msg: str) -> None:
    import sys
    print(f"{C.RED}[{_ts()}] ✖ {msg}{C.RESET}", file=sys.stderr)


def log_step(i: int, n: int, msg: str, width: int = 24) -> None:
    n = max(n, 1)
    filled = int(width * min(i, n) / n)
    bar = "█" * filled + "░" * (width - filled)
    print(f"{C.CYAN}[{bar}] {i}/{n} {msg}{C.RESET}")


# ───────────────────────── config ─────────────────────────

def load_config(path: str = "config.json") -> dict:
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(f"config file not found: {path}")
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    Path(cfg.get("output_dir", "output")).mkdir(parents=True, exist_ok=True)
    Path(cfg.get("logs_dir", "logs")).mkdir(parents=True, exist_ok=True)

    lang = cfg.get("language", "pt")
    if lang not in cfg.get("base_urls", {}):
        raise ValueError(f"language '{lang}' not present in base_urls")
    cfg["base_url"] = cfg["base_urls"][lang].rstrip("/")
    return cfg


def base_site_url(cfg: dict) -> str:
    """Root site (no /en suffix) — needed because some links must be
    built off the bare domain regardless of language path (e.g. /geo/...)."""
    return cfg["base_urls"]["pt"].rstrip("/")


# ───────────────────────── number parsing ─────────────────────────

_NUM_RE = re.compile(r"[\d.,]+")


def parse_pt_number(text: Optional[str]) -> int:
    """'1.769' -> 1769 ; '954' -> 954 ; '' / None -> 0"""
    if not text:
        return 0
    m = _NUM_RE.search(text.replace("\xa0", "").strip())
    if not m:
        return 0
    digits = m.group(0).replace(".", "").replace(",", "")
    return int(digits) if digits.isdigit() else 0


def parse_price(text: Optional[str]) -> Optional[float]:
    """'325.000€' -> 325000.0"""
    if not text:
        return None
    cleaned = re.sub(r"[^\d,\.]", "", text)
    if not cleaned:
        return None
    # Portuguese format uses '.' as thousands sep, ',' as decimal sep
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def epoch_now() -> float:
    return datetime.now(timezone.utc).timestamp()


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


PT_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4,
    "maio": 5, "junho": 6, "julho": 7, "agosto": 8, "setembro": 9,
    "outubro": 10, "novembro": 11, "dezembro": 12,
}


def parse_pt_date(text: Optional[str]) -> str:
    """'Anúncio atualizado no dia 28 de Agosto' -> '2026-08-28'
    (no year on the site; assumes current year, rolling back one year if
    that would land in the future relative to today)."""
    if not text:
        return ""
    m = re.search(r"(\d{1,2})\s+de\s+([A-Za-zçÇãÃáéíóú]+)", text, re.IGNORECASE)
    if not m:
        return ""
    day = int(m.group(1))
    month_name = m.group(2).lower()
    month = PT_MONTHS.get(month_name)
    if not month:
        return ""
    now = datetime.now()
    year = now.year
    try:
        candidate = datetime(year, month, day)
    except ValueError:
        return ""
    if candidate > now:
        candidate = datetime(year - 1, month, day)
    return candidate.strftime("%Y-%m-%d")


def jitter_sleep(bounds: list) -> None:
    lo, hi = (bounds + bounds)[:2]
    time.sleep(random.uniform(lo, hi))


# ───────────────────────── cloudflare markers ─────────────────────────

CF_MARKERS = [
    "just a moment",
    "verify you are human",
    "cf-turnstile",
    "challenge-platform",
    "performing security verification",
    "security service to protect against malicious bots",
    "attention required! | cloudflare",
]


def looks_like_cloudflare(html: str, status_code: Optional[int] = None) -> bool:
    if status_code in (403, 429, 503):
        return True
    if not html:
        return True
    low = html.lower()
    return any(marker in low for marker in CF_MARKERS)


# ───────────────────────── progress / checkpoint ─────────────────────────

def load_progress(path: str) -> dict:
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_progress(path: str, progress: dict) -> None:
    Path(path).write_text(json.dumps(progress, indent=2, ensure_ascii=False), encoding="utf-8")


# ───────────────────────── retry decorator-ish helper ─────────────────────────

def retry_call(fn, *, max_retries=5, base_delay=5, label="call"):
    """Call fn() with exponential-ish backoff. fn takes no args (use a lambda/partial)."""
    for attempt in range(1, max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == max_retries:
                log_warn(f"Giving up on {label} after {max_retries} attempts: {e}")
                return None
            wait = base_delay * attempt + random.uniform(0, 3)
            log_warn(f"{label}: attempt {attempt} failed ({e}) — retrying in {wait:.1f}s")
            time.sleep(wait)
    return None
