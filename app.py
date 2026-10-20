"""
the aesthetic edge — Kalshi Sports Picks Dashboard
where sports strategy meets clean aesthetics.

Run with:  streamlit run app.py   (or double-click "Start Aesthetic Edge.command")

What it does
  * Pulls open sports markets straight from Kalshi's PUBLIC market-data API
    (no login, no API key, no account access).
  * Covers game lines (winner / spread / total), player props, team props,
    game props, and optionally futures & awards.
  * Filter by sport (ALL, NBA, NFL, ...) and by bet type.
  * Gives every market a verdict: "YES — pick this" or "NO — skip this".

What it does NOT do
  * It never logs into Kalshi and never places a trade. You place picks yourself.

How verdicts work (rule-based)
  A Kalshi price is the crowd's estimate of the chance something happens
  ($0.72 ~ 72%). A market earns "YES — pick this" only when:
    1. in a one-winner market (like "who wins the game"), it's the favorite;
       in a yes/no market (like a player prop), the likelier side is taken,
    2. that side's chance is between your "at least" and "at most" bars
       (the top bar skips near-certain picks that pay pennies),
    3. buy/sell prices are close together (a real, active market),
    4. enough contracts have traded,
    5. it's the safest qualifying line for that player/game (no stacking
       10+, 15+, 20+ points for the same player).
  Favorites still lose sometimes. This follows the market; it doesn't beat it.
"""

import html
import re
from zoneinfo import ZoneInfo
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# PAGE SETUP (must be the first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(page_title="the aesthetic edge", page_icon="🎀", layout="wide")

# ---------------------------------------------------------------------------
# SETTINGS
# ---------------------------------------------------------------------------
KALSHI_API = "https://external-api.kalshi.com/trade-api/v2"   # public market data
LOCAL_TZ = "America/Chicago"                                     # times shown in Central
MAX_BOARD_ROWS = 200                                             # keep the table snappy

# Sport detection from Kalshi series tickers (KX + league + type, e.g. KXNBAPTS).
# Order matters: longer / more specific prefixes first.
SPORT_PREFIXES = [
    ("KXWNBA", "WNBA"),
    ("KXNBA", "NBA"),
    ("KXNFL", "NFL"),
    ("KXNHL", "NHL"),
    ("KXMLB", "MLB"),
    ("KXNCAAF", "College Football"),
    ("KXCFB", "College Football"),
    ("KXNCAAMB", "College Basketball"),
    ("KXNCAAWB", "College Basketball"),
    ("KXNCAAB", "College Basketball"),
    ("KXEPL", "Soccer"),
    ("KXMLS", "Soccer"),
    ("KXUCL", "Soccer"),
    ("KXLALIGA", "Soccer"),
    ("KXSERIEA", "Soccer"),
    ("KXBUNDESLIGA", "Soccer"),
    ("KXLIGUE1", "Soccer"),
    ("KXUFC", "UFC / MMA"),
    ("KXATP", "Tennis"),
    ("KXWTA", "Tennis"),
    ("KXPGA", "Golf"),
    ("KXF1", "F1"),
    ("KXNASCAR", "NASCAR"),
]
SPORT_WORDS = [  # fallback: words in the series title
    ("wnba", "WNBA"), ("nba", "NBA"), ("pro basketball", "NBA"),
    ("nfl", "NFL"), ("pro football", "NFL"), ("nhl", "NHL"), ("hockey", "NHL"),
    ("mlb", "MLB"), ("baseball", "MLB"),
    ("college football", "College Football"), ("ncaaf", "College Football"),
    ("college basketball", "College Basketball"), ("march madness", "College Basketball"),
    ("premier league", "Soccer"), ("mls", "Soccer"), ("champions league", "Soccer"),
    ("soccer", "Soccer"), ("ufc", "UFC / MMA"), ("tennis", "Tennis"), ("golf", "Golf"),
    ("formula 1", "F1"), ("nascar", "NASCAR"),
]
SPORT_ORDER = ["NFL", "NBA", "WNBA", "NHL", "MLB", "College Football",
               "College Basketball", "Soccer", "UFC / MMA", "Tennis", "Golf", "F1", "NASCAR"]

# Bet-type detection (ticker code after the league, then title words)
BET_TYPES = ["Game lines", "Player props", "Team props", "Game props", "Futures & awards"]
DEFAULT_BET_TYPES = ["Game lines", "Player props", "Team props", "Game props"]

CODE_RULES = [  # (substring in ticker code, bet type) — checked in order
    ("TEAMTOTAL", "Team props"), ("TEAMPTS", "Team props"), ("TEAM", "Team props"),
    ("1H", "Game props"), ("2H", "Game props"), ("HALF", "Game props"), ("Q1", "Game props"),
    ("QTR", "Game props"), ("1P", "Game props"), ("1Q", "Game props"), ("F5", "Game props"),
    ("SPREAD", "Game lines"), ("TOTAL", "Game lines"),
    ("FIRST", "Game props"), ("OVERTIME", "Game props"), ("MARGIN", "Game props"), ("BTTS", "Game props"),
    ("CHAMP", "Futures & awards"), ("MVP", "Futures & awards"), ("ROY", "Futures & awards"),
    ("DPOY", "Futures & awards"), ("AWARD", "Futures & awards"), ("WINS", "Futures & awards"),
    ("PLAYOFF", "Futures & awards"), ("DIV", "Futures & awards"), ("CONF", "Futures & awards"),
    ("SERIES", "Futures & awards"), ("SUPERBOWL", "Futures & awards"), ("FINALS", "Futures & awards"),
    ("DRAFT", "Futures & awards"), ("COACH", "Futures & awards"),
    ("PTS", "Player props"), ("REB", "Player props"), ("AST", "Player props"), ("3PT", "Player props"),
    ("BLK", "Player props"), ("STL", "Player props"), ("PRA", "Player props"),
    ("PASS", "Player props"), ("RUSH", "Player props"), ("REC", "Player props"), ("YDS", "Player props"),
    ("TD", "Player props"), ("SACK", "Player props"), ("INT", "Player props"),
    ("HR", "Player props"), ("HIT", "Player props"), ("KS", "Player props"), ("SO", "Player props"),
    ("RBI", "Player props"), ("SOG", "Player props"), ("GOAL", "Player props"), ("SAVE", "Player props"),
    ("GAME", "Game lines"), ("SPREAD", "Game lines"), ("TOTAL", "Game lines"),
    ("MATCH", "Game lines"), ("FIGHT", "Game lines"), ("WIN", "Game lines"),
]
TITLE_RULES = [
    ("team total", "Team props"),
    ("first half", "Game props"), ("quarter", "Game props"), ("period", "Game props"),
    ("overtime", "Game props"), ("first to score", "Game props"),
    ("champion", "Futures & awards"), ("mvp", "Futures & awards"), ("award", "Futures & awards"),
    ("season", "Futures & awards"), ("playoff", "Futures & awards"), ("draft", "Futures & awards"),
    ("player", "Player props"), ("points", "Player props"), ("yards", "Player props"),
    ("touchdown", "Player props"), ("rebounds", "Player props"), ("assists", "Player props"),
    ("strikeouts", "Player props"), ("home run", "Player props"),
    ("game", "Game lines"), ("spread", "Game lines"), ("total", "Game lines"),
    ("match", "Game lines"), ("fight", "Game lines"),
]

# Used only if Kalshi's series list can't be read
FALLBACK_SERIES = [
    {"ticker": "KXNFLGAME", "title": "Pro Football Game"},
    {"ticker": "KXNFLSPREAD", "title": "Pro Football Spread"},
    {"ticker": "KXNFLTOTAL", "title": "Pro Football Total"},
    {"ticker": "KXNBAGAME", "title": "Pro Basketball Game"},
    {"ticker": "KXNBASPREAD", "title": "Pro Basketball Spread"},
    {"ticker": "KXNBATOTAL", "title": "Pro Basketball Total"},
    {"ticker": "KXNBAPTS", "title": "NBA Player Points"},
    {"ticker": "KXNBAREB", "title": "NBA Player Rebounds"},
    {"ticker": "KXNBAAST", "title": "NBA Player Assists"},
    {"ticker": "KXNHLGAME", "title": "Pro Hockey Game"},
    {"ticker": "KXMLBGAME", "title": "Pro Baseball Game"},
    {"ticker": "KXNCAAFGAME", "title": "College Football Game"},
]

# ---------------------------------------------------------------------------
# BRAND PALETTE
# ---------------------------------------------------------------------------
NUDE = "#FDFBF7"       # page background
PINK = "#FFB7B2"       # primary accent (buttons, table headers, chips)
SAGE = "#B5E2B8"       # secondary highlight (sidebar, YES badges)
BROWN = "#4A3728"      # main text + structure
ESPRESSO = "#2B1E17"   # headings / deepest text

# ---------------------------------------------------------------------------
# CUSTOM CSS — plain string so CSS braces don't clash with Python
# ---------------------------------------------------------------------------
CUSTOM_CSS = """
<style>
/* Fonts: Sacramento = soft, thin cursive for the logo/header; Poppins = clean body */
@import url('https://fonts.googleapis.com/css2?family=Sacramento&family=Poppins:wght@300;400;500;600&display=swap');

/* NUDE background (#FDFBF7) + EARTH BROWN (#4A3728) text everywhere */
.stApp { background-color: #FDFBF7; color: #4A3728; font-family: 'Poppins', sans-serif; }
html, body, p, span, label, div, li, td, th, button { font-family: 'Poppins', sans-serif; }
.stApp p, .stApp label, .stApp li, .stApp span { color: #4A3728; }

header[data-testid="stHeader"] { background: transparent; }
footer { visibility: hidden; }

/* ESPRESSO (#2B1E17) headings */
h1, h2, h3, h4 { color: #2B1E17 !important; font-family: 'Poppins', sans-serif; }

/* SIDEBAR: soft SAGE (#B5E2B8) wash */
section[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #EEF8EF 0%, #FDFBF7 100%);
    border-right: 1px solid rgba(74, 55, 40, 0.12);
}
section[data-testid="stSidebar"] * { color: #4A3728; }

/* ---------- HEADER / LOGO BANNER (centered, soft lowercase cursive) ---------- */
.ae-banner {
    text-align: center;
    background: linear-gradient(135deg, #FFE9E7 0%, #FDFBF7 50%, #E9F6EA 100%);  /* PINK → NUDE → SAGE */
    border: 1px solid rgba(255, 183, 178, 0.6);                                  /* PINK border */
    border-radius: 28px;
    padding: 26px 16px 20px 16px;
    margin-bottom: 22px;
    box-shadow: 0 8px 24px rgba(74, 55, 40, 0.06);
}
.ae-banner svg { display: block; margin: 0 auto; max-width: 100%; height: auto; }
.ae-banner h1 {
    text-align: center;
    font-family: 'Sacramento', cursive !important;   /* soft cursive */
    text-transform: lowercase;                         /* all lower case */
    font-weight: 400;
    color: #4A3728 !important;                         /* EARTH BROWN */
    font-size: clamp(3rem, 7vw, 5rem);
    line-height: 1.05;
    letter-spacing: 0.01em;
    margin: 4px 0 0 0;
    padding: 0;
}
.ae-banner .ae-tagline {
    text-align: center;
    font-family: 'Sacramento', cursive;                /* soft cursive tagline */
    text-transform: lowercase;
    color: #2B1E17;                                    /* ESPRESSO */
    font-size: clamp(1.6rem, 3.4vw, 2.3rem);
    margin: 0;
}
.ae-banner .ae-date {
    margin-top: 8px; font-size: 0.78rem; letter-spacing: 0.18em;
    text-transform: lowercase; color: #4A3728; opacity: 0.7;
}

/* ---------- DROPDOWNS + CALENDAR ---------- */
div[data-baseweb="select"] > div, div[data-testid="stDateInput"] div[data-baseweb="input"] {
    background-color: #FFFFFF !important;
    border: 1px solid #FFB7B2 !important;              /* PINK outline */
    border-radius: 14px !important;
}
div[data-baseweb="select"] > div:hover, div[data-testid="stDateInput"] div[data-baseweb="input"]:hover {
    border-color: #B5E2B8 !important;                  /* SAGE on hover */
}
div[data-testid="stSelectbox"] label p, div[data-testid="stDateInput"] label p {
    font-weight: 600; color: #2B1E17 !important;       /* ESPRESSO labels */
}
/* Calendar pop-up: PINK selected day */
div[data-baseweb="calendar"] [aria-selected="true"] div, div[data-baseweb="calendar"] [aria-selected="true"]::after {
    background-color: #FFB7B2 !important; border-color: #FFB7B2 !important; color: #2B1E17 !important;
}

/* ---------- "SHOWING" FILTER SUMMARY ---------- */
.ae-showing { margin: 6px 0 2px 2px; font-size: 0.85rem; color: #4A3728; }
.ae-showing b { color: #2B1E17; margin-right: 6px; }

/* ---------- FOOTER CREDIT ---------- */
.ae-footer {
    text-align: center; margin: 40px 0 10px 0; padding-top: 18px;
    border-top: 1px solid rgba(255, 183, 178, 0.6);    /* PINK hairline */
}
.ae-footer .ae-credit {
    font-family: 'Sacramento', cursive; font-size: 2rem; color: #4A3728;   /* soft cursive */
}
.ae-footer .ae-small { font-size: 0.72rem; opacity: 0.65; letter-spacing: 0.08em; }

/* ---------- CARDS ---------- */
div[data-testid="stVerticalBlockBorderWrapper"] {
    background-color: #FFFFFF;
    border: 1px solid rgba(74, 55, 40, 0.10) !important;
    border-radius: 22px !important;                    /* rounded corners */
    padding: 10px 8px;
    box-shadow: 0 6px 18px rgba(74, 55, 40, 0.05);
}
.ae-section-title { font-size: 1.15rem; font-weight: 600; color: #2B1E17; margin: 4px 0 12px 0; }

/* ---------- BOARD TABLE ---------- */
.ae-table-wrap { overflow-x: auto; max-height: 820px; overflow-y: auto; border-radius: 16px; }
table.ae-table { width: 100%; border-collapse: separate; border-spacing: 0; font-size: 0.84rem; }
table.ae-table thead th {
    position: sticky; top: 0; z-index: 1;
    background-color: #FFB7B2;                         /* PINK header row */
    color: #2B1E17; text-align: left; font-weight: 600;
    padding: 12px 14px; white-space: nowrap;
}
table.ae-table thead th:first-child { border-top-left-radius: 14px; }
table.ae-table thead th:last-child  { border-top-right-radius: 14px; }
table.ae-table tbody td {
    padding: 10px 14px; color: #4A3728; vertical-align: top;
    border-bottom: 1px solid rgba(74, 55, 40, 0.08);
}
table.ae-table tbody tr:nth-child(even) td { background-color: #FBF6F0; }  /* nude stripe */
table.ae-table tbody tr:hover td { background-color: #EEF8EF; }            /* SAGE hover */
.ae-why { font-size: 0.74rem; opacity: 0.8; }

/* ---------- BADGES ---------- */
.ae-badge {
    display: inline-block; padding: 3px 11px; border-radius: 999px;
    font-size: 0.72rem; font-weight: 600; margin: 0 6px 3px 0; white-space: nowrap;
}
.ae-yes  { background-color: #B5E2B8; color: #2B1E17; }   /* SAGE = YES, pick this */
.ae-no   { background-color: #FFE3E0; color: #7A4B45; }   /* soft PINK = NO, skip */
.ae-pink { background-color: #FFB7B2; color: #2B1E17; }   /* PINK sport tag */
.ae-tan  { background-color: #F1E6DA; color: #4A3728; }   /* tan bet-type tag */

.ae-pick-meta { font-size: 0.8rem; color: #4A3728; opacity: 0.9; margin: -6px 0 16px 32px; line-height: 1.6; }

/* ---------- WIDGETS ---------- */
.stButton > button, .stDownloadButton > button {
    background-color: #FFB7B2; color: #2B1E17; border: none;   /* PINK pill buttons */
    border-radius: 999px; padding: 0.5rem 1.4rem; font-weight: 600;
}
.stButton > button:hover, .stDownloadButton > button:hover { background-color: #B5E2B8; color: #2B1E17; }

div[data-testid="stMetric"] {
    background-color: #FFFFFF; border: 1px solid rgba(181, 226, 184, 0.9);  /* SAGE border */
    border-radius: 18px; padding: 12px 16px;
}
div[data-testid="stMetricValue"] { color: #2B1E17; }
.stCode, pre { border-radius: 16px !important; }
</style>
"""
st.markdown(CUSTOM_CSS, unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# LOGO — inline SVG monogram badge, lowercase "ae" in the soft cursive
# ---------------------------------------------------------------------------
LOGO_SVG = (
    '<svg width="124" height="124" viewBox="0 0 132 132" xmlns="http://www.w3.org/2000/svg" '
    'role="img" aria-label="the aesthetic edge logo">'
    '<circle cx="66" cy="66" r="62" fill="#FDFBF7" stroke="#B5E2B8" stroke-width="4"/>'
    '<circle cx="66" cy="66" r="52" fill="#FFB7B2"/>'
    '<circle cx="66" cy="66" r="52" fill="none" stroke="#4A3728" stroke-width="1" '
    'stroke-dasharray="2 5" opacity="0.55"/>'
    '<text x="66" y="84" text-anchor="middle" font-family="Sacramento, cursive" '
    'font-size="62" fill="#2B1E17">ae</text>'
    '<path d="M42 98 Q66 107 90 98" stroke="#4A3728" stroke-width="1.4" fill="none" '
    'stroke-linecap="round"/>'
    '<circle cx="66" cy="22" r="3" fill="#B5E2B8"/>'
    "</svg>"
)


def render_header() -> None:
    today = pd.Timestamp.now(tz=LOCAL_TZ).strftime("%A · %B %d, %Y").lower()
    st.markdown(
        '<div class="ae-banner">'
        f"{LOGO_SVG}"
        '<h1 style="text-align:center; color:#4A3728;">the aesthetic edge</h1>'
        '<p class="ae-tagline">where sports strategy meets clean aesthetics.</p>'
        f'<div class="ae-date">{today}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# CLASSIFY KALSHI SERIES BY SPORT + BET TYPE
# ---------------------------------------------------------------------------
def detect_sport(ticker: str, title: str):
    t = ticker.upper()
    for prefix, sport in SPORT_PREFIXES:
        if t.startswith(prefix):
            return sport, t[len(prefix):]
    low = title.lower()
    for word, sport in SPORT_WORDS:
        if word in low:
            return sport, t[2:] if t.startswith("KX") else t
    return None, ""


def detect_bet_type(code: str, title: str) -> str:
    for key, kind in CODE_RULES:
        if key in code:
            return kind
    low = title.lower()
    for word, kind in TITLE_RULES:
        if word in low:
            return kind
    return "Game props"


def classify(series: list[dict]) -> list[dict]:
    out = []
    for s in series:
        ticker, title = s.get("ticker") or "", s.get("title") or ""
        sport, code = detect_sport(ticker, title)
        if not sport:
            continue
        out.append({"ticker": ticker, "title": title or ticker,
                    "sport": sport, "bet_type": detect_bet_type(code, title)})
    return out


# ---------------------------------------------------------------------------
# KALSHI PUBLIC DATA (read-only; no keys, no login)
# ---------------------------------------------------------------------------
@st.cache_resource
def http_session() -> requests.Session:
    """One shared connection pool, reused for every request (much faster)."""
    sess = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16)
    sess.mount("https://", adapter)
    sess.headers.update({"Accept": "application/json"})
    return sess


def kalshi_get(path: str, params: dict | None = None) -> dict:
    for attempt in range(3):
        resp = http_session().get(f"{KALSHI_API}{path}", params=params or {}, timeout=10)
        if resp.status_code == 429 and attempt < 2:   # rate-limited: wait a beat, retry
            time.sleep(1.0 + attempt)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return resp.json()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sports_series() -> list[dict]:
    data = kalshi_get("/series", {"category": "Sports"})
    return classify(data.get("series") or [])


def _open_events(series_ticker: str, max_pages: int = 3) -> list[dict]:
    events, cursor = [], ""
    for _ in range(max_pages):
        params = {"series_ticker": series_ticker, "status": "open",
                  "with_nested_markets": "true", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = kalshi_get("/events", params)
        events.extend(data.get("events") or [])
        cursor = data.get("cursor") or ""
        if not cursor:
            break
    return events


# Per-series cache shared across reruns: tapping filter chips reuses what's
# already loaded, and only brand-new series are downloaded.
CACHE_SECONDS = 300        # refresh prices every 5 minutes
ERROR_RETRY_SECONDS = 60   # retry a failed list after a minute


@st.cache_resource
def market_store() -> dict:
    return {"lock": threading.Lock(), "data": {}}   # ticker -> (fetched_at, rows, error)


def load_markets(series_meta: tuple) -> tuple[list[dict], list[str]]:
    """series_meta = ((ticker, title, sport, bet_type), ...). Uses cache, fetches the rest in parallel."""
    store = market_store()
    now = time.time()
    todo = []
    for meta in series_meta:
        hit = store["data"].get(meta[0])
        max_age = ERROR_RETRY_SECONDS if (hit and hit[2]) else CACHE_SECONDS
        if not hit or now - hit[0] > max_age:
            todo.append(meta)

    if todo:
        def work(meta):
            ticker, title, sport, bet_type = meta
            try:
                return meta, markets_to_rows(_open_events(ticker), sport, bet_type), None
            except Exception as exc:
                return meta, [], f"{title}: {exc}"

        bar = st.progress(0.0, text=f"Loading Kalshi markets… 0 of {len(todo)}")
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(work, m) for m in todo]
            for i, fut in enumerate(as_completed(futures), start=1):
                meta, got, err = fut.result()
                with store["lock"]:
                    store["data"][meta[0]] = (time.time(), got, err)
                if i % 3 == 0 or i == len(todo):
                    bar.progress(i / len(todo), text=f"Loading Kalshi markets… {i} of {len(todo)}")
        bar.empty()

    rows, failures = [], []
    for meta in series_meta:
        _, got, err = store["data"].get(meta[0], (0, [], None))
        rows.extend(got)
        if err:
            failures.append(err)
    return rows, failures


def to_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


MONTHS = {m: i for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], start=1)}


def game_date_from_ticker(event_ticker: str):
    """Kalshi event tickers carry the game date, e.g. KXNBAGAME-26OCT21BOSNYK -> 2026-10-21."""
    parts = event_ticker.split("-")
    if len(parts) < 2:
        return None
    m = re.match(r"^(\d{2})([A-Z]{3})(\d{2})", parts[1].upper())
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(2000 + int(m.group(1)), MONTHS[m.group(2)], int(m.group(3))).isoformat()
    except ValueError:
        return None


def markets_to_rows(events: list[dict], sport: str, bet_type: str) -> list[dict]:
    """One row per Kalshi market (a yes/no contract)."""
    rows = []
    for ev in events:
        game = ev.get("title") or ev.get("sub_title") or ev.get("event_ticker", "")
        ev_ticker = ev.get("event_ticker", "")
        parts = ev_ticker.split("-")
        game_key = f"{sport}|{parts[1]}" if len(parts) >= 2 else f"{sport}|{game}"
        game_date = game_date_from_ticker(ev_ticker)
        exclusive = bool(ev.get("mutually_exclusive")) or (
            bet_type == "Game lines" and "GAME" in (ev.get("series_ticker") or "").upper()
        )
        for m in ev.get("markets") or []:
            yb, ya = to_float(m.get("yes_bid_dollars")), to_float(m.get("yes_ask_dollars"))
            nb, na = to_float(m.get("no_bid_dollars")), to_float(m.get("no_ask_dollars"))
            last = to_float(m.get("last_price_dollars"))
            if yb > 0 and ya > 0 and ya >= yb:
                p_yes, spread = (yb + ya) / 2, ya - yb
            elif last > 0:
                p_yes, spread = last, None
            else:
                continue  # no price yet — nothing to judge
            ticker = m.get("ticker", "")
            rows.append({
                "event": ev.get("event_ticker", ""), "game": game, "sport": sport,
                "bet_type": bet_type, "exclusive": exclusive,
                "label": m.get("yes_sub_title") or m.get("title") or ticker,
                "p_yes": p_yes, "spread": spread,
                "yes_cost": ya if ya > 0 else p_yes,
                "no_cost": na if na > 0 else (1 - yb if yb > 0 else 1 - p_yes),
                "volume": to_float(m.get("volume_fp")),
                "when": m.get("expected_expiration_time") or m.get("close_time"),
                "ticker": ticker, "game_key": game_key, "game_date": game_date,
                "line_group": ticker.rsplit("-", 1)[0] if ticker.count("-") >= 3 else ev.get("event_ticker", ""),
            })
    return rows


def demo_rows() -> list[dict]:
    """Sample board so the app still works if Kalshi can't be reached."""
    now = datetime.now(timezone.utc)
    t = lambda h: (now + timedelta(hours=h)).isoformat()
    rows = []

    def add(ev, game, sport, kind, label, p, hrs, spr=0.01, vol=20000, exclusive=False, group=None):
        rows.append({
            "event": ev, "game": game, "sport": sport, "bet_type": kind, "exclusive": exclusive,
            "label": label, "p_yes": p, "spread": spr, "yes_cost": p + spr / 2,
            "no_cost": 1 - p + spr / 2, "volume": vol, "when": t(hrs),
            "ticker": f"DEMO-{ev}-{label}", "line_group": group or ev,
            "game_key": f"{sport}|{game}",
            "game_date": (now + timedelta(hours=hrs)).astimezone(ZoneInfo(LOCAL_TZ)).date().isoformat()
            if kind != "Futures & awards" else None,
        })

    add("KCLV", "Kansas City at Las Vegas", "NFL", "Game lines", "Kansas City", 0.78, 5, exclusive=True)
    add("KCLV", "Kansas City at Las Vegas", "NFL", "Game lines", "Las Vegas", 0.23, 5, exclusive=True)
    add("KCLVS", "Kansas City at Las Vegas", "NFL", "Game lines", "Kansas City wins by over 6.5", 0.57, 5)
    add("KCLVT", "Kansas City at Las Vegas", "NFL", "Game lines", "Over 44.5 points scored", 0.41, 5)
    add("KCTD", "Kansas City at Las Vegas", "NFL", "Player props", "Travis Kelce: anytime TD", 0.38, 5)
    add("KCYD", "Kansas City at Las Vegas", "NFL", "Player props", "Patrick Mahomes: 225+ passing yds", 0.71, 5, group="MAHOMES")
    add("KCYD", "Kansas City at Las Vegas", "NFL", "Player props", "Patrick Mahomes: 250+ passing yds", 0.58, 5, group="MAHOMES")
    add("BOSNY", "Boston at New York", "NBA", "Game lines", "Boston", 0.66, 3, spr=0.02, exclusive=True)
    add("BOSNY", "Boston at New York", "NBA", "Game lines", "New York", 0.35, 3, spr=0.02, exclusive=True)
    add("BOSPTS", "Boston at New York", "NBA", "Player props", "Jayson Tatum: 20+ points", 0.74, 3, group="TATUM")
    add("BOSPTS", "Boston at New York", "NBA", "Player props", "Jayson Tatum: 25+ points", 0.55, 3, group="TATUM")
    add("BOSPTS", "Boston at New York", "NBA", "Player props", "Jayson Tatum: 15+ points", 0.93, 3, group="TATUM")
    add("BOSREB", "Boston at New York", "NBA", "Player props", "Karl-Anthony Towns: 10+ rebounds", 0.31, 3)
    add("BOSTT", "Boston at New York", "NBA", "Team props", "Boston: 110+ team points", 0.69, 3)
    add("BOS1H", "Boston at New York", "NBA", "Game props", "Boston leads at halftime", 0.61, 3)
    add("PHXLAL", "Phoenix at Los Angeles L", "NBA", "Game lines", "Los Angeles L", 0.53, 4, exclusive=True)
    add("PHXLAL", "Phoenix at Los Angeles L", "NBA", "Game lines", "Phoenix", 0.48, 4, exclusive=True)
    add("NYRBOS", "NY Rangers at Boston", "NHL", "Game lines", "Boston", 0.58, 6, exclusive=True)
    add("NYRBOS", "NY Rangers at Boston", "NHL", "Game lines", "NY Rangers", 0.43, 6, exclusive=True)
    add("LSUALA", "LSU at Alabama", "College Football", "Game lines", "Alabama", 0.81, 28, exclusive=True)
    add("LSUALA", "LSU at Alabama", "College Football", "Game lines", "LSU", 0.20, 28, exclusive=True)
    add("NYMLAD", "NY Mets at Los Angeles D", "MLB", "Game lines", "Los Angeles D", 0.69, 2, spr=0.09, exclusive=True)
    add("NYMLAD", "NY Mets at Los Angeles D", "MLB", "Game lines", "NY Mets", 0.32, 2, spr=0.09, exclusive=True)
    add("NYMHR", "NY Mets at Los Angeles D", "MLB", "Player props", "Shohei Ohtani: home run", 0.27, 2, vol=300)
    add("NFLMVP", "NFL MVP", "NFL", "Futures & awards", "Josh Allen", 0.22, 40)
    return rows


# ---------------------------------------------------------------------------
# VERDICT LOGIC — "YES, pick this" / "NO, skip this"
# ---------------------------------------------------------------------------
def judge(df: pd.DataFrame, min_prob: float, max_prob: float,
          max_spread: float, min_volume: float) -> pd.DataFrame:
    df = df.copy()

    # Which side would we take?
    #  - one-winner markets: the YES side of the favorite only
    #  - yes/no markets (props, spreads, totals): whichever side is likelier
    df["rank"] = df.groupby("event")["p_yes"].rank(ascending=False, method="first")
    fav = (df[df["rank"] == 1][["event", "label", "p_yes"]]
           .rename(columns={"label": "fav_label", "p_yes": "fav_p"}))
    df = df.merge(fav, on="event", how="left")

    take_no = (~df["exclusive"]) & (df["p_yes"] < 0.5)
    df["side"] = take_no.map({True: "NO", False: "YES"})
    df["prob"] = df["p_yes"].where(~take_no, 1 - df["p_yes"])
    df["cost"] = df["yes_cost"].where(~take_no, df["no_cost"])

    verdicts, reasons = [], []
    for r in df.itertuples():
        pct = round(r.prob * 100)
        if r.exclusive and r.rank != 1:
            v, why = "NO", f"Not the favorite — Kalshi favors {r.fav_label} ({round(r.fav_p * 100)}%)"
        elif r.prob < min_prob:
            v, why = "NO", f"Too close to call — {pct}%, under your {round(min_prob * 100)}% bar"
        elif r.prob > max_prob:
            v, why = "NO", f"Too safe — {pct}% but only pays ~{max(1, round((1 - r.cost) * 100))}¢ profit"
        elif r.spread is not None and pd.notna(r.spread) and r.spread > max_spread:
            v, why = "NO", f"Buy/sell prices {round(r.spread * 100)}¢ apart — hard to get a fair price"
        elif r.volume < min_volume:
            v, why = "NO", f"Barely traded ({int(r.volume):,} contracts)"
        else:
            v, why = "YES", f"{pct}% chance with an active market"
        verdicts.append(v)
        reasons.append(why)
    df["verdict"], df["why"] = verdicts, reasons

    # One pick per player/line ladder: keep the safest qualifying rung
    yes = df[df["verdict"] == "YES"].sort_values("prob", ascending=False)
    dupes = yes.index[yes.duplicated("line_group", keep="first")]
    df.loc[dupes, "verdict"] = "NO"
    df.loc[dupes, "why"] = "Same player/line — a safer version is already on your slip"
    return df


def fmt_time(ts) -> str:
    if pd.isna(ts):
        return "—"
    return ts.tz_convert(LOCAL_TZ).strftime("%a %b %d · %I:%M %p").replace(" 0", " ") + " CT"


def bet_text(r) -> str:
    """Plain-English version of what to buy."""
    if r.side == "YES":
        return f"Bet YES · {r.label}"
    return f"Bet NO · {r.label} (it won't happen)"


def board_html(df: pd.DataFrame) -> str:
    rows = []
    for r in df.itertuples():
        badge = ('<span class="ae-badge ae-yes">YES — pick this</span>' if r.verdict == "YES"
                 else '<span class="ae-badge ae-no">NO — skip this</span>')
        rows.append(
            "<tr>"
            f"<td><b>{html.escape(str(r.game))}</b><br>"
            f'<span class="ae-why">{html.escape(r.sport)} · settles ~{fmt_time(r.when_ts)}</span></td>'
            f'<td><span class="ae-badge ae-tan">{html.escape(r.bet_type)}</span><br>{html.escape(bet_text(r))}</td>'
            f"<td>{round(r.prob * 100)}%</td>"
            f'<td>{badge}<br><span class="ae-why">{html.escape(r.why)}</span></td>'
            "</tr>"
        )
    return ('<div class="ae-table-wrap"><table class="ae-table">'
            "<thead><tr><th>Game</th><th>Bet</th><th>Chance</th><th>Verdict</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def slip_text(picks: pd.DataFrame) -> str:
    lines = ["the aesthetic edge — my kalshi slip",
             f"games on {pd.Timestamp(pick_date).strftime('%B %d, %Y')}", ""]
    for i, r in enumerate(picks.itertuples(), start=1):
        lines.append(f"{i}. [{r.sport} · {r.bet_type}] {r.game} — {bet_text(r)} (~{round(r.cost * 100)}¢)")
    return "\n".join(lines)


def short_game_name(title: str) -> str:
    """'Boston at New York: Points' -> 'Boston at New York'."""
    return title.split(":")[0].strip() if ":" in title else title.strip()


# ---------------------------------------------------------------------------
# SIDEBAR — pick rules
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🎀 Pick rules")
    min_conf = st.slider("Say YES only at or above (chance %)", 50, 95, 65, step=5)
    max_conf = st.slider("…and at or below (skip near-locks that pay pennies)", 70, 99, 92)
    max_spread_c = st.slider("Max gap between buy/sell price (¢)", 1, 20, 6)
    min_volume = st.number_input("Min contracts traded", min_value=0, value=500, step=100)
    max_picks = st.slider("Max picks on slip", 1, 30, 10)
    show_skips = st.toggle("Show NO (skip) rows on the board", value=True)

    if st.button("🔄 Refresh from Kalshi"):
        st.cache_data.clear()
        market_store()["data"].clear()
        st.rerun()

    st.markdown("---")
    st.caption("Reads Kalshi's public prices only — no login, no API key, "
               "and it never places a trade. You enter picks yourself.")

# ---------------------------------------------------------------------------
# HEADER + FILTERS
# ---------------------------------------------------------------------------
render_header()

series_error = None
try:
    series = fetch_sports_series() or classify(FALLBACK_SERIES)
except Exception as exc:
    series_error = exc
    series = classify(FALLBACK_SERIES)

sports_available = [s for s in SPORT_ORDER if any(x["sport"] == s for x in series)]
today_local = pd.Timestamp.now(tz=LOCAL_TZ).date()
ALL_SPORTS, ALL_TYPES, ALL_GAMES = "ALL sports", "ALL bet types", "ALL games"

with st.container(border=True):
    c_date, c_sport, c_type, c_game = st.columns([1, 1, 1, 1.4], gap="medium")
    with c_date:
        pick_date = st.date_input("📅 Game date", value=today_local, format="MM/DD/YYYY", key="game_date")
    with c_sport:
        pick_sport = st.selectbox("🏆 Sport", [ALL_SPORTS] + sports_available, key="sport_pick")
    with c_type:
        pick_type = st.selectbox("🎯 Bet type", [ALL_TYPES] + BET_TYPES, key="type_pick")
    showing_slot = st.empty()

chosen_sports = sports_available if pick_sport == ALL_SPORTS else [pick_sport]
# "ALL bet types" covers everything except season-long futures (keeps loading quick)
chosen_types = DEFAULT_BET_TYPES if pick_type == ALL_TYPES else [pick_type]
futures_mode = chosen_types == ["Futures & awards"]

wanted = tuple(
    (s["ticker"], s["title"], s["sport"], s["bet_type"])
    for s in series
    if s["sport"] in chosen_sports and s["bet_type"] in chosen_types
)

# ---------------------------------------------------------------------------
# LOAD MARKETS
# ---------------------------------------------------------------------------
rows, failures, using_demo = [], [], False
if wanted:
    rows, failures = load_markets(wanted)

if not rows and (series_error is not None or (failures and len(failures) == len(wanted))):
    st.warning("Couldn't reach Kalshi right now, so you're seeing **sample games**. "
               "Check your internet connection and hit **Refresh from Kalshi**.")
    rows = [r for r in demo_rows() if r["sport"] in chosen_sports and r["bet_type"] in chosen_types]
    using_demo = True
elif failures:
    st.caption(f"{len(failures)} market list(s) didn't load — the rest are shown.")

df = pd.DataFrame(rows)
if not df.empty:
    df["when_ts"] = pd.to_datetime(df["when"], errors="coerce", utc=True)
    # Game date: from Kalshi's event ticker; otherwise the settle time in Central
    fallback = df["when_ts"].dt.tz_convert(LOCAL_TZ).dt.date.astype("string")
    df["game_day"] = df["game_date"].fillna(fallback)
    if not futures_mode:   # futures settle months out, so the calendar doesn't apply to them
        df = df[df["game_day"] == pick_date.isoformat()]

# Game dropdown — built from what's on the chosen date
game_names = {}
if not df.empty:
    lines_first = df.sort_values("bet_type", key=lambda c: c != "Game lines")
    for key, title in zip(lines_first["game_key"], lines_first["game"]):
        game_names.setdefault(key, short_game_name(str(title)))
game_keys = sorted(game_names, key=lambda k: game_names[k].lower())
with c_game:
    pick_game = st.selectbox(
        "🏟️ Game", [ALL_GAMES] + game_keys, key="game_pick",
        format_func=lambda k: k if k == ALL_GAMES else f"{game_names[k]}  ·  {k.split('|')[0]}",
    )
if pick_game != ALL_GAMES and not df.empty:
    df = df[df["game_key"] == pick_game]

# "Showing:" line so you always see what's selected
date_label = "all dates (futures)" if futures_mode else pd.Timestamp(pick_date).strftime("%a, %b %d, %Y")
chips = [
    f'<span class="ae-badge ae-tan">📅 {date_label}</span>',
    f'<span class="ae-badge ae-pink">{html.escape(pick_sport)}</span>',
    f'<span class="ae-badge ae-yes">{html.escape(pick_type)}</span>',
    f'<span class="ae-badge ae-tan">{html.escape(ALL_GAMES if pick_game == ALL_GAMES else game_names.get(pick_game, ""))}</span>',
]
showing_slot.markdown(f'<div class="ae-showing"><b>Showing:</b>{"".join(chips)}</div>', unsafe_allow_html=True)


def footer() -> None:
    st.markdown(
        '<div class="ae-footer">'
        '<div class="ae-credit">created &amp; designed by Tarnisha A.</div>'
        '<div class="ae-small">the aesthetic edge · for personal review only · not betting advice</div>'
        "</div>",
        unsafe_allow_html=True,
    )


if df.empty:
    when = "" if futures_mode else f" on {pd.Timestamp(pick_date).strftime('%b %d')}"
    st.info(f"No open Kalshi markets for these filters{when}. Try another date, ALL sports, or ALL bet types.")
    footer()
    st.stop()

judged = judge(df, min_conf / 100, max_conf / 100, max_spread_c / 100, float(min_volume))
picks = judged[judged["verdict"] == "YES"].sort_values("prob", ascending=False).head(max_picks)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Markets checked", f"{len(judged):,}")
m2.metric("YES — pick", int((judged["verdict"] == "YES").sum()))
m3.metric("NO — skip", int((judged["verdict"] == "NO").sum()))
m4.metric("Updated", pd.Timestamp.now(tz=LOCAL_TZ).strftime("%I:%M %p").lstrip("0"))
st.write("")

# ---------------------------------------------------------------------------
# MAIN PANEL SPLIT
# ---------------------------------------------------------------------------
left, right = st.columns([1.5, 1], gap="large")

with left:
    with st.container(border=True):
        st.markdown('<div class="ae-section-title">📋 Kalshi Board</div>', unsafe_allow_html=True)
        st.caption("Sample games (Kalshi unreachable)" if using_demo
                   else "Live from Kalshi · every market, with a verdict")
        board = judged if show_skips else judged[judged["verdict"] == "YES"]
        board = board.sort_values(["verdict", "prob"], ascending=[False, False])
        if board.empty:
            st.info("No rows to show with these filters.")
        else:
            st.markdown(board_html(board.head(MAX_BOARD_ROWS)), unsafe_allow_html=True)
            if len(board) > MAX_BOARD_ROWS:
                st.caption(f"Showing the top {MAX_BOARD_ROWS} of {len(board):,}. "
                           "Pick a sport, bet type, or game above to see the rest.")
        st.caption("Chance = Kalshi's current price for that side (e.g. 72¢ ≈ 72%).")

with right:
    with st.container(border=True):
        st.markdown('<div class="ae-section-title">✨ My AI Generated Slip</div>', unsafe_allow_html=True)
        st.caption("The app's YES picks. Check each one off once you've placed it on Kalshi yourself.")

        if picks.empty:
            st.warning("No YES picks with these rules. Lower the chance % in the sidebar "
                       "or add more sports / bet types.")
        else:
            placed = 0
            for r in picks.itertuples():
                if st.checkbox(f"✅ **Pick this** — {bet_text(r)}", key=f"placed_{r.ticker}_{r.side}"):
                    placed += 1
                st.markdown(
                    '<div class="ae-pick-meta">'
                    f'<span class="ae-badge ae-pink">{html.escape(r.sport)}</span>'
                    f'<span class="ae-badge ae-tan">{html.escape(r.bet_type)}</span>'
                    f'<span class="ae-badge ae-yes">{round(r.prob * 100)}% chance</span><br>'
                    f"{html.escape(short_game_name(str(r.game)))} · settles ~{fmt_time(r.when_ts)}<br>"
                    f"Costs about {round(r.cost * 100)}¢, pays $1.00 if right"
                    "</div>",
                    unsafe_allow_html=True,
                )

            st.markdown("---")
            st.markdown(f"**Placed:** {placed} of {len(picks)}")
            st.caption("Copy your slip (copy icon, top-right of the box):")
            st.code(slip_text(picks), language=None)
            st.download_button(
                "⬇️ Download slip (.txt)",
                data=slip_text(picks),
                file_name=f"aesthetic-edge-slip-{pick_date.isoformat()}.txt",
                mime="text/plain",
            )

        st.caption("Picks follow Kalshi's prices — favorites still lose sometimes. "
                   "Only stake what you're okay losing.")

footer()
