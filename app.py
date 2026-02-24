"""
Tamasha Free Channel HLS Stream Extractor API — v2.0 (Production Hardened)
==========================================================================
Flask + Playwright (sync API) service that extracts fresh signed HLS/m3u8
stream URLs from Tamashaweb.com for FREE (no-login) channels only.

Optimized for Render.com Docker deployment with limited RAM (512MB-1GB).
For personal/educational testing only.
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
from threading import Lock, Event
from flask import Flask, jsonify, request

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ==========================================================================
# App Configuration
# ==========================================================================
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("tamasha")

# ==========================================================================
# Configurable parameters via environment
# ==========================================================================
EXTRA_WAIT_SECONDS = int(os.environ.get("EXTRA_WAIT_SECONDS", "10"))
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
MAX_EXTRACTION_RETRIES = int(os.environ.get("MAX_EXTRACTION_RETRIES", "1"))

# Base URL for Tamasha — allows override if domain changes
TAMASHA_BASE_URL = os.environ.get("TAMASHA_BASE_URL", "https://tamashaweb.com")

# ==========================================================================
# Channel Slug Registry — ONLY confirmed free/no-login channels
# ==========================================================================
CHANNEL_SLUGS = {
    # ---- Pakistani News Channels (generally all free on Tamasha) ----
    "ary-news": "ary-news",
    "geo-news-live": "geo-news-live",
    "express-news-live": "express-news-live",
    "dunya-news-live": "dunya-news-live",
    "samaa-news-live": "samaa-news-live",
    "92-news-live": "92-news-live",
    "24-news-hd-live": "24-news-hd-live",
    "hum-news-live": "hum-news-live",
    "aaj-news-live": "aaj-news-live",
    "bol-news-live": "bol-news-live",
    "neo-news-live": "neo-news-live",
    "public-news-live": "public-news-live",
    "gnn-news-live": "gnn-news-live",
    "capital-news-live": "capital-news-live",
    "ab-tak-news-live": "ab-tak-news-live",
    "city-42-live": "city-42-live",
    "dawn-news-live": "dawn-news-live",
    "din-news-live": "din-news-live",
    "such-news-live": "such-news-live",
    "k-21-news-live": "k-21-news-live",
    "roze-news-live": "roze-news-live",
    "sun-news-hd": "sun-news-hd",
    "metro-one-news": "metro-one-news",
    "pashto-1-news": "pashto-1-news",

    # ---- Entertainment (free tier) ----
    "green-entertainment": "green-entertainment",
    "geo-entertainment-live": "geo-entertainment-live",
    "ary-digital-live": "ary-digital-live",
    "hum-tv-live": "hum-tv-live",
    "express-entertainment-live": "express-entertainment-live",
    "a-plus-live": "a-plus-live",
    "tv-one-live": "tv-one-live",
    "urdu-1-live": "urdu-1-live",
    "see-tv-live": "see-tv-live",
    "play-tv-live": "play-tv-live",
    "geo-kahani-live": "geo-kahani-live",
    "ary-zindagi-live": "ary-zindagi-live",

    # ---- Tamasha Originals / Misc ----
    "tamasha-life-hd": "tamasha-life-hd",

    # ---- Regional ----
    "khyber-news-live": "khyber-news-live",
    "avt-khyber-live": "avt-khyber-live",
    "sindh-tv-news-live": "sindh-tv-news-live",
    "ktn-news-live": "ktn-news-live",
    "waseb-tv-live": "waseb-tv-live",
    "mehran-tv-live": "mehran-tv-live",

    # ---- Religious ----
    "madani-channel-live": "madani-channel-live",
    "qtv-live": "qtv-live",
    "paigham-tv-live": "paigham-tv-live",
    "ary-qtv-live": "ary-qtv-live",

    # ---- Music / Lifestyle ----
    "ary-musik-live": "ary-musik-live",
}

# ==========================================================================
# User Agent Rotation — helps avoid fingerprinting
# ==========================================================================
USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
]
_ua_index = 0


def _next_user_agent() -> str:
    global _ua_index
    ua = USER_AGENTS[_ua_index % len(USER_AGENTS)]
    _ua_index += 1
    return ua


# ==========================================================================
# Simple in-memory cache with TTL
# ==========================================================================
_cache = {}
_cache_lock = Lock()


def _cache_key(channel: str) -> str:
    return channel.lower().strip()


def _get_cached(channel: str):
    key = _cache_key(channel)
    with _cache_lock:
        entry = _cache.get(key)
        if not entry:
            return None
        age = (datetime.utcnow() - entry["timestamp"]).total_seconds()
        if age < CACHE_TTL_SECONDS:
            logger.info(f"Cache HIT for '{channel}' (age: {int(age)}s)")
            return entry
        else:
            del _cache[key]
            return None


def _set_cached(channel: str, url: str, all_urls: list = None):
    key = _cache_key(channel)
    with _cache_lock:
        _cache[key] = {
            "url": url,
            "all_urls": all_urls or [],
            "timestamp": datetime.utcnow(),
        }


def _clear_cache(channel: str = None):
    with _cache_lock:
        if channel:
            _cache.pop(_cache_key(channel), None)
        else:
            _cache.clear()


# ==========================================================================
# Premium / Login Detection
# ==========================================================================
PREMIUM_URL_INDICATORS = [
    "/plans", "/login", "/subscribe", "/signup", "/otp",
    "/get-pro", "/upgrade", "/signin", "/auth",
]

PREMIUM_TEXT_PATTERNS = [
    "please login to continue",
    "please sign in",
    "subscribe to watch",
    "get tamasha pro",
    "login to watch",
    "sign in to continue",
    "this content is for pro",
    "premium content",
    "enter your otp",
    "enter your phone",
    "enter mobile number",
    "subscription required",
    "upgrade your plan",
    "start your free trial",
    "jazz/warid number",
]


def _detect_premium(page_url: str, page_content: str = "") -> dict:
    """
    Detect if the page is a premium/login wall.
    Returns {"is_premium": bool, "reason": str or None}
    """
    url_lower = page_url.lower()
    for indicator in PREMIUM_URL_INDICATORS:
        if indicator in url_lower:
            return {"is_premium": True, "reason": f"URL contains '{indicator}'"}

    if page_content:
        content_lower = page_content.lower()
        for pattern in PREMIUM_TEXT_PATTERNS:
            if pattern in content_lower:
                return {"is_premium": True, "reason": f"Page contains '{pattern}'"}

    return {"is_premium": False, "reason": None}


# ==========================================================================
# M3U8 URL Scoring
# ==========================================================================
def _score_m3u8_url(url: str) -> int:
    """
    Score an m3u8 URL by usefulness. Higher = better.
    Prioritizes signed playlist URLs over unsigned master manifests.
    """
    score = 0
    url_lower = url.lower()

    # Type scoring
    if "playlist.m3u8" in url_lower:
        score += 100
    elif "chunklist" in url_lower:
        score += 95
    elif "index.m3u8" in url_lower:
        score += 80
    elif "mono.m3u8" in url_lower:
        score += 75
    elif "master.m3u8" in url_lower:
        score += 50
    elif ".m3u8" in url_lower:
        score += 40

    # Auth token presence — critical
    if "wmsauthsign" in url_lower:
        score += 200
    if "hdnts=" in url_lower or "hdntl=" in url_lower:
        score += 150
    if "token=" in url_lower:
        score += 100
    if "auth" in url_lower:
        score += 50

    # Session tracking
    if "nimblesessionid" in url_lower:
        score += 30

    # Prefer HTTPS
    if url_lower.startswith("https://"):
        score += 10

    # Penalize ad/tracking URLs
    ad_indicators = ["ad.", "ads.", "adserver", "doubleclick", "googlesyndication", "analytics"]
    for ad in ad_indicators:
        if ad in url_lower:
            score -= 500

    # Query parameter richness
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    score += len(params) * 8

    return score


# ==========================================================================
# Core Extraction Engine
# ==========================================================================
_browser_lock = Lock()
_extraction_in_progress = {}  # channel -> Event, prevents duplicate extractions


def _extract_stream_url(channel_slug: str, attempt: int = 1) -> dict:
    """
    Launch headless Chromium, navigate to channel page, intercept HLS URLs.
    Returns a result dict with success/failure info.
    """
    page_url = f"{TAMASHA_BASE_URL}/{channel_slug}"
    logger.info(f"[Attempt {attempt}] Extracting stream for '{channel_slug}' from {page_url}")

    captured_urls = []
    captured_lock = Lock()
    errors_seen = []

    def on_response(response):
        """Network response interceptor — captures HLS manifest URLs."""
        try:
            resp_url = response.url
            resp_lower = resp_url.lower()

            # Quick filter: only care about potential HLS URLs
            is_hls = any(marker in resp_lower for marker in [
                ".m3u8", "wmsauthsign", "jazzauth", "playlist",
                "master.m3u8", "chunklist", "index.m3u8", "manifest",
            ])

            if not is_hls:
                return

            status = response.status
            content_type = ""
            try:
                headers = response.headers
                content_type = headers.get("content-type", "")
            except Exception:
                pass

            entry = {
                "url": resp_url,
                "status": status,
                "content_type": content_type,
                "timestamp": time.time(),
            }

            if 200 <= status < 400:
                with captured_lock:
                    captured_urls.append(entry)
                logger.debug(f"  ✓ Captured [{status}]: {resp_url[:150]}")
            elif status >= 400:
                logger.debug(f"  ✗ Failed [{status}]: {resp_url[:150]}")

        except Exception as e:
            logger.debug(f"  Response callback error: {e}")

    def on_request_failed(request_obj):
        """Track failed requests for diagnostics."""
        try:
            url = request_obj.url
            if ".m3u8" in url.lower():
                failure = request_obj.failure
                errors_seen.append({"url": url[:150], "failure": failure})
                logger.debug(f"  ✗ Request failed: {url[:100]} — {failure}")
        except Exception:
            pass

    acquired = _browser_lock.acquire(timeout=90)
    if not acquired:
        return {
            "success": False,
            "error": "Server busy — another extraction is in progress. Try again in 30 seconds.",
            "channel": channel_slug,
            "hint": "The server processes one channel at a time to manage memory.",
        }

    try:
        with sync_playwright() as pw:
            logger.info("Launching Chromium headless...")
            launch_start = time.time()

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
                    "--disable-ipc-flooding-protection",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--single-process",
                    "--metrics-recording-only",
                    "--mute-audio",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            logger.info(f"Browser launched in {time.time() - launch_start:.1f}s")

            ua = _next_user_agent()
            context = browser.new_context(
                user_agent=ua,
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
                bypass_csp=True,
                locale="en-US",
                timezone_id="Asia/Karachi",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9,ur;q=0.8",
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;"
                        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
                    ),
                    "Sec-CH-UA": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "DNT": "1",
                },
            )

            # Stealth init script
            context.add_init_script("""
                // Hide webdriver
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

                // Fake plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => {
                        const plugins = [
                            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                            { name: 'Native Client', filename: 'internal-nacl-plugin' },
                        ];
                        plugins.length = 3;
                        return plugins;
                    },
                });

                // Fake languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });

                // Chrome object
                window.chrome = {
                    runtime: {},
                    loadTimes: function() {},
                    csi: function() {},
                    app: {},
                };

                // Permissions
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) =>
                    parameters.name === 'notifications'
                        ? Promise.resolve({ state: Notification.permission })
                        : originalQuery(parameters);

                // Prevent detection via toString
                const cleanToString = Function.prototype.toString;
                Function.prototype.toString = function() {
                    if (this === Function.prototype.toString) return 'function toString() { [native code] }';
                    return cleanToString.call(this);
                };
            """)

            # Block unnecessary resources to save memory and speed up
            def route_handler(route):
                """Block heavy resources we don't need."""
                resource_type = route.request.resource_type
                url = route.request.url.lower()

                # Block images, fonts, stylesheets, media (we only need JS + XHR)
                blocked_types = {"image", "font", "stylesheet", "media"}
                if resource_type in blocked_types:
                    # Exception: don't block if it's an HLS segment
                    if ".m3u8" not in url and ".ts" not in url:
                        route.abort()
                        return

                # Block known ad/tracking domains
                blocked_domains = [
                    "google-analytics.com", "googletagmanager.com",
                    "facebook.net", "facebook.com", "fbcdn.net",
                    "doubleclick.net", "googlesyndication.com",
                    "googleadservices.com", "analytics.",
                    "tracker.", "pixel.", "ads.",
                    "hotjar.com", "clarity.ms",
                    "sentry.io", "bugsnag.com",
                ]
                for domain in blocked_domains:
                    if domain in url:
                        route.abort()
                        return

                route.continue_()

            context.route("**/*", route_handler)

            page = context.new_page()

            # Attach interceptors BEFORE navigation
            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)

            # Navigate
            logger.info(f"Navigating to {page_url}...")
            nav_start = time.time()
            try:
                page.goto(page_url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                logger.info(f"Page DOM loaded in {time.time() - nav_start:.1f}s")
            except PlaywrightTimeout:
                logger.warning(
                    f"Navigation timeout after {time.time() - nav_start:.1f}s "
                    f"(domcontentloaded), proceeding..."
                )

            # Wait for network to settle
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
                logger.info("Network idle reached")
            except PlaywrightTimeout:
                logger.warning("networkidle timeout, proceeding...")

            # Check for premium redirect
            current_url = page.url
            logger.info(f"Current page URL: {current_url}")

            try:
                # Only get a portion of content to save memory
                page_text = page.evaluate("() => document.body ? document.body.innerText.substring(0, 3000) : ''")
            except Exception:
                page_text = ""

            premium_check = _detect_premium(current_url, page_text)
            if premium_check["is_premium"]:
                browser.close()
                return {
                    "success": False,
                    "error": "Premium channel — login/subscription required.",
                    "channel": channel_slug,
                    "reason": premium_check["reason"],
                    "detected_url": current_url,
                    "hint": "This channel requires a Tamasha Pro subscription. Only free channels are supported.",
                }

            # Check if page returned 404 / channel not found
            try:
                title = page.title().lower()
                if "404" in title or "not found" in title or "page not found" in title:
                    browser.close()
                    return {
                        "success": False,
                        "error": f"Channel '{channel_slug}' not found on Tamasha (404).",
                        "channel": channel_slug,
                        "hint": "This channel slug may be incorrect or the channel was removed.",
                    }
            except Exception:
                pass

            # Wait for video element
            video_found = False
            logger.info("Looking for video player...")
            try:
                page.wait_for_selector("video", timeout=15000)
                video_found = True
                logger.info("✓ Video element found in main page")
            except PlaywrightTimeout:
                logger.info("No <video> in main page, checking iframes...")
                try:
                    for frame_element in page.query_selector_all("iframe"):
                        try:
                            frame = frame_element.content_frame()
                            if frame:
                                frame.wait_for_selector("video", timeout=5000)
                                video_found = True
                                logger.info("✓ Video element found inside iframe")
                                break
                        except Exception:
                            continue
                except Exception as e:
                    logger.debug(f"Iframe scan error: {e}")

            if not video_found:
                logger.warning("No video element found anywhere on page")

            # Click play if needed
            try:
                play_selectors = [
                    "button.vjs-big-play-button",
                    ".play-button",
                    ".vjs-play-control.vjs-paused",
                    "button[aria-label='Play']",
                    "button[title='Play']",
                    ".jw-icon-playback",
                    ".bmpui-ui-playbacktogglebutton",
                    "[data-testid='play-button']",
                ]
                for selector in play_selectors:
                    try:
                        el = page.query_selector(selector)
                        if el:
                            visible = el.is_visible()
                            if visible:
                                el.click(timeout=3000)
                                logger.info(f"Clicked play button: {selector}")
                                time.sleep(1)
                                break
                    except Exception:
                        continue
            except Exception as e:
                logger.debug(f"Play button click: {e}")

            # Autoplay via JS
            try:
                page.evaluate("""
                    () => {
                        document.querySelectorAll('video').forEach(v => {
                            v.muted = true;
                            v.play().catch(() => {});
                        });
                    }
                """)
            except Exception:
                pass

            # Wait for HLS requests
            logger.info(f"Waiting {EXTRA_WAIT_SECONDS}s for HLS manifest requests...")
            time.sleep(EXTRA_WAIT_SECONDS)

            # If nothing captured yet, try harder
            if not captured_urls:
                logger.info("No m3u8 captured yet, attempting additional extraction methods...")

                # Method 1: Extract from video.src / currentSrc
                try:
                    js_sources = page.evaluate("""
                        () => {
                            const sources = new Set();
                            document.querySelectorAll('video').forEach(v => {
                                if (v.src) sources.add(v.src);
                                if (v.currentSrc) sources.add(v.currentSrc);
                                v.querySelectorAll('source').forEach(s => {
                                    if (s.src) sources.add(s.src);
                                });
                            });

                            // Check iframes
                            document.querySelectorAll('iframe').forEach(iframe => {
                                try {
                                    const doc = iframe.contentDocument || iframe.contentWindow.document;
                                    if (doc) {
                                        doc.querySelectorAll('video').forEach(v => {
                                            if (v.src) sources.add(v.src);
                                            if (v.currentSrc) sources.add(v.currentSrc);
                                        });
                                    }
                                } catch(e) {}
                            });

                            return Array.from(sources);
                        }
                    """)
                    for src in (js_sources or []):
                        if src and (".m3u8" in src.lower() or "wmsauthsign" in src.lower()):
                            with captured_lock:
                                captured_urls.append({
                                    "url": src,
                                    "status": 200,
                                    "content_type": "application/x-mpegURL",
                                    "timestamp": time.time(),
                                })
                            logger.info(f"  Extracted from video.src: {src[:150]}")
                except Exception as e:
                    logger.debug(f"  video.src extraction: {e}")

                # Method 2: Check player JS objects
                try:
                    player_src = page.evaluate("""
                        () => {
                            // hls.js
                            try {
                                const videos = document.querySelectorAll('video');
                                for (const v of videos) {
                                    // hls.js attaches to video.__hls or video._hls
                                    for (const key of Object.keys(v)) {
                                        if (key.toLowerCase().includes('hls')) {
                                            const hls = v[key];
                                            if (hls && hls.url) return hls.url;
                                            if (hls && hls.config && hls.config.url) return hls.config.url;
                                        }
                                    }
                                }
                            } catch(e) {}

                            // video.js
                            try {
                                if (window.videojs) {
                                    const players = window.videojs.getAllPlayers
                                        ? window.videojs.getAllPlayers()
                                        : Object.values(window.videojs.getPlayers());
                                    for (const p of players) {
                                        if (p && typeof p.currentSrc === 'function') {
                                            const s = p.currentSrc();
                                            if (s) return s;
                                        }
                                    }
                                }
                            } catch(e) {}

                            // jwplayer
                            try {
                                if (window.jwplayer) {
                                    const p = window.jwplayer();
                                    if (p && p.getPlaylistItem) {
                                        const item = p.getPlaylistItem();
                                        if (item && item.file) return item.file;
                                        if (item && item.sources) {
                                            for (const s of item.sources) {
                                                if (s.file) return s.file;
                                            }
                                        }
                                    }
                                }
                            } catch(e) {}

                            // Shaka Player
                            try {
                                if (window.shaka) {
                                    const videos = document.querySelectorAll('video');
                                    for (const v of videos) {
                                        if (v.shakaPlayerInstance) {
                                            const uri = v.shakaPlayerInstance.getAssetUri();
                                            if (uri) return uri;
                                        }
                                    }
                                }
                            } catch(e) {}

                            // Look for source in any global config/state
                            try {
                                const scripts = document.querySelectorAll('script');
                                for (const s of scripts) {
                                    const text = s.textContent || '';
                                    const match = text.match(/["'](https?:\/\/[^"']*\.m3u8[^"']*)/);
                                    if (match) return match[1];
                                }
                            } catch(e) {}

                            return null;
                        }
                    """)
                    if player_src and (".m3u8" in player_src.lower() or "wmsauthsign" in player_src.lower()):
                        with captured_lock:
                            captured_urls.append({
                                "url": player_src,
                                "status": 200,
                                "content_type": "application/x-mpegURL",
                                "timestamp": time.time(),
                            })
                        logger.info(f"  Extracted from player JS: {player_src[:150]}")
                except Exception as e:
                    logger.debug(f"  Player JS extraction: {e}")

                # Method 3: Parse page source for m3u8 URLs via regex
                try:
                    page_source = page.content()
                    m3u8_pattern = re.compile(
                        r'(https?://[^\s"\'<>]*\.m3u8[^\s"\'<>]*)',
                        re.IGNORECASE
                    )
                    matches = m3u8_pattern.findall(page_source)
                    for match in matches:
                        # Unescape common JS escapes
                        clean = match.replace("\\u0026", "&").replace("\\/", "/").replace("\\u003d", "=")
                        with captured_lock:
                            captured_urls.append({
                                "url": clean,
                                "status": 200,
                                "content_type": "text/regex-match",
                                "timestamp": time.time(),
                            })
                        logger.info(f"  Regex match in page source: {clean[:150]}")
                except Exception as e:
                    logger.debug(f"  Regex extraction: {e}")

                # If still nothing, wait a bit more
                if not captured_urls:
                    logger.info("Still nothing, waiting 5 more seconds...")
                    time.sleep(5)

            # Final diagnostic: check for any network errors related to m3u8
            if not captured_urls and errors_seen:
                logger.warning(f"No successful m3u8 but {len(errors_seen)} failed m3u8 requests:")
                for err in errors_seen[:5]:
                    logger.warning(f"  Failed: {err}")

            browser.close()
            logger.info(f"Browser closed. Total captured: {len(captured_urls)}")

    except PlaywrightTimeout as e:
        logger.error(f"Playwright timeout: {e}")
        return {
            "success": False,
            "error": f"Timeout during extraction: {str(e)[:200]}",
            "channel": channel_slug,
            "hint": "Channel page took too long. Try again or check if the channel exists.",
        }
    except Exception as e:
        logger.error(f"Extraction error: {e}", exc_info=True)
        return {
            "success": False,
            "error": f"Browser automation error: {str(e)[:300]}",
            "channel": channel_slug,
            "hint": "Unexpected error. Check server logs.",
        }
    finally:
        _browser_lock.release()

    # ------------------------------------------------------------------
    # Process captured URLs
    # ------------------------------------------------------------------
    if not captured_urls:
        result = {
            "success": False,
            "error": "No m3u8 stream URL captured.",
            "channel": channel_slug,
            "video_element_found": video_found,
            "failed_requests": errors_seen[:3] if errors_seen else [],
            "hint": (
                "Channel may require login or is premium — only use free channels. "
                "If this is a free channel, try again (some streams take longer to load). "
                "Use ?force=1 to bypass cache."
            ),
        }

        # Retry once automatically
        if attempt < MAX_EXTRACTION_RETRIES:
            logger.info(f"Auto-retrying extraction for '{channel_slug}' (attempt {attempt + 1})...")
            time.sleep(2)
            return _extract_stream_url(channel_slug, attempt + 1)

        return result

    # Deduplicate by normalized URL
    seen_normalized = set()
    unique_urls = []
    for entry in captured_urls:
        # Normalize: remove nimblesessionid for dedup, keep everything else
        parsed = urlparse(entry["url"])
        params = parse_qs(parsed.query, keep_blank_values=True)
        params.pop("nimblesessionid", None)
        norm_query = urlencode(params, doseq=True)
        norm_url = urlunparse(parsed._replace(query=norm_query))

        if norm_url not in seen_normalized:
            seen_normalized.add(norm_url)
            unique_urls.append(entry)

    logger.info(f"Unique m3u8 URLs: {len(unique_urls)}")
    for i, entry in enumerate(unique_urls):
        score = _score_m3u8_url(entry["url"])
        logger.info(f"  [{i}] score={score:>4d}  [{entry['status']}]  {entry['url'][:180]}")

    # Select best URL
    best = max(unique_urls, key=lambda e: (_score_m3u8_url(e["url"]), e["timestamp"]))
    best_url = best["url"]
    best_score = _score_m3u8_url(best_url)

    logger.info(f"✓ Selected best URL (score={best_score}): {best_url[:180]}")

    # Cache it
    all_urls_list = [e["url"] for e in sorted(unique_urls, key=lambda e: _score_m3u8_url(e["url"]), reverse=True)]
    _set_cached(channel_slug, best_url, all_urls_list)

    return {
        "success": True,
        "stream_url": best_url,
        "channel": channel_slug,
        "captured_count": len(unique_urls),
        "selected_score": best_score,
        "video_element_found": video_found,
        "note": (
            "Fresh HLS link generated. Estimated expiry ~10-30 min. "
            "Test in VLC, ffplay, or https://hlsjs.video-dev.org/demo/"
        ),
    }


# ==========================================================================
# Flask Routes
# ==========================================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Tamasha Free Channel HLS Stream Extractor",
        "version": "2.0.0",
        "status": "running",
        "endpoints": {
            "GET /": "This documentation",
            "GET /api/health": "Health check",
            "GET /api/channels": "List all supported free channels",
            "GET /api/fresh_stream?channel=<slug>": "Extract fresh HLS stream URL",
            "GET /api/fresh_stream?channel=<slug>&force=1": "Force-refresh (bypass cache)",
            "DELETE /api/cache": "Clear all cached URLs",
        },
        "example": f"{request.host_url}api/fresh_stream?channel=green-entertainment",
        "disclaimer": (
            "For personal/educational use only. Only free/public channels supported. "
            "No DRM bypass, no piracy, no premium content access."
        ),
    })


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "cache_entries": len(_cache),
        "total_channels": len(CHANNEL_SLUGS),
    })


@app.route("/api/channels", methods=["GET"])
def list_channels():
    categories = {
        "news": [],
        "entertainment": [],
        "religious": [],
        "regional": [],
        "other": [],
    }
    for slug in sorted(CHANNEL_SLUGS.keys()):
        s = slug.lower()
        if any(kw in s for kw in ["news", "city-42"]):
            categories["news"].append(slug)
        elif any(kw in s for kw in [
            "entertainment", "digital", "tv-one", "urdu", "play-tv",
            "see-tv", "hum-tv", "a-plus", "zindagi", "kahani", "musik",
        ]):
            categories["entertainment"].append(slug)
        elif any(kw in s for kw in ["madani", "qtv", "paigham"]):
            categories["religious"].append(slug)
        elif any(kw in s for kw in ["khyber", "avt", "sindh", "ktn", "waseb", "mehran", "pashto"]):
            categories["regional"].append(slug)
        else:
            categories["other"].append(slug)

    return jsonify({
        "total_channels": len(CHANNEL_SLUGS),
        "channels_by_category": categories,
        "all_slugs": sorted(CHANNEL_SLUGS.keys()),
        "usage": "GET /api/fresh_stream?channel=<any-slug-from-above>",
    })


@app.route("/api/fresh_stream", methods=["GET"])
def fresh_stream():
    """Main endpoint — extract fresh signed HLS URL for a free channel."""
    channel = request.args.get("channel", "").strip().lower()
    force = request.args.get("force", "0") == "1"

    # Validate input
    if not channel:
        return jsonify({
            "success": False,
            "error": "Missing required query parameter: 'channel'",
            "usage": "/api/fresh_stream?channel=green-entertainment",
            "available_channels": sorted(CHANNEL_SLUGS.keys()),
        }), 400

    if channel not in CHANNEL_SLUGS:
        # Fuzzy suggestions
        suggestions = [
            s for s in CHANNEL_SLUGS
            if channel in s or s in channel or
            any(part in s for part in channel.split("-") if len(part) > 2)
        ]
        return jsonify({
            "success": False,
            "error": f"Unknown channel: '{channel}'",
            "suggestions": sorted(set(suggestions))[:8] if suggestions else None,
            "hint": "See /api/channels for all available slugs.",
        }), 404

    slug = CHANNEL_SLUGS[channel]

    # Check cache
    if not force:
        cached = _get_cached(channel)
        if cached:
            age_s = int((datetime.utcnow() - cached["timestamp"]).total_seconds())
            return jsonify({
                "success": True,
                "stream_url": cached["url"],
                "channel": channel,
                "source": "cache",
                "cache_age_seconds": age_s,
                "alternative_urls": cached.get("all_urls", [])[:3],
                "note": f"Cached URL ({age_s}s old, TTL={CACHE_TTL_SECONDS}s). Use &force=1 for fresh extraction.",
            })

    # Run extraction
    logger.info(f"{'='*60}")
    logger.info(f"EXTRACTION REQUEST: channel='{channel}' slug='{slug}' force={force}")
    logger.info(f"{'='*60}")

    start = time.time()
    result = _extract_stream_url(slug)
    elapsed = round(time.time() - start, 2)
    result["extraction_time_seconds"] = elapsed
    result["channel"] = channel  # Ensure canonical name

    logger.info(f"EXTRACTION COMPLETE: success={result.get('success')} time={elapsed}s")

    status_code = 200 if result.get("success") else 502
    return jsonify(result), status_code


@app.route("/api/cache", methods=["DELETE"])
def clear_cache():
    """Clear cached stream URLs."""
    channel = request.args.get("channel", "").strip().lower()
    if channel:
        _clear_cache(channel)
        return jsonify({"message": f"Cache cleared for '{channel}'"})
    else:
        count = len(_cache)
        _clear_cache()
        return jsonify({"message": f"All cache cleared ({count} entries removed)"})


# Error handlers
@app.errorhandler(404)
def not_found(e):
    return jsonify({
        "error": "Endpoint not found",
        "hint": "Visit / for API documentation",
    }), 404


@app.errorhandler(500)
def server_error(e):
    logger.error(f"500 error: {e}", exc_info=True)
    return jsonify({"error": "Internal server error"}), 500


@app.errorhandler(429)
def too_many_requests(e):
    return jsonify({
        "error": "Too many requests. Please wait between extractions.",
    }), 429


# ==========================================================================
# Entry Point
# ==========================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info(f"Starting Tamasha Stream Extractor v2.0 on port {port}")
    logger.info(f"Channels configured: {len(CHANNEL_SLUGS)}")
    logger.info(f"Cache TTL: {CACHE_TTL_SECONDS}s | Extra wait: {EXTRA_WAIT_SECONDS}s")
    app.run(host="0.0.0.0", port=port, debug=debug)
