"""
Tamasha Free Channel HLS Stream Extractor API
==============================================
Flask + Playwright (sync) API that extracts fresh HLS/m3u8 signed stream URLs
from Tamashaweb.com for FREE (no-login-required) channels only.

For personal/educational testing only.
"""

import os
import re
import time
import logging
import hashlib
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from threading import Lock
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Playwright sync import — we use sync_playwright inside a thread-safe lock
# because each request spawns a full browser context.
# ---------------------------------------------------------------------------
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# App Configuration
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tamasha-extractor")

# ---------------------------------------------------------------------------
# Channel Slug Registry — ONLY confirmed free/no-login channels
# Keys are our canonical names; values are the URL slugs on tamashaweb.com.
# Expand as needed. Channels that require login/Pro should NEVER be added.
# ---------------------------------------------------------------------------
CHANNEL_SLUGS = {
    # News Channels (generally free)
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

    # Entertainment Channels (free tier)
    "green-entertainment": "green-entertainment",
    "geo-entertainment-live": "geo-entertainment-live",
    "ary-digital-live": "ary-digital-live",
    "hum-tv-live": "hum-tv-live",
    "see-tv-live": "see-tv-live",
    "play-tv-live": "play-tv-live",
    "express-entertainment-live": "express-entertainment-live",
    "a-plus-live": "a-plus-live",
    "tv-one-live": "tv-one-live",
    "urdu-1-live": "urdu-1-live",

    # Tamasha Original / Misc
    "tamasha-life-hd": "tamasha-life-hd",

    # Regional / Pashto / Sindhi / Balochi etc. (if free)
    "khyber-news-live": "khyber-news-live",
    "avt-khyber-live": "avt-khyber-live",
    "sindh-tv-news-live": "sindh-tv-news-live",
    "ktn-news-live": "ktn-news-live",
    "waseb-tv-live": "waseb-tv-live",

    # Religious
    "madani-channel-live": "madani-channel-live",
    "qtv-live": "qtv-live",
    "paigham-tv-live": "paigham-tv-live",
    "ary-qtv-live": "ary-qtv-live",

    # Kids / Music
    "ary-zindagi-live": "ary-zindagi-live",
}

# ---------------------------------------------------------------------------
# Simple in-memory cache with TTL (5 minutes)
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 300  # 5 minutes — well within the ~10-30 min token expiry

_cache = {}       # key -> {"url": str, "timestamp": datetime}
_cache_lock = Lock()


def _cache_key(channel: str) -> str:
    return hashlib.md5(channel.encode()).hexdigest()


def _get_cached(channel: str):
    key = _cache_key(channel)
    with _cache_lock:
        entry = _cache.get(key)
        if entry and (datetime.utcnow() - entry["timestamp"]) < timedelta(seconds=CACHE_TTL_SECONDS):
            logger.info(f"Cache HIT for '{channel}' (age: {(datetime.utcnow() - entry['timestamp']).seconds}s)")
            return entry["url"]
        if entry:
            del _cache[key]
    return None


def _set_cached(channel: str, url: str):
    key = _cache_key(channel)
    with _cache_lock:
        _cache[key] = {"url": url, "timestamp": datetime.utcnow()}


# ---------------------------------------------------------------------------
# Premium / Login Detection Patterns
# ---------------------------------------------------------------------------
PREMIUM_INDICATORS = [
    "/plans", "/login", "/subscribe", "/signup", "/otp",
    "login-required", "subscription", "premium",
    "sign-in", "signin", "get-pro", "upgrade",
]


def _is_premium_redirect(page_url: str, page_content: str = "") -> bool:
    """Check if the page redirected to a login/subscription wall."""
    url_lower = page_url.lower()
    for indicator in PREMIUM_INDICATORS:
        if indicator in url_lower:
            return True
    # Check page content for common premium modals
    content_lower = page_content.lower() if page_content else ""
    premium_text_patterns = [
        "please login to continue",
        "subscribe to watch",
        "get tamasha pro",
        "login to watch",
        "sign in to continue",
        "this content is for pro",
        "premium content",
        "enter your otp",
    ]
    for pattern in premium_text_patterns:
        if pattern in content_lower:
            return True
    return False


# ---------------------------------------------------------------------------
# M3U8 URL Scoring — pick the "best" captured URL
# ---------------------------------------------------------------------------
def _score_m3u8_url(url: str) -> int:
    """
    Score an m3u8 URL for quality/completeness. Higher is better.
    We prefer:
      - playlist.m3u8 over master.m3u8 (playlist = actual segments)
      - URLs with wmsAuthSign (signed, ready to use)
      - URLs with more query parameters (more auth info)
      - Longer URLs (typically more complete)
    """
    score = 0
    url_lower = url.lower()

    if "playlist.m3u8" in url_lower:
        score += 100
    elif "chunklist" in url_lower:
        score += 90
    elif "index.m3u8" in url_lower:
        score += 80
    elif "master.m3u8" in url_lower:
        score += 50
    elif ".m3u8" in url_lower:
        score += 40

    if "wmsauthsign" in url_lower:
        score += 200

    if "nimblesessionid" in url_lower:
        score += 30

    # Prefer more query parameters
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    score += len(params) * 10

    # Slight bonus for URL length (more complete)
    score += min(len(url) // 50, 20)

    return score


# ---------------------------------------------------------------------------
# Core Extraction Logic using Playwright
# ---------------------------------------------------------------------------
# Global lock to prevent too many concurrent browser instances
# On a small server (512MB-1GB RAM), running multiple Chromium is dangerous.
_browser_lock = Lock()

# How many seconds to wait after page load for HLS requests to fire
EXTRA_WAIT_SECONDS = int(os.environ.get("EXTRA_WAIT_SECONDS", "10"))

# Navigation timeout
NAV_TIMEOUT_MS = int(os.environ.get("NAV_TIMEOUT_MS", "45000"))

# User agent — modern Chrome on Windows
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def extract_stream_url(channel_slug: str) -> dict:
    """
    Launch headless browser, navigate to the Tamasha channel page,
    intercept network responses to capture the signed m3u8 URL.
    
    Returns dict with either:
      {"success": True, "stream_url": "...", ...}
      {"success": False, "error": "...", ...}
    """
    page_url = f"https://tamashaweb.com/{channel_slug}"
    logger.info(f"Extracting stream for '{channel_slug}' from {page_url}")

    captured_urls = []
    capture_lock = Lock()

    def on_response(response):
        """Callback for every network response — filter for m3u8 / HLS URLs."""
        try:
            resp_url = response.url
            # Check if this response is an HLS-related URL
            if any(pattern in resp_url.lower() for pattern in [
                ".m3u8", "wmsauthsign", "jazzauth", "playlist", "master.m3u8",
                "chunklist", "index.m3u8"
            ]):
                # Only consider successful responses
                status = response.status
                if 200 <= status < 400:
                    with capture_lock:
                        captured_urls.append({
                            "url": resp_url,
                            "status": status,
                            "timestamp": time.time(),
                        })
                    logger.debug(f"  Captured m3u8 URL [{status}]: {resp_url[:120]}...")
        except Exception as e:
            # Don't let callback errors kill the page
            logger.debug(f"  Response callback error: {e}")

    with _browser_lock:
        try:
            with sync_playwright() as pw:
                logger.info("Launching Chromium headless...")
                browser = pw.chromium.launch(
                    headless=True,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                        "--disable-blink-features=AutomationControlled",
                        "--single-process",
                    ],
                )

                context = browser.new_context(
                    user_agent=USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    java_script_enabled=True,
                    bypass_csp=True,
                    locale="en-US",
                    timezone_id="Asia/Karachi",
                    # Pretend we're a real user
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )

                # Anti-bot evasion: mask webdriver
                context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => false });
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5],
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en'],
                    });
                    window.chrome = { runtime: {} };
                    const originalQuery = window.navigator.permissions.query;
                    window.navigator.permissions.query = (parameters) =>
                        parameters.name === 'notifications'
                            ? Promise.resolve({ state: Notification.permission })
                            : originalQuery(parameters);
                """)

                page = context.new_page()

                # Attach response interceptor BEFORE navigation
                page.on("response", on_response)

                logger.info(f"Navigating to {page_url}...")
                try:
                    page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=NAV_TIMEOUT_MS,
                    )
                except PlaywrightTimeout:
                    logger.warning("Navigation timed out (domcontentloaded), continuing anyway...")

                # Wait for network to settle
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PlaywrightTimeout:
                    logger.warning("networkidle timeout, continuing...")

                # Check for premium redirect
                current_url = page.url
                logger.info(f"Page landed at: {current_url}")

                try:
                    page_content = page.content()
                except Exception:
                    page_content = ""

                if _is_premium_redirect(current_url, page_content):
                    browser.close()
                    return {
                        "success": False,
                        "error": "Premium channel — login/subscription required.",
                        "channel": channel_slug,
                        "hint": "This channel is not free. Only use confirmed free channels.",
                        "detected_url": current_url,
                    }

                # Try to find and interact with the video player
                logger.info("Waiting for video element...")
                video_found = False
                try:
                    page.wait_for_selector("video", timeout=15000)
                    video_found = True
                    logger.info("Video element found!")

                    # Try clicking play button if video is paused
                    try:
                        # Common play button selectors on Tamasha
                        play_selectors = [
                            "button.vjs-big-play-button",
                            ".play-button",
                            ".vjs-play-control",
                            "button[aria-label='Play']",
                            ".jw-icon-playback",
                            "video",  # Clicking the video itself sometimes starts playback
                        ]
                        for selector in play_selectors:
                            try:
                                el = page.query_selector(selector)
                                if el and el.is_visible():
                                    el.click()
                                    logger.info(f"Clicked play element: {selector}")
                                    break
                            except Exception:
                                continue
                    except Exception as e:
                        logger.debug(f"Play button interaction: {e}")

                except PlaywrightTimeout:
                    logger.warning("No <video> element found within 15s — page might use iframe or canvas player.")

                    # Check for iframes that might contain the player
                    try:
                        iframes = page.query_selector_all("iframe")
                        for iframe in iframes:
                            src = iframe.get_attribute("src") or ""
                            if any(kw in src.lower() for kw in ["player", "embed", "stream", "video", "live"]):
                                logger.info(f"Found potential player iframe: {src[:100]}")
                                # Navigate into the iframe context
                                try:
                                    frame = iframe.content_frame()
                                    if frame:
                                        frame.wait_for_selector("video", timeout=10000)
                                        video_found = True
                                        logger.info("Video found inside iframe!")
                                        break
                                except Exception:
                                    continue
                    except Exception as e:
                        logger.debug(f"Iframe scan: {e}")

                # Extra wait for HLS playlist requests to fire
                wait_time = EXTRA_WAIT_SECONDS
                logger.info(f"Waiting {wait_time}s for HLS requests to fire...")
                time.sleep(wait_time)

                # If still no captures, try scrolling / clicking to trigger lazy load
                if not captured_urls:
                    logger.info("No m3u8 captured yet, trying additional triggers...")
                    try:
                        # Scroll to video
                        page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
                        time.sleep(2)

                        # Try autoplay via JS
                        page.evaluate("""
                            () => {
                                const videos = document.querySelectorAll('video');
                                videos.forEach(v => {
                                    v.muted = true;
                                    v.play().catch(() => {});
                                });
                            }
                        """)
                        time.sleep(5)
                    except Exception as e:
                        logger.debug(f"Additional trigger error: {e}")

                # Also try to extract the source directly from the video element
                try:
                    video_src = page.evaluate("""
                        () => {
                            const videos = document.querySelectorAll('video');
                            const sources = [];
                            videos.forEach(v => {
                                if (v.src) sources.push(v.src);
                                if (v.currentSrc) sources.push(v.currentSrc);
                                v.querySelectorAll('source').forEach(s => {
                                    if (s.src) sources.push(s.src);
                                });
                            });
                            return sources;
                        }
                    """)
                    if video_src:
                        for src in video_src:
                            if ".m3u8" in src or "wmsauthsign" in src.lower():
                                with capture_lock:
                                    captured_urls.append({
                                        "url": src,
                                        "status": 200,
                                        "timestamp": time.time(),
                                    })
                                logger.info(f"Extracted video src: {src[:120]}...")
                except Exception as e:
                    logger.debug(f"Video src extraction: {e}")

                # Also check for HLS source in player JS objects
                try:
                    hls_src = page.evaluate("""
                        () => {
                            // Check hls.js instance
                            if (window.Hls) {
                                const videos = document.querySelectorAll('video');
                                for (const v of videos) {
                                    if (v._hls && v._hls.url) return v._hls.url;
                                }
                            }
                            // Check videojs
                            if (window.videojs) {
                                const players = window.videojs.getAllPlayers();
                                for (const p of players) {
                                    const src = p.currentSrc();
                                    if (src) return src;
                                }
                            }
                            // Check jwplayer
                            if (window.jwplayer) {
                                try {
                                    const p = window.jwplayer();
                                    if (p && p.getPlaylistItem) {
                                        const item = p.getPlaylistItem();
                                        if (item && item.file) return item.file;
                                    }
                                } catch(e) {}
                            }
                            return null;
                        }
                    """)
                    if hls_src and (".m3u8" in hls_src or "wmsauthsign" in hls_src.lower()):
                        with capture_lock:
                            captured_urls.append({
                                "url": hls_src,
                                "status": 200,
                                "timestamp": time.time(),
                            })
                        logger.info(f"Extracted HLS source from player JS: {hls_src[:120]}...")
                except Exception as e:
                    logger.debug(f"Player JS extraction: {e}")

                browser.close()
                logger.info(f"Browser closed. Total captured URLs: {len(captured_urls)}")

        except PlaywrightTimeout as e:
            logger.error(f"Playwright timeout: {e}")
            return {
                "success": False,
                "error": f"Timeout while loading channel page: {str(e)}",
                "channel": channel_slug,
                "hint": "The channel page took too long to load. Try again.",
            }
        except Exception as e:
            logger.error(f"Playwright error: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"Browser automation error: {str(e)}",
                "channel": channel_slug,
                "hint": "Internal error during stream extraction.",
            }

    # -----------------------------------------------------------------------
    # Process captured URLs
    # -----------------------------------------------------------------------
    if not captured_urls:
        return {
            "success": False,
            "error": "No m3u8 stream URL captured.",
            "channel": channel_slug,
            "hint": (
                "Channel may require login or is premium — only use free ones. "
                "If this is a free channel, the page structure may have changed."
            ),
            "video_element_found": video_found,
        }

    # Deduplicate
    seen = set()
    unique_urls = []
    for entry in captured_urls:
        # Normalize for dedup (ignore trivial differences)
        norm = entry["url"].split("&nimblesessionid=")[0] if "&nimblesessionid=" in entry["url"] else entry["url"]
        if norm not in seen:
            seen.add(norm)
            unique_urls.append(entry)

    logger.info(f"Unique m3u8 URLs captured: {len(unique_urls)}")
    for i, entry in enumerate(unique_urls):
        score = _score_m3u8_url(entry["url"])
        logger.info(f"  [{i}] score={score} status={entry['status']} url={entry['url'][:150]}...")

    # Pick the best URL by score, then by recency
    best = max(unique_urls, key=lambda e: (_score_m3u8_url(e["url"]), e["timestamp"]))
    best_url = best["url"]

    logger.info(f"Selected best URL (score={_score_m3u8_url(best_url)}): {best_url[:150]}...")

    # Cache it
    _set_cached(channel_slug, best_url)

    return {
        "success": True,
        "stream_url": best_url,
        "channel": channel_slug,
        "captured_count": len(unique_urls),
        "selected_score": _score_m3u8_url(best_url),
        "note": "Fresh link generated. Estimated expiry 10-30 min. Test in any HLS player (VLC, ffplay, hls.js demo).",
    }


# ---------------------------------------------------------------------------
# Flask Routes
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    """API documentation / health check."""
    return jsonify({
        "service": "Tamasha Free Channel HLS Stream Extractor",
        "version": "1.0.0",
        "status": "running",
        "usage": {
            "endpoint": "GET /api/fresh_stream?channel=<channel-slug>",
            "example": "/api/fresh_stream?channel=green-entertainment",
        },
        "available_channels": "GET /api/channels",
        "disclaimer": (
            "For personal/educational testing only. "
            "Only free/public channels (no login required) are supported. "
            "No DRM bypass, no piracy."
        ),
    })


@app.route("/api/channels", methods=["GET"])
def list_channels():
    """List all known free channel slugs."""
    # Group them by category for readability
    categories = {
        "news": [],
        "entertainment": [],
        "religious": [],
        "regional": [],
        "other": [],
    }
    for slug in sorted(CHANNEL_SLUGS.keys()):
        slug_lower = slug.lower()
        if "news" in slug_lower or "city-42" in slug_lower:
            categories["news"].append(slug)
        elif any(kw in slug_lower for kw in ["entertainment", "digital", "tv-one", "urdu", "play-tv", "see-tv", "hum-tv", "a-plus", "zindagi"]):
            categories["entertainment"].append(slug)
        elif any(kw in slug_lower for kw in ["madani", "qtv", "paigham", "ary-qtv"]):
            categories["religious"].append(slug)
        elif any(kw in slug_lower for kw in ["khyber", "avt", "sindh", "ktn", "waseb"]):
            categories["regional"].append(slug)
        else:
            categories["other"].append(slug)

    return jsonify({
        "total_channels": len(CHANNEL_SLUGS),
        "channels_by_category": categories,
        "all_slugs": sorted(CHANNEL_SLUGS.keys()),
        "note": "All listed channels are believed to be free/public. If one requires login, it will return an error.",
    })


@app.route("/api/fresh_stream", methods=["GET"])
def fresh_stream():
    """
    Main endpoint: Extract a fresh signed HLS stream URL for a free Tamasha channel.
    
    Query params:
        channel (required): Channel slug, e.g. "green-entertainment", "ary-news"
        force   (optional): Set to "1" to bypass cache
    
    Returns JSON with stream_url or error.
    """
    channel = request.args.get("channel", "").strip().lower()
    force_refresh = request.args.get("force", "0") == "1"

    if not channel:
        return jsonify({
            "success": False,
            "error": "Missing 'channel' query parameter.",
            "hint": "Use ?channel=green-entertainment",
            "available_channels": sorted(CHANNEL_SLUGS.keys()),
        }), 400

    # Check if channel is in our registry
    if channel not in CHANNEL_SLUGS:
        # Try fuzzy match
        close_matches = [s for s in CHANNEL_SLUGS if channel in s or s in channel]
        return jsonify({
            "success": False,
            "error": f"Unknown channel slug: '{channel}'",
            "hint": "Check /api/channels for available slugs.",
            "close_matches": close_matches[:5] if close_matches else None,
            "available_channels": sorted(CHANNEL_SLUGS.keys()),
        }), 404

    slug = CHANNEL_SLUGS[channel]

    # Check cache first (unless force refresh)
    if not force_refresh:
        cached = _get_cached(channel)
        if cached:
            return jsonify({
                "success": True,
                "stream_url": cached,
                "channel": channel,
                "source": "cache",
                "note": "Cached URL (< 5 min old). Use ?force=1 for fresh extraction.",
            })

    # Extract fresh URL
    logger.info(f"=== Starting fresh extraction for '{channel}' (slug: {slug}) ===")
    start_time = time.time()
    result = extract_stream_url(slug)
    elapsed = round(time.time() - start_time, 2)

    result["extraction_time_seconds"] = elapsed
    logger.info(f"=== Extraction complete for '{channel}' in {elapsed}s — success={result.get('success')} ===")

    if result.get("success"):
        return jsonify(result)
    else:
        return jsonify(result), 502


@app.route("/api/health", methods=["GET"])
def health():
    """Health check endpoint for deployment monitoring."""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "cache_entries": len(_cache),
    })


# ---------------------------------------------------------------------------
# Error Handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found", "hint": "Try GET / for API docs"}), 404


@app.errorhandler(500)
def internal_error(e):
    logger.error(f"Internal server error: {e}", exc_info=True)
    return jsonify({"error": "Internal server error", "details": str(e)}), 500


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    logger.info(f"Starting Tamasha Stream Extractor API on port {port} (debug={debug})")
    app.run(host="0.0.0.0", port=port, debug=debug)
