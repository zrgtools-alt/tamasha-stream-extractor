"""
Tamasha Free Channel HLS Stream Extractor API — v2.1 (Production)
=================================================================
Flask + Playwright headless Chromium API that extracts fresh signed
HLS/m3u8 stream URLs from Tamashaweb.com for FREE channels only.

Optimized for Render.com Docker deployment (512MB–2GB RAM).
For personal/educational use only — no piracy, no DRM bypass.
"""

import os
import re
import sys
import time
import json
import logging
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from threading import Lock
from flask import Flask, jsonify, request

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ==========================================================================
# Flask App
# ==========================================================================
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tamasha")

# ==========================================================================
# Configuration via environment
# ==========================================================================
EXTRA_WAIT = int(os.environ.get("EXTRA_WAIT_SECONDS", "12"))
NAV_TIMEOUT = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "300"))

# Tamasha base — they sometimes switch between tamashaweb.com and tamasha.com
TAMASHA_BASE = os.environ.get("TAMASHA_BASE_URL", "https://tamashaweb.com")

# ==========================================================================
# FREE Channel Registry
# Only channels confirmed playable WITHOUT login/OTP/subscription.
# Format: "our-slug" -> "tamasha-url-slug"
# Some channels may have alternate URL patterns — we handle that below.
# ==========================================================================
CHANNEL_SLUGS = {
    # ─── News ───
    "ary-news":             "ary-news",
    "geo-news-live":        "geo-news-live",
    "express-news-live":    "express-news-live",
    "dunya-news-live":      "dunya-news-live",
    "samaa-news-live":      "samaa-news-live",
    "92-news-live":         "92-news-live",
    "24-news-hd-live":      "24-news-hd-live",
    "hum-news-live":        "hum-news-live",
    "aaj-news-live":        "aaj-news-live",
    "bol-news-live":        "bol-news-live",
    "neo-news-live":        "neo-news-live",
    "public-news-live":     "public-news-live",
    "gnn-news-live":        "gnn-news-live",
    "capital-news-live":    "capital-news-live",
    "ab-tak-news-live":     "ab-tak-news-live",
    "city-42-live":         "city-42-live",
    "dawn-news-live":       "dawn-news-live",
    "din-news-live":        "din-news-live",
    "such-news-live":       "such-news-live",
    "k-21-news-live":       "k-21-news-live",
    "roze-news-live":       "roze-news-live",
    "sun-news-hd":          "sun-news-hd",
    "metro-one-news":       "metro-one-news",

    # ─── Entertainment ───
    "green-entertainment":          "green-entertainment",
    "geo-entertainment-live":       "geo-entertainment-live",
    "ary-digital-live":             "ary-digital-live",
    "hum-tv-live":                  "hum-tv-live",
    "express-entertainment-live":   "express-entertainment-live",
    "a-plus-live":                  "a-plus-live",
    "tv-one-live":                  "tv-one-live",
    "urdu-1-live":                  "urdu-1-live",
    "see-tv-live":                  "see-tv-live",
    "play-tv-live":                 "play-tv-live",
    "geo-kahani-live":              "geo-kahani-live",
    "ary-zindagi-live":             "ary-zindagi-live",

    # ─── Tamasha Original ───
    "tamasha-life-hd": "tamasha-life-hd",

    # ─── Regional ───
    "khyber-news-live":     "khyber-news-live",
    "avt-khyber-live":      "avt-khyber-live",
    "sindh-tv-news-live":   "sindh-tv-news-live",
    "ktn-news-live":        "ktn-news-live",
    "waseb-tv-live":        "waseb-tv-live",
    "mehran-tv-live":       "mehran-tv-live",

    # ─── Religious ───
    "madani-channel-live":  "madani-channel-live",
    "qtv-live":             "qtv-live",
    "paigham-tv-live":      "paigham-tv-live",
    "ary-qtv-live":         "ary-qtv-live",

    # ─── Music / Lifestyle ───
    "ary-musik-live": "ary-musik-live",
}

# ==========================================================================
# URL pattern variants — Tamasha sometimes uses different URL structures
# We try multiple patterns if the primary one fails.
# ==========================================================================
URL_PATTERNS = [
    "{base}/{slug}",           # Primary: tamashaweb.com/ary-news
    "{base}/watch/{slug}",     # Alt: tamashaweb.com/watch/ary-news
    "{base}/live/{slug}",      # Alt: tamashaweb.com/live/ary-news
    "{base}/live-tv/{slug}",   # Alt: tamashaweb.com/live-tv/ary-news
    "{base}/channel/{slug}",   # Alt: tamashaweb.com/channel/ary-news
]

# ==========================================================================
# User-Agent pool
# ==========================================================================
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
]
_ua_idx = 0


def _get_ua():
    global _ua_idx
    ua = _UA_POOL[_ua_idx % len(_UA_POOL)]
    _ua_idx += 1
    return ua


# ==========================================================================
# In-memory cache
# ==========================================================================
_cache = {}
_cache_lock = Lock()


def cache_get(channel):
    with _cache_lock:
        entry = _cache.get(channel)
        if not entry:
            return None
        age = (datetime.utcnow() - entry["ts"]).total_seconds()
        if age < CACHE_TTL:
            logger.info(f"Cache HIT: '{channel}' (age {int(age)}s)")
            return entry
        del _cache[channel]
        return None


def cache_set(channel, url, all_urls=None):
    with _cache_lock:
        _cache[channel] = {
            "url": url,
            "all_urls": all_urls or [],
            "ts": datetime.utcnow(),
        }


def cache_clear(channel=None):
    with _cache_lock:
        if channel:
            _cache.pop(channel, None)
        else:
            _cache.clear()


# ==========================================================================
# Premium / Login Detection
# ==========================================================================
PREMIUM_URL_KEYWORDS = [
    "/plans", "/login", "/subscribe", "/signup", "/otp",
    "/get-pro", "/upgrade", "/signin", "/auth", "/verify",
]

PREMIUM_TEXT_KEYWORDS = [
    "please login", "please sign in", "subscribe to watch",
    "get tamasha pro", "login to watch", "sign in to continue",
    "premium content", "enter your otp", "enter your phone",
    "enter mobile number", "subscription required",
    "upgrade your plan", "start your free trial",
    "jazz/warid number", "verify your number",
    "login or signup", "create account to watch",
]


def detect_premium(url, text=""):
    url_l = url.lower()
    for kw in PREMIUM_URL_KEYWORDS:
        if kw in url_l:
            return True, f"URL redirect to '{kw}'"

    text_l = text.lower() if text else ""
    for kw in PREMIUM_TEXT_KEYWORDS:
        if kw in text_l:
            return True, f"Page shows '{kw}'"

    return False, None


# ==========================================================================
# M3U8 URL Scoring — pick the best captured URL
# ==========================================================================
def score_url(url):
    s = 0
    ul = url.lower()

    # Playlist type bonus
    if "playlist.m3u8" in ul:
        s += 100
    elif "chunklist" in ul:
        s += 95
    elif "index.m3u8" in ul:
        s += 80
    elif "mono.m3u8" in ul:
        s += 75
    elif "master.m3u8" in ul:
        s += 50
    elif ".m3u8" in ul:
        s += 40

    # Auth token — most important signal
    if "wmsauthsign" in ul:
        s += 200
    elif "hdnts=" in ul or "hdntl=" in ul:
        s += 150
    elif "token=" in ul:
        s += 100
    elif "auth" in ul:
        s += 50

    if "nimblesessionid" in ul:
        s += 30
    if ul.startswith("https://"):
        s += 10

    # Penalize ad URLs
    for ad in ["ad.", "ads.", "adserver", "doubleclick", "googlesyndication"]:
        if ad in ul:
            s -= 500

    # Query richness
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    s += len(params) * 8

    return s


# ==========================================================================
# HLS Marker Detection — what URLs to capture
# ==========================================================================
HLS_MARKERS = [
    ".m3u8", "wmsauthsign", "jazzauth", "playlist.m3u8",
    "master.m3u8", "chunklist", "index.m3u8", "manifest.m3u8",
]


def is_hls_url(url):
    ul = url.lower()
    return any(m in ul for m in HLS_MARKERS)


# ==========================================================================
# Core Extraction — Playwright Headless Browser
# ==========================================================================
_browser_lock = Lock()


def extract_stream(channel_slug, attempt=1, max_attempts=2):
    """
    Launch headless Chromium, navigate to channel page,
    intercept network responses to capture signed m3u8 URL.
    """

    # Build URL candidates (try multiple patterns)
    url_candidates = []
    for pattern in URL_PATTERNS:
        url_candidates.append(pattern.format(base=TAMASHA_BASE, slug=channel_slug))

    logger.info(f"[Attempt {attempt}/{max_attempts}] Channel: '{channel_slug}'")
    logger.info(f"  URL candidates: {url_candidates[:3]}")

    captured = []
    captured_lock = Lock()
    failed_m3u8 = []
    page_urls_visited = []

    def on_response(response):
        try:
            rurl = response.url
            if not is_hls_url(rurl):
                return
            status = response.status
            if 200 <= status < 400:
                with captured_lock:
                    captured.append({
                        "url": rurl,
                        "status": status,
                        "time": time.time(),
                    })
                logger.debug(f"  ✓ [{status}] {rurl[:160]}")
            else:
                logger.debug(f"  ✗ [{status}] {rurl[:120]}")
        except Exception:
            pass

    def on_request_failed(req):
        try:
            if ".m3u8" in req.url.lower():
                failed_m3u8.append({"url": req.url[:150], "error": req.failure})
        except Exception:
            pass

    # Acquire browser lock — only one extraction at a time
    acquired = _browser_lock.acquire(timeout=90)
    if not acquired:
        return {
            "success": False,
            "error": "Server busy — another extraction in progress. Retry in ~30s.",
            "channel": channel_slug,
        }

    browser = None
    try:
        with sync_playwright() as pw:
            t0 = time.time()
            logger.info("  Launching Chromium...")

            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-software-rasterizer",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--disable-translate",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--single-process",
                    "--mute-audio",
                    "--autoplay-policy=no-user-gesture-required",
                    "--disable-hang-monitor",
                    "--disable-prompt-on-repost",
                    "--disable-client-side-phishing-detection",
                    "--disable-component-update",
                    "--disable-domain-reliability",
                ],
            )
            logger.info(f"  Chromium launched in {time.time()-t0:.1f}s")

            ua = _get_ua()
            ctx = browser.new_context(
                user_agent=ua,
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
                bypass_csp=True,
                locale="en-US",
                timezone_id="Asia/Karachi",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9,ur;q=0.8",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Sec-CH-UA": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                },
            )

            # ── Stealth script ──
            ctx.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const p = [
                            {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
                            {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                            {name:'Native Client', filename:'internal-nacl-plugin'},
                        ];
                        p.length = 3;
                        return p;
                    }
                });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
                window.chrome = { runtime: {}, loadTimes: ()=>{}, csi: ()=>{}, app: {} };
                const oq = window.navigator.permissions.query;
                window.navigator.permissions.query = (p) =>
                    p.name === 'notifications'
                        ? Promise.resolve({state: Notification.permission})
                        : oq(p);
            """)

            # ── Block heavy resources to save RAM ──
            def route_handler(route):
                rt = route.request.resource_type
                rurl = route.request.url.lower()

                # Block images, fonts, stylesheets (except HLS-related)
                if rt in {"image", "font", "stylesheet"}:
                    if ".m3u8" not in rurl and ".ts" not in rurl:
                        route.abort()
                        return

                # Block ads / trackers
                blocked = [
                    "google-analytics.com", "googletagmanager.com",
                    "facebook.net", "facebook.com", "fbcdn.net",
                    "doubleclick.net", "googlesyndication.com",
                    "googleadservices.com", "hotjar.com",
                    "clarity.ms", "sentry.io", "segment.io",
                    "mixpanel.com", "amplitude.com",
                ]
                for d in blocked:
                    if d in rurl:
                        route.abort()
                        return

                route.continue_()

            ctx.route("**/*", route_handler)

            page = ctx.new_page()
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            # ── Try URL candidates until one works ──
            nav_success = False
            final_url = None

            for candidate_url in url_candidates:
                logger.info(f"  Trying: {candidate_url}")
                try:
                    resp = page.goto(
                        candidate_url,
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT,
                    )
                    final_url = page.url
                    page_urls_visited.append(final_url)

                    # Check if we got a 404
                    if resp and resp.status == 404:
                        logger.info(f"  Got 404 for {candidate_url}, trying next...")
                        continue

                    # Check if page title says 404
                    try:
                        title = page.title().lower()
                        if "404" in title or "not found" in title:
                            logger.info(f"  Page title indicates 404, trying next...")
                            continue
                    except Exception:
                        pass

                    nav_success = True
                    logger.info(f"  ✓ Navigation success → {final_url}")
                    break

                except PlaywrightTimeout:
                    logger.warning(f"  Timeout for {candidate_url}")
                    # Still might have loaded enough — check if we captured anything
                    if captured:
                        nav_success = True
                        final_url = page.url
                        break
                    continue
                except Exception as e:
                    logger.warning(f"  Error for {candidate_url}: {e}")
                    continue

            if not nav_success and not captured:
                browser.close()
                return {
                    "success": False,
                    "error": f"Could not load channel page. Tried {len(url_candidates)} URL patterns.",
                    "channel": channel_slug,
                    "urls_tried": url_candidates[:3],
                    "hint": "Channel slug may be wrong or Tamasha's URL structure changed.",
                }

            # ── Wait for networkidle ──
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                logger.debug("  networkidle timeout, continuing...")

            # ── Premium detection ──
            current_url = page.url
            try:
                body_text = page.evaluate(
                    "() => document.body ? document.body.innerText.substring(0, 5000) : ''"
                )
            except Exception:
                body_text = ""

            is_prem, prem_reason = detect_premium(current_url, body_text)
            if is_prem:
                browser.close()
                return {
                    "success": False,
                    "error": "Premium channel — requires login/subscription.",
                    "channel": channel_slug,
                    "reason": prem_reason,
                    "final_url": current_url,
                    "hint": "This channel is not free. Remove it from your requests.",
                }

            # ── Look for video player ──
            video_found = False
            logger.info("  Looking for video player...")

            # Try multiple selectors
            video_selectors = [
                "video",
                "video[src]",
                ".video-js video",
                ".jw-video",
                "#player video",
                "[class*='player'] video",
            ]
            for sel in video_selectors:
                try:
                    page.wait_for_selector(sel, timeout=8000)
                    video_found = True
                    logger.info(f"  ✓ Found video via: {sel}")
                    break
                except PlaywrightTimeout:
                    continue

            if not video_found:
                # Check iframes
                try:
                    for iframe_el in page.query_selector_all("iframe"):
                        try:
                            frame = iframe_el.content_frame()
                            if frame:
                                frame.wait_for_selector("video", timeout=5000)
                                video_found = True
                                logger.info("  ✓ Found video inside iframe")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            if not video_found:
                logger.warning("  ⚠ No video element found")

            # ── Dismiss overlays / click play ──
            try:
                # Cookie consent / overlays
                dismiss_selectors = [
                    "button[class*='accept']",
                    "button[class*='consent']",
                    "button[class*='close']",
                    "button[class*='dismiss']",
                    "[class*='cookie'] button",
                    "[class*='overlay'] button",
                    ".modal-close",
                    "button[aria-label='Close']",
                ]
                for sel in dismiss_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=2000)
                            logger.debug(f"  Dismissed overlay: {sel}")
                            time.sleep(0.5)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            try:
                # Play buttons
                play_selectors = [
                    "button.vjs-big-play-button",
                    ".play-button",
                    ".vjs-play-control.vjs-paused",
                    "button[aria-label='Play']",
                    "button[title='Play']",
                    ".jw-icon-playback",
                    ".bmpui-ui-playbacktogglebutton",
                    "[data-testid='play-button']",
                    ".play-icon",
                    "video",  # clicking video itself
                ]
                for sel in play_selectors:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=2000)
                            logger.info(f"  ▶ Clicked play: {sel}")
                            time.sleep(1)
                            break
                    except Exception:
                        continue
            except Exception:
                pass

            # ── Force autoplay via JS ──
            try:
                page.evaluate("""
                    () => {
                        document.querySelectorAll('video').forEach(v => {
                            v.muted = true;
                            v.play().catch(() => {});
                        });
                        // Also try iframes
                        document.querySelectorAll('iframe').forEach(f => {
                            try {
                                const doc = f.contentDocument || f.contentWindow.document;
                                if (doc) {
                                    doc.querySelectorAll('video').forEach(v => {
                                        v.muted = true;
                                        v.play().catch(() => {});
                                    });
                                }
                            } catch(e) {}
                        });
                    }
                """)
            except Exception:
                pass

            # ── Main HLS wait ──
            logger.info(f"  Waiting {EXTRA_WAIT}s for HLS manifest requests...")
            time.sleep(EXTRA_WAIT)

            # ── If nothing captured, try deeper extraction ──
            if not captured:
                logger.info("  No network captures yet — trying JS extraction...")

                # Method A: video.src / currentSrc
                try:
                    srcs = page.evaluate("""
                        () => {
                            const s = new Set();
                            document.querySelectorAll('video').forEach(v => {
                                if (v.src) s.add(v.src);
                                if (v.currentSrc) s.add(v.currentSrc);
                                v.querySelectorAll('source').forEach(src => {
                                    if (src.src) s.add(src.src);
                                });
                            });
                            // Iframes
                            document.querySelectorAll('iframe').forEach(f => {
                                try {
                                    const d = f.contentDocument || f.contentWindow.document;
                                    if (d) d.querySelectorAll('video').forEach(v => {
                                        if (v.src) s.add(v.src);
                                        if (v.currentSrc) s.add(v.currentSrc);
                                    });
                                } catch(e) {}
                            });
                            return [...s];
                        }
                    """)
                    for src in (srcs or []):
                        if src and is_hls_url(src):
                            with captured_lock:
                                captured.append({"url": src, "status": 200, "time": time.time()})
                            logger.info(f"  ✓ From video.src: {src[:150]}")
                except Exception as e:
                    logger.debug(f"  video.src: {e}")

                # Method B: Player JS instances
                try:
                    ps = page.evaluate("""
                        () => {
                            const urls = [];
                            // hls.js
                            try {
                                document.querySelectorAll('video').forEach(v => {
                                    for (const k of Object.keys(v)) {
                                        const obj = v[k];
                                        if (obj && typeof obj === 'object') {
                                            if (obj.url && typeof obj.url === 'string')
                                                urls.push(obj.url);
                                            if (obj.levels && Array.isArray(obj.levels)) {
                                                obj.levels.forEach(l => {
                                                    if (l.url && typeof l.url === 'string')
                                                        urls.push(l.url);
                                                    if (l.uri) urls.push(l.uri);
                                                });
                                            }
                                        }
                                    }
                                });
                            } catch(e) {}
                            // videojs
                            try {
                                if (window.videojs) {
                                    const pp = window.videojs.getAllPlayers
                                        ? window.videojs.getAllPlayers()
                                        : Object.values(window.videojs.getPlayers());
                                    pp.forEach(p => {
                                        if (p) {
                                            try { urls.push(p.currentSrc()); } catch(e) {}
                                            try {
                                                const t = p.tech({IWillNotUseThisInPlugins:true});
                                                if (t && t.currentSource_)
                                                    urls.push(t.currentSource_.src);
                                            } catch(e) {}
                                        }
                                    });
                                }
                            } catch(e) {}
                            // jwplayer
                            try {
                                if (window.jwplayer) {
                                    const p = window.jwplayer();
                                    if (p && p.getPlaylistItem) {
                                        const item = p.getPlaylistItem();
                                        if (item) {
                                            if (item.file) urls.push(item.file);
                                            if (item.sources)
                                                item.sources.forEach(s => { if(s.file) urls.push(s.file); });
                                        }
                                    }
                                }
                            } catch(e) {}
                            // Shaka
                            try {
                                document.querySelectorAll('video').forEach(v => {
                                    for (const k of Object.keys(v)) {
                                        const obj = v[k];
                                        if (obj && typeof obj.getAssetUri === 'function') {
                                            urls.push(obj.getAssetUri());
                                        }
                                    }
                                });
                            } catch(e) {}
                            return urls.filter(u => u && typeof u === 'string');
                        }
                    """)
                    for src in (ps or []):
                        if is_hls_url(src):
                            with captured_lock:
                                captured.append({"url": src, "status": 200, "time": time.time()})
                            logger.info(f"  ✓ From player JS: {src[:150]}")
                except Exception as e:
                    logger.debug(f"  Player JS: {e}")

                # Method C: Regex scan of page source
                try:
                    html = page.content()
                    pattern = re.compile(
                        r'(https?://[^\s"\'<>\\]*\.m3u8[^\s"\'<>\\]*)',
                        re.IGNORECASE,
                    )
                    for m in pattern.findall(html):
                        clean = (m.replace("\\u0026", "&")
                                  .replace("\\/", "/")
                                  .replace("\\u003d", "=")
                                  .replace("&amp;", "&"))
                        with captured_lock:
                            captured.append({"url": clean, "status": 200, "time": time.time()})
                        logger.info(f"  ✓ Regex match: {clean[:150]}")
                except Exception as e:
                    logger.debug(f"  Regex: {e}")

                # Method D: Check for data attributes
                try:
                    data_srcs = page.evaluate("""
                        () => {
                            const urls = [];
                            document.querySelectorAll('[data-src], [data-url], [data-stream], [data-video-url], [data-hls]').forEach(el => {
                                ['data-src', 'data-url', 'data-stream', 'data-video-url', 'data-hls'].forEach(attr => {
                                    const v = el.getAttribute(attr);
                                    if (v) urls.push(v);
                                });
                            });
                            return urls;
                        }
                    """)
                    for src in (data_srcs or []):
                        if is_hls_url(src):
                            with captured_lock:
                                captured.append({"url": src, "status": 200, "time": time.time()})
                            logger.info(f"  ✓ Data attribute: {src[:150]}")
                except Exception as e:
                    logger.debug(f"  Data attrs: {e}")

                # Last resort: wait more
                if not captured:
                    logger.info("  Still nothing — final 5s wait...")
                    time.sleep(5)

            browser.close()
            browser = None
            logger.info(f"  Browser closed. Captured: {len(captured)} URLs")

    except PlaywrightTimeout as e:
        logger.error(f"Playwright timeout: {e}")
        return {
            "success": False,
            "error": f"Timeout: {str(e)[:200]}",
            "channel": channel_slug,
            "hint": "Page took too long. Try again.",
        }
    except Exception as e:
        logger.error(f"Extraction error: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"Browser error: {str(e)[:300]}",
            "channel": channel_slug,
        }
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass
        _browser_lock.release()

    # ==================================================================
    # Process results
    # ==================================================================
    if not captured:
        # Auto-retry once
        if attempt < max_attempts:
            logger.info(f"  Auto-retrying (attempt {attempt+1})...")
            time.sleep(3)
            return extract_stream(channel_slug, attempt + 1, max_attempts)

        return {
            "success": False,
            "error": "No m3u8 stream URL captured.",
            "channel": channel_slug,
            "video_found": video_found,
            "pages_visited": page_urls_visited[:3],
            "failed_requests": failed_m3u8[:5],
            "hint": (
                "Channel may require login/Pro, or the player didn't load. "
                "Only use confirmed free channels. Try again — some streams "
                "are intermittent."
            ),
        }

    # Deduplicate
    seen = set()
    unique = []
    for e in captured:
        # Simple dedup key
        key = e["url"].split("?")[0] + "?" + "&".join(
            sorted(p for p in e["url"].split("?")[1].split("&") if "nimblesessionid" not in p.lower())
        ) if "?" in e["url"] else e["url"]

        if key not in seen:
            seen.add(key)
            unique.append(e)

    logger.info(f"  Unique URLs: {len(unique)}")
    for i, e in enumerate(unique):
        sc = score_url(e["url"])
        logger.info(f"    [{i}] score={sc:>4d} {e['url'][:180]}")

    # Pick best
    best = max(unique, key=lambda e: (score_url(e["url"]), e["time"]))
    best_url = best["url"]
    best_score = score_url(best_url)

    logger.info(f"  ★ Best (score={best_score}): {best_url[:180]}")

    # Cache
    all_urls = [e["url"] for e in sorted(unique, key=lambda e: score_url(e["url"]), reverse=True)]
    cache_set(channel_slug, best_url, all_urls)

    return {
        "success": True,
        "stream_url": best_url,
        "channel": channel_slug,
        "captured_count": len(unique),
        "score": best_score,
        "video_found": video_found,
        "alternative_urls": all_urls[1:4] if len(all_urls) > 1 else [],
        "note": (
            "Fresh HLS link. Estimated expiry ~10-30 min. "
            "Play in VLC, ffplay, or https://hlsjs.video-dev.org/demo/"
        ),
    }


# ==========================================================================
# Routes
# ==========================================================================

@app.route("/", methods=["GET"])
def index():
    base = request.host_url.rstrip("/")
    return jsonify({
        "service": "Tamasha Free Channel HLS Stream Extractor",
        "version": "2.1.0",
        "status": "running",
        "endpoints": {
            "GET /":                        "API documentation (this page)",
            "GET /api/health":              "Health check",
            "GET /api/channels":            "List all supported free channels",
            "GET /api/fresh_stream?channel=SLUG": "Extract fresh HLS stream URL",
            "GET /api/fresh_stream?channel=SLUG&force=1": "Force-refresh (skip cache)",
            "DELETE /api/cache":            "Clear all cached URLs",
            "DELETE /api/cache?channel=SLUG": "Clear cache for one channel",
        },
        "quick_start": f"curl \"{base}/api/fresh_stream?channel=ary-news\"",
        "disclaimer": (
            "Personal/educational use only. Free/public channels only. "
            "No DRM bypass. No piracy."
        ),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "version": "2.1.0",
        "cache_entries": len(_cache),
        "channels_available": len(CHANNEL_SLUGS),
    })


@app.route("/api/channels", methods=["GET"])
def list_channels():
    cats = {"news": [], "entertainment": [], "religious": [], "regional": [], "other": []}
    for slug in sorted(CHANNEL_SLUGS.keys()):
        s = slug.lower()
        if any(k in s for k in ["news", "city-42"]):
            cats["news"].append(slug)
        elif any(k in s for k in [
            "entertainment", "digital", "tv-one", "urdu", "play-tv",
            "see-tv", "hum-tv", "a-plus", "zindagi", "kahani", "musik",
        ]):
            cats["entertainment"].append(slug)
        elif any(k in s for k in ["madani", "qtv", "paigham"]):
            cats["religious"].append(slug)
        elif any(k in s for k in ["khyber", "avt", "sindh", "ktn", "waseb", "mehran", "pashto"]):
            cats["regional"].append(slug)
        else:
            cats["other"].append(slug)

    return jsonify({
        "total": len(CHANNEL_SLUGS),
        "by_category": cats,
        "all_slugs": sorted(CHANNEL_SLUGS.keys()),
        "usage": "GET /api/fresh_stream?channel=<slug>",
    })


@app.route("/api/fresh_stream", methods=["GET"])
def fresh_stream():
    channel = request.args.get("channel", "").strip().lower()
    force = request.args.get("force", "0") == "1"

    if not channel:
        return jsonify({
            "success": False,
            "error": "Missing 'channel' parameter.",
            "usage": "/api/fresh_stream?channel=ary-news",
            "channels": sorted(CHANNEL_SLUGS.keys()),
        }), 400

    if channel not in CHANNEL_SLUGS:
        # Fuzzy suggestions
        parts = [p for p in channel.split("-") if len(p) > 2]
        suggestions = sorted(set(
            s for s in CHANNEL_SLUGS
            if channel in s or s in channel or
            any(p in s for p in parts)
        ))[:8]

        return jsonify({
            "success": False,
            "error": f"Unknown channel: '{channel}'",
            "suggestions": suggestions or None,
            "hint": "GET /api/channels for all available slugs.",
        }), 404

    slug = CHANNEL_SLUGS[channel]

    # Cache check
    if not force:
        cached = cache_get(channel)
        if cached:
            age = int((datetime.utcnow() - cached["ts"]).total_seconds())
            return jsonify({
                "success": True,
                "stream_url": cached["url"],
                "channel": channel,
                "source": "cache",
                "cache_age_seconds": age,
                "alternative_urls": cached.get("all_urls", [])[1:4],
                "note": f"Cached ({age}s old, TTL {CACHE_TTL}s). Use &force=1 to refresh.",
            })

    # Extract
    logger.info("=" * 60)
    logger.info(f"REQUEST: channel='{channel}' slug='{slug}' force={force}")
    logger.info("=" * 60)

    t0 = time.time()
    result = extract_stream(slug)
    elapsed = round(time.time() - t0, 2)

    result["extraction_time_seconds"] = elapsed
    result["channel"] = channel

    logger.info(f"RESULT: success={result.get('success')} time={elapsed}s")

    return jsonify(result), 200 if result.get("success") else 502


@app.route("/api/cache", methods=["DELETE"])
def clear_cache_endpoint():
    ch = request.args.get("channel", "").strip().lower()
    if ch:
        cache_clear(ch)
        return jsonify({"message": f"Cache cleared for '{ch}'"})
    n = len(_cache)
    cache_clear()
    return jsonify({"message": f"All cache cleared ({n} entries)"})


@app.errorhandler(404)
def err_404(e):
    return jsonify({"error": "Not found", "hint": "GET / for docs"}), 404


@app.errorhandler(500)
def err_500(e):
    return jsonify({"error": "Internal error"}), 500


# ==========================================================================
# Main
# ==========================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting v2.1 on :{port} | {len(CHANNEL_SLUGS)} channels | TTL {CACHE_TTL}s")
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
