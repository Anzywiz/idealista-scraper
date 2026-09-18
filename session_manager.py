"""
CloudflareSession — a lightweight per-thread object used by phase1/2/3.

Fixes vs. the earlier version:
  * Only ONE SeleniumBase browser is ever launched for the whole process,
    shared by every worker thread (via a process-wide singleton +
    threading.Lock), instead of one browser per thread. Cookies obtained
    by whichever thread triggers a Cloudflare challenge are broadcast to
    every thread's curl_cffi session.
  * The shared browser is registered for cleanup via atexit AND a SIGINT
    handler, so Ctrl+C (or a crash) actually calls driver.quit() instead
    of leaving orphaned Chrome processes behind.

Strategy per request:
  1. Try curl_cffi (fast, no browser) with a Chrome TLS fingerprint.
  2. If blocked / shows the Cloudflare interstitial, acquire the shared
     browser lock (other threads just wait — they don't spawn their own
     browser) and refresh clearance once via SeleniumBase uc=True CDP mode.
  3. Copy the resulting cookies + user agent into every thread's curl_cffi
     session so subsequent requests go back to being fast HTTP calls.
"""

from __future__ import annotations

import atexit
import random
import signal
import sys
import threading
from typing import Optional

from curl_cffi import requests as cffi_requests

from utils import log_info, log_ok, log_warn, log_err, looks_like_cloudflare

IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome123"]

DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "upgrade-insecure-requests": "1",
}


class _SharedBrowserClearance:
    """Process-wide singleton: exactly one SeleniumBase browser, however
    many threads/CloudflareSession instances are created."""

    _instance: "_SharedBrowserClearance | None" = None
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
        self.browser_lock = threading.RLock()
        self.cookie_lock = threading.Lock()
        self._sb = None
        self._sb_cm = None
        self.cookies: list = []
        self.user_agent: Optional[str] = None
        _register_for_cleanup(self)

    def _ensure_browser(self):
        if self._sb is not None:
            return self._sb
        from seleniumbase import SB

        log_info("Launching the single shared SeleniumBase browser (uc=True, CDP mode)…")
        self._sb_cm = SB(
            uc=True,
            test=True,
            locale=self.cfg.get("language", "pt"),
            headed=bool(self.cfg.get("headed", True)),
            user_data_dir=self.cfg.get("chrome_profile_dir", "output/chrome_profile"),
        )
        self._sb = self._sb_cm.__enter__()
        return self._sb

    def refresh_clearance(self, url: str) -> Optional[str]:
        """Blocks until it (or another thread that got here first) has
        fresh Cloudflare cookies. Only one browser navigation happens at a
        time — other threads calling this concurrently simply queue on
        browser_lock instead of spawning their own browser."""
        with self.browser_lock:
            try:
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
                with self.cookie_lock:
                    self.cookies = cookies
                    try:
                        self.user_agent = sb.execute_script("return navigator.userAgent")
                    except Exception:
                        pass
                log_ok("Shared browser refreshed Cloudflare clearance cookies")
                return html
            except Exception as e:
                log_err(f"Shared browser fetch failed for {url}: {e}")
                return None

    def apply_cookies(self, http_session: cffi_requests.Session) -> None:
        with self.cookie_lock:
            for c in self.cookies:
                try:
                    http_session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""))
                except Exception:
                    continue
            if self.user_agent:
                http_session.headers["user-agent"] = self.user_agent

    def close(self) -> None:
        with self.browser_lock:
            if self._sb_cm is not None:
                log_info("Closing the shared SeleniumBase browser…")
                try:
                    self._sb_cm.__exit__(None, None, None)
                except Exception:
                    pass
                self._sb_cm = None
                self._sb = None


# ───────────────────────── cleanup wiring ─────────────────────────

_cleanup_registered = False
_cleanup_lock = threading.Lock()
_instances_to_close: list = []


def _register_for_cleanup(instance: "_SharedBrowserClearance") -> None:
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
    log_warn("Interrupted — closing the shared browser before exiting…")
    close_all_browsers()
    sys.exit(130)


# ───────────────────────── public API ─────────────────────────

class CloudflareSession:
    """Cheap to create — one per worker thread. All instances share the
    single browser above; only the curl_cffi session is per-thread."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._shared = _SharedBrowserClearance(cfg)
        self.http = cffi_requests.Session(
            impersonate=random.choice(IMPERSONATE_PROFILES), headers=DEFAULT_HEADERS
        )
        self._shared.apply_cookies(self.http)

    def _http_get(self, url: str, timeout: int = 30):
        return self.http.get(url, timeout=timeout, allow_redirects=True)

    def get_html(self, url: str, force_browser: bool = False) -> Optional[str]:
        if not force_browser:
            try:
                resp = self._http_get(url)
                if not looks_like_cloudflare(resp.text, resp.status_code):
                    return resp.text
                log_warn(f"curl_cffi blocked/challenged on {url} (status={resp.status_code}) — escalating to shared browser")
            except Exception as e:
                log_warn(f"curl_cffi error on {url}: {e} — escalating to shared browser")

        html = self._shared.refresh_clearance(url)
        self._shared.apply_cookies(self.http)  # pick up fresh cookies for this thread too
        if html and looks_like_cloudflare(html):
            log_err(f"Still blocked after browser fetch: {url}")
        return html

    def close(self):
        """No-op: the browser is shared/process-wide and closed centrally
        via close_all_browsers() (atexit / SIGINT). Kept for API compat."""
        pass
