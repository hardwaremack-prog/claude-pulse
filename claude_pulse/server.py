#!/usr/bin/env python3
"""
Claude Pulse - a local dashboard for Anthropic / Claude.

Runs on a Raspberry Pi (or any Linux box) using only the Python standard
library. Background threads collect:
  * ANT stock quotes + intraday history (Yahoo Finance, Stooq fallback)
  * Claude service status, incidents and maintenance (status.claude.com)
  * Model speed probes + API rate-limit headroom (optional API key)
  * Available models from the API (detects newly released models)
  * News headlines (Google News RSS) tagged for releases / stock / outages
and stores history in SQLite so the charts can show the last few days.
"""
import http.cookiejar
import json
import os
import re
import shutil
import sqlite3
import statistics
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

IS_WIN = os.name == "nt"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
if IS_WIN:
    _LOCAL = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
    _ROAM = os.environ.get("APPDATA") or os.path.expanduser("~\\AppData\\Roaming")
    _DEF_DATA = os.path.join(_LOCAL, "ClaudePulse", "data")
    _DEF_CONFIG = os.path.join(_ROAM, "ClaudePulse", "config.json")
else:
    _DEF_DATA = os.path.expanduser("~/.local/share/claude-pulse")
    _DEF_CONFIG = os.path.expanduser("~/.config/claude-pulse/config.json")
DATA_DIR = os.environ.get("CLAUDE_PULSE_DATA", _DEF_DATA)
CONFIG_PATH = os.environ.get("CLAUDE_PULSE_CONFIG", _DEF_CONFIG)

# Started with pythonw.exe (Windows, no console) there is nowhere to print: log to a file instead.
if sys.stdout is None or sys.stderr is None:
    os.makedirs(DATA_DIR, exist_ok=True)
    _log = open(os.path.join(DATA_DIR, "claude-pulse.log"), "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

try:
    ET_TZ = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    sys.exit("Claude Pulse needs time-zone data. Run:  python -m pip install tzdata")
VERSION = "1.5.2"
REPO = "hardwaremack-prog/claude-pulse"
REPO_BRANCH = "main"

DEFAULT_CONFIG = {
    "bind": "127.0.0.1" if IS_WIN else "0.0.0.0",
    "port": 8787,
    "ticker": "ANT",
    "ipo_price": 83.0,
    "ipo_date": "2026-11-19",
    "shares_outstanding": 13.37e9,
    "peers": ["^IXIC", "NVDA", "GOOGL", "AMZN", "MSFT"],
    "anthropic_api_key": "",
    "openrouter_api_key": "",
    "openrouter_models": [],
    "claude_usage_auto": True,
    # Anthropic's announced peak window (Mar 2026): weekdays 5-11 AM Pacific = 8 AM-2 PM Eastern.
    # 5-hour sessions drain faster then; weekly limits are the same at any hour.
    "peak_hours": {"tz": "America/Los_Angeles", "days": [0, 1, 2, 3, 4], "start_hour": 5, "end_hour": 11},
    "claude_credentials_path": "",
    "probe_models": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"],
    "probe_interval_min": 15,
    "status_base": "https://status.claude.com",
    "status_fallback": "https://status.anthropic.com",
    "news_queries": ["Anthropic Claude", "Anthropic stock ANT", "Claude new model"],
    "history_keep_days": 45,
    "offline": False,
}

# NYSE/Nasdaq full-day holidays and 1 p.m. early closes.
HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", "2026-06-19",
    "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
    "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31", "2027-06-18",
    "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24",
}
EARLY_CLOSE = {"2026-11-27", "2026-12-24", "2027-11-26"}

UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36 ClaudePulse/" + VERSION
# Quote sites (Yahoo, Nasdaq, Stooq) answer 429/403 to anything that looks like a bot,
# so stock requests send a plain desktop-Chrome identity with no app suffix.
if sys.platform.startswith("win"):
    BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
else:
    BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


# --------------------------------------------------------------------------- config

def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:  # bad JSON should not kill the app
        print(f"[config] could not read {CONFIG_PATH}: {e}", file=sys.stderr)
    if os.environ.get("ANTHROPIC_API_KEY"):
        cfg["anthropic_api_key"] = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("OPENROUTER_API_KEY"):
        cfg["openrouter_api_key"] = os.environ["OPENROUTER_API_KEY"]
    if os.environ.get("CLAUDE_PULSE_PORT"):
        cfg["port"] = int(os.environ["CLAUDE_PULSE_PORT"])
    if os.environ.get("CLAUDE_PULSE_OFFLINE") == "1":
        cfg["offline"] = True
    return cfg


CFG = load_config()
_CFG_MTIME = [None]


def reload_config():
    """Pick up edits to config.json (keys, models, peak hours) without a restart."""
    try:
        m = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return
    if _CFG_MTIME[0] is None:
        _CFG_MTIME[0] = m
        return
    if m != _CFG_MTIME[0]:
        _CFG_MTIME[0] = m
        new = load_config()
        for k in ("port", "bind"):
            new[k] = CFG[k]  # these need a restart
        CFG.update(new)
        KICK.set()


# --------------------------------------------------------------------------- storage

class DB:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            c = self.conn
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript("""
            CREATE TABLE IF NOT EXISTS stock (symbol TEXT, ts INTEGER, price REAL, volume REAL,
                PRIMARY KEY(symbol, ts));
            CREATE TABLE IF NOT EXISTS daily (symbol TEXT, day TEXT, open REAL, high REAL, low REAL,
                close REAL, volume REAL, PRIMARY KEY(symbol, day));
            CREATE TABLE IF NOT EXISTS status_snap (ts INTEGER, component TEXT, state TEXT);
            CREATE INDEX IF NOT EXISTS status_snap_ts ON status_snap(ts);
            CREATE TABLE IF NOT EXISTS probe (ts INTEGER, model TEXT, ok INTEGER, ttft REAL, total REAL,
                out_tokens INTEGER, tps REAL, error TEXT);
            CREATE INDEX IF NOT EXISTS probe_ts ON probe(ts);
            CREATE TABLE IF NOT EXISTS orstat (ts INTEGER, model TEXT, provider TEXT, ttft REAL, tps REAL,
                up5 REAL, up30 REAL, up1d REAL);
            CREATE INDEX IF NOT EXISTS orstat_ts ON orstat(ts);
            CREATE TABLE IF NOT EXISTS usage_hist (ts INTEGER, meter TEXT, pct REAL);
            CREATE INDEX IF NOT EXISTS usage_hist_ts ON usage_hist(ts);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
            """)
            c.commit()

    def execute(self, sql, args=(), many=False):
        with self.lock:
            cur = self.conn.executemany(sql, args) if many else self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def query(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def kv_get(self, key, default=None):
        rows = self.query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0][0])
        except Exception:
            return default

    def kv_set(self, key, value):
        self.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value)))


DB_ = DB(os.path.join(DATA_DIR, "pulse.db"))

STATE_LOCK = threading.Lock()
STATE = {
    "quote": None,
    "peers": [],
    "status": None,
    "recent_incidents": [],
    "upcoming_maint": [],
    "probes": {},
    "rate_limits": None,
    "available_models": [],
    "or_models": [],
    "or_watch": [],
    "or_detail": {},
    "plan_usage": None,
    "news": [],
    "jobs": {},
}


def set_state(key, value):
    with STATE_LOCK:
        STATE[key] = value


def job_ok(name, note=None):
    with STATE_LOCK:
        j = STATE["jobs"].setdefault(name, {})
        j["ok_at"] = int(time.time())
        j["err"] = None
        if note:
            j["note"] = note


def job_err(name, err):
    with STATE_LOCK:
        j = STATE["jobs"].setdefault(name, {})
        j["err"] = str(err)[:300]
        j["err_at"] = int(time.time())


# --------------------------------------------------------------------------- http helpers

def fetch(url, headers=None, data=None, timeout=20, method=None):
    if CFG.get("offline"):
        raise RuntimeError("offline mode")
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()


def fetch_json(url, **kw):
    _, _, body = fetch(url, **kw)
    return json.loads(body)


def iso_to_ts(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


# --------------------------------------------------------------------------- market clock

def market_state(now=None):
    now = now or datetime.now(ET_TZ)
    d = now.date().isoformat()
    if now.weekday() >= 5 or d in HOLIDAYS:
        return "closed"
    t = now.hour * 60 + now.minute
    early = d in EARLY_CLOSE
    close = 13 * 60 if early else 16 * 60
    post_end = 17 * 60 if early else 20 * 60
    if 4 * 60 <= t < 9 * 60 + 30:
        return "pre"
    if 9 * 60 + 30 <= t < close:
        return "open"
    if close <= t < post_end:
        return "post"
    return "closed"


def next_market_open(now=None):
    now = now or datetime.now(ET_TZ)
    d = now
    for _ in range(10):
        cand = d.replace(hour=9, minute=30, second=0, microsecond=0)
        if cand > now and cand.weekday() < 5 and cand.date().isoformat() not in HOLIDAYS:
            return int(cand.timestamp())
        d = (d + timedelta(days=1)).replace(hour=0, minute=0)
    return None


# --------------------------------------------------------------------------- stock

# Yahoo now often refuses requests without its cookie + "crumb", so we keep a small session.
YJAR = http.cookiejar.CookieJar()
YOPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(YJAR))
YCRUMB = {"crumb": None, "at": 0}


def _yopen(url, timeout=20):
    if CFG.get("offline"):
        raise RuntimeError("offline mode")
    req = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json,text/plain,*/*",
                                               "Accept-Language": "en-US,en;q=0.9",
                                               "Referer": "https://finance.yahoo.com/"})
    with YOPENER.open(req, timeout=timeout) as r:
        return r.read()


def yahoo_crumb(force=False):
    if YCRUMB["crumb"] and not force and time.time() - YCRUMB["at"] < 3600:
        return YCRUMB["crumb"]
    for url in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
        try:
            _yopen(url, timeout=15)
        except Exception:
            pass  # fc.yahoo.com answers 404 but still sets the cookie
    crumb = _yopen("https://query2.finance.yahoo.com/v1/test/getcrumb", timeout=15).decode().strip()
    if not crumb or "<" in crumb or len(crumb) > 40:
        raise RuntimeError("no Yahoo crumb")
    YCRUMB.update(crumb=crumb, at=time.time())
    return crumb


def yahoo_chart(symbol, rng, interval, prepost=False):
    params = {"range": rng, "interval": interval, "includePrePost": "true" if prepost else "false"}
    path = f"/v8/finance/chart/{urllib.parse.quote(symbol)}?"
    last = None
    # 1) plain request  2) with cookie + crumb (what Yahoo's own site does)
    for attempt in ("plain", "crumb", "crumb-refresh"):
        try:
            if attempt == "plain":
                body = _yopen("https://query1.finance.yahoo.com" + path + urllib.parse.urlencode(params))
            else:
                q = dict(params, crumb=yahoo_crumb(force=(attempt == "crumb-refresh")))
                body = _yopen("https://query2.finance.yahoo.com" + path + urllib.parse.urlencode(q))
            j = json.loads(body)
            res = (j.get("chart") or {}).get("result")
            if res:
                return res[0]
            last = RuntimeError(json.dumps((j.get("chart") or {}).get("error"))[:200])
        except urllib.error.HTTPError as e:
            last = RuntimeError(f"Yahoo HTTP {e.code}")
        except Exception as e:
            last = e
    raise last or RuntimeError("no data")


def series_points(res):
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    closes = q.get("close") or []
    vols = q.get("volume") or []
    out = []
    for i, t in enumerate(ts):
        c = closes[i] if i < len(closes) else None
        if c is None:
            continue
        v = vols[i] if i < len(vols) else None
        out.append((int(t), float(c), float(v) if v is not None else None))
    return out


# --- Nasdaq.com's public quote API (no key; wants browser-like headers)
NASDAQ_INDEX = {"^IXIC": "COMP", "^GSPC": "SPX", "^NDX": "NDX"}


def _num(s):
    if s in (None, "", "N/A"):
        return None
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(s)))
    except ValueError:
        return None


def nasdaq_get(path):
    _, _, b = fetch("https://api.nasdaq.com/api" + path, headers={
        "User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/"})
    j = json.loads(b)
    if not j.get("data"):
        raise RuntimeError("Nasdaq: " + json.dumps(j.get("status", {}).get("bCodeMessage") or "no data")[:150])
    return j["data"]


def nasdaq_quote(symbol):
    sym, cls = (NASDAQ_INDEX[symbol], "index") if symbol in NASDAQ_INDEX else (symbol, "stocks")
    d = nasdaq_get(f"/quote/{sym}/info?assetclass={cls}")
    p = d.get("primaryData") or {}
    price, chg = _num(p.get("lastSalePrice")), _num(p.get("netChange"))
    ks = d.get("keyStats") or {}
    out = {"price": price, "prev_close": (price - chg) if (price is not None and chg is not None) else None,
           "volume": _num(p.get("volume")), "name": d.get("companyName"), "source": "Nasdaq.com"}
    dr = ((ks.get("dayrange") or ks.get("dayRange") or {}).get("value") or "").split("-")
    if len(dr) == 2:
        out["day_low"], out["day_high"] = _num(dr[0]), _num(dr[1])
    yr = ((ks.get("fiftyTwoWeekHighLow") or {}).get("value") or "").split("-")
    if len(yr) == 2:
        out["low52"], out["high52"] = _num(yr[0]), _num(yr[1])
    s = d.get("secondaryData") or {}
    if s.get("lastSalePrice") and p.get("isRealTime") is not None:
        ext = _num(s.get("lastSalePrice"))
        if ext and price and abs(ext - price) > 1e-9:
            out["ext_price"] = ext
    return out


def nasdaq_intraday(symbol):
    d = nasdaq_get(f"/quote/{symbol}/chart?assetclass=stocks")
    pts = []
    for row in d.get("chart") or []:
        x, y = row.get("x"), _num(row.get("y"))
        if x is None or y is None:
            continue
        # Nasdaq sends New York wall-clock time encoded as if it were UTC.
        wall = datetime.fromtimestamp(x / 1000, timezone.utc).replace(tzinfo=None)
        pts.append((int(wall.replace(tzinfo=ET_TZ).timestamp()), y, None))
    return pts


def stooq_quote(symbol):
    _, _, b = fetch(f"https://stooq.com/q/l/?s={symbol.lower()}.us&f=sd2t2ohlcv&h&e=csv", headers={"User-Agent": BROWSER_UA})
    lines = b.decode("utf-8", "replace").strip().splitlines()
    d = dict(zip(lines[0].split(","), lines[1].split(",")))
    if _num(d.get("Close")) is None:
        raise RuntimeError("Stooq has no data for " + symbol)
    return {"price": _num(d["Close"]), "day_high": _num(d.get("High")), "day_low": _num(d.get("Low")),
            "volume": _num(d.get("Volume")), "source": "Stooq (delayed)"}


def save_points(sym, pts, replace=True):
    if pts:
        DB_.execute(("INSERT OR REPLACE" if replace else "INSERT OR IGNORE") +
                    " INTO stock(symbol, ts, price, volume) VALUES(?,?,?,?)",
                    [(sym, t, p, v) for t, p, v in pts], many=True)


def collect_stock():
    sym = CFG["ticker"]
    quote = {"symbol": sym, "ipo_price": CFG["ipo_price"], "updated": int(time.time())}
    errors = []
    try:
        r1 = yahoo_chart(sym, "1d", "1m", prepost=True)
        m = r1.get("meta", {})
        pts = series_points(r1)
        price = m.get("regularMarketPrice")
        quote.update({
            "source": "Yahoo Finance",
            "name": m.get("longName") or m.get("shortName") or "Anthropic, PBC",
            "price": price,
            "prev_close": m.get("chartPreviousClose") or m.get("previousClose"),
            "day_high": m.get("regularMarketDayHigh"), "day_low": m.get("regularMarketDayLow"),
            "volume": m.get("regularMarketVolume"),
            "high52": m.get("fiftyTwoWeekHigh"), "low52": m.get("fiftyTwoWeekLow"),
            "market_time": m.get("regularMarketTime"),
        })
        if pts and m.get("regularMarketTime") and pts[-1][0] > m["regularMarketTime"] + 60 and price:
            quote["ext_price"], quote["ext_time"] = pts[-1][1], pts[-1][0]
        save_points(sym, pts)
    except Exception as e:
        errors.append(f"Yahoo: {e}")
        for name, fn in (("Nasdaq", nasdaq_quote), ("Stooq", stooq_quote)):
            try:
                quote.update(fn(sym))
                break
            except Exception as e2:
                errors.append(f"{name}: {e2}")
        else:
            raise RuntimeError("; ".join(errors))
        try:
            save_points(sym, nasdaq_intraday(sym))
        except Exception as e3:
            job_err("stock_history", f"Nasdaq chart: {e3}")
            if quote.get("price") is not None:
                save_points(sym, [(int(time.time()), quote["price"], None)])
        if quote.get("prev_close") is None:
            quote["prev_close"] = (STATE.get("quote") or {}).get("prev_close")
    p, pc = quote.get("price"), quote.get("prev_close")
    if p is not None and pc:
        quote["change"] = p - pc
        quote["change_pct"] = (p - pc) / pc * 100
    if p is not None and CFG.get("ipo_price"):
        quote["vs_ipo_pct"] = (p - CFG["ipo_price"]) / CFG["ipo_price"] * 100
    if quote.get("ext_price") and p:
        quote["ext_change_pct"] = (quote["ext_price"] - p) / p * 100
    if p is not None and CFG.get("shares_outstanding"):
        quote["market_cap_est"] = p * CFG["shares_outstanding"]
    if errors:
        quote["fallback_note"] = errors[0][:160]
    set_state("quote", quote)


HIST_STATE = {"days": 0}


def stock_days():
    rows = DB_.query("SELECT ts FROM stock WHERE symbol=? AND ts>=?", (CFG["ticker"], int(time.time()) - 10 * 86400))
    return len({datetime.fromtimestamp(t, ET_TZ).date() for (t,) in rows})


def collect_backfill():
    """Last few trading days of intraday prices (for the 4D chart)."""
    sym = CFG["ticker"]
    errors = []
    for rng, iv in (("5d", "5m"), ("5d", "15m"), ("1mo", "30m")):
        try:
            pts = series_points(yahoo_chart(sym, rng, iv, prepost=True))
            if pts:
                save_points(sym, pts, replace=False)
                break
        except Exception as e:
            errors.append(f"{rng}/{iv}: {e}")
    HIST_STATE["days"] = stock_days()
    if errors and HIST_STATE["days"] < 2:
        raise RuntimeError("couldn't download older prices (" + "; ".join(errors)[:200] + ")")


def nasdaq_daily(sym):
    start = (CFG.get("ipo_date") or (datetime.now(ET_TZ) - timedelta(days=90)).date().isoformat())
    d = nasdaq_get(f"/quote/{sym}/historical?assetclass=stocks&fromdate={start}"
                   f"&todate={datetime.now(ET_TZ).date().isoformat()}&limit=200")
    rows = []
    for r in ((d.get("tradesTable") or {}).get("rows") or []):
        try:
            day = datetime.strptime(r.get("date", ""), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        c = _num(r.get("close"))
        if c is not None:
            rows.append((sym, day, _num(r.get("open")), _num(r.get("high")), _num(r.get("low")), c, _num(r.get("volume"))))
    if not rows:
        raise RuntimeError("Nasdaq history empty")
    return rows


def collect_daily():
    """Daily closes since the IPO (for the Since IPO chart)."""
    sym = CFG["ticker"]
    rows, errors = [], []
    try:
        r = yahoo_chart(sym, "3mo", "1d")
        ts = r.get("timestamp") or []
        q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        for i, t in enumerate(ts):
            c = (q.get("close") or [None])[i]
            if c is None:
                continue
            day = datetime.fromtimestamp(t, ET_TZ).date().isoformat()
            rows.append((sym, day, q["open"][i], q["high"][i], q["low"][i], c, q["volume"][i]))
    except Exception as e:
        errors.append(f"Yahoo: {e}")
    if not rows:
        try:
            rows = nasdaq_daily(sym)
        except Exception as e:
            errors.append(f"Nasdaq: {e}")
    if rows:
        DB_.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?,?,?,?)", rows, many=True)
    # Always keep today's close from the live quote once the market has closed.
    q = STATE.get("quote") or {}
    now = datetime.now(ET_TZ)
    if q.get("price") is not None and market_state() in ("post", "closed") and now.weekday() < 5 \
            and now.date().isoformat() not in HOLIDAYS and now.hour >= 16:
        DB_.execute("INSERT OR IGNORE INTO daily VALUES(?,?,?,?,?,?,?)",
                    (sym, now.date().isoformat(), None, q.get("day_high"), q.get("day_low"), q["price"], q.get("volume")))
    if not rows:
        raise RuntimeError("; ".join(errors)[:300])


def collect_peers():
    out = []
    for s in CFG.get("peers", []):
        item = {"symbol": s, "name": s, "price": None, "change_pct": None}
        try:
            m = yahoo_chart(s, "1d", "5m").get("meta", {})
            item.update(price=m.get("regularMarketPrice"), pc=m.get("chartPreviousClose") or m.get("previousClose"),
                        name=m.get("shortName") or s)
        except Exception as e:
            try:
                n = nasdaq_quote(s)
                item.update(price=n.get("price"), pc=n.get("prev_close"), name=n.get("name") or s)
            except Exception as e2:
                item["error"] = f"{e}; {e2}"[:160]
        p, pc = item.get("price"), item.pop("pc", None)
        if p is not None and pc:
            item["change_pct"] = (p - pc) / pc * 100
        out.append(item)
    set_state("peers", out)
    if out and all(x.get("price") is None for x in out):
        raise RuntimeError(out[0].get("error") or "no peer data")


# --------------------------------------------------------------------------- status page

def status_get(path):
    last = None
    for base in (CFG["status_base"], CFG.get("status_fallback")):
        if not base:
            continue
        try:
            return base, fetch_json(base.rstrip("/") + path)
        except Exception as e:
            last = e
    raise last


def collect_status():
    base, j = status_get("/api/v2/summary.json")
    comps = [{"id": c.get("id"), "name": c.get("name"), "status": c.get("status")}
             for c in j.get("components", []) if not c.get("group")]
    incidents = []
    for i in j.get("incidents", []):
        ups = i.get("incident_updates") or []
        incidents.append({
            "name": i.get("name"), "status": i.get("status"), "impact": i.get("impact"),
            "url": i.get("shortlink"), "created": iso_to_ts(i.get("created_at")),
            "updated": iso_to_ts(i.get("updated_at")),
            "latest": (ups[0].get("body") if ups else "")[:600],
            "components": [c.get("name") for c in i.get("components", [])],
        })
    maint = [{
        "name": m.get("name"), "status": m.get("status"), "url": m.get("shortlink"),
        "start": iso_to_ts(m.get("scheduled_for")), "end": iso_to_ts(m.get("scheduled_until")),
        "components": [c.get("name") for c in m.get("components", [])],
    } for m in j.get("scheduled_maintenances", [])]
    st = j.get("status", {})
    set_state("status", {
        "indicator": st.get("indicator"), "description": st.get("description"),
        "page_url": (j.get("page") or {}).get("url") or base,
        "components": comps, "incidents": incidents, "maintenances": maint,
    })
    now = int(time.time())
    DB_.execute("INSERT INTO status_snap(ts, component, state) VALUES(?,?,?)",
                [(now, c["name"], c["status"]) for c in comps], many=True)


def collect_incidents():
    _, j = status_get("/api/v2/incidents.json")
    out = []
    cutoff = time.time() - 14 * 86400
    for i in j.get("incidents", []):
        created = iso_to_ts(i.get("created_at"))
        if not created or created < cutoff:
            continue
        out.append({
            "name": i.get("name"), "status": i.get("status"), "impact": i.get("impact"),
            "url": i.get("shortlink"), "created": created,
            "resolved": iso_to_ts(i.get("resolved_at")),
            "components": [c.get("name") for c in i.get("components", [])],
        })
    set_state("recent_incidents", out)
    try:
        _, u = status_get("/api/v2/scheduled-maintenances/upcoming.json")
        set_state("upcoming_maint", [{
            "name": m.get("name"), "status": m.get("status"), "url": m.get("shortlink"),
            "start": iso_to_ts(m.get("scheduled_for")), "end": iso_to_ts(m.get("scheduled_until")),
            "components": [c.get("name") for c in m.get("components", [])],
        } for m in u.get("scheduled_maintenances", [])])
    except Exception as e:
        job_err("maintenance", e)


IMPACT_LEVEL = {"none": 0, "minor": 1, "major": 2, "critical": 3, "maintenance": 0}
STATE_LEVEL = {"operational": 0, "under_maintenance": 0, "degraded_performance": 1,
               "partial_outage": 2, "major_outage": 3}


# How status pages count downtime: major outage = down, partial outage = 30%, degraded = 0.
LEVEL_WEIGHT = {0: 0.0, 1: 0.0, 2: 0.3, 3: 1.0}


def build_timeline(start, end):
    """Per-component outage intervals from incidents + our own status snapshots."""
    with STATE_LOCK:
        comp_list = (STATE.get("status") or {}).get("components", [])
        incidents = list(STATE.get("recent_incidents") or [])
    comps = [c["name"] for c in comp_list]
    current = {c["name"]: STATE_LEVEL.get(c["status"], 0) for c in comp_list}
    rows = {name: [] for name in comps}
    now = int(time.time())
    for inc in incidents:
        s = inc["created"]
        e = inc["resolved"] or now
        if e < start or s > end:
            continue
        lvl = max(IMPACT_LEVEL.get(inc.get("impact") or "minor", 1), 1)
        for cname in inc.get("components") or ["Other (no component listed)"]:
            l = lvl
            # An ongoing incident is drawn at the component's *current* severity, so a
            # long-running "degraded" issue isn't shown as a multi-day outage.
            if not inc["resolved"] and cname in current:
                l = current[cname] or 1
            rows.setdefault(cname, []).append({"start": max(s, start), "end": min(e, end), "level": l,
                                               "label": inc["name"], "url": inc.get("url")})
    snaps = DB_.query("SELECT ts, component, state FROM status_snap WHERE ts>=? ORDER BY ts", (start,))
    open_iv, last_ts = {}, {}
    for ts, comp, state in snaps:
        lvl = STATE_LEVEL.get(state, 0)
        cur = open_iv.get(comp)
        gap = ts - last_ts.get(comp, ts)
        if cur and (lvl == 0 or lvl != cur["level"] or gap > 900):
            cur["end"] = last_ts[comp] if gap > 900 else ts
            rows.setdefault(comp, []).append(cur)
            open_iv.pop(comp)
            cur = None
        if lvl > 0 and not cur:
            open_iv[comp] = {"start": ts, "end": ts, "level": lvl, "label": state.replace("_", " "), "src": "snap"}
        last_ts[comp] = ts
    for comp, cur in open_iv.items():
        cur["end"] = last_ts[comp]
        rows.setdefault(comp, []).append(cur)
    span = max(1, end - start)
    step = 60
    out = []
    for name, ivs in rows.items():
        ivs.sort(key=lambda x: x["start"])
        down = 0.0
        if ivs:
            # Minute by minute, take the worst level covering that minute.
            n = span // step + 1
            worst = bytearray(n)
            for iv in ivs:
                a = max(0, (iv["start"] - start) // step)
                b = min(n - 1, (iv["end"] - start) // step)
                for i in range(a, b + 1):
                    if iv["level"] > worst[i]:
                        worst[i] = iv["level"]
            down = sum(LEVEL_WEIGHT.get(w, 0) for w in worst) * step
        out.append({"component": name, "intervals": ivs,
                    "uptime_pct": round(max(0.0, 100 * (1 - down / span)), 2)})
    first = DB_.query("SELECT MIN(ts) FROM status_snap")[0][0]
    return {"rows": out, "sampled_since": first}


# --------------------------------------------------------------------------- model probes

PROBE_PROMPT = "Count from 1 to 60. Reply with only the numbers separated by single spaces."


def probe_model(model, key):
    body = json.dumps({"model": model, "max_tokens": 120, "stream": True,
                       "messages": [{"role": "user", "content": PROBE_PROMPT}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        "user-agent": UA})
    t0 = time.monotonic()
    ttft = None
    out_tokens = None
    headers = {}
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            headers = {k.lower(): v for k, v in r.headers.items()}
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                typ = ev.get("type")
                if typ == "content_block_delta" and ttft is None:
                    ttft = time.monotonic() - t0
                elif typ == "message_delta":
                    out_tokens = (ev.get("usage") or {}).get("output_tokens", out_tokens)
                elif typ == "error":
                    raise RuntimeError((ev.get("error") or {}).get("message", "stream error"))
        total = time.monotonic() - t0
        tps = None
        if out_tokens and ttft is not None and total - ttft > 0.05:
            tps = max(0.0, (out_tokens - 1) / (total - ttft))
        return {"ok": True, "ttft": ttft, "total": total, "out_tokens": out_tokens, "tps": tps,
                "error": None, "headers": headers}
    except urllib.error.HTTPError as e:
        headers = {k.lower(): v for k, v in e.headers.items()}
        try:
            msg = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            msg = ""
        return {"ok": False, "ttft": None, "total": time.monotonic() - t0, "out_tokens": None, "tps": None,
                "error": f"HTTP {e.code} {msg}"[:200], "headers": headers, "code": e.code}
    except Exception as e:
        return {"ok": False, "ttft": None, "total": time.monotonic() - t0, "out_tokens": None, "tps": None,
                "error": str(e)[:200], "headers": headers}


def parse_rate_limits(h):
    if not h:
        return None
    out = {}
    for k, v in h.items():
        if k.startswith("anthropic-ratelimit-"):
            out[k[len("anthropic-ratelimit-"):]] = v
        elif k == "retry-after":
            out["retry-after"] = v
    return out or None


def collect_probes():
    key = CFG.get("anthropic_api_key")
    if not key:
        job_ok("probes", "no API key - probes off")
        return
    models = CFG.get("probe_models") or []
    errors = []
    for m in models:
        r = probe_model(m, key)
        DB_.execute("INSERT INTO probe(ts, model, ok, ttft, total, out_tokens, tps, error) VALUES(?,?,?,?,?,?,?,?)",
                    (int(time.time()), m, 1 if r["ok"] else 0, r["ttft"], r["total"], r["out_tokens"], r["tps"],
                     r["error"]))
        rl = parse_rate_limits(r.get("headers"))
        if rl:
            set_state("rate_limits", {"at": int(time.time()), "model": m, "limits": rl})
        if not r["ok"]:
            errors.append(f"{m}: {r['error']}")
        time.sleep(2)
    if errors and len(errors) == len(models):
        raise RuntimeError("; ".join(errors)[:300])


def collect_models():
    key = CFG.get("anthropic_api_key")
    if not key:
        job_ok("models", "no API key")
        return
    j = fetch_json("https://api.anthropic.com/v1/models?limit=100",
                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    data = j.get("data", [])
    seen = DB_.kv_get("seen_models")
    now = int(time.time())
    baseline = seen is None
    seen = seen or {}
    for m in data:
        if m["id"] not in seen:
            seen[m["id"]] = {"first_seen": now, "baseline": baseline}
    DB_.kv_set("seen_models", seen)
    out = []
    for m in data:
        s = seen.get(m["id"], {})
        out.append({"id": m["id"], "display_name": m.get("display_name") or m["id"],
                    "created": iso_to_ts(m.get("created_at")),
                    "first_seen": s.get("first_seen"),
                    "new": (not s.get("baseline")) and now - s.get("first_seen", 0) < 14 * 86400})
    out.sort(key=lambda x: x.get("created") or 0, reverse=True)
    set_state("available_models", out)


def probe_summary():
    since = int(time.time()) - 4 * 86400
    rows = DB_.query("SELECT ts, model, ok, ttft, total, out_tokens, tps, error FROM probe WHERE ts>=? ORDER BY ts",
                     (since,))
    by = {}
    for ts, model, ok, ttft, total, out_tokens, tps, err in rows:
        by.setdefault(model, []).append((ts, ok, ttft, tps, total, err))
    names = {m["id"]: m["display_name"] for m in STATE.get("available_models") or []}
    out = []
    for model in (CFG.get("probe_models") or []):
        pts = by.get(model, [])
        good = [p for p in pts if p[1] and p[2] is not None]
        last = pts[-1] if pts else None
        med_ttft = statistics.median([p[2] for p in good]) if len(good) >= 3 else None
        med_tps = statistics.median([p[3] for p in good if p[3]]) if len([p for p in good if p[3]]) >= 3 else None
        recent = [p for p in good if p[0] >= time.time() - 3600]
        load = None
        if last and last[1] and med_ttft and med_tps and last[3]:
            load = load_label(last[2], last[3], med_ttft, med_tps)
        elif last and not last[1]:
            load = {"label": "Failing", "level": 3, "ratio": None}
        ok_rate = (sum(1 for p in pts if p[1]) / len(pts) * 100) if pts else None
        out.append({
            "model": model, "display_name": names.get(model, model),
            "at": last[0] if last else None, "ok": bool(last[1]) if last else None,
            "ttft": last[2] if last else None, "tps": last[3] if last else None,
            "total": last[4] if last else None, "error": last[5] if last else None,
            "med_ttft": med_ttft, "med_tps": med_tps, "samples": len(pts),
            "success_pct": ok_rate, "load": load,
            "recent_tps": statistics.mean([p[3] for p in recent if p[3]]) if any(p[3] for p in recent) else None,
        })
    return out


def load_grid():
    """Typical load by weekday and hour (Eastern): each model's first-token wait divided by
    that model's own median, averaged, so fast and slow models count equally."""
    since = int(time.time()) - 30 * 86400
    rows = DB_.query("SELECT ts, model, ttft FROM orstat WHERE ttft IS NOT NULL AND ts>=? AND provider LIKE '%Anthropic%'",
                     (since,))
    src = "openrouter"
    if len(rows) < 50:
        rows = DB_.query("SELECT ts, model, ttft FROM probe WHERE ok=1 AND ttft IS NOT NULL AND ts>=?", (since,))
        src = "probes"
    by_model = {}
    for ts, model, ttft in rows:
        by_model.setdefault(model, []).append(ttft)
    med = {m: statistics.median(v) for m, v in by_model.items() if v}
    cells = {}
    for ts, model, ttft in rows:
        if not med.get(model):
            continue
        d = datetime.fromtimestamp(ts, ET_TZ)
        cells.setdefault((d.weekday(), d.hour), []).append(ttft / med[model])
    grid = [{"dow": k[0], "hour": k[1], "ratio": round(statistics.mean(v), 3), "n": len(v)}
            for k, v in sorted(cells.items())]
    return {"source": src if rows else None, "cells": grid, "days_of_data": round(
        (max(r[0] for r in rows) - min(r[0] for r in rows)) / 86400, 1) if rows else 0}


def hourly_profile():
    """Average time-to-first-token by hour of day (ET) across all stored probes."""
    since = int(time.time()) - 30 * 86400
    rows = DB_.query("SELECT ts, ttft FROM orstat WHERE ttft IS NOT NULL AND ts>=? AND provider LIKE '%Anthropic%'",
                     (since,))
    if len(rows) < 24:
        rows = DB_.query("SELECT ts, ttft FROM probe WHERE ok=1 AND ttft IS NOT NULL AND ts>=?", (since,))
    buckets = {}
    for ts, ttft in rows:
        h = datetime.fromtimestamp(ts, ET_TZ).hour
        buckets.setdefault(h, []).append(ttft)
    return [{"hour": h, "avg_ttft": statistics.mean(buckets[h]) if h in buckets else None,
             "n": len(buckets.get(h, []))} for h in range(24)]


# --------------------------------------------------------------------------- OpenRouter (no Anthropic key needed)
# Public stats from OpenRouter's real traffic to each Claude model. Uptime is public;
# latency/throughput need a free OpenRouter key (no credits required).

OR_BASE = "https://openrouter.ai/api/v1"
RE_FAMILY = re.compile(r"claude-(haiku|sonnet|opus|fable|mythos)", re.I)


def or_headers():
    h = {"Accept": "application/json"}
    if CFG.get("openrouter_api_key"):
        h["Authorization"] = "Bearer " + CFG["openrouter_api_key"]
    return h


def load_label(ttft, tps, med_ttft, med_tps, up=None):
    """Compare the latest speed to the model's usual speed."""
    if ttft and tps and med_ttft and med_tps:
        ratio = ((ttft / med_ttft) + (med_tps / tps)) / 2
        if up is not None and up < 95:
            return {"label": "Errors", "level": 3, "ratio": round(ratio, 2)}
        if ratio < 0.85:
            return {"label": "Faster than usual", "level": 0, "ratio": round(ratio, 2)}
        if ratio < 1.2:
            return {"label": "Normal", "level": 1, "ratio": round(ratio, 2)}
        if ratio < 1.6:
            return {"label": "Busy", "level": 2, "ratio": round(ratio, 2)}
        return {"label": "Very busy", "level": 3, "ratio": round(ratio, 2)}
    if up is not None:
        if up >= 99:
            return {"label": "Healthy", "level": 0, "ratio": None}
        if up >= 95:
            return {"label": "Some errors", "level": 2, "ratio": None}
        return {"label": "Failing", "level": 3, "ratio": None}
    return None


def collect_or_models():
    j = fetch_json(OR_BASE + "/models", headers=or_headers())
    # Skip variants like ":thinking" / ":beta" - they're the same model.
    ants = [m for m in j.get("data", []) if str(m.get("id", "")).startswith("anthropic/") and ":" not in m["id"]]
    now = int(time.time())
    seen = DB_.kv_get("seen_or_models")
    baseline = seen is None
    seen = seen or {}
    for m in ants:
        if m["id"] not in seen:
            seen[m["id"]] = {"first_seen": now, "baseline": baseline}
    DB_.kv_set("seen_or_models", seen)
    out = []
    for m in ants:
        s = seen.get(m["id"], {})
        name = re.sub(r"^Anthropic:\s*", "", m.get("name") or m["id"])
        out.append({"id": m["id"], "display_name": name, "created": m.get("created"),
                    "first_seen": s.get("first_seen"),
                    "new": (not s.get("baseline")) and now - s.get("first_seen", 0) < 14 * 86400})
    out.sort(key=lambda x: x.get("created") or 0, reverse=True)
    set_state("or_models", out)
    # Which models to watch: config list, else the newest model of each family.
    watch = list(CFG.get("openrouter_models") or [])
    if not watch:
        best = {}
        for m in out:
            mid = m["id"]
            if ":" in mid or re.search(r"(preview|beta|-\d{8})$", mid):
                continue
            fam = RE_FAMILY.search(mid)
            if not fam:
                continue
            f = fam.group(1).lower()
            if f not in best or (m.get("created") or 0) > (best[f].get("created") or 0):
                best[f] = m
        order = ["haiku", "sonnet", "opus", "fable", "mythos"]
        watch = [best[f]["id"] for f in order if f in best]
    set_state("or_watch", [{"id": w, "display_name": next((m["display_name"] for m in out if m["id"] == w), w)}
                           for w in watch])


def _ms_to_s(v):
    if v is None:
        return None
    v = float(v)
    return v / 1000.0 if v >= 30 else v  # docs say ms; guard in case seconds are sent


def collect_or_stats():
    with STATE_LOCK:
        watch = [w["id"] for w in STATE.get("or_watch") or []]
    if not watch:
        collect_or_models()
        with STATE_LOCK:
            watch = [w["id"] for w in STATE.get("or_watch") or []]
    now = int(time.time())
    rows, detail, errors = [], {}, []
    for slug in watch:
        try:
            j = fetch_json(f"{OR_BASE}/models/{slug}/endpoints", headers=or_headers())
        except urllib.error.HTTPError as e:
            errors.append(f"{slug}: HTTP {e.code}")
            continue
        except Exception as e:
            errors.append(f"{slug}: {e}")
            continue
        eps = (j.get("data") or {}).get("endpoints") or []
        detail[slug] = []
        for ep in eps:
            prov = ep.get("provider_name") or ep.get("tag") or "?"
            ttft = _ms_to_s((ep.get("latency_last_30m") or {}).get("p50"))
            tps = (ep.get("throughput_last_30m") or {}).get("p50")
            r = (now, slug, prov, ttft, float(tps) if tps is not None else None,
                 ep.get("uptime_last_5m"), ep.get("uptime_last_30m"), ep.get("uptime_last_1d"))
            rows.append(r)
            detail[slug].append({"provider": prov, "ttft": ttft, "tps": r[4], "up5": r[5], "up30": r[6], "up1d": r[7]})
    if rows:
        DB_.execute("INSERT INTO orstat VALUES(?,?,?,?,?,?,?,?)", rows, many=True)
    set_state("or_detail", detail)
    if errors and not rows:
        raise RuntimeError("; ".join(errors)[:300])


def main_provider(providers):
    """Prefer Anthropic's own endpoint, else the first one listed."""
    for p in providers:
        if "anthropic" in p.lower():
            return p
    return providers[0] if providers else None


def or_series(start):
    rows = DB_.query("SELECT ts, model, provider, ttft, tps, up5, up30, up1d FROM orstat WHERE ts>=? ORDER BY ts",
                     (start,))
    by = {}
    for ts, model, prov, ttft, tps, up5, up30, up1d in rows:
        by.setdefault(model, {}).setdefault(prov, []).append((ts, ttft, tps, up5, up30, up1d))
    out = {}
    for model, provs in by.items():
        mp = main_provider(list(provs))
        out[model] = provs[mp] if mp else []
    return out


def or_summary():
    with STATE_LOCK:
        watch = list(STATE.get("or_watch") or [])
        detail = dict(STATE.get("or_detail") or {})
    series = or_series(int(time.time()) - 4 * 86400)
    out = []
    for w in watch:
        pts = series.get(w["id"], [])
        last = pts[-1] if pts else None
        good_t = [p[1] for p in pts if p[1]]
        good_s = [p[2] for p in pts if p[2]]
        med_ttft = statistics.median(good_t) if len(good_t) >= 3 else None
        med_tps = statistics.median(good_s) if len(good_s) >= 3 else None
        provs = detail.get(w["id"], [])
        mp = main_provider([p["provider"] for p in provs])
        out.append({
            "model": w["id"], "display_name": w["display_name"],
            "provider": mp, "at": last[0] if last else None,
            "ttft": last[1] if last else None, "tps": last[2] if last else None,
            "up5": last[3] if last else None, "up30": last[4] if last else None, "up1d": last[5] if last else None,
            "med_ttft": med_ttft, "med_tps": med_tps, "samples": len(pts),
            "load": load_label(last[1], last[2], med_ttft, med_tps, last[4]) if last else None,
            "others": [p for p in provs if p["provider"] != mp],
        })
    return out


# --------------------------------------------------------------------------- Claude plan usage (via Claude Code's sign-in)
# Unofficial: reads the OAuth token Claude Code saved on this machine and asks the same
# internal endpoint Claude Code uses. The token never leaves this server process.

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
METER_NAMES = {
    "five_hour": "Current session (5-hour)",
    "seven_day": "Weekly · all models",
    "seven_day_opus": "Weekly · Opus",
    "seven_day_sonnet": "Weekly · Sonnet",
    "seven_day_omelette": "Weekly · Claude Design",
    "seven_day_oauth_apps": "Weekly · connected apps",
}


def claude_cred_paths():
    paths = []
    if CFG.get("claude_credentials_path"):
        paths.append(os.path.expanduser(CFG["claude_credentials_path"]))
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        paths.append(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".credentials.json"))
    paths.append(os.path.expanduser("~/.claude/.credentials.json"))
    return paths


def read_claude_token():
    for p in claude_cred_paths():
        try:
            with open(p) as f:
                o = (json.load(f) or {}).get("claudeAiOauth") or {}
        except (FileNotFoundError, PermissionError, ValueError):
            continue
        if o.get("accessToken"):
            return {"token": o["accessToken"], "expires": (o.get("expiresAt") or 0) / 1000 or None,
                    "plan": o.get("subscriptionType"), "path": p}
    return None


def meter_label(key):
    if key in METER_NAMES:
        return METER_NAMES[key]
    return key.replace("seven_day_", "Weekly · ").replace("_", " ")


def collect_plan_usage():
    if not CFG.get("claude_usage_auto", True):
        set_state("plan_usage", {"status": "off"})
        return
    cred = read_claude_token()
    now = int(time.time())
    if not cred:
        set_state("plan_usage", {"status": "no_claude_code", "at": now})
        return
    prev = STATE.get("plan_usage") or {}
    try:
        j = fetch_json(USAGE_URL, headers={"Authorization": "Bearer " + cred["token"],
                                           "anthropic-beta": "oauth-2025-04-20", "Accept": "application/json"})
    except urllib.error.HTTPError as e:
        status = "expired" if e.code in (401, 403) else "error"
        set_state("plan_usage", {"status": status, "at": now, "code": e.code, "plan": cred.get("plan"),
                                 "token_expires": cred.get("expires"),
                                 "last_ok": prev.get("last_ok"), "meters": prev.get("meters")})
        if status == "error":
            raise RuntimeError(f"usage endpoint HTTP {e.code}")
        return
    meters = []
    for k, v in (j or {}).items():
        if isinstance(v, dict) and v.get("utilization") is not None:
            meters.append({"key": k, "label": meter_label(k), "pct": max(0.0, min(100.0, float(v["utilization"]))),
                           "resets": iso_to_ts(v.get("resets_at"))})
    order = {"five_hour": 0, "seven_day": 1}
    meters.sort(key=lambda m: (order.get(m["key"], 2), m["key"]))
    if not meters:
        raise RuntimeError("usage endpoint returned no meters")
    DB_.execute("INSERT INTO usage_hist(ts, meter, pct) VALUES(?,?,?)",
                [(now, m["key"], m["pct"]) for m in meters], many=True)
    set_state("plan_usage", {"status": "ok", "at": now, "last_ok": now, "plan": cred.get("plan"),
                             "token_expires": cred.get("expires"), "meters": meters})


# --------------------------------------------------------------------------- news

RE_RELEASE = re.compile(r"\b(launch\w*|releas\w*|unveil\w*|introduc\w*|debut\w*|rolls? out|new model|announc\w*|now available)\b", re.I)
RE_PRODUCT = re.compile(r"\b(claude|opus|sonnet|haiku|mythos|fable|claude code)\b", re.I)
RE_STOCK = re.compile(r"\b(stock|shares|IPO|nasdaq|ANT|valuation|investors?|analysts?)\b")
RE_OUTAGE = re.compile(r"\b(outage|down|disruption|errors?|degraded)\b", re.I)


def collect_news():
    items = {}
    for q in CFG.get("news_queries", []):
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": q + " when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
        try:
            _, _, b = fetch(url)
        except Exception as e:
            job_err("news:" + q, e)
            continue
        root = ET.fromstring(b)
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            src_el = it.find("source")
            source = (src_el.text or "").strip() if src_el is not None else ""
            if source and title.endswith(" - " + source):
                title = title[: -len(" - " + source)]
            link = (it.findtext("link") or "").strip()
            try:
                ts = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
            except Exception:
                ts = 0
            key = re.sub(r"\W+", "", title.lower())[:80]
            if not key or key in items:
                continue
            tags = []
            if RE_RELEASE.search(title) and RE_PRODUCT.search(title):
                tags.append("release")
            if RE_STOCK.search(title):
                tags.append("stock")
            if RE_OUTAGE.search(title) and RE_PRODUCT.search(title):
                tags.append("outage")
            items[key] = {"title": title, "source": source, "url": link, "ts": ts, "tags": tags}
    if not items:
        raise RuntimeError("no headlines fetched")
    news = sorted(items.values(), key=lambda x: x["ts"], reverse=True)[:60]
    set_state("news", news)


# --------------------------------------------------------------------------- usage (manual)

def usage_view():
    u = DB_.kv_get("usage", {}) or {}
    now = int(time.time())
    out = dict(u)
    sr = u.get("session_reset")
    if sr and sr <= now:
        out["session_pct"] = 0
        out["session_note"] = "Window reset. A new 5-hour window starts with your next message."
        out["session_reset"] = None
    wr = u.get("weekly_reset")
    if wr and wr <= now:
        while wr <= now:
            wr += 7 * 86400
        out["weekly_reset"] = wr
        out["weekly_pct"] = 0
        out["weekly_note"] = "Weekly limit reset (next reset estimated 7 days later)."
    return out


def save_usage(payload):
    allowed = {"plan", "session_pct", "session_reset", "weekly_pct", "weekly_reset", "weekly_model_pct",
               "weekly_model_label"}
    u = {k: payload.get(k) for k in allowed if k in payload}
    for k in ("session_pct", "weekly_pct", "weekly_model_pct"):
        if u.get(k) not in (None, ""):
            u[k] = max(0.0, min(100.0, float(u[k])))
        else:
            u[k] = None
    for k in ("session_reset", "weekly_reset"):
        if u.get(k) not in (None, ""):
            u[k] = int(u[k])
        else:
            u[k] = None
    for k in ("plan", "weekly_model_label"):
        if u.get(k) is not None:
            u[k] = str(u[k])[:40]
    u["updated"] = int(time.time())
    DB_.kv_set("usage", u)
    return usage_view()


# --------------------------------------------------------------------------- self-update

def vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "0"))


def check_update():
    _, _, b = fetch(f"https://raw.githubusercontent.com/{REPO}/{REPO_BRANCH}/claude_pulse/server.py?t={int(time.time())}")
    m = re.search(rb'VERSION = "([^"]+)"', b)
    if not m:
        raise RuntimeError("couldn't read the version on GitHub")
    latest = m.group(1).decode()
    set_state("update", {"latest": latest, "checked": int(time.time()),
                         "available": vtuple(latest) > vtuple(VERSION)})


UPDATE_LOCK = threading.Lock()


def restart_self():
    args = [sys.executable, os.path.join(APP_DIR, "server.py")]
    if IS_WIN:
        # Windows can't replace a running process: start a fresh one (it waits for the port), then exit.
        subprocess.Popen(args, creationflags=0x00000008 | 0x00000200, close_fds=True)  # DETACHED | NEW_GROUP
        os._exit(0)
    os.execv(sys.executable, args)


def do_update():
    """Download the latest app from GitHub, replace the app files, then restart."""
    if not UPDATE_LOCK.acquire(blocking=False):
        raise RuntimeError("an update is already running")
    try:
        _, _, b = fetch(f"https://codeload.github.com/{REPO}/tar.gz/refs/heads/{REPO_BRANCH}", timeout=90)
        tmp = tempfile.mkdtemp(prefix="claude-pulse-update-")
        tar_path = os.path.join(tmp, "src.tgz")
        with open(tar_path, "wb") as f:
            f.write(b)
        with tarfile.open(tar_path) as tf:
            for mbr in tf.getmembers():
                target = os.path.realpath(os.path.join(tmp, mbr.name))
                if not target.startswith(os.path.realpath(tmp) + os.sep) or not (mbr.isfile() or mbr.isdir()):
                    raise RuntimeError("unexpected file in update: " + mbr.name)
            tf.extractall(tmp)
        roots = [d for d in os.listdir(tmp) if d.startswith("claude-pulse-")]
        if not roots:
            raise RuntimeError("update archive looks wrong")
        src_root = os.path.join(tmp, roots[0])
        new_app = os.path.join(src_root, "claude_pulse")
        with open(os.path.join(new_app, "server.py"), "rb") as f:
            m = re.search(rb'VERSION = "([^"]+)"', f.read())
        new_version = m.group(1).decode() if m else "?"
        compile(open(os.path.join(new_app, "server.py")).read(), "server.py", "exec")  # refuse a broken file
        # Copy app files over the installed ones (config and history live elsewhere).
        for dirpath, _, files in os.walk(new_app):
            rel = os.path.relpath(dirpath, new_app)
            dst_dir = os.path.join(APP_DIR, rel) if rel != "." else APP_DIR
            os.makedirs(dst_dir, exist_ok=True)
            for fn in files:
                tmp_dst = os.path.join(dst_dir, fn + ".new")
                shutil.copy2(os.path.join(dirpath, fn), tmp_dst)
                os.replace(tmp_dst, os.path.join(dst_dir, fn))
        parent = os.path.dirname(APP_DIR)
        extras = ["README.md", "config.example.json"] + (
            [os.path.join("windows", "uninstall.ps1")] if IS_WIN else ["uninstall.sh", "install.sh", "get.sh"])
        for fn in extras:
            src = os.path.join(src_root, fn)
            if os.path.exists(src) and os.access(parent, os.W_OK):
                shutil.copy2(src, os.path.join(parent, os.path.basename(fn)))
        shutil.rmtree(tmp, ignore_errors=True)

        def restart():
            time.sleep(1.5)
            restart_self()
        threading.Thread(target=restart, daemon=True).start()
        return {"ok": True, "from": VERSION, "to": new_version}
    except Exception:
        UPDATE_LOCK.release()
        raise


# --------------------------------------------------------------------------- scheduler

KICK = threading.Event()


def stock_interval():
    return {"open": 60, "pre": 120, "post": 120}.get(market_state(), 900)


JOBS = [
    ("stock", collect_stock, stock_interval),
    ("daily", collect_daily, lambda: 1800 if DB_.query("SELECT COUNT(*) FROM daily")[0][0] >= 2 else 180),
    ("backfill", collect_backfill, lambda: 1800 if HIST_STATE["days"] >= 4 else 180),
    ("peers", collect_peers, lambda: 300 if market_state() == "open" else 1800),
    ("status", collect_status, lambda: 120),
    ("incidents", collect_incidents, lambda: 600),
    ("models", collect_models, lambda: 3600),
    ("or_models", collect_or_models, lambda: 3600),
    ("openrouter", collect_or_stats, lambda: 300),
    ("probes", collect_probes, lambda: max(5, int(CFG.get("probe_interval_min", 15))) * 60),
    ("news", collect_news, lambda: 1200),
    ("plan_usage", collect_plan_usage, lambda: 300),
    ("update_check", check_update, lambda: 6 * 3600),
    ("prune", lambda: prune(), lambda: 86400),
    ("config", reload_config, lambda: 30),
]


def prune():
    cutoff = int(time.time()) - int(CFG.get("history_keep_days", 45)) * 86400
    for t in ("stock", "status_snap", "probe", "orstat", "usage_hist"):
        DB_.execute(f"DELETE FROM {t} WHERE ts<?", (cutoff,))


def scheduler():
    next_run = {name: 0 for name, _, _ in JOBS}
    running = set()
    lock = threading.Lock()

    def run(name, fn):
        try:
            fn()
            if name not in ("probes", "models") or CFG.get("anthropic_api_key"):
                job_ok(name)
        except Exception as e:
            job_err(name, e)
            if os.environ.get("CLAUDE_PULSE_DEBUG"):
                traceback.print_exc()
        finally:
            with lock:
                running.discard(name)

    while True:
        now = time.time()
        for name, fn, interval in JOBS:
            with lock:
                busy = name in running
            if not busy and now >= next_run[name]:
                next_run[name] = now + interval()
                with lock:
                    running.add(name)
                threading.Thread(target=run, args=(name, fn), daemon=True, name=name).start()
        if KICK.wait(5):
            KICK.clear()
            for name in ("stock", "peers", "status", "incidents", "news", "plan_usage", "backfill", "daily"):
                next_run[name] = 0


# --------------------------------------------------------------------------- API

def api_state():
    with STATE_LOCK:
        s = json.loads(json.dumps(STATE))
    ms = market_state()
    return {
        "version": VERSION,
        "now": int(time.time()),
        "market": {"state": ms, "next_open": next_market_open() if ms != "open" else None},
        "config": {"ticker": CFG["ticker"], "ipo_price": CFG["ipo_price"], "ipo_date": CFG.get("ipo_date"),
                   "has_key": bool(CFG.get("anthropic_api_key")), "probe_interval_min": CFG.get("probe_interval_min"),
                   "has_or_key": bool(CFG.get("openrouter_api_key")),
                   "peak_hours": CFG.get("peak_hours"),
                   "platform": "windows" if IS_WIN else "linux",
                   "offline": bool(CFG.get("offline"))},
        "quote": s["quote"],
        "peers": s["peers"],
        "status": s["status"],
        "recent_incidents": s["recent_incidents"],
        "upcoming_maint": s["upcoming_maint"],
        "probes": probe_summary(),
        "speed": or_summary(),
        "rate_limits": s["rate_limits"],
        # Anthropic's own model list when a key is set, otherwise OpenRouter's public list.
        "available_models": s["available_models"] or s["or_models"],
        "models_source": "Anthropic API" if s["available_models"] else ("OpenRouter" if s["or_models"] else None),
        "news": s["news"],
        "usage": usage_view(),
        "plan_usage": s.get("plan_usage"),
        "jobs": s["jobs"],
        "update": s.get("update"),
    }


def usage_hist(start):
    out = {}
    for ts, meter, pct in DB_.query("SELECT ts, meter, pct FROM usage_hist WHERE ts>=? ORDER BY ts", (start,)):
        out.setdefault(meter, []).append([ts, pct])
    return out


def api_history(days):
    days = max(1, min(30, days))
    end = int(time.time())
    start = end - days * 86400
    sym = CFG["ticker"]
    # Stock goes back further so the page can show the last N *trading* days.
    stock = DB_.query("SELECT ts, price, volume FROM stock WHERE symbol=? AND ts>=? ORDER BY ts",
                      (sym, end - max(days, 10) * 86400))
    daily = DB_.query("SELECT day, open, high, low, close, volume FROM daily WHERE symbol=? ORDER BY day", (sym,))
    probes = {}
    for ts, model, ok, ttft, tps in DB_.query(
            "SELECT ts, model, ok, ttft, tps FROM probe WHERE ts>=? ORDER BY ts", (start,)):
        probes.setdefault(model, []).append([ts, ok, ttft, tps])
    return {
        "start": start, "end": end,
        "stock": [[t, p, v] for t, p, v in stock],
        "daily": [list(r) for r in daily],
        "probes": probes,
        "orstats": {m: [list(p) for p in pts] for m, pts in or_series(start).items()},
        "usage_hist": usage_hist(start),
        "timeline": build_timeline(start, end),
        "hourly": hourly_profile(),
        "load_grid": load_grid(),
    }


LAST_REFRESH = [0.0]


class Handler(BaseHTTPRequestHandler):
    server_version = "ClaudePulse/" + VERSION

    def log_message(self, fmt, *args):
        if os.environ.get("CLAUDE_PULSE_DEBUG"):
            super().log_message(fmt, *args)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as f:
                body = f.read()
        except FileNotFoundError:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self.send_file(os.path.join(STATIC_DIR, "index.html"), "text/html; charset=utf-8")
            if u.path == "/icon.svg":
                return self.send_file(os.path.join(STATIC_DIR, "icon.svg"), "image/svg+xml")
            if u.path == "/api/state":
                return self.send_json(api_state())
            if u.path == "/api/history":
                q = urllib.parse.parse_qs(u.query)
                return self.send_json(api_history(int((q.get("days") or ["4"])[0])))
            if u.path == "/api/health":
                return self.send_json({"ok": True, "version": VERSION})
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 10000:
                return self.send_json({"error": "too large"}, 413)
            payload = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/usage":
                return self.send_json(save_usage(payload))
            if u.path == "/api/update/check":
                check_update()
                return self.send_json({"version": VERSION, "update": STATE.get("update")})
            if u.path == "/api/update":
                return self.send_json(do_update())
            if u.path == "/api/refresh":
                if time.time() - LAST_REFRESH[0] > 20:
                    LAST_REFRESH[0] = time.time()
                    KICK.set()
                    return self.send_json({"ok": True})
                return self.send_json({"ok": False, "error": "Refreshed moments ago, try again shortly."})
            self.send_error(404)
        except Exception as e:
            self.send_json({"error": str(e)}, 400)


class PulseServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR would let a second copy bind the same port; use exclusive binding instead.
    allow_reuse_address = not IS_WIN

    def server_bind(self):
        if IS_WIN and hasattr(__import__("socket"), "SO_EXCLUSIVEADDRUSE"):
            import socket
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def bind_server():
    """Bind the port, waiting a little if an old copy (e.g. during an update restart) still holds it."""
    last = None
    for _ in range(20):
        try:
            return PulseServer((CFG["bind"], int(CFG["port"])), Handler)
        except OSError as e:
            last = e
            time.sleep(1)
    sys.exit(f"Claude Pulse: port {CFG['port']} is busy ({last}). Is another copy already running?")


def main():
    srv = bind_server()
    threading.Thread(target=scheduler, daemon=True, name="scheduler").start()
    print(f"Claude Pulse {VERSION} on http://{CFG['bind']}:{CFG['port']}  (data: {DATA_DIR})", flush=True)
    if not CFG.get("anthropic_api_key"):
        print("No Anthropic API key set: model speed probes are off.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
