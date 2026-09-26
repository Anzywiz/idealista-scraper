"""
CloudflareSession — a lightweight per-thread object used by phase1/2/3.

v4 — proxy support, adaptive Cloudflare wait, a proactive background
session-refresher, serialized browser launches, and a smarter curl_cffi
circuit breaker. Builds on v3's browser POOL design.

WHAT CHANGED FROM v3, AND WHY:

1. Proxies (.env PROXY_URL). A single rotating-proxy URL, if present in
   the environment (or a .env file next to the scripts), is applied to
   BOTH curl_cffi's session and every SeleniumBase pool slot. Nothing
   changes if PROXY_URL isn't set.

2. Adaptive Cloudflare wait. v3 always slept the full
   `cloudflare_wait_seconds` (default 25s) on every single browser fetch,
   even when the page loaded clean with no challenge at all. Now the pool
   polls the page every `cloudflare_poll_interval_seconds` and returns as
   soon as the Cloudflare markers are gone, instead of always paying the
   full fixed wait — only a genuinely-challenged page waits the full
   budget.

3. Proactive background session refresher. v3 only ever sent a browser to
   refresh cookies REACTIVELY — after curl_cffi had already failed on a
   real request. That means curl_cffi is scraping on a decaying session
   the whole time between clearances, guaranteeing a rising 429 rate right
   up until the next failure forces a refresh. Now a background thread
   proactively cycles an idle pool slot every
   `session_refresh_interval_seconds` purely to mint a fresh session and
   broadcast its cookies — independent of whether anything has failed —
   so curl_cffi is kept riding a young session instead of only being
   rescued after the fact. If every slot is busy with real scraping when
   a refresh is due, that cycle is skipped (real traffic is already
   refreshing cookies just by hitting the browser pool) rather than
   competing with it.

4. Serialized browser launches. The actual bug behind "only one Chrome
   really opens a page, but the logs show all of them succeeding": v3's
   warm_up() launched every pool slot's undetected-chromedriver instance
   from separate threads AT THE SAME TIME. Concurrent UC-mode launches can
   race on chromedriver's local port/profile-lock selection — a later
   launch can end up silently attached to an EARLIER instance's Chrome
   process instead of spawning its own, so two "slots" are actually
   driving the same tab; each slot's log lines are honest about what THAT
   thread asked for, but not about which physical browser it landed on.
   Fix: browser launches (not fetches — those still run fully concurrently
   once a slot is up) are now serialized through a single lock with a
   short stagger between them, and each slot logs its driver session_id
   on launch so you can confirm in the logs that slots are genuinely
   distinct processes.

5. Periodic browser recycling. Each slot now closes and relaunches its own
   Chrome process every `browser_recycle_after_requests` fetches (jittered
   per slot so they don't all recycle on the same fetch) or after
   `browser_recycle_after_seconds`, whichever comes first — a long-lived
   Chrome process on this site appears to accumulate a worse Cloudflare
   trust score over a run, matching the observed pattern of a run getting
   noticeably buggier after 60-80 requests and a plain script restart
   (same on-disk profile, brand-new process) clearing it back up.

6. Smarter circuit breaker. v3 tripped after a fixed 5 straight failures
   and always cooled down for a fixed 20 requests, and kept incrementing
   the fail streak forever after tripping (which is why the "5 times in a
   row" message kept firing). Now: the trip threshold is raised and
   configurable (retry more before giving up on curl_cffi entirely), each
   consecutive trip grows the cooldown (persistent blocking backs off
   further) while any clean success resets the trip count back to zero
   (a blip recovers fast), and the fail streak itself resets on trip so it
   doesn't uselessly keep counting past the threshold.

7. Self-healing dead sessions. A slot's browser can die outside of our
   control — crash, OOM, or a launch race during recycling — and without a
   health check, every future fetch on that slot repeated the identical
   "connect call failed" to the same dead debugger port forever, which is
   what you just hit. Any exception during a fetch now force-closes that
   slot's browser and retries once immediately with a freshly launched one,
   instead of getting stuck reusing a dead session indefinitely.

Strategy per request (unchanged in spirit from v3):
  1. If the circuit breaker says curl_cffi is currently worth trying, try
     it first (fast, no browser) — retrying once with a different TLS
     impersonation profile before counting the request as a failure.
  2. If blocked / shows the Cloudflare interstitial (or the breaker says
     skip it), hand the fetch to the pool: grab a slot (round-robin via a
     checkout queue; a slot's own lock provides backpressure if it's
     mid-fetch), navigate with SeleniumBase uc=True CDP mode, solve the
     challenge if present, polling instead of blindly sleeping the full
     wait.
  3. Broadcast the resulting cookies + user agent to every thread's
     curl_cffi session, so opportunistic curl_cffi attempts keep being
     worth trying whenever the site allows it — topped up continuously by
     the background refresher, not just after failures.
"""

from __future__ import annotations

import atexit
import queue
import random
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional

from curl_cffi import requests as cffi_requests

from utils import log_info, log_ok, log_warn, log_err, looks_like_cloudflare, get_proxy_url

IMPERSONATE_PROFILES = ["chrome124", "chrome120", "chrome123"]

DEFAULT_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "pt-PT,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "upgrade-insecure-requests": "1",
}

# Fallback circuit-breaker tuning (all overridable via config.json)
DEFAULT_CB_THRESHOLD = 8       # consecutive curl_cffi failures before tripping
DEFAULT_CB_COOLDOWN_BASE = 15  # requests to skip curl_cffi for, on the FIRST trip
DEFAULT_CB_COOLDOWN_GROWTH = 1.6  # each successive trip multiplies the cooldown by this

# Serializes undetected-chromedriver LAUNCHES (not fetches) across every
# slot in the process, to avoid the port/profile-lock race described above.
_LAUNCH_LOCK = threading.Lock()
_LAST_LAUNCH_MONOTONIC = [0.0]


def _proxy_dict(proxy_url: Optional[str]) -> Optional[dict]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


class _BrowserSlot:
    """One persistent SeleniumBase browser + its own lock. Multiple slots
    give real concurrency instead of one global bottleneck."""

    def __init__(self, cfg: dict, slot_id: int):
        self.cfg = cfg
        self.slot_id = slot_id
        self.lock = threading.RLock()
        self._sb = None
        self._sb_cm = None
        self._fetch_count = 0
        self._launched_at = None
        self._recycle_at_count = None

    def _ensure_browser(self):
        if self._sb is not None:
            return self._sb
        from seleniumbase import SB

        with _LAUNCH_LOCK:
            if self._sb is not None:  # someone else launched it while we waited
                return self._sb
            stagger = self.cfg.get("browser_launch_stagger_seconds", 2.0)
            elapsed = time.monotonic() - _LAST_LAUNCH_MONOTONIC[0]
            if elapsed < stagger:
                time.sleep(stagger - elapsed)

            log_info(f"Launching browser pool slot #{self.slot_id} (uc=True, CDP mode)…")
            profile_dir = self.cfg.get("chrome_profile_dir", "output/chrome_profile")
            sb_kwargs = dict(
                uc=True,
                test=True,
                locale=self.cfg.get("language", "pt"),
                headed=bool(self.cfg.get("headed", True)),
                user_data_dir=f"{profile_dir}_slot{self.slot_id}",
            )
            proxy = get_proxy_url()
            if proxy:
                sb_kwargs["proxy"] = proxy
            try:
                self._sb_cm = SB(**sb_kwargs)
                self._sb = self._sb_cm.__enter__()
            except Exception:
                # leave clean None state so the NEXT call retries fresh
                # instead of being stuck with a half-initialized object
                self._sb_cm = None
                self._sb = None
                raise
            _LAST_LAUNCH_MONOTONIC[0] = time.monotonic()

            session_id = None
            try:
                session_id = self._sb.driver.session_id
            except Exception:
                pass
            log_ok(f"Slot #{self.slot_id} launched (session_id={session_id}) "
                   f"— compare session_ids across slots if fetches ever look like they're "
                   f"landing on the same browser again")

            self._launched_at = time.monotonic()
            self._fetch_count = 0
            base = self.cfg.get("browser_recycle_after_requests", 25)
            jitter = self.cfg.get("browser_recycle_jitter", 5)
            # jitter desyncs slots so they don't all hit their recycle point
            # on the same fetch and momentarily drop the pool to 0 capacity
            self._recycle_at_count = (base + random.randint(-jitter, jitter)) if base else None
        return self._sb

    def _close_locked(self) -> None:
        """Assumes self.lock is already held."""
        if self._sb_cm is not None:
            log_info(f"Closing browser pool slot #{self.slot_id}…")
            try:
                self._sb_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._sb_cm = None
            self._sb = None

    def _maybe_recycle(self) -> None:
        """Assumes self.lock is already held. A long-lived Chrome process on
        this site appears to accumulate a worse Cloudflare trust score over
        a run (matches the observed pattern: a fresh script restart — same
        profile dir, brand-new Chrome process — clears the bugginess for
        another 60-80 requests) — so periodically kill and relaunch the
        browser itself rather than only re-navigating it for fresh cookies."""
        if self._sb is None:
            return  # nothing running yet, nothing to recycle
        recycle_after_seconds = self.cfg.get("browser_recycle_after_seconds", 1800)
        age = (time.monotonic() - self._launched_at) if self._launched_at else 0
        due_by_count = self._recycle_at_count and self._fetch_count >= self._recycle_at_count
        due_by_age = recycle_after_seconds and age >= recycle_after_seconds
        if due_by_count or due_by_age:
            reason = f"{self._fetch_count} fetches" if due_by_count else f"{int(age)}s old"
            log_info(f"Slot #{self.slot_id} recycling browser ({reason}) — closing and "
                     f"relaunching a fresh Chrome process before the next fetch")
            self._close_locked()

    def _wait_for_clearance(self, sb) -> str:
        """Poll instead of blindly sleeping the full budget: many loads on
        this site hit no Cloudflare interstitial at all, so return the
        moment the page is clean rather than always paying the max wait."""
        max_wait = self.cfg.get("cloudflare_wait_seconds", 25)
        poll = max(0.25, self.cfg.get("cloudflare_poll_interval_seconds", 1.0))

        first_look = min(1.0, max_wait)
        sb.sleep(first_look)
        waited = first_look

        html = sb.get_page_source()
        if not looks_like_cloudflare(html):
            return html  # cleared instantly — no challenge was shown at all

        while waited < max_wait:
            try:
                sb.solve_captcha()
            except Exception:
                pass
            sb.sleep(poll)
            waited += poll
            html = sb.get_page_source()
            if not looks_like_cloudflare(html):
                return html  # cleared early — stop waiting the rest of the budget

        return html  # still blocked after the full budget; caller logs it

    def fetch(self, url: str, _retry: bool = True) -> tuple[Optional[str], list, Optional[str]]:
        """Navigate this slot's browser to url and return (html, cookies,
        user_agent). Blocks other callers of THIS slot until done — other
        slots are unaffected, which is what gives the pool its concurrency.

        Self-healing: if the browser this slot is holding has died (crashed,
        OOM'd, or was left half-broken by a launch race), the exception
        would otherwise repeat identically forever — every future fetch on
        this slot hitting the same now-dead debugger port. Instead, any
        failure here forces the slot closed so the NEXT attempt is
        guaranteed a fresh browser, and retries once immediately before
        giving up."""
        with self.lock:
            self._maybe_recycle()
            sb = self._ensure_browser()
            try:
                sb.activate_cdp_mode(url)
                html = self._wait_for_clearance(sb)
                cookies = sb.get_cookies()
                user_agent = None
                try:
                    user_agent = sb.execute_script("return navigator.userAgent")
                except Exception:
                    pass
                self._fetch_count += 1
                return html, cookies, user_agent
            except Exception as e:
                log_warn(f"Slot #{self.slot_id} browser session appears dead ({e}) — "
                         f"closing it so the next attempt launches a fresh one")
                self._close_locked()
                if _retry:
                    return self.fetch(url, _retry=False)
                raise

    def close(self) -> None:
        with self.lock:
            self._close_locked()


class _BrowserPool:
    """Process-wide singleton: a fixed pool of browser slots, shared by
    every worker thread/CloudflareSession, plus the curl_cffi circuit
    breaker state (a property of "is this site currently blocking us",
    which is global, not per-thread), and the proactive session
    refresher."""

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
        # A checkout queue instead of blind round-robin: _pick_slot()
        # blocks until an actually-IDLE slot is available, so a request
        # never queues behind a busy slot while a different slot sits idle
        # waiting for its "turn" in a fixed rotation.
        self._available: "queue.Queue[_BrowserSlot]" = queue.Queue()
        for slot in self.slots:
            self._available.put(slot)

        self.cookie_lock = threading.Lock()
        self.cookies: list = []
        self.user_agent: Optional[str] = None

        self.breaker_lock = threading.Lock()
        self._curl_fail_streak = 0
        self._total_requests = 0
        self._curl_disabled_until = 0
        self._cb_trip_count = 0
        self.cb_threshold = cfg.get("circuit_breaker_threshold", DEFAULT_CB_THRESHOLD)
        self.cb_cooldown_base = cfg.get("circuit_breaker_cooldown_base", DEFAULT_CB_COOLDOWN_BASE)
        self.cb_cooldown_growth = cfg.get("circuit_breaker_cooldown_growth", DEFAULT_CB_COOLDOWN_GROWTH)

        self._warmed_up = False
        self._shutdown = False
        self._refresher_thread: Optional[threading.Thread] = None
        _register_for_cleanup(self)

    # ── pool dispatch ──────────────────────────────────────────
    def _checkout_slot(self) -> _BrowserSlot:
        return self._available.get()  # blocks only when every slot is busy

    def _checkin_slot(self, slot: _BrowserSlot) -> None:
        self._available.put(slot)

    def _broadcast(self, cookies: list, user_agent: Optional[str]) -> None:
        with self.cookie_lock:
            self.cookies = cookies
            if user_agent:
                self.user_agent = user_agent

    def refresh_clearance(self, url: str) -> Optional[str]:
        slot = self._checkout_slot()
        try:
            try:
                html, cookies, user_agent = slot.fetch(url)
            except Exception as e:
                log_err(f"Browser pool slot #{slot.slot_id} fetch failed for {url}: {e}")
                return None
            self._broadcast(cookies, user_agent)
            ok = not looks_like_cloudflare(html)
            (log_ok if ok else log_warn)(
                f"Slot #{slot.slot_id} {'cleared Cloudflare' if ok else 'still blocked'} for {url}"
            )
            return html
        finally:
            self._checkin_slot(slot)

    def warm_up(self) -> None:
        """Visit the homepage on every pool slot BEFORE real scraping
        starts, so the pool already holds valid cookies instead of paying
        the clearance cost on the first real request from each slot.
        Launches are serialized (via _LAUNCH_LOCK inside _ensure_browser),
        so running this concurrently across slots is safe now — it just
        queues the actual browser-startup moment for each one."""
        with self.breaker_lock:
            if self._warmed_up:
                return
            self._warmed_up = True

        home = self.cfg["base_urls"]["pt"].rstrip("/") + "/"
        log_info(f"Warming up {len(self.slots)} browser pool slot(s) against {home}…")

        def _warm_one(slot: _BrowserSlot):
            try:
                html, cookies, user_agent = slot.fetch(home)
                self._broadcast(cookies, user_agent)
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

    def start_background_refresher(self) -> None:
        """Keep curl_cffi's session young proactively, not just after a
        failure. Every `session_refresh_interval_seconds`, borrow an idle
        slot (skip the cycle entirely if every slot is busy — real
        scraping traffic through the pool refreshes cookies too) and mint
        a fresh clearance purely to keep the broadcast cookies current."""
        if self._refresher_thread is not None:
            return
        interval = self.cfg.get("session_refresh_interval_seconds", 20)
        if interval <= 0:
            return  # disabled via config

        def _loop():
            home = self.cfg["base_urls"]["pt"].rstrip("/") + "/"
            while not self._shutdown:
                # sleep in small increments so shutdown is responsive
                slept = 0.0
                while slept < interval and not self._shutdown:
                    time.sleep(min(0.5, interval - slept))
                    slept += 0.5
                if self._shutdown:
                    return
                try:
                    slot = self._available.get(timeout=3)
                except queue.Empty:
                    continue  # pool fully busy — skip this cycle
                try:
                    html, cookies, user_agent = slot.fetch(home)
                    self._broadcast(cookies, user_agent)
                    ok = not looks_like_cloudflare(html)
                    (log_ok if ok else log_warn)(
                        f"Background refresh via slot #{slot.slot_id} "
                        f"{'renewed' if ok else 'attempted but still blocked'} the curl_cffi session"
                    )
                except Exception as e:
                    log_warn(f"Background session refresh failed: {e}")
                finally:
                    self._checkin_slot(slot)

        self._refresher_thread = threading.Thread(
            target=_loop, daemon=True, name="session-refresher"
        )
        self._refresher_thread.start()
        log_info(f"Background session refresher started (~every {interval}s) — keeps curl_cffi "
                 f"cookies warm continuously instead of only refreshing after a failure")

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
                self._cb_trip_count = 0  # a clean success earns back full trust
                return
            self._curl_fail_streak += 1
            if self._curl_fail_streak >= self.cb_threshold and \
                    self._total_requests >= self._curl_disabled_until:
                self._cb_trip_count += 1
                cooldown = int(self.cb_cooldown_base * (self.cb_cooldown_growth ** (self._cb_trip_count - 1)))
                self._curl_disabled_until = self._total_requests + cooldown
                self._curl_fail_streak = 0  # reset — don't keep counting past the threshold
                log_warn(f"curl_cffi failed {self.cb_threshold} times in a row "
                         f"(trip #{self._cb_trip_count}) — skipping it for the next "
                         f"{cooldown} request(s), going straight to the browser pool")

    def close(self) -> None:
        self._shutdown = True
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
    clearance cost, then starts the proactive background session
    refresher."""
    pool = _BrowserPool(cfg)
    pool.warm_up()
    pool.start_background_refresher()


class CloudflareSession:
    """Cheap to create — one per worker thread. All instances share the
    same browser pool above; only the curl_cffi session is per-thread."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._pool = _BrowserPool(cfg)
        self._proxies = _proxy_dict(get_proxy_url())
        self.http = self._new_http_session()
        self._pool.apply_cookies(self.http)

    def _new_http_session(self) -> cffi_requests.Session:
        return cffi_requests.Session(
            impersonate=random.choice(IMPERSONATE_PROFILES),
            headers=DEFAULT_HEADERS,
            proxies=self._proxies,
        )

    def _http_get(self, url: str, timeout: int = 30):
        return self.http.get(url, timeout=timeout, allow_redirects=True)

    def get_html(self, url: str, force_browser: bool = False) -> Optional[str]:
        self._pool.note_request()
        if not force_browser and self._pool.should_try_curl():
            attempts = max(1, self.cfg.get("curl_retry_attempts", 2))
            for attempt in range(1, attempts + 1):
                try:
                    resp = self._http_get(url)
                    if not looks_like_cloudflare(resp.text, resp.status_code):
                        self._pool.note_curl_result(True)
                        return resp.text
                    if attempt < attempts:
                        # rotate TLS impersonation profile and retry once
                        # more before this counts as one breaker failure —
                        # cheap (no browser) and sometimes enough on its own
                        self.http = self._new_http_session()
                        self._pool.apply_cookies(self.http)
                        continue
                    self._pool.note_curl_result(False)
                    log_warn(f"curl_cffi blocked/challenged on {url} "
                             f"(status={resp.status_code}) after {attempts} attempt(s) "
                             f"— escalating to browser pool")
                except Exception as e:
                    if attempt < attempts:
                        self.http = self._new_http_session()
                        self._pool.apply_cookies(self.http)
                        continue
                    self._pool.note_curl_result(False)
                    log_warn(f"curl_cffi error on {url}: {e} after {attempts} attempt(s) "
                             f"— escalating to browser pool")

        html = self._pool.refresh_clearance(url)
        self._pool.apply_cookies(self.http)  # pick up fresh cookies for this thread too
        if html and looks_like_cloudflare(html):
            log_err(f"Still blocked after browser pool fetch: {url}")
        return html

    def close(self):
        """No-op: the pool is shared/process-wide and closed centrally via
        close_all_browsers() (atexit / SIGINT). Kept for API compat."""
        pass