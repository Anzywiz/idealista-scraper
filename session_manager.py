"""
CloudflareSession — a lightweight per-thread object used by phase1/2/3.

v3 — browser POOL instead of a single shared browser.

Why: on this site, curl_cffi ends up blocked on virtually every request
once Cloudflare is in "Performing security verification" mode — cookies
obtained by a real browser don't reliably transfer to curl_cffi's TLS-
impersonated requests. With a single shared browser (v2), that meant every
fetch serialized through one lock — throughput was ~1 request per ~12-15s
*no matter how many worker threads were configured*, since they were all
waiting on the same browser.

v3 fixes this the way the user asked for: a POOL of `browser_pool_size`
independent SeleniumBase browsers (each with its own lock), so that many
Cloudflare-blocked requests can be resolved concurrently — throughput now
scales with pool size instead of being capped at 1. On top of that:
  * `prime(cfg)` warms up every pool slot against the homepage BEFORE any
    real scraping starts, so the pool is already holding valid cookies
    instead of paying the clearance cost on the first real request.
  * A simple circuit breaker tracks curl_cffi's recent success rate; once
    it's failed several times in a row, curl_cffi is skipped for a cooldown
    window and requests go straight to the pool, saving the wasted
    attempt+timeout on every single call when curl_cffi clearly isn't
    working for the current session.
  * The whole pool is registered for cleanup via atexit AND a SIGINT/
    SIGTERM handler, so Ctrl+C actually closes every browser instead of
    leaving orphaned Chrome processes behind.

Strategy per request:
  1. If the circuit breaker says curl_cffi is currently worth trying, try
     it first (fast, no browser).
  2. If blocked / shows the Cloudflare interstitial (or the breaker says
     skip it), hand the fetch to the pool: grab a slot (round-robin; a
     slot's own lock provides backpressure if it's mid-fetch), navigate
     with SeleniumBase uc=True CDP mode, solve the challenge if present.
  3. Broadcast the resulting cookies + user agent to every thread's
     curl_cffi session, so opportunistic curl_cffi attempts keep being
     worth trying whenever the site allows it.
"""

from __future__ import annotations

import atexit
import random
import signal
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from curl_cffi import requests as cffi_requests

from utils import log_info, log_ok, log_warn, log_err, looks_like_cloudflare

IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome123"]

DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "upgrade-insecure-requests": "1",
}

# curl_cffi circuit breaker tuning
CIRCUIT_BREAKER_THRESHOLD = 5     # consecutive curl_cffi failures before tripping
CIRCUIT_BREAKER_COOLDOWN = 20     # requests to skip curl_cffi for once tripped


class _BrowserSlot:
    """One persistent SeleniumBase browser + its own lock. Multiple slots
    give real concurrency instead of one global bottleneck."""

    def __init__(self, cfg: dict, slot_id: int):
        self.cfg = cfg
        self.slot_id = slot_id
        self.lock = threading.RLock()
        self._sb = None
        self._sb_cm = None

    def _ensure_browser(self):
        if self._sb is not None:
            return self._sb
        from seleniumbase import SB

        log_info(f"Launching browser pool slot #{self.slot_id} (uc=True, CDP mode)…")
        profile_dir = self.cfg.get("chrome_profile_dir", "output/chrome_profile")
        self._sb_cm = SB(
            uc=True,
            test=True,
            locale=self.cfg.get("language", "pt"),
            headed=bool(self.cfg.get("headed", True)),
            user_data_dir=f"{profile_dir}_slot{self.slot_id}",
        )
        self._sb = self._sb_cm.__enter__()
        return self._sb

    def fetch(self, url: str) -> tuple[Optional[str], list, Optional[str]]:
        """Navigate this slot's browser to url and return (html, cookies,
        user_agent). Blocks other callers of THIS slot until done — other
        slots are unaffected, which is what gives the pool its concurrency."""
        with self.lock:
            sb = self._ensure_browser()
            sb.activate_cdp_mode(url)
            sb.sleep(self.cfg.get("cloudflare_wait_seconds", 25))
            try:
                sb.solve_captcha()
            except Exception:
                pass
            sb.sleep(2)
            html = sb.get_page_source()
            cookies = sb.get_cookies()
            user_agent = None
            try:
                user_agent = sb.execute_script("return navigator.userAgent")
            except Exception:
                pass
            return html, cookies, user_agent

    def close(self) -> None:
        with self.lock:
            if self._sb_cm is not None:
                log_info(f"Closing browser pool slot #{self.slot_id}…")
                try:
                    self._sb_cm.__exit__(None, None, None)
                except Exception:
                    pass
                self._sb_cm = None
                self._sb = None


class _BrowserPool:
    """Process-wide singleton: a fixed pool of browser slots, shared by
    every worker thread/CloudflareSession, plus the curl_cffi circuit
    breaker state (a property of "is this site currently blocking us",
    which is global, not per-thread)."""

    _instance: "_BrowserPool | None" = None
    _create_lock = threading.Lock()

    def __new__(cls, cfg: dict):
        with cls._create_lock:
            if cls._instance is None:
                inst = super().__new__(cls)
                inst._init(cfg)
                cls._instance = inst
            return cls._instance

    def _init(self, cfg: dict) -> None:
        self.cfg = cfg
        size = max(1, int(cfg.get("browser_pool_size", 3)))
        self.slots = [_BrowserSlot(cfg, i) for i in range(size)]
        self._rr_index = 0
        self._rr_lock = threading.Lock()

        self.cookie_lock = threading.Lock()
        self.cookies: list = []
        self.user_agent: Optional[str] = None

        self.breaker_lock = threading.Lock()
        self._curl_fail_streak = 0
        self._total_requests = 0
        self._curl_disabled_until = 0

        self._warmed_up = False
        _register_for_cleanup(self)

    # ── pool dispatch ──────────────────────────────────────────
    def _pick_slot(self) -> _BrowserSlot:
        with self._rr_lock:
            slot = self.slots[self._rr_index % len(self.slots)]
            self._rr_index += 1
        return slot

    def refresh_clearance(self, url: str) -> Optional[str]:
        slot = self._pick_slot()
        try:
            html, cookies, user_agent = slot.fetch(url)
        except Exception as e:
            log_err(f"Browser pool slot #{slot.slot_id} fetch failed for {url}: {e}")
            return None
        with self.cookie_lock:
            self.cookies = cookies
            if user_agent:
                self.user_agent = user_agent
        ok = not looks_like_cloudflare(html)
        (log_ok if ok else log_warn)(
            f"Slot #{slot.slot_id} {'cleared Cloudflare' if ok else 'still blocked'} for {url}"
        )
        return html

    def warm_up(self) -> None:
        """Visit the homepage on every pool slot BEFORE real scraping
        starts, so the pool already holds valid cookies instead of paying
        the clearance cost on the first real request from each slot."""
        with self.breaker_lock:
            if self._warmed_up:
                return
            self._warmed_up = True

        home = self.cfg["base_urls"]["pt"].rstrip("/") + "/"
        log_info(f"Warming up {len(self.slots)} browser pool slot(s) against {home}…")

        def _warm_one(slot: _BrowserSlot):
            try:
                html, cookies, user_agent = slot.fetch(home)
                with self.cookie_lock:
                    self.cookies = cookies
                    if user_agent:
                        self.user_agent = user_agent
                ok = not looks_like_cloudflare(html)
                (log_ok if ok else log_warn)(
                    f"Slot #{slot.slot_id} warm-up {'cleared Cloudflare' if ok else 'still blocked'}"
                )
            except Exception as e:
                log_warn(f"Slot #{slot.slot_id} warm-up failed: {e}")

        with ThreadPoolExecutor(max_workers=len(self.slots)) as pool:
            futures = [pool.submit(_warm_one, slot) for slot in self.slots]
            for fut in as_completed(futures):
                fut.result()

    def apply_cookies(self, http_session: cffi_requests.Session) -> None:
        with self.cookie_lock:
            for c in self.cookies:
                try:
                    http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
                except Exception:
                    continue
            if self.user_agent:
                http_session.headers["user-agent"] = self.user_agent

    # ── curl_cffi circuit breaker ──────────────────────────────
    def note_request(self) -> None:
        """Call once per get_html() invocation, regardless of whether
        curl_cffi ends up being tried — the cooldown window is measured in
        total request volume, so it must advance even while curl_cffi is
        being skipped, or the breaker would never reopen."""
        with self.breaker_lock:
            self._total_requests += 1

    def should_try_curl(self) -> bool:
        with self.breaker_lock:
            return self._total_requests >= self._curl_disabled_until

    def note_curl_result(self, success: bool) -> None:
        with self.breaker_lock:
            if success:
                self._curl_fail_streak = 0
                return
            self._curl_fail_streak += 1
            if self._curl_fail_streak >= CIRCUIT_BREAKER_THRESHOLD and \
                    self._total_requests >= self._curl_disabled_until:
                self._curl_disabled_until = self._total_requests + CIRCUIT_BREAKER_COOLDOWN
                log_warn(f"curl_cffi has failed {self._curl_fail_streak} times in a row — "
                         f"skipping it for the next {CIRCUIT_BREAKER_COOLDOWN} request(s) and "
                         f"going straight to the browser pool")

    def close(self) -> None:
        for slot in self.slots:
            slot.close()


# ───────────────────────── cleanup wiring ─────────────────────────

_cleanup_registered = False
_cleanup_lock = threading.Lock()
_instances_to_close: list = []


def _register_for_cleanup(instance: "_BrowserPool") -> None:
    global _cleanup_registered
    with _cleanup_lock:
        _instances_to_close.append(instance)
        if not _cleanup_registered:
            atexit.register(close_all_browsers)
            try:
                signal.signal(signal.SIGINT, _sigint_handler)
                signal.signal(signal.SIGTERM, _sigint_handler)
            except (ValueError, OSError):
                pass  # not the main thread / not supported on this platform
            _cleanup_registered = True


def close_all_browsers() -> None:
    for inst in list(_instances_to_close):
        inst.close()


def _sigint_handler(signum, frame):
    log_warn("Interrupted — closing the browser pool before exiting…")
    close_all_browsers()
    sys.exit(130)


# ───────────────────────── public API ─────────────────────────

def prime(cfg: dict) -> None:
    """Call once from the MAIN thread, before spawning any worker threads
    or ThreadPoolExecutor. signal.signal() only succeeds when called from
    the main thread, and this also warms up every pool slot up front so
    the first real requests from worker threads don't each pay the full
    clearance cost."""
    pool = _BrowserPool(cfg)
    pool.warm_up()


class CloudflareSession:
    """Cheap to create — one per worker thread. All instances share the
    same browser pool above; only the curl_cffi session is per-thread."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._pool = _BrowserPool(cfg)
        self.http = cffi_requests.Session(
            impersonate=random.choice(IMPERSONATE_PROFILES), headers=DEFAULT_HEADERS
        )
        self._pool.apply_cookies(self.http)

    def _http_get(self, url: str, timeout: int = 30):
        return self.http.get(url, timeout=timeout, allow_redirects=True)

    def get_html(self, url: str, force_browser: bool = False) -> Optional[str]:
        self._pool.note_request()
        if not force_browser and self._pool.should_try_curl():
            try:
                resp = self._http_get(url)
                if not looks_like_cloudflare(resp.text, resp.status_code):
                    self._pool.note_curl_result(True)
                    return resp.text
                self._pool.note_curl_result(False)
                log_warn(f"curl_cffi blocked/challenged on {url} (status={resp.status_code}) — escalating to browser pool")
            except Exception as e:
                self._pool.note_curl_result(False)
                log_warn(f"curl_cffi error on {url}: {e} — escalating to browser pool")

        html = self._pool.refresh_clearance(url)
        self._pool.apply_cookies(self.http)  # pick up fresh cookies for this thread too
        if html and looks_like_cloudflare(html):
            log_err(f"Still blocked after browser pool fetch: {url}")
        return html

    def close(self):
        """No-op: the pool is shared/process-wide and closed centrally via
        close_all_browsers() (atexit / SIGINT). Kept for API compat."""
        pass
