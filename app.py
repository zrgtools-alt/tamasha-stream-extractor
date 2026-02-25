"""
Tamasha Free Channel HLS Stream Extractor — v2.4
=================================================
Fixed: Lock-free design using a simple busy flag instead of threading.Lock.
This prevents stuck-lock issues when gunicorn recycles workers.
"""

import os
import re
import sys
import time
import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from flask import Flask, jsonify, request
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
log = logging.getLogger("tamasha")

# ── Config ──
TAMASHA = os.environ.get("TAMASHA_BASE_URL", "https://tamashaweb.com")
EXTRA_WAIT = int(os.environ.get("EXTRA_WAIT_SECONDS", "10"))
NAV_TIMEOUT = int(os.environ.get("NAV_TIMEOUT_MS", "35000"))
CACHE_TTL = int(os.environ.get("CACHE_TTL_SECONDS", "300"))

# ── Channels ──
CH = {
    "ary-news":"ary-news","geo-news-live":"geo-news-live",
    "express-news-live":"express-news-live","dunya-news-live":"dunya-news-live",
    "samaa-news-live":"samaa-news-live","92-news-live":"92-news-live",
    "24-news-hd-live":"24-news-hd-live","hum-news-live":"hum-news-live",
    "aaj-news-live":"aaj-news-live","bol-news-live":"bol-news-live",
    "neo-news-live":"neo-news-live","public-news-live":"public-news-live",
    "gnn-news-live":"gnn-news-live","capital-news-live":"capital-news-live",
    "ab-tak-news-live":"ab-tak-news-live","city-42-live":"city-42-live",
    "dawn-news-live":"dawn-news-live","din-news-live":"din-news-live",
    "such-news-live":"such-news-live","k-21-news-live":"k-21-news-live",
    "roze-news-live":"roze-news-live","sun-news-hd":"sun-news-hd",
    "metro-one-news":"metro-one-news",
    "green-entertainment":"green-entertainment",
    "geo-entertainment-live":"geo-entertainment-live",
    "ary-digital-live":"ary-digital-live","hum-tv-live":"hum-tv-live",
    "express-entertainment-live":"express-entertainment-live",
    "a-plus-live":"a-plus-live","tv-one-live":"tv-one-live",
    "urdu-1-live":"urdu-1-live","see-tv-live":"see-tv-live",
    "play-tv-live":"play-tv-live","geo-kahani-live":"geo-kahani-live",
    "ary-zindagi-live":"ary-zindagi-live","tamasha-life-hd":"tamasha-life-hd",
    "khyber-news-live":"khyber-news-live","avt-khyber-live":"avt-khyber-live",
    "sindh-tv-news-live":"sindh-tv-news-live","ktn-news-live":"ktn-news-live",
    "waseb-tv-live":"waseb-tv-live","mehran-tv-live":"mehran-tv-live",
    "madani-channel-live":"madani-channel-live","qtv-live":"qtv-live",
    "paigham-tv-live":"paigham-tv-live","ary-qtv-live":"ary-qtv-live",
    "ary-musik-live":"ary-musik-live",
}

# ── Busy flag (not a Lock — resets naturally if worker dies) ──
_busy = False
_busy_since = None

def _is_busy():
    global _busy, _busy_since
    if not _busy:
        return False
    # Auto-reset if stuck for more than 90s (safety valve)
    if _busy_since and (time.time() - _busy_since) > 90:
        log.warning("⚠ Auto-resetting stuck busy flag (>90s)")
        _busy = False
        _busy_since = None
        return False
    return True

def _set_busy(val):
    global _busy, _busy_since
    _busy = val
    _busy_since = time.time() if val else None

# ── Cache ──
_cache = {}

def cget(ch):
    e = _cache.get(ch)
    if not e: return None
    if (datetime.utcnow() - e["ts"]).total_seconds() < CACHE_TTL: return e
    _cache.pop(ch, None)
    return None

def cset(ch, url, alts=None):
    _cache[ch] = {"url": url, "alts": alts or [], "ts": datetime.utcnow()}

# ── Constants ──
CHROME_ARGS = [
    "--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage",
    "--disable-gpu","--disable-software-rasterizer","--disable-extensions",
    "--disable-background-networking","--disable-default-apps",
    "--disable-sync","--disable-translate",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=IsolateOrigins,site-per-process,TranslateUI",
    "--disable-blink-features=AutomationControlled",
    "--no-first-run","--no-default-browser-check",
    "--single-process","--mute-audio",
    "--autoplay-policy=no-user-gesture-required",
    "--disable-hang-monitor","--disable-component-update",
]

STEALTH = """
Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
Object.defineProperty(navigator,'languages',{get:()=>['en-US','en']});
window.chrome={runtime:{},loadTimes:()=>{},csi:()=>{},app:{}};
"""

UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]
_ui = 0
def _ua():
    global _ui; u=UA[_ui%len(UA)]; _ui+=1; return u

BLOCKED = ["google-analytics.com","googletagmanager.com","facebook.net","facebook.com",
           "doubleclick.net","googlesyndication.com","hotjar.com","clarity.ms","sentry.io"]

HLS_M = [".m3u8","wmsauthsign","playlist.m3u8","master.m3u8","chunklist","index.m3u8","jazzauth","manifest"]
def _is_hls(u): return any(m in u.lower() for m in HLS_M)

PREM_URL = ["/plans","/login","/subscribe","/signup","/otp","/get-pro","/signin","/auth"]
PREM_TXT = ["please login","subscribe to watch","get tamasha pro","login to watch",
            "premium content","enter your otp","subscription required","enter mobile","jazz/warid"]
def _prem(url, txt=""):
    ul=url.lower()
    for k in PREM_URL:
        if k in ul: return True, k
    tl=(txt or "").lower()
    for k in PREM_TXT:
        if k in tl: return True, k
    return False, None

def _score(u):
    s=0; ul=u.lower()
    if "playlist.m3u8" in ul: s+=100
    elif "chunklist" in ul: s+=95
    elif "index.m3u8" in ul: s+=80
    elif "master.m3u8" in ul: s+=50
    elif ".m3u8" in ul: s+=40
    if "wmsauthsign" in ul: s+=200
    elif "token=" in ul: s+=100
    if "nimblesessionid" in ul: s+=30
    for ad in ["doubleclick","googlesyndication","adserver"]:
        if ad in ul: s-=500
    s += len(parse_qs(urlparse(u).query)) * 8
    return s


# ══════════════════════════════════════════════════════════════════
# Shared browser helper — launches, navigates, returns page + browser
# ══════════════════════════════════════════════════════════════════
def _launch_and_navigate(slug, block_resources=True):
    """
    Launch browser, navigate to channel page, return (browser, page, nav_info).
    Caller MUST close browser in finally block.
    """
    pw_instance = sync_playwright().start()
    browser = pw_instance.chromium.launch(headless=True, args=CHROME_ARGS)

    ctx = browser.new_context(
        user_agent=_ua(),
        viewport={"width": 1366, "height": 768},
        java_script_enabled=True,
        bypass_csp=True,
        locale="en-US",
        timezone_id="Asia/Karachi",
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-CH-UA": '"Chromium";v="122"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": '"Windows"',
        },
    )
    ctx.add_init_script(STEALTH)

    if block_resources:
        def rh(route):
            rt = route.request.resource_type
            ru = route.request.url.lower()
            if rt in {"image","font","stylesheet","media"}:
                if ".m3u8" not in ru and ".ts" not in ru:
                    route.abort(); return
            for d in BLOCKED:
                if d in ru: route.abort(); return
            route.continue_()
        ctx.route("**/*", rh)

    page = ctx.new_page()
    target = f"{TAMASHA}/{slug}"

    nav_status = None
    try:
        r = page.goto(target, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        nav_status = r.status if r else None
    except PlaywrightTimeout:
        nav_status = "TIMEOUT"

    try:
        page.wait_for_load_state("networkidle", timeout=12000)
    except PlaywrightTimeout:
        pass

    return pw_instance, browser, page, target, nav_status


def _click_play(page):
    """Try to dismiss overlays and click play."""
    # Dismiss overlays
    for sel in ["button[class*='accept']","button[class*='close']",
                "[class*='cookie'] button",".modal-close","button[aria-label='Close']"]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible(): el.click(timeout=1500); time.sleep(0.3); break
        except Exception: continue

    # Click play
    for sel in ["button.vjs-big-play-button",".play-button",
                "button[aria-label='Play']","button[title='Play']",
                ".jw-icon-playback",".vjs-play-control","video"]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=1500)
                log.info(f"  ▶ {sel}")
                time.sleep(0.5)
                break
        except Exception: continue

    # Autoplay JS
    try:
        page.evaluate("""()=>{
            document.querySelectorAll('video').forEach(v=>{v.muted=true;v.play().catch(()=>{})});
            document.querySelectorAll('iframe').forEach(f=>{
                try{(f.contentDocument||f.contentWindow.document).querySelectorAll('video')
                    .forEach(v=>{v.muted=true;v.play().catch(()=>{})});}catch(e){}
            });
        }""")
    except Exception: pass


# ══════════════════════════════════════════════════════════════════
# Debug Extraction
# ══════════════════════════════════════════════════════════════════
def do_debug(slug):
    log.info(f"🔍 Debug: {slug}")
    responses = []

    def on_r(resp):
        try: responses.append({"url":resp.url[:300],"status":resp.status,"type":resp.request.resource_type})
        except: pass

    pw = browser = page = None
    try:
        pw, browser, page, target, nav_status = _launch_and_navigate(slug, block_resources=False)
        page.on("response", on_r)

        # Re-navigate to capture responses (since we attached listener after first nav)
        try:
            page.reload(wait_until="domcontentloaded", timeout=25000)
        except PlaywrightTimeout:
            pass
        try:
            page.wait_for_load_state("networkidle", timeout=12000)
        except PlaywrightTimeout:
            pass

        _click_play(page)
        time.sleep(8)

        cur = page.url
        title = ""
        try: title = page.title()
        except: pass

        # Video elements
        vinfo = []
        try:
            vinfo = page.evaluate("""()=>{
                const r=[];
                document.querySelectorAll('video').forEach((v,i)=>{
                    r.push({i,src:v.src||null,currentSrc:v.currentSrc||null,
                        paused:v.paused,readyState:v.readyState,networkState:v.networkState,
                        duration:v.duration,id:v.id||null,cls:v.className||null,
                        sources:Array.from(v.querySelectorAll('source')).map(s=>({src:s.src,type:s.type}))
                    });
                });
                return r;
            }""")
        except Exception as e: vinfo=[{"error":str(e)}]

        # Iframes
        iinfo = []
        try:
            iinfo = page.evaluate("""()=>Array.from(document.querySelectorAll('iframe')).slice(0,5).map((f,i)=>({
                i,src:f.src||null,id:f.id||null,cls:f.className||null
            }))""")
        except: pass

        # Player libs
        plibs = {}
        try:
            plibs = page.evaluate("""()=>({
                hls:typeof Hls!=='undefined',
                videojs:typeof videojs!=='undefined',
                jw:typeof jwplayer!=='undefined',
                shaka:typeof shaka!=='undefined',
                dash:typeof dashjs!=='undefined',
                bitmovin:typeof bitmovin!=='undefined',
                clappr:typeof Clappr!=='undefined',
            })""")
        except Exception as e: plibs={"error":str(e)}

        # Tamasha-specific globals
        tglobals = {}
        try:
            tglobals = page.evaluate("""()=>{
                const r={};
                for(const k of Object.keys(window)){
                    const kl=k.toLowerCase();
                    if(kl.includes('player')||kl.includes('stream')||kl.includes('hls')||kl.includes('video'))
                        r[k]=typeof window[k];
                }
                return r;
            }""")
        except: pass

        # __NEXT_DATA__
        ndata = None
        try:
            ndata = page.evaluate("""()=>{
                const el=document.getElementById('__NEXT_DATA__');
                if(!el)return 'NOT_FOUND';
                try{
                    const d=JSON.parse(el.textContent);
                    const pp=d.props?.pageProps||{};
                    // Return keys and any stream-related values
                    const info={keys:Object.keys(pp)};
                    for(const[k,v] of Object.entries(pp)){
                        if(typeof v==='string'&&(v.includes('.m3u8')||v.includes('stream')||v.includes('http')))
                            info[k]=v;
                        if(typeof v==='object'&&v!==null){
                            const vs=JSON.stringify(v);
                            if(vs.includes('.m3u8')||vs.includes('stream_url')||vs.includes('wmsAuth'))
                                info[k+'_snippet']=vs.substring(0,500);
                        }
                    }
                    return info;
                }catch(e){return 'PARSE_ERROR: '+e.message}
            }""")
        except: pass

        # Body text
        body = ""
        try: body = page.evaluate("()=>document.body?document.body.innerText.substring(0,2000):''")
        except: pass

        # HLS responses
        hls_r = [r for r in responses if any(m in r["url"].lower() for m in
                 [".m3u8","wmsauth","playlist","manifest","hls","stream","nimble"])]
        xhr = [r for r in responses if r["type"] in ("fetch","xhr")]

        # m3u8 in source
        m3u8s = []
        try:
            html = page.content()
            for m in re.findall(r'(https?://[^\s"\'<>\\]*\.m3u8[^\s"\'<>\\]*)', html, re.I):
                m3u8s.append(m.replace("\\u0026","&").replace("\\/","/").replace("\\u003d","=")[:400])
        except: pass

        prem, pr = _prem(cur, body)

        return {
            "slug":slug, "target":target, "final_url":cur, "nav_status":nav_status,
            "title":title, "videos":vinfo, "iframes":iinfo,
            "player_libs":plibs, "tamasha_globals":tglobals,
            "next_data":ndata,
            "hls_responses":hls_r[:20], "xhr_responses":xhr[:30],
            "m3u8_in_source":m3u8s[:10],
            "total_responses":len(responses),
            "body_preview":body[:1000],
            "premium":{"is":prem,"reason":pr},
        }

    except Exception as e:
        log.error(f"Debug error: {e}", exc_info=True)
        return {"error": str(e)}
    finally:
        try:
            if browser: browser.close()
        except: pass
        try:
            if pw: pw.stop()
        except: pass


# ══════════════════════════════════════════════════════════════════
# Main Extraction
# ══════════════════════════════════════════════════════════════════
def do_extract(slug):
    log.info(f"▶ Extract: {slug}")
    captured = []
    failed = []
    video_found = False

    def on_r(resp):
        try:
            u=resp.url
            if _is_hls(u) and 200<=resp.status<400:
                captured.append({"url":u,"status":resp.status,"t":time.time()})
                log.info(f"  ✓ [{resp.status}] {u[:180]}")
        except: pass

    def on_f(req):
        try:
            if ".m3u8" in req.url.lower():
                failed.append({"url":req.url[:150],"err":req.failure})
        except: pass

    pw = browser = page = None
    try:
        pw, browser, page, target, nav_status = _launch_and_navigate(slug)
        page.on("response", on_r)
        page.on("requestfailed", on_f)

        cur = page.url
        log.info(f"  Landed: {cur}")

        # If redirected away, try alt URLs
        if slug not in cur.lower().replace("-","") and not captured:
            for alt in [f"{TAMASHA}/watch/{slug}", f"{TAMASHA}/live/{slug}"]:
                try:
                    page.goto(alt, wait_until="domcontentloaded", timeout=20000)
                    cur = page.url
                    if "404" not in (page.title() or "").lower():
                        try: page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeout: pass
                        break
                except: continue

        # Premium check
        try: body = page.evaluate("()=>document.body?document.body.innerText.substring(0,3000):''")
        except: body = ""
        prem, reason = _prem(page.url, body)
        if prem:
            return {"success":False,"error":"Premium — login required.","reason":reason}

        # Find video
        for sel in ["video",".video-js video",".jw-video","video[src]"]:
            try:
                page.wait_for_selector(sel, timeout=6000)
                video_found = True
                log.info(f"  ✓ Video: {sel}")
                break
            except PlaywrightTimeout: continue

        if not video_found:
            try:
                for f in page.query_selector_all("iframe"):
                    try:
                        fr = f.content_frame()
                        if fr:
                            fr.wait_for_selector("video", timeout=4000)
                            video_found = True
                            log.info("  ✓ Video in iframe")
                            break
                    except: continue
            except: pass

        _click_play(page)

        # ── Main wait for HLS ──
        log.info(f"  Waiting {EXTRA_WAIT}s...")
        time.sleep(EXTRA_WAIT)

        # ── Deep extraction if needed ──
        if not captured:
            log.info("  Deep extraction...")

            # A: video.src
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
                        captured.append({"url":src,"status":200,"t":time.time()})
                        log.info(f"  ✓ src: {src[:160]}")
            except: pass

            # B: Player JS objects
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
                        if(i&&i.file)u.push(i.file);}
                    }}catch(e){}
                    return u.filter(x=>x&&typeof x==='string');
                }""")
                for src in (ps or []):
                    if _is_hls(src):
                        captured.append({"url":src,"status":200,"t":time.time()})
                        log.info(f"  ✓ JS: {src[:160]}")
            except: pass

            # C: __NEXT_DATA__ (Tamasha is Next.js!)
            try:
                nd = page.evaluate("""()=>{
                    const el=document.getElementById('__NEXT_DATA__');
                    if(!el)return null;
                    const d=JSON.parse(el.textContent);
                    const s=JSON.stringify(d);
                    const urls=[];
                    const re=/https?:\/\/[^"'\\s]*\.m3u8[^"'\\s]*/gi;
                    let m;while((m=re.exec(s))!==null)urls.push(m[0]);
                    return urls;
                }""")
                for src in (nd or []):
                    c = src.replace("\\u0026","&").replace("\\/","/")
                    captured.append({"url":c,"status":200,"t":time.time()})
                    log.info(f"  ✓ NEXT_DATA: {c[:160]}")
            except: pass

            # D: Regex page source
            try:
                html = page.content()
                for m in re.findall(r'(https?://[^\s"\'<>\\]*\.m3u8[^\s"\'<>\\]*)', html, re.I):
                    c=m.replace("\\u0026","&").replace("\\/","/").replace("\\u003d","=").replace("&amp;","&")
                    captured.append({"url":c,"status":200,"t":time.time()})
                    log.info(f"  ✓ Regex: {c[:160]}")
            except: pass

            # E: data attributes
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
                        captured.append({"url":src,"status":200,"t":time.time()})
            except: pass

            if not captured:
                time.sleep(4)

        log.info(f"  Captured: {len(captured)}")

    except Exception as e:
        log.error(f"Extract error: {e}", exc_info=True)
        return {"success":False,"error":str(e)[:300]}
    finally:
        try:
            if browser: browser.close()
        except: pass
        try:
            if pw: pw.stop()
        except: pass

    if not captured:
        return {
            "success":False,
            "error":"No m3u8 captured.",
            "video_found":video_found,
            "failed_reqs":failed[:5],
            "hint":"Try /api/debug_channel for diagnostics.",
        }

    # Dedup & score
    seen=set(); uniq=[]
    for e in captured:
        k=e["url"].split("&nimblesessionid=")[0] if "&nimblesessionid=" in e["url"] else e["url"]
        if k not in seen: seen.add(k); uniq.append(e)

    best = max(uniq, key=lambda e:(_score(e["url"]),e["t"]))
    url = best["url"]
    sc = _score(url)
    alts = [e["url"] for e in sorted(uniq,key=lambda e:_score(e["url"]),reverse=True)]
    cset(slug, url, alts)
    log.info(f"  ★ Best (score={sc}): {url[:180]}")

    return {
        "success":True,"stream_url":url,"channel":slug,
        "captured":len(uniq),"score":sc,"video_found":video_found,
        "alternatives":alts[1:4] if len(alts)>1 else [],
        "note":"Fresh HLS link ~10-30min expiry. Play in VLC or hlsjs.video-dev.org/demo/",
    }


# ══════════════════════════════════════════════════════════════════
# Routes
# ══════════════════════════════════════════════════════════════════
@app.route("/")
def index():
    return jsonify({
        "service":"Tamasha Free HLS Extractor","v":"2.4.0","status":"running",
        "endpoints":{
            "/":"Docs", "/api/health":"Health", "/api/channels":"Channels",
            "/api/fresh_stream?channel=SLUG":"Extract", "/api/debug_channel?channel=SLUG":"Debug",
        },
    })

@app.route("/api/health")
def health():
    return jsonify({"status":"healthy","v":"2.4.0","ts":datetime.utcnow().isoformat()+"Z",
                    "cache":len(_cache),"channels":len(CH),"busy":_busy})

@app.route("/api/channels")
def channels():
    cats={"news":[],"entertainment":[],"religious":[],"regional":[],"other":[]}
    for s in sorted(CH):
        sl=s.lower()
        if any(k in sl for k in ["news","city-42"]): cats["news"].append(s)
        elif any(k in sl for k in ["entertainment","digital","tv-one","urdu","play-tv","see-tv","hum-tv","a-plus","zindagi","kahani","musik"]): cats["entertainment"].append(s)
        elif any(k in sl for k in ["madani","qtv","paigham"]): cats["religious"].append(s)
        elif any(k in sl for k in ["khyber","avt","sindh","ktn","waseb","mehran"]): cats["regional"].append(s)
        else: cats["other"].append(s)
    return jsonify({"total":len(CH),"by_category":cats,"all":sorted(CH)})

@app.route("/api/fresh_stream")
def fresh_stream():
    ch=request.args.get("channel","").strip().lower()
    force=request.args.get("force","0")=="1"
    if not ch:
        return jsonify({"success":False,"error":"Missing 'channel'.","channels":sorted(CH)}),400
    if ch not in CH:
        parts=[p for p in ch.split("-") if len(p)>2]
        sug=sorted(set(s for s in CH if ch in s or s in ch or any(p in s for p in parts)))[:8]
        return jsonify({"success":False,"error":f"Unknown: '{ch}'","suggestions":sug}),404

    slug=CH[ch]

    if not force:
        c=cget(ch)
        if c:
            age=int((datetime.utcnow()-c["ts"]).total_seconds())
            return jsonify({"success":True,"stream_url":c["url"],"channel":ch,"source":"cache",
                           "age_s":age,"alternatives":c.get("alts",[])[1:4]})

    if _is_busy():
        return jsonify({"success":False,"error":"Server busy — extraction in progress. Retry in 30s.",
                        "channel":ch,"hint":"Only one extraction at a time."}),503

    _set_busy(True)
    t0=time.time()
    try:
        r=do_extract(slug)
    finally:
        _set_busy(False)

    r["extraction_time_seconds"]=round(time.time()-t0,2)
    r["channel"]=ch
    return jsonify(r), 200 if r.get("success") else 502

@app.route("/api/debug_channel")
def debug_ep():
    ch=request.args.get("channel","").strip().lower()
    if not ch:
        return jsonify({"error":"Need ?channel=slug"}),400
    slug=CH.get(ch,ch)

    if _is_busy():
        return jsonify({"error":"Server busy — retry in 30s."}),503

    _set_busy(True)
    t0=time.time()
    try:
        r=do_debug(slug)
    finally:
        _set_busy(False)

    r["debug_time_seconds"]=round(time.time()-t0,2)
    return jsonify(r)

@app.route("/api/cache",methods=["DELETE"])
def cache_ep():
    ch=request.args.get("channel","").strip().lower()
    if ch: _cache.pop(ch,None); return jsonify({"msg":f"Cleared '{ch}'"})
    n=len(_cache); _cache.clear(); return jsonify({"msg":f"Cleared {n}"})

@app.route("/api/reset_busy",methods=["POST","GET"])
def reset_busy():
    """Emergency endpoint to reset stuck busy flag."""
    _set_busy(False)
    return jsonify({"msg":"Busy flag reset.","busy":_busy})

@app.errorhandler(404)
def e404(e): return jsonify({"error":"Not found"}),404
@app.errorhandler(500)
def e500(e): return jsonify({"error":"Server error"}),500

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    log.info(f"v2.4 :{port} | {len(CH)} ch")
    app.run(host="0.0.0.0",port=port)
