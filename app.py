"""
Tamasha Free Channel HLS Stream Extractor API — v2.3
=====================================================
Flask + Playwright API for extracting free HLS streams from Tamashaweb.com.
Hardened for Render.com: timeout recovery, stuck-process cleanup, debug endpoint.
For personal/educational use only.
"""

import os
import re
import sys
import time
import signal
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from threading import Lock, Timer
from flask import Flask, jsonify, request

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ══════════════════════════════════════════════════════════════════════════
# App Setup
# ══════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("tamasha")

# ══════════════════════════════════════════════════════════════════════════
# Config
# ══════════════════════════════════════════════════════════════════════════
TAMASHA = os.environ.get("TAMASHA_BASE_URL", "https://tamashaweb.com")
EXTRA_WAIT = int(os.environ.get("EXTRA_WAIT_SECONDS", "10"))
NAV_TIMEOUT = int(os.environ.get("NAV_TIMEOUT_MS", "35000"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
# Maximum time for entire extraction before force-killing browser
MAX_EXTRACTION_SECONDS = int(os.environ.get("MAX_EXTRACTION_SECONDS", "75"))

# ══════════════════════════════════════════════════════════════════════════
# Free Channel Registry
# ══════════════════════════════════════════════════════════════════════════
CHANNELS = {
    # News
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
    # Entertainment
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
    # Other
    "tamasha-life-hd": "tamasha-life-hd",
    # Regional
    "khyber-news-live": "khyber-news-live",
    "avt-khyber-live": "avt-khyber-live",
    "sindh-tv-news-live": "sindh-tv-news-live",
    "ktn-news-live": "ktn-news-live",
    "waseb-tv-live": "waseb-tv-live",
    "mehran-tv-live": "mehran-tv-live",
    # Religious
    "madani-channel-live": "madani-channel-live",
    "qtv-live": "qtv-live",
    "paigham-tv-live": "paigham-tv-live",
    "ary-qtv-live": "ary-qtv-live",
    # Music
    "ary-musik-live": "ary-musik-live",
}

# ══════════════════════════════════════════════════════════════════════════
# Constants
# ══════════════════════════════════════════════════════════════════════════
CHROME_ARGS = [
    "--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage",
    "--disable-gpu", "--disable-software-rasterizer", "--disable-extensions",
    "--disable-background-networking", "--disable-default-apps",
    "--disable-sync", "--disable-translate",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run", "--no-default-browser-check",
    "--single-process", "--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-hang-monitor", "--disable-component-update",
]

STEALTH_JS = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'plugins',{get:()=>[{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}]});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
window.chrome={runtime:{},loadTimes:()=>{},csi:()=>{},app:{}};
"""

UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]
_ua_i = 0
def _ua():
    global _ua_i
    u = UA_LIST[_ua_i % len(UA_LIST)]
    _ua_i += 1
    return u

HLS_MARKERS = [".m3u8", "wmsauthsign", "playlist.m3u8", "master.m3u8",
               "chunklist", "index.m3u8", "manifest", "jazzauth"]

def _is_hls(url):
    ul = url.lower()
    return any(m in ul for m in HLS_MARKERS)

PREMIUM_URL_KW = ["/plans","/login","/subscribe","/signup","/otp","/get-pro","/signin","/auth","/verify"]
PREMIUM_TEXT_KW = ["please login","subscribe to watch","get tamasha pro","login to watch",
                   "premium content","enter your otp","enter your phone","subscription required",
                   "enter mobile number","jazz/warid number","verify your number","create account"]

def _is_premium(url, text=""):
    ul = url.lower()
    for k in PREMIUM_URL_KW:
        if k in ul: return True, f"URL→'{k}'"
    tl = (text or "").lower()
    for k in PREMIUM_TEXT_KW:
        if k in tl: return True, f"Page→'{k}'"
    return False, None

def _score(url):
    s, ul = 0, url.lower()
    if "playlist.m3u8" in ul: s += 100
    elif "chunklist" in ul: s += 95
    elif "index.m3u8" in ul: s += 80
    elif "master.m3u8" in ul: s += 50
    elif ".m3u8" in ul: s += 40
    if "wmsauthsign" in ul: s += 200
    elif "hdnts=" in ul: s += 150
    elif "token=" in ul: s += 100
    if "nimblesessionid" in ul: s += 30
    if ul.startswith("https://"): s += 10
    for ad in ["doubleclick","googlesyndication","adserver"]:
        if ad in ul: s -= 500
    s += len(parse_qs(urlparse(url).query)) * 8
    return s

BLOCKED_DOMAINS = [
    "google-analytics.com","googletagmanager.com","facebook.net","facebook.com",
    "fbcdn.net","doubleclick.net","googlesyndication.com","googleadservices.com",
    "hotjar.com","clarity.ms","sentry.io","segment.io","mixpanel.com","amplitude.com",
]

# ══════════════════════════════════════════════════════════════════════════
# Cache
# ══════════════════════════════════════════════════════════════════════════
_cache = {}
_cL = Lock()

def _cget(ch):
    with _cL:
        e = _cache.get(ch)
        if not e: return None
        if (datetime.utcnow()-e["ts"]).total_seconds() < CACHE_TTL: return e
        del _cache[ch]
        return None

def _cset(ch, url, alts=None):
    with _cL:
        _cache[ch] = {"url": url, "alts": alts or [], "ts": datetime.utcnow()}

def _cdel(ch=None):
    with _cL:
        if ch: _cache.pop(ch, None)
        else: _cache.clear()

# ══════════════════════════════════════════════════════════════════════════
# Browser Lock — with stuck-process recovery
# ══════════════════════════════════════════════════════════════════════════
_bL = Lock()
_active_browser = None  # Reference to active browser for force-kill


def _force_kill_browser():
    """Emergency kill if browser hangs past timeout."""
    global _active_browser
    if _active_browser:
        log.warning("⚠ FORCE-KILLING hung browser!")
        try:
            _active_browser.close()
        except Exception:
            pass
        _active_browser = None


# ══════════════════════════════════════════════════════════════════════════
# Core Extraction
# ══════════════════════════════════════════════════════════════════════════
def _extract(slug):
    global _active_browser

    log.info(f"▶ Extracting: {slug}")
    captured = []
    cL = Lock()
    failed = []
    video_found = False

    def on_resp(resp):
        try:
            u = resp.url
            if _is_hls(u) and 200 <= resp.status < 400:
                with cL:
                    captured.append({"url": u, "status": resp.status, "t": time.time()})
                log.info(f"  ✓ HLS [{resp.status}]: {u[:180]}")
        except Exception:
            pass

    def on_fail(req):
        try:
            if ".m3u8" in req.url.lower():
                failed.append({"url": req.url[:150], "err": req.failure})
        except Exception:
            pass

    # Acquire lock with short timeout
    acquired = _bL.acquire(timeout=30)
    if not acquired:
        return {"success": False, "error": "Server busy — try again in 30s."}

    # Set a watchdog timer — force-kills browser if extraction hangs
    watchdog = Timer(MAX_EXTRACTION_SECONDS, _force_kill_browser)
    watchdog.daemon = True
    watchdog.start()

    browser = None
    try:
        with sync_playwright() as pw:
            t0 = time.time()
            browser = pw.chromium.launch(headless=True, args=CHROME_ARGS)
            _active_browser = browser
            log.info(f"  Chromium up ({time.time()-t0:.1f}s)")

            ctx = browser.new_context(
                user_agent=_ua(),
                viewport={"width": 1366, "height": 768},
                java_script_enabled=True,
                bypass_csp=True,
                locale="en-US",
                timezone_id="Asia/Karachi",
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9,ur;q=0.8",
                    "Sec-CH-UA": '"Chromium";v="122"',
                    "Sec-CH-UA-Mobile": "?0",
                    "Sec-CH-UA-Platform": '"Windows"',
                },
            )
            ctx.add_init_script(STEALTH_JS)

            # Block heavy resources
            def route_h(route):
                rt = route.request.resource_type
                ru = route.request.url.lower()
                if rt in {"image", "font", "stylesheet", "media"}:
                    if ".m3u8" not in ru and ".ts" not in ru:
                        route.abort()
                        return
                for d in BLOCKED_DOMAINS:
                    if d in ru:
                        route.abort()
                        return
                route.continue_()
            ctx.route("**/*", route_h)

            page = ctx.new_page()
            page.on("response", on_resp)
            page.on("requestfailed", on_fail)

            # ── Navigate ──
            target = f"{TAMASHA}/{slug}"
            log.info(f"  Nav: {target}")

            try:
                page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
            except PlaywrightTimeout:
                log.warning("  domcontentloaded timeout — continuing")

            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeout:
                pass

            # ── Check final URL ──
            cur = page.url
            log.info(f"  Landed: {cur}")

            # If URL is completely different (redirected away), check other patterns
            if slug not in cur.lower() and not captured:
                alt_patterns = [f"{TAMASHA}/watch/{slug}", f"{TAMASHA}/live/{slug}"]
                for alt in alt_patterns:
                    log.info(f"  Trying alt: {alt}")
                    try:
                        page.goto(alt, wait_until="domcontentloaded", timeout=20000)
                        cur = page.url
                        if slug in cur.lower() or "404" not in page.title().lower():
                            log.info(f"  Alt worked: {cur}")
                            try:
                                page.wait_for_load_state("networkidle", timeout=10000)
                            except PlaywrightTimeout:
                                pass
                            break
                    except Exception:
                        continue

            # ── Premium check ──
            try:
                body = page.evaluate("()=>document.body?document.body.innerText.substring(0,3000):''")
            except Exception:
                body = ""

            prem, reason = _is_premium(page.url, body)
            if prem:
                browser.close()
                _active_browser = None
                return {"success": False, "error": "Premium — login required.", "reason": reason, "channel": slug}

            # ── Find video ──
            for sel in ["video", ".video-js video", ".jw-video", "video[src]"]:
                try:
                    page.wait_for_selector(sel, timeout=6000)
                    video_found = True
                    log.info(f"  ✓ Video: {sel}")
                    break
                except PlaywrightTimeout:
                    continue

            if not video_found:
                # Check iframes
                try:
                    for f in page.query_selector_all("iframe"):
                        try:
                            frame = f.content_frame()
                            if frame:
                                frame.wait_for_selector("video", timeout=4000)
                                video_found = True
                                log.info("  ✓ Video in iframe")
                                break
                        except Exception:
                            continue
                except Exception:
                    pass

            # ── Dismiss overlays ──
            for sel in ["button[class*='accept']", "button[class*='close']",
                        "[class*='cookie'] button", ".modal-close", "button[aria-label='Close']"]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=1500)
                        time.sleep(0.3)
                        break
                except Exception:
                    continue

            # ── Click play ──
            for sel in ["button.vjs-big-play-button", ".play-button",
                        "button[aria-label='Play']", "button[title='Play']",
                        ".jw-icon-playback", ".vjs-play-control", "video"]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=1500)
                        log.info(f"  ▶ Clicked: {sel}")
                        time.sleep(0.5)
                        break
                except Exception:
                    continue

            # ── Autoplay JS ──
            try:
                page.evaluate("""()=>{
                    document.querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{})});
                    document.querySelectorAll('iframe').forEach(f=>{
                        try{(f.contentDocument||f.contentWindow.document).querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{})});}catch(e){}
                    });
                }""")
            except Exception:
                pass

            # ── Wait for HLS ──
            log.info(f"  Waiting {EXTRA_WAIT}s for HLS...")
            time.sleep(EXTRA_WAIT)

            # ── Deep extraction if needed ──
            if not captured:
                log.info("  Deep extraction...")

                # A: video.src / currentSrc
                try:
                    srcs = page.evaluate("""()=>{
                        const s=new Set();
                        document.querySelectorAll('video').forEach(v=>{
                            if(v.src)s.add(v.src);if(v.currentSrc)s.add(v.currentSrc);
                            v.querySelectorAll('source').forEach(x=>{if(x.src)s.add(x.src)});
                        });
                        document.querySelectorAll('iframe').forEach(f=>{
                            try{const d=f.contentDocument||f.contentWindow.document;
                            d.querySelectorAll('video').forEach(v=>{if(v.src)s.add(v.src);if(v.currentSrc)s.add(v.currentSrc)});}catch(e){}
                        });
                        return[...s];
                    }""")
                    for src in (srcs or []):
                        if src and _is_hls(src):
                            with cL: captured.append({"url": src, "status": 200, "t": time.time()})
                            log.info(f"  ✓ src: {src[:160]}")
                except Exception:
                    pass

                # B: Player objects
                try:
                    ps = page.evaluate("""()=>{
                        const u=[];
                        try{document.querySelectorAll('video').forEach(v=>{
                            for(const k of Object.keys(v)){const o=v[k];
                            if(o&&typeof o==='object'){
                                if(o.url&&typeof o.url==='string')u.push(o.url);
                                if(o.levels)o.levels.forEach(l=>{if(l.url)u.push(l.url);if(l.uri)u.push(l.uri)});
                            }}});
                        }catch(e){}
                        try{if(window.videojs){
                            const p=window.videojs.getAllPlayers?window.videojs.getAllPlayers():Object.values(window.videojs.getPlayers());
                            p.forEach(x=>{try{u.push(x.currentSrc())}catch(e){}});
                        }}catch(e){}
                        try{if(window.jwplayer){const p=window.jwplayer();
                            if(p&&p.getPlaylistItem){const i=p.getPlaylistItem();
                            if(i&&i.file)u.push(i.file);if(i&&i.sources)i.sources.forEach(s=>{if(s.file)u.push(s.file)});}
                        }}catch(e){}
                        try{if(typeof Hls!=='undefined'&&Hls.DefaultConfig){
                            document.querySelectorAll('video').forEach(v=>{
                                for(const k of Object.getOwnPropertyNames(v)){
                                    try{const h=v[k];if(h&&h.constructor&&h.constructor.name==='Hls'&&h.url)u.push(h.url)}catch(e){}
                                }});
                        }}catch(e){}
                        return u.filter(x=>x&&typeof x==='string');
                    }""")
                    for src in (ps or []):
                        if _is_hls(src):
                            with cL: captured.append({"url": src, "status": 200, "t": time.time()})
                            log.info(f"  ✓ JS: {src[:160]}")
                except Exception:
                    pass

                # C: Regex in page HTML
                try:
                    html = page.content()
                    for m in re.findall(r'(https?://[^\s"\'<>\\]*\.m3u8[^\s"\'<>\\]*)', html, re.I):
                        c = m.replace("\\u0026","&").replace("\\/","/").replace("\\u003d","=").replace("&amp;","&")
                        with cL: captured.append({"url": c, "status": 200, "t": time.time()})
                        log.info(f"  ✓ Regex: {c[:160]}")
                except Exception:
                    pass

                # D: Data attributes
                try:
                    da = page.evaluate("""()=>{
                        const u=[];
                        document.querySelectorAll('[data-src],[data-url],[data-stream],[data-video-url],[data-hls],[data-manifest]').forEach(el=>{
                            ['data-src','data-url','data-stream','data-video-url','data-hls','data-manifest'].forEach(a=>{
                                const v=el.getAttribute(a);if(v)u.push(v);
                            });
                        });
                        return u;
                    }""")
                    for src in (da or []):
                        if _is_hls(src):
                            with cL: captured.append({"url": src, "status": 200, "t": time.time()})
                except Exception:
                    pass

                # E: Last wait
                if not captured:
                    log.info("  Final 4s wait...")
                    time.sleep(4)

            browser.close()
            _active_browser = None
            browser = None
            log.info(f"  Done. Captured: {len(captured)}")

    except Exception as e:
        log.error(f"Extraction error: {e}", exc_info=True)
        return {"success": False, "error": f"Error: {str(e)[:300]}", "channel": slug}
    finally:
        watchdog.cancel()
        if browser:
            try: browser.close()
            except Exception: pass
        _active_browser = None
        try: _bL.release()
        except RuntimeError: pass

    # ── Process results ──
    if not captured:
        return {
            "success": False,
            "error": "No m3u8 stream URL captured.",
            "channel": slug,
            "video_found": video_found,
            "failed_requests": failed[:5],
            "hint": "Channel may need login, or player uses blob: URLs. Try /api/debug_channel?channel=... for diagnostics.",
        }

    # Dedup
    seen = set()
    uniq = []
    for e in captured:
        k = e["url"].split("&nimblesessionid=")[0] if "&nimblesessionid=" in e["url"] else e["url"]
        if k not in seen:
            seen.add(k)
            uniq.append(e)

    best = max(uniq, key=lambda e: (_score(e["url"]), e["t"]))
    url = best["url"]
    sc = _score(url)
    log.info(f"  ★ Best (score={sc}): {url[:180]}")

    alts = [e["url"] for e in sorted(uniq, key=lambda e: _score(e["url"]), reverse=True)]
    _cset(slug, url, alts)

    return {
        "success": True,
        "stream_url": url,
        "channel": slug,
        "captured_count": len(uniq),
        "score": sc,
        "video_found": video_found,
        "alternative_urls": alts[1:4] if len(alts) > 1 else [],
        "note": "Fresh HLS link. Expiry ~10-30min. Play in VLC or hlsjs.video-dev.org/demo/",
    }


# ══════════════════════════════════════════════════════════════════════════
# Debug Endpoint — lightweight diagnostics
# ══════════════════════════════════════════════════════════════════════════
def _debug_extract(slug):
    """Lightweight page analysis — faster than full extraction."""
    log.info(f"🔍 Debug: {slug}")

    responses = []
    rL = Lock()

    def on_resp(resp):
        try:
            with rL:
                responses.append({"url": resp.url[:300], "status": resp.status, "type": resp.request.resource_type})
        except Exception:
            pass

    acquired = _bL.acquire(timeout=20)
    if not acquired:
        return {"error": "Server busy — retry in 20s."}

    watchdog = Timer(60, _force_kill_browser)
    watchdog.daemon = True
    watchdog.start()

    global _active_browser
    browser = None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=CHROME_ARGS)
            _active_browser = browser

            ctx = browser.new_context(user_agent=_ua(), viewport={"width":1366,"height":768},
                                       java_script_enabled=True, bypass_csp=True)
            page = ctx.new_page()
            page.on("response", on_resp)

            target = f"{TAMASHA}/{slug}"
            nav_status = None
            try:
                r = page.goto(target, wait_until="domcontentloaded", timeout=25000)
                nav_status = r.status if r else None
            except PlaywrightTimeout:
                nav_status = "TIMEOUT"

            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass

            # Click play + autoplay
            for sel in ["button.vjs-big-play-button",".play-button","button[aria-label='Play']","video"]:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click(timeout=1500)
                        break
                except Exception:
                    continue
            try:
                page.evaluate("()=>{document.querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{})})}")
            except Exception:
                pass

            time.sleep(8)

            cur = page.url
            title = ""
            try: title = page.title()
            except Exception: pass

            # Video elements
            vinfo = []
            try:
                vinfo = page.evaluate("""()=>{
                    const r=[];
                    document.querySelectorAll('video').forEach((v,i)=>{
                        r.push({i:i,src:v.src||null,currentSrc:v.currentSrc||null,
                            paused:v.paused,readyState:v.readyState,networkState:v.networkState,
                            duration:v.duration,id:v.id||null,className:v.className||null,
                            sources:Array.from(v.querySelectorAll('source')).map(s=>({src:s.src,type:s.type}))
                        });
                    });
                    return r;
                }""")
            except Exception as e:
                vinfo = [{"error": str(e)}]

            # Iframes
            iinfo = []
            try:
                iinfo = page.evaluate("""()=>Array.from(document.querySelectorAll('iframe')).slice(0,5).map((f,i)=>({
                    i:i,src:f.src||null,id:f.id||null,cls:f.className||null
                }))""")
            except Exception:
                pass

            # Player libs
            plibs = {}
            try:
                plibs = page.evaluate("""()=>({
                    hls_js:typeof Hls!=='undefined',
                    video_js:typeof videojs!=='undefined',
                    jwplayer:typeof jwplayer!=='undefined',
                    shaka:typeof shaka!=='undefined',
                    dashjs:typeof dashjs!=='undefined',
                    bitmovin:typeof bitmovin!=='undefined',
                    clappr:typeof Clappr!=='undefined',
                    plyr:typeof Plyr!=='undefined',
                    flowplayer:typeof flowplayer!=='undefined',
                })""")
            except Exception as e:
                plibs = {"error": str(e)}

            # Body text
            body = ""
            try:
                body = page.evaluate("()=>document.body?document.body.innerText.substring(0,2000):''")
            except Exception:
                pass

            # HLS-related responses
            hls_r = [r for r in responses if any(m in r["url"].lower() for m in
                     [".m3u8","wmsauth","playlist","manifest","hls","stream","live"])]

            # XHR/fetch
            xhr = [r for r in responses if r["type"] in ("fetch","xhr")]

            # m3u8 in source
            m3u8s = []
            try:
                html = page.content()
                for m in re.findall(r'(https?://[^\s"\'<>\\]*\.m3u8[^\s"\'<>\\]*)', html, re.I):
                    m3u8s.append(m.replace("\\u0026","&").replace("\\/","/")[:300])
            except Exception:
                pass

            # Next.js data / __NEXT_DATA__
            next_data_keys = []
            try:
                nd = page.evaluate("""()=>{
                    const el=document.getElementById('__NEXT_DATA__');
                    if(!el)return null;
                    try{const d=JSON.parse(el.textContent);return Object.keys(d.props||{}).concat(Object.keys(d.props?.pageProps||{}));}catch(e){return'parse_error'}
                }""")
                next_data_keys = nd
            except Exception:
                pass

            # Check for specific Tamasha player patterns
            tamasha_player = {}
            try:
                tamasha_player = page.evaluate("""()=>{
                    const r={};
                    // Check for any global with 'player' or 'stream' in name
                    for(const k of Object.keys(window)){
                        const kl=k.toLowerCase();
                        if(kl.includes('player')||kl.includes('stream')||kl.includes('hls')){
                            r[k]=typeof window[k];
                        }
                    }
                    // Check for React fiber
                    const vids=document.querySelectorAll('video');
                    vids.forEach((v,i)=>{
                        for(const k of Object.keys(v)){
                            if(k.startsWith('__react')){
                                r['video_'+i+'_react']=k;
                                break;
                            }
                        }
                    });
                    return r;
                }""")
            except Exception:
                pass

            prem, preason = _is_premium(cur, body)

            browser.close()
            _active_browser = None

            return {
                "channel": slug,
                "target": target,
                "final_url": cur,
                "nav_status": nav_status,
                "title": title,
                "videos": vinfo,
                "iframes": iinfo,
                "player_libs": plibs,
                "tamasha_player_globals": tamasha_player,
                "next_data_keys": next_data_keys,
                "hls_responses": hls_r[:20],
                "xhr_responses": xhr[:30],
                "m3u8_in_source": m3u8s[:10],
                "total_responses": len(responses),
                "body_preview": body[:800],
                "premium": {"detected": prem, "reason": preason},
            }

    except Exception as e:
        return {"error": str(e)}
    finally:
        watchdog.cancel()
        if browser:
            try: browser.close()
            except Exception: pass
        _active_browser = None
        try: _bL.release()
        except RuntimeError: pass


# ══════════════════════════════════════════════════════════════════════════
# Flask Routes
# ══════════════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    b = request.host_url.rstrip("/")
    return jsonify({
        "service": "Tamasha Free Channel HLS Extractor",
        "version": "2.3.0",
        "status": "running",
        "endpoints": {
            "GET /": "Docs",
            "GET /api/health": "Health",
            "GET /api/channels": "List channels",
            "GET /api/fresh_stream?channel=SLUG": "Extract stream",
            "GET /api/fresh_stream?channel=SLUG&force=1": "Force refresh",
            "GET /api/debug_channel?channel=SLUG": "Page diagnostics",
            "DELETE /api/cache": "Clear cache",
        },
        "quick_start": f"curl \"{b}/api/fresh_stream?channel=ary-news\"",
    })

@app.route("/api/health")
def health():
    return jsonify({
        "status": "healthy", "v": "2.3.0",
        "ts": datetime.utcnow().isoformat()+"Z",
        "cache": len(_cache), "channels": len(CHANNELS),
    })

@app.route("/api/channels")
def channels():
    cats = {"news":[],"entertainment":[],"religious":[],"regional":[],"other":[]}
    for s in sorted(CHANNELS):
        sl = s.lower()
        if any(k in sl for k in ["news","city-42"]): cats["news"].append(s)
        elif any(k in sl for k in ["entertainment","digital","tv-one","urdu","play-tv","see-tv","hum-tv","a-plus","zindagi","kahani","musik"]): cats["entertainment"].append(s)
        elif any(k in sl for k in ["madani","qtv","paigham"]): cats["religious"].append(s)
        elif any(k in sl for k in ["khyber","avt","sindh","ktn","waseb","mehran"]): cats["regional"].append(s)
        else: cats["other"].append(s)
    return jsonify({"total":len(CHANNELS),"by_category":cats,"all":sorted(CHANNELS)})

@app.route("/api/fresh_stream")
def fresh_stream():
    ch = request.args.get("channel","").strip().lower()
    force = request.args.get("force","0") == "1"
    if not ch:
        return jsonify({"success":False,"error":"Missing 'channel'.","channels":sorted(CHANNELS)}), 400
    if ch not in CHANNELS:
        parts = [p for p in ch.split("-") if len(p)>2]
        sug = sorted(set(s for s in CHANNELS if ch in s or s in ch or any(p in s for p in parts)))[:8]
        return jsonify({"success":False,"error":f"Unknown: '{ch}'","suggestions":sug or None}), 404

    slug = CHANNELS[ch]
    if not force:
        c = _cget(ch)
        if c:
            age = int((datetime.utcnow()-c["ts"]).total_seconds())
            return jsonify({"success":True,"stream_url":c["url"],"channel":ch,"source":"cache",
                           "age_seconds":age,"alternatives":c.get("alts",[])[1:4],
                           "note":f"Cached ({age}s). &force=1 to refresh."})

    log.info(f"{'='*50}\nREQ: {ch} slug={slug}\n{'='*50}")
    t0 = time.time()
    r = _extract(slug)
    r["extraction_time_seconds"] = round(time.time()-t0, 2)
    r["channel"] = ch
    return jsonify(r), 200 if r.get("success") else 502

@app.route("/api/debug_channel")
def debug_ep():
    ch = request.args.get("channel","").strip().lower()
    if not ch:
        return jsonify({"error":"Need ?channel=slug"}), 400
    slug = CHANNELS.get(ch, ch)
    t0 = time.time()
    r = _debug_extract(slug)
    r["debug_time_seconds"] = round(time.time()-t0, 2)
    return jsonify(r)

@app.route("/api/cache", methods=["DELETE"])
def cache_ep():
    ch = request.args.get("channel","").strip().lower()
    if ch: _cdel(ch); return jsonify({"msg":f"Cleared '{ch}'"})
    n=len(_cache); _cdel(); return jsonify({"msg":f"Cleared {n}"})

@app.errorhandler(404)
def e404(e): return jsonify({"error":"Not found"}), 404
@app.errorhandler(500)
def e500(e): return jsonify({"error":"Server error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"v2.3 :{port} | {len(CHANNELS)} channels")
    app.run(host="0.0.0.0", port=port)
