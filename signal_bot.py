"""
ALPHAQUANT V6.2
Strategi: T-A-P-T-E (Trend → Area → Pattern → Trigger → Exit)
Berdasarkan: Checklist Trading Crypto 1H (Akademi Crypto - Crypto Trading Guide hlm. 138-139)
Pair   : BTC-USDT-SWAP (eksklusif)
TF     : 15m
Mode   : Signal-only → Telegram

=== URUTAN WAJIB ===
1. TREND   : Market structure (HH/HL atau LH/LL) + EMA13/21 + Supertrend + Bollinger MA20
2. AREA    : Support/Resistance horizontal, trendline, EMA dynamic, Fibonacci 0.5-0.618 golden pocket
3. PATTERN : Konfirmasi: bull/bear flag, pennant, triangle, H&S, double top/bottom, wedge
4. TRIGGER : Candlestick (hammer, engulfing, tweezer) + Stochastic 5-3-3 cross oversold/overbought
5. EXIT    : SL di bawah HL/support (long) atau di atas LH/resistance (short), ATR-adjusted
             TP di resistance terdekat (long) atau support terdekat (short), RR min 1:2

=== MONEY MANAGEMENT ===
- Risiko maks 1% modal per posisi
- Maks 3 posisi bersamaan (total risiko 3%)
"""

from __future__ import annotations

import os, json, time, math, logging
from datetime import datetime, timezone
import requests

# ================= CONFIG =================

OKX = "https://www.okx.com"

def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""

TOKEN   = env_first("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
CHAT_ID = env_first("CHAT_ID", "TELEGRAM_CHAT_ID", "TELEGRAM_CHAT")

# --- Instrument ---
SYMBOL      = "BTC-USDT-SWAP"

TF          = "15m"
TF_MS       = 15 * 60_000
MONITOR_TF  = "5m"
MONITOR_MS  = 5  * 60_000
HISTORY_BARS = 300
MIN_BARS     = 100

# --- EMA ---
EMA_FAST = 13
EMA_SLOW = 21
EMA_MID  = 20               # Bollinger MA20 (center line)

# --- Supertrend ---
ST_ATR_PERIOD = 10
ST_MULT       = 3.0

# --- ATR (untuk SL & ukuran candle) ---
ATR_PERIOD = 14
SL_ATR_BUFFER = 0.30        # buffer di luar swing extreme

# --- Fibonacci golden pocket ---
FIB_LOW  = 0.50
FIB_HIGH = 0.618

# --- Stochastic ---
STOCH_K    = 5
STOCH_D    = 3
STOCH_SLOWING = 3
STOCH_OS   = 25             # oversold threshold
STOCH_OB   = 75             # overbought threshold

# --- Swing detection ---
SWING_LEN  = 3

# --- RR & risiko ---
MIN_RR           = 2.0
def _env_float(key, default):
    v = os.getenv(key, "").strip()
    try:
        return float(v) if v else default
    except ValueError:
        return default

TARGET_RR        = _env_float("TARGET_RR", 2.0)
MAX_OPEN         = 1   # BTC-only: cukup 1 posisi aktif
COOLDOWN_HOURS_PER_SYMBOL = _env_float("COOLDOWN_HOURS", 4.0)
RISK_PER_TRADE   = _env_float("RISK_PER_TRADE", 0.01)   # 1% per posisi
ACCOUNT_EQUITY   = _env_float("ACCOUNT_EQUITY", 1000.0)
FEE_PER_SIDE     = _env_float("FEE_PER_SIDE", 0.0005)
MIN_SL_PCT       = 0.003    # SL minimal 0.3% dari harga (menghindari fee habiskan R)

COOLDOWN_HOURS   = COOLDOWN_HOURS_PER_SYMBOL
MONITOR_MAX_BARS = 1400

# --- Runtime ---
def _env_int(key, default):
    v = os.getenv(key, "").strip()
    try:
        return int(v) if v else default
    except ValueError:
        return default

SCAN_INTERVAL = _env_int("SCAN_INTERVAL", 60)
_RF = os.getenv("RUN_FOREVER", "").strip()
RUN_FOREVER = (_RF == "1") if _RF else (os.getenv("GITHUB_ACTIONS", "").strip().lower() != "true")
HISTORY_FILE = os.getenv("SIGNAL_HISTORY_FILE", "signals_history.json")
TIMEOUT  = 15
RETRIES  = 3
MIN_REQUEST_GAP = 0.09

REPORT_EVERY_HOURS = _env_float("REPORT_EVERY_HOURS", 4.0)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("V6.2")
S = requests.Session()
S.headers.update({"User-Agent": "ALPHAQUANT-V6.2"})
TELEGRAM = bool(TOKEN and CHAT_ID)

# ================= HELPERS =================

def iso(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()

def now_iso():
    return datetime.now(timezone.utc).isoformat()

_LAST_REQ = [0.0]

def api(path, params=None):
    for attempt in range(RETRIES):
        wait = MIN_REQUEST_GAP - (time.time() - _LAST_REQ[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_REQ[0] = time.time()
        try:
            r = S.get(OKX + path, params=params, timeout=TIMEOUT)
            r.raise_for_status()
            d = r.json()
            if d.get("code") == "0":
                return d
            log.warning("OKX error: %s", d)
        except Exception as e:
            log.warning("Request failed %s/%s: %s", attempt + 1, RETRIES, e)
        if attempt + 1 < RETRIES:
            time.sleep(1.5)
    return None

def tg(text):
    if not TELEGRAM:
        return False
    try:
        r = S.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            },
            timeout=TIMEOUT
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error("Telegram error: %s", e)
        return False

def load_history():
    if not os.path.exists(HISTORY_FILE):
        return {"open": [], "closed": []}
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("open", [])
        d.setdefault("closed", [])
        return d
    except Exception as e:
        log.error("History load failed: %s", e)
        return {"open": [], "closed": []}

def save_history(h):
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(h, f, indent=2, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)

def archive_legacy(h):
    """Pindahkan trade dari strategi lama (SMC_SCALP dll.) ke archive."""
    moved = 0
    for key in ("open", "closed"):
        keep, drop = [], []
        for x in h.get(key, []):
            (keep if x.get("setup") == "TAPTE" else drop).append(x)
        if drop:
            h[key] = keep
            h.setdefault("archive", []).extend(dict(x, archived_from=key) for x in drop)
            moved += len(drop)
    return moved

def _safe(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ================= MARKET DATA =================

def candles(inst, bar, needed):
    """Ambil confirmed candles OKX, oldest→newest."""
    found = {}
    cursor = None
    max_requests = math.ceil(needed / 100) + 6

    for _ in range(max_requests):
        p = {"instId": inst, "bar": bar, "limit": "100"}
        if cursor is not None:
            p["after"] = str(cursor)
        d = api("/api/v5/market/candles", p)
        if not d:
            break
        rows = d.get("data", [])
        if not rows:
            break
        oldest = None
        for x in rows:
            try:
                ts = int(x[0])
                oldest = ts if oldest is None else min(oldest, ts)
                if int(x[8]) != 1:
                    continue
                found[ts] = [ts, float(x[1]), float(x[2]), float(x[3]), float(x[4]), float(x[5])]
            except:
                continue
        if len(found) >= needed:
            break
        if oldest is None or oldest == cursor:
            break
        cursor = oldest
        time.sleep(0.12)

    out = sorted(found.values(), key=lambda x: x[0])
    return out[-needed:]

def latest_candle_ts():
    cs = candles(SYMBOL, TF, 3)
    return cs[-1][0] if cs else None

def current_price(inst=SYMBOL):
    d = api("/api/v5/market/ticker", {"instId": inst})
    try:
        return float(d["data"][0]["last"])
    except:
        return None

def get_tick_size(inst=SYMBOL):
    d = api("/api/v5/public/instruments", {"instType": "SWAP", "instId": inst})
    try:
        return float(d["data"][0]["tickSz"])
    except:
        return 0.1   # BTC default

def tick_round(x, tick):
    """Bulatkan x ke kelipatan tick. Aman untuk harga sangat kecil."""
    if tick <= 0 or x == 0:
        return x
    # Hitung presisi desimal dari tick agar round() tidak memotong angka penting
    decimals = max(0, -int(math.floor(math.log10(tick))))
    return round(round(x / tick) * tick, decimals + 2)

# ================= INDICATORS =================

def _rma(v, n):
    out = [None] * len(v)
    if len(v) < n:
        return out
    x = sum(v[:n]) / n
    out[n - 1] = x
    for i in range(n, len(v)):
        x = ((x * (n - 1)) + v[i]) / n
        out[i] = x
    return out

def calc_atr(cs, n=ATR_PERIOD):
    tr = []
    for i, c in enumerate(cs):
        if i == 0:
            tr.append(c[2] - c[3])
        else:
            pc = cs[i-1][4]
            tr.append(max(c[2]-c[3], abs(c[2]-pc), abs(c[3]-pc)))
    return _rma(tr, n)

def calc_ema(closes, n):
    out = [None] * len(closes)
    if len(closes) < n:
        return out
    k = 2 / (n + 1)
    x = sum(closes[:n]) / n
    out[n - 1] = x
    for i in range(n, len(closes)):
        x = closes[i] * k + x * (1 - k)
        out[i] = x
    return out

def calc_bollinger_mid(closes, n=EMA_MID):
    """Bollinger Band center = SMA20."""
    out = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        out[i] = sum(closes[i - n + 1:i + 1]) / n
    return out

def calc_supertrend(cs, atr_list, mult=ST_MULT):
    """
    Supertrend: returns list of (trend_val, direction)
    direction = 1  → bullish (harga di atas, warna hijau)
    direction = -1 → bearish
    """
    n = len(cs)
    st   = [None] * n
    dir_ = [None] * n
    upper = [None] * n
    lower = [None] * n

    for i in range(n):
        a = atr_list[i]
        if a is None:
            continue
        hl2 = (cs[i][2] + cs[i][3]) / 2
        upper[i] = hl2 + mult * a
        lower[i] = hl2 - mult * a

    for i in range(n):
        if upper[i] is None:
            continue
        if i == 0 or st[i-1] is None:
            st[i]   = lower[i]
            dir_[i] = 1
            continue

        prev_st  = st[i-1]
        prev_dir = dir_[i-1]
        close    = cs[i][4]
        prev_close = cs[i-1][4]

        # Finalise bands (can only move in one direction)
        lower[i] = max(lower[i], lower[i-1]) if prev_close > lower[i-1] else lower[i]
        upper[i] = min(upper[i], upper[i-1]) if prev_close < upper[i-1] else upper[i]

        if prev_dir == 1:
            st[i]   = lower[i]
            dir_[i] = 1 if close > st[i] else -1
        else:
            st[i]   = upper[i]
            dir_[i] = -1 if close < st[i] else 1

    return list(zip(st, dir_))

def calc_stochastic(cs, k=STOCH_K, d=STOCH_D, slowing=STOCH_SLOWING):
    """Stochastic 5-3-3 (full). Returns (K%, D%) lists."""
    n = len(cs)
    raw_k = [None] * n
    for i in range(k - 1, n):
        hi = max(c[2] for c in cs[i-k+1:i+1])
        lo = min(c[3] for c in cs[i-k+1:i+1])
        rng = hi - lo
        raw_k[i] = 100 * (cs[i][4] - lo) / rng if rng else 50.0

    # Slowing (SMA of raw_k)
    slow_k = [None] * n
    for i in range(k - 1 + slowing - 1, n):
        vals = [raw_k[j] for j in range(i - slowing + 1, i + 1) if raw_k[j] is not None]
        slow_k[i] = sum(vals) / len(vals) if vals else None

    # %D = SMA(slow_k, d)
    stoch_d = [None] * n
    for i in range(n):
        if slow_k[i] is None:
            continue
        vals = [slow_k[j] for j in range(max(0, i - d + 1), i + 1) if slow_k[j] is not None]
        stoch_d[i] = sum(vals) / len(vals) if vals else None

    return slow_k, stoch_d

def calc_swings(cs, length=SWING_LEN):
    """Fractal swing highs/lows → (index, price) pairs."""
    highs, lows = [], []
    for i in range(length, len(cs) - length):
        h = cs[i][2]
        l = cs[i][3]
        if all(h >= cs[i-j][2] for j in range(1, length+1)) and \
           all(h >  cs[i+j][2] for j in range(1, length+1)):
            highs.append((i, h))
        if all(l <= cs[i-j][3] for j in range(1, length+1)) and \
           all(l <  cs[i+j][3] for j in range(1, length+1)):
            lows.append((i, l))
    return highs, lows

# ================= STEP 1: TREND =================

def check_trend(cs, ema_fast, ema_slow, ema_mid, st_list, highs, lows):
    """
    Evaluasi tren: bullish / bearish / neutral.
    Makin banyak konfirmator searah, makin kuat sinyalnya.

    Returns: bias ('BULL'|'BEAR'|None), score (int), detail (str)
    """
    i = len(cs) - 1
    close = cs[i][4]

    scores = {"BULL": 0, "BEAR": 0}
    notes  = []

    # 1) Market structure: HH+HL = bullish, LH+LL = bearish
    if len(highs) >= 2 and len(lows) >= 2:
        hh = highs[-1][1] > highs[-2][1]
        hl = lows[-1][1]  > lows[-2][1]
        lh = highs[-1][1] < highs[-2][1]
        ll = lows[-1][1]  < lows[-2][1]
        if hh and hl:
            scores["BULL"] += 2
            notes.append("Struktur HH+HL ✓")
        elif lh and ll:
            scores["BEAR"] += 2
            notes.append("Struktur LH+LL ✓")
        else:
            notes.append("Struktur campuran")

    # 2) EMA13/21
    ef = ema_fast[i]
    es = ema_slow[i]
    if ef is not None and es is not None:
        if close > ef > es:
            scores["BULL"] += 1
            notes.append("Harga > EMA13 > EMA21 ✓")
        elif close < ef < es:
            scores["BEAR"] += 1
            notes.append("Harga < EMA13 < EMA21 ✓")
        else:
            notes.append("EMA tidak selaras")

    # 3) Supertrend
    st_val, st_dir = st_list[i] if st_list[i][0] is not None else (None, None)
    if st_dir is not None:
        if st_dir == 1:
            scores["BULL"] += 1
            notes.append("Supertrend hijau ✓")
        else:
            scores["BEAR"] += 1
            notes.append("Supertrend merah ✓")

    # 4) Bollinger MA20 (center line)
    em = ema_mid[i]
    if em is not None:
        if close > em:
            scores["BULL"] += 1
            notes.append("Harga > BB MA20 ✓")
        else:
            scores["BEAR"] += 1
            notes.append("Harga < BB MA20 ✓")

    # Butuh minimal 3 dari 5 poin untuk bias kuat
    bull_score = scores["BULL"]
    bear_score = scores["BEAR"]

    if bull_score >= 3 and bull_score > bear_score:
        return "BULL", bull_score, " | ".join(notes)
    if bear_score >= 3 and bear_score > bull_score:
        return "BEAR", bear_score, " | ".join(notes)
    return None, max(bull_score, bear_score), " | ".join(notes)

# ================= STEP 2: AREA =================

def find_key_levels(cs, highs, lows, atr_val):
    """
    Kumpulkan level kunci: swing highs/lows terbaru + Fibonacci golden pocket.
    Returns: (supports, resistances) masing-masing list of (level, label)
    """
    close = cs[-1][4]
    tol   = atr_val * 0.5   # toleransi pengelompokan level

    supports    = []
    resistances = []

    # Swing lows = support potensial
    for idx, p in lows[-6:]:
        if p < close:
            supports.append((p, f"SwingLow@{p:.1f}"))

    # Swing highs = resistance potensial
    for idx, p in highs[-6:]:
        if p > close:
            resistances.append((p, f"SwingHigh@{p:.1f}"))

    # Fibonacci golden pocket (0.5–0.618) dari swing terbaru
    if highs and lows:
        last_h = highs[-1][1]
        last_l = lows[-1][1]
        swing  = last_h - last_l
        if swing > 0:
            fib50  = last_h - FIB_LOW  * swing
            fib618 = last_h - FIB_HIGH * swing
            fib_mid = (fib50 + fib618) / 2
            if fib_mid < close:
                supports.append((fib_mid, f"Fib50-618@{fib_mid:.1f}"))
            else:
                resistances.append((fib_mid, f"Fib50-618@{fib_mid:.1f}"))

    # EMA dinamis sebagai support/resistance
    ema_fast_val = None
    # Akan diisi dari konteks pemanggil
    return supports, resistances

def price_near_level(price_val, levels, atr_val, factor=1.5):
    """True jika price_val berada dalam jangkauan factor*ATR dari salah satu level."""
    for lvl, lbl in levels:
        if abs(price_val - lvl) <= factor * atr_val:
            return True, lvl, lbl
    return False, None, None

# ================= STEP 3: PATTERN =================

BULL_PATTERNS = {"bull_flag", "bull_pennant", "ascending_triangle", "double_bottom",
                 "triple_bottom", "inv_head_shoulders", "falling_wedge_breakout"}
BEAR_PATTERNS = {"bear_flag", "bear_pennant", "descending_triangle", "double_top",
                 "triple_top", "head_shoulders", "rising_wedge_breakout"}

def detect_pattern(cs, bias, atr_val):
    """
    Deteksi pola sederhana (berbasis price action terbaru).
    Returns: (pattern_name | None, detail)
    Pola kompleks (H&S lengkap) butuh banyak bar; di sini deteksi ringkas.
    """
    n = len(cs)
    if n < 20:
        return None, "data kurang"

    closes = [c[4] for c in cs]
    highs  = [c[2] for c in cs]
    lows   = [c[3] for c in cs]

    # Window 20 bar terakhir
    w = 20
    c_w  = closes[-w:]
    h_w  = highs[-w:]
    l_w  = lows[-w:]

    highest = max(h_w)
    lowest  = min(l_w)
    rng     = highest - lowest
    if rng <= 0:
        return None, "range nol"

    last_close  = closes[-1]
    prev_close  = closes[-2]
    prev2_close = closes[-3]

    # --- Bull flag: rally → konsolidasi turun pendek → breakout naik ---
    if bias == "BULL":
        # Rally di 10 bar awal window
        rally_end = closes[-w + 10]
        consol_low = min(lows[-10:])
        if rally_end > closes[-w] and consol_low > closes[-w] and last_close > max(closes[-10:-1]):
            return "bull_flag", f"Bull flag breakout close {last_close:.1f}"

        # Double bottom: dua lows dalam ATR 1x satu sama lain
        recent_lows = sorted(lows[-15:])
        if abs(recent_lows[0] - recent_lows[1]) < atr_val:
            return "double_bottom", f"Double bottom ~{recent_lows[0]:.1f}"

    # --- Bear flag: drop → konsolidasi naik pendek → breakdown ---
    if bias == "BEAR":
        drop_end = closes[-w + 10]
        consol_high = max(highs[-10:])
        if drop_end < closes[-w] and consol_high < closes[-w] and last_close < min(closes[-10:-1]):
            return "bear_flag", f"Bear flag breakdown close {last_close:.1f}"

        recent_highs = sorted(highs[-15:], reverse=True)
        if abs(recent_highs[0] - recent_highs[1]) < atr_val:
            return "double_top", f"Double top ~{recent_highs[0]:.1f}"

    # Tidak ada pola yang terdeteksi — masih bisa lanjut bila trigger kuat
    return None, "tidak ada pola terdeteksi"

# ================= STEP 4: TRIGGER =================

def _is_hammer(c, atr_val):
    o, h, l, cl = c[1], c[2], c[3], c[4]
    body  = abs(cl - o)
    total = h - l
    lower_shadow = min(o, cl) - l
    if total <= 0:
        return False
    return lower_shadow >= 2 * body and body <= 0.4 * total

def _is_inv_hammer(c, atr_val):
    o, h, l, cl = c[1], c[2], c[3], c[4]
    body   = abs(cl - o)
    total  = h - l
    upper_shadow = h - max(o, cl)
    if total <= 0:
        return False
    return upper_shadow >= 2 * body and body <= 0.4 * total

def _is_bull_engulf(c_prev, c):
    return (c_prev[4] < c_prev[1] and  # prev bearish
            c[4]     > c[1]      and    # curr bullish
            c[1]     <= c_prev[4] and   # open di bawah prev close
            c[4]     >= c_prev[1])      # close di atas prev open

def _is_bear_engulf(c_prev, c):
    return (c_prev[4] > c_prev[1] and
            c[4]     < c[1]      and
            c[1]     >= c_prev[4] and
            c[4]     <= c_prev[1])

def _is_tweezer_bottom(c_prev, c, atr_val):
    tol = atr_val * 0.1
    return abs(c_prev[3] - c[3]) <= tol and c[4] > c[1]

def _is_tweezer_top(c_prev, c, atr_val):
    tol = atr_val * 0.1
    return abs(c_prev[2] - c[2]) <= tol and c[4] < c[1]

def check_trigger(cs, bias, stoch_k, stoch_d, atr_val):
    """
    Evaluasi trigger: candlestick + stochastic.
    Returns: triggered (bool), trigger_name (str), detail (str)
    """
    i     = len(cs) - 1
    c     = cs[i]
    c_prev = cs[i - 1] if i > 0 else None

    sk = stoch_k[i]
    sd = stoch_d[i]
    sk_prev = stoch_k[i-1] if i > 0 else None
    sd_prev = stoch_d[i-1] if i > 0 else None

    candle_triggers = []
    stoch_trigger   = False
    stoch_note      = ""

    if bias == "BULL":
        # Candlestick bullish
        if _is_hammer(c, atr_val):
            candle_triggers.append("Hammer")
        if _is_inv_hammer(c, atr_val):
            candle_triggers.append("Inverted Hammer")
        if c_prev and _is_bull_engulf(c_prev, c):
            candle_triggers.append("Bullish Engulfing")
        if c_prev and _is_tweezer_bottom(c_prev, c, atr_val):
            candle_triggers.append("Tweezer Bottom")

        # Stochastic cross di oversold
        if (sk is not None and sd is not None and
                sk_prev is not None and sd_prev is not None):
            if sk_prev < sd_prev and sk > sd and sk < STOCH_OS + 15:
                stoch_trigger = True
                stoch_note    = f"Stoch cross atas (K={sk:.1f}, area oversold)"

    elif bias == "BEAR":
        # Candlestick bearish
        if c_prev and _is_bear_engulf(c_prev, c):
            candle_triggers.append("Bearish Engulfing")
        if c_prev and _is_tweezer_top(c_prev, c, atr_val):
            candle_triggers.append("Tweezer Top")
        # Shooting star (inv hammer bearish)
        if _is_inv_hammer(c, atr_val) and c[4] < c[1]:
            candle_triggers.append("Shooting Star")

        # Stochastic cross di overbought
        if (sk is not None and sd is not None and
                sk_prev is not None and sd_prev is not None):
            if sk_prev > sd_prev and sk < sd and sk > STOCH_OB - 15:
                stoch_trigger = True
                stoch_note    = f"Stoch cross bawah (K={sk:.1f}, area overbought)"

    triggered = bool(candle_triggers) or stoch_trigger
    trigger_name = " + ".join(candle_triggers) + (" + " + stoch_note if stoch_trigger else (stoch_note or ""))
    if not triggered:
        trigger_name = "tidak ada trigger"

    return triggered, trigger_name.strip(" + "), stoch_note

# ================= STEP 5: EXIT (SL & TP) =================

def calc_sl_tp(cs, bias, highs, lows, atr_val, tick):
    """
    SL di luar swing extreme (+ ATR buffer).
    TP di resistance/support terdekat yang memenuhi RR >= MIN_RR.
    Returns: (entry, sl, tp, rr, sl_atr, risk_pct) atau None
    """
    i     = len(cs) - 1
    entry = cs[i][4]

    if bias == "BULL":
        # SL di bawah swing low terakhir
        recent_lows = [l for idx, l in lows if idx >= i - 20]
        if not recent_lows:
            return None
        sl_anchor = min(recent_lows)
        sl_raw    = sl_anchor - SL_ATR_BUFFER * atr_val
        risk      = entry - sl_raw
        if risk <= 0:
            return None

        # TP: swing high atau fibonacci resistance
        tp_candidates = [h for idx, h in highs if h > entry and idx >= i - 50]
        if not tp_candidates:
            tp_candidates = [entry + TARGET_RR * risk]
        # Ambil yang paling dekat tapi tetap memenuhi MIN_RR
        viable_tps = [h for h in tp_candidates if (h - entry) / risk >= MIN_RR]
        if not viable_tps:
            tp_raw = entry + TARGET_RR * risk
        else:
            tp_raw = min(viable_tps)

    else:  # BEAR
        recent_highs = [h for idx, h in highs if idx >= i - 20]
        if not recent_highs:
            return None
        sl_anchor = max(recent_highs)
        sl_raw    = sl_anchor + SL_ATR_BUFFER * atr_val
        risk      = sl_raw - entry
        if risk <= 0:
            return None

        tp_candidates = [l for idx, l in lows if l < entry and idx >= i - 50]
        if not tp_candidates:
            tp_candidates = [entry - TARGET_RR * risk]
        viable_tps = [l for l in tp_candidates if (entry - l) / risk >= MIN_RR]
        if not viable_tps:
            tp_raw = entry - TARGET_RR * risk
        else:
            tp_raw = max(viable_tps)

    rr       = abs(tp_raw - entry) / risk
    risk_pct = risk / entry

    if rr < MIN_RR:
        return None
    if risk_pct < MIN_SL_PCT:
        return None

    sl_atr = risk / atr_val

    return (
        tick_round(entry, tick),
        tick_round(sl_raw, tick),
        tick_round(tp_raw, tick),
        round(rr, 2),
        sl_atr,
        risk_pct
    )

# ================= CORE BUILD =================

REJECT_LOG = []   # reset tiap scan

def _rej(stage, detail):
    REJECT_LOG.append(f"{stage}: {detail}")
    return None

def build_tapte_signal(cs, inst=SYMBOL, tick=0.1):
    """
    Jalankan urutan TAPTE pada candle 15m untuk pair `inst`.
    Returns signal dict atau None.
    """
    REJECT_LOG.clear()

    if len(cs) < MIN_BARS:
        return _rej("Data", f"hanya {len(cs)} candle")

    closes = [c[4] for c in cs]

    # --- Indikator ---
    atr_list  = calc_atr(cs, ATR_PERIOD)
    ema_fast  = calc_ema(closes, EMA_FAST)
    ema_slow  = calc_ema(closes, EMA_SLOW)
    ema_mid   = calc_bollinger_mid(closes, EMA_MID)
    st_atr    = calc_atr(cs, ST_ATR_PERIOD)
    st_list   = calc_supertrend(cs, st_atr, ST_MULT)
    stoch_k, stoch_d = calc_stochastic(cs, STOCH_K, STOCH_D, STOCH_SLOWING)
    highs, lows = calc_swings(cs, SWING_LEN)

    i = len(cs) - 1
    atr_val = atr_list[i]

    if atr_val is None or atr_val <= 0:
        return _rej("Data", "ATR tidak valid")
    if ema_fast[i] is None or ema_slow[i] is None:
        return _rej("Data", "EMA belum siap")
    if len(highs) < 2 or len(lows) < 2:
        return _rej("Struktur", "swing kurang dari 2")

    # === STEP 1: TREND ===
    bias, t_score, t_detail = check_trend(cs, ema_fast, ema_slow, ema_mid, st_list, highs, lows)
    if bias is None:
        return _rej("Tren", f"bias tidak jelas (score {t_score}/5) | {t_detail}")

    # === STEP 2: AREA ===
    supports, resistances = find_key_levels(cs, highs, lows, atr_val)
    close = cs[i][4]

    if bias == "BULL":
        near_area, lvl, lbl = price_near_level(close, supports, atr_val, factor=2.0)
    else:
        near_area, lvl, lbl = price_near_level(close, resistances, atr_val, factor=2.0)

    if not near_area:
        return _rej("Area", f"harga {close:.1f} tidak dekat support/resistance kunci")

    # === STEP 3: PATTERN ===
    pattern, p_detail = detect_pattern(cs, bias, atr_val)
    # Pattern bukan syarat wajib, tapi dicatat

    # === STEP 4: TRIGGER ===
    triggered, trigger_name, stoch_note = check_trigger(cs, bias, stoch_k, stoch_d, atr_val)
    if not triggered:
        return _rej("Trigger", "tidak ada candlestick/stochastic trigger")

    # === STEP 5: EXIT ===
    result = calc_sl_tp(cs, bias, highs, lows, atr_val, tick)
    if result is None:
        return _rej("SL/TP", "tidak memenuhi RR >= 1:2 atau SL terlalu kecil")

    entry, sl, tp, rr, sl_atr, risk_pct = result
    direction = "LONG" if bias == "BULL" else "SHORT"

    note = (
        f"Tren: {bias} ({t_score}/5) | {t_detail}\n"
        f"Area: {lbl}\n"
        f"Pattern: {pattern or '-'} ({p_detail})\n"
        f"Trigger: {trigger_name}"
    )

    sk_val = stoch_k[i]

    return {
        "signal_id"        : f"{inst}|{direction}|{cs[-1][0]}|TAPTE",
        "symbol"           : inst,
        "direction"        : direction,
        "timeframe"        : TF,
        "setup"            : "TAPTE",
        "note"             : note,
        "signal_candle_time" : cs[-1][0],
        "signal_candle_iso"  : iso(cs[-1][0]),
        "created_at"       : now_iso(),
        "entry"            : entry,
        "sl"               : sl,
        "tp"               : tp,
        "rr"               : round(rr, 2),
        "atr"              : round(atr_val, 4),
        "sl_atr"           : round(sl_atr, 2),
        "risk_pct"         : round(risk_pct * 100, 2),
        "trend_bias"       : bias,
        "trend_score"      : t_score,
        "area_level"       : lbl,
        "pattern"          : pattern or "-",
        "trigger"          : trigger_name,
        "stoch_k"          : round(sk_val, 1) if sk_val else None,
        "ema_fast"         : round(ema_fast[i], 2),
        "ema_slow"         : round(ema_slow[i], 2),
        "risk_usdt"        : round(ACCOUNT_EQUITY * RISK_PER_TRADE, 2),
        "status"           : "OPEN",
        "exit_price"       : None,
        "exit_time"        : None,
        "result_r"         : None,
        "result"           : None,
    }

# ================= DEDUPE =================

def is_blocked(h, sig):
    sid = sig["signal_id"]
    if any(x.get("signal_id") == sid for x in h["open"] + h["closed"]):
        return True
    # Cooldown: jangan buka lagi jika baru tutup dalam COOLDOWN_HOURS
    cutoff = time.time() - COOLDOWN_HOURS * 3600
    for x in h["closed"]:
        if x.get("symbol") != sig["symbol"]:
            continue
        try:
            if datetime.fromisoformat(x["exit_time"]).timestamp() > cutoff:
                return True
        except:
            pass
    return False

# ================= MONITOR =================

def monitor(trade):
    start   = trade.get("signal_candle_time", 0) + TF_MS
    age_ms  = max(0, int(time.time() * 1000) - start)
    needed  = min(MONITOR_MAX_BARS, max(60, age_ms // MONITOR_MS + 24))

    cs = candles(trade["symbol"], MONITOR_TF, needed)
    if not cs:
        return None

    for c in [x for x in cs if x[0] >= start]:
        hi, lo = c[2], c[3]
        sl, tp  = trade["sl"], trade["tp"]

        if trade["direction"] == "LONG":
            hit_sl, hit_tp = lo <= sl, hi >= tp
        else:
            hit_sl, hit_tp = hi >= sl, lo <= tp

        if hit_sl and hit_tp:
            exit_price, r, result = sl, -1.0, "LOSS"
        elif hit_tp:
            exit_price, r, result = tp, float(trade.get("rr", TARGET_RR)), "WIN"
        elif hit_sl:
            exit_price, r, result = sl, -1.0, "LOSS"
        else:
            continue

        t = dict(trade)
        t.update({
            "status"     : "CLOSED",
            "exit_price" : exit_price,
            "exit_time"  : iso(c[0]),
            "result_r"   : r,
            "result"     : result,
        })
        return t
    return None

# ================= CLOSE MESSAGE =================

def _send_close_message(t):
    """Kirim ringkasan trade yang baru tutup ke Telegram."""
    is_win   = t["result"] == "WIN"
    emoji    = "✅" if is_win else "❌"
    medal    = "🏆" if is_win else "💸"

    entry    = float(t.get("entry") or 0)
    exit_p   = float(t.get("exit_price") or 0)
    sl       = float(t.get("sl") or 0)
    tp       = float(t.get("tp") or 0)

    if entry == 0:
        log.warning("_send_close_message: entry=0, skip pesan close untuk %s", t.get("symbol","?"))
        return
    rr_val   = float(t.get("rr", TARGET_RR))
    risk_r   = float(t.get("risk_usdt", ACCOUNT_EQUITY * RISK_PER_TRADE))
    result_r = float(t.get("result_r", 0))

    # Durasi trade
    try:
        open_ts  = t.get("signal_candle_time", 0)
        close_ts = int(datetime.fromisoformat(t["exit_time"]).timestamp() * 1000)
        dur_min  = (close_ts - open_ts) // 60_000
        if dur_min < 60:
            dur_str = f"{dur_min} menit"
        else:
            dur_str = f"{dur_min // 60}j {dur_min % 60}m"
    except:
        dur_str = "-"

    # Estimasi PnL USDT
    risk_usdt = risk_r
    pnl_gross = result_r * risk_usdt
    fee_usdt  = 2 * FEE_PER_SIDE * entry * (risk_usdt / max(abs(entry - sl), 1e-9))
    pnl_net   = pnl_gross - fee_usdt

    # Move (harga entry → exit dalam %)
    move_pct  = (exit_p - entry) / entry * 100 if entry != 0 else 0.0
    move_pct  = move_pct if t["direction"] == "LONG" else -move_pct

    # Ringkasan sinyal asal (note)
    note_short = ""
    note_raw   = t.get("note", "")
    for line in note_raw.split("\n"):
        if line.startswith("Tren:") or line.startswith("Trigger:"):
            note_short += _safe(line) + "\n"

    tg(
        f"<b>{emoji} TRADE CLOSED  {medal}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{t['symbol']}</b> · {t['direction']} · {t['timeframe']}\n\n"
        f"<b>HASIL   : {'WIN 🎯' if is_win else 'LOSS 🛑'}</b>\n"
        f"R        : <b>{result_r:+.2f}R</b>  (target 1:{rr_val:.2f})\n"
        f"PnL est  : <b>{pnl_net:+.2f} USDT</b> (gross {pnl_gross:+.2f}, fee ≈{fee_usdt:.2f})\n\n"
        f"Entry    : <code>{entry}</code>\n"
        f"Exit     : <code>{exit_p}</code>\n"
        f"SL       : <code>{sl}</code>\n"
        f"TP       : <code>{tp}</code>\n"
        f"Gerak    : {move_pct:+.2f}%\n"
        f"Durasi   : {dur_str}\n\n"
        f"{note_short}"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Buka  : {t.get('signal_candle_iso', '-')}\n"
        f"Tutup : {t['exit_time']}"
    )

def check_open(h):
    if not h["open"]:
        return 0

    remain, closed = [], []
    for t in h["open"]:
        try:
            result = monitor(t)
            if result:
                closed.append(result)
            else:
                remain.append(t)
        except Exception as e:
            log.error("Monitor %s failed: %s", t["symbol"], e)
            remain.append(t)

    h["open"] = remain
    if closed:
        h["closed"].extend(closed)
        save_history(h)

    for t in closed:
        _send_close_message(t)

    return len(closed)

# ================= REPORT =================

def float_r(trade, p):
    risk = abs(trade["entry"] - trade["sl"])
    if risk <= 0:
        return 0.0
    return ((p - trade["entry"]) / risk
            if trade["direction"] == "LONG"
            else (trade["entry"] - p) / risk)

def running_report(h):
    if not h["open"]:
        return
    blocks, rs = [], []
    for t in h["open"]:
        p = current_price(t["symbol"])
        if p is None:
            continue
        r = float_r(t, p)
        rs.append(r)
        emoji = "🟢" if r > 0 else "🔴" if r < 0 else "⚪"
        blocks.append(
            f"{emoji} <b>{t['symbol']}</b> · {t['direction']}\n"
            f"Entry <code>{t['entry']}</code> | Sekarang <code>{p}</code>\n"
            f"SL <code>{t['sl']}</code> | TP <code>{t['tp']}</code>\n"
            f"Float <b>{r:+.2f}R</b>"
        )
    if not rs:
        return
    tg(
        "<b>📡 POSISI BERJALAN</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n" +
        "\n\n".join(blocks) +
        f"\n\n━━━━━━━━━━━━━━━━━━\n"
        f"Posisi buka : {len(h['open'])}\n"
        f"Floating R  : <b>{sum(rs):+.2f}R</b>\n"
        f"⚠️ Belum terealisasi."
    )

def fee_r_of(t):
    try:
        risk = abs(float(t["entry"]) - float(t["sl"]))
        return (2 * FEE_PER_SIDE * float(t["entry"]) / risk) if risk > 0 else 0.0
    except:
        return 0.0

def stats_report(h):
    ts = h["closed"]
    if not ts:
        return
    wins   = [x for x in ts if x.get("result") == "WIN"]
    losses = [x for x in ts if x.get("result") == "LOSS"]
    total_r = sum(float(x.get("result_r", 0)) for x in ts)
    gp  = sum(max(float(x.get("result_r", 0)), 0) for x in ts)
    gl  = abs(sum(min(float(x.get("result_r", 0)), 0) for x in ts))
    pf  = gp / gl if gl else float("inf")
    pf_txt = "∞" if math.isinf(pf) else f"{pf:.2f}"

    eq = peak = dd = 0.0
    for x in ts:
        eq  += float(x.get("result_r", 0))
        peak = max(peak, eq)
        dd   = max(dd, peak - eq)

    fees   = sum(fee_r_of(x) for x in ts)
    net_r  = total_r - fees
    avg_rr = sum(float(x.get("rr", TARGET_RR)) for x in ts) / len(ts)
    bep    = 100 / (1 + avg_rr)
    bep_net= 100 * (1 + fees / len(ts)) / (1 + avg_rr)
    note   = "\n\n<i>< 30 trade: belum signifikan.</i>" if len(ts) < 30 else ""

    tg(
        "<b>📊 ALPHAQUANT V6.0 PERFORMA</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Trade        : {len(ts)}\n"
        f"Win          : {len(wins)}\n"
        f"Loss         : {len(losses)}\n"
        f"Win Rate     : <b>{len(wins)/len(ts)*100:.2f}%</b>\n"
        f"Total R      : <b>{total_r:+.2f}R</b> gross\n"
        f"Total R net  : <b>{net_r:+.2f}R</b> (setelah fee ≈)\n"
        f"Profit Factor: {pf_txt}\n"
        f"Max Drawdown : -{dd:.2f}R\n"
        f"Avg RR Target: 1:{avg_rr:.2f}\n"
        f"BEP WR       : {bep:.2f}% gross | <b>{bep_net:.2f}%</b> net" + note
    )

# ================= SIGNAL MESSAGE =================

def signal_message(s):
    risk    = abs(s["entry"] - s["sl"])
    fee_r   = (2 * FEE_PER_SIDE * s["entry"] / risk) if risk > 0 else 0.0
    note_lines = _safe(s.get("note", "")).replace("\\n", "\n")

    return (
        "<b>🚨 ALPHAQUANT V6.0 SIGNAL</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{s['symbol']}</b> · <b>{s['direction']}</b> · {s['timeframe']}\n\n"
        f"{note_lines}\n\n"
        "<b>RENCANA TRADE</b>\n"
        f"Entry : <code>{s['entry']}</code>\n"
        f"SL    : <code>{s['sl']}</code>\n"
        f"TP    : <code>{s['tp']}</code>\n"
        f"RR    : <b>1:{s['rr']:.2f}</b>\n"
        f"SL    : {s['sl_atr']:.2f} ATR · {s['risk_pct']:.2f}% harga\n"
        f"Fee ≈ {fee_r:.2f}R (taker {FEE_PER_SIDE*100:.2f}%/sisi)\n\n"
        f"EMA13/21: {s['ema_fast']} / {s['ema_slow']}\n"
        f"Stoch K : {s['stoch_k']}\n"
        f"Risiko  : {s['risk_usdt']:.2f} USDT (1% modal)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "⚠️ SIGNAL ONLY — TIDAK ADA EKSEKUSI OTOMATIS\n"
        f"Candle: {s['signal_candle_iso']}"
    )

# ================= SCAN =================

def scan(h):
    """Scan BTC-USDT-SWAP 15m dengan strategi TAPTE. Returns jumlah sinyal baru."""
    ref = latest_candle_ts()
    if ref is not None and h.get("last_candle") == ref:
        log.info("Candle %s belum baru — scan dilewati.", iso(ref))
        return 0

    if len(h["open"]) >= MAX_OPEN:
        log.info("Posisi BTC sudah aktif — scan dilewati.")
        if ref is not None:
            h["last_candle"] = ref
        return 0

    # Ambil candle BTC
    cs = candles(SYMBOL, TF, HISTORY_BARS)
    if len(cs) < MIN_BARS:
        log.warning("Data kurang: %s candle.", len(cs))
        return 0

    if ref is not None and cs[-1][0] != ref:
        log.info("Candle terakhir tidak sinkron.")
        return 0

    tick = get_tick_size(SYMBOL)
    log.info("TAPTE scan %s %s (tick=%s)...", SYMBOL, TF, tick)

    sig = build_tapte_signal(cs, inst=SYMBOL, tick=tick)

    if ref is not None:
        h["last_candle"] = ref

    if sig is None:
        log.info("Tidak ada sinyal. %s", " → ".join(REJECT_LOG))
        return 0

    if is_blocked(h, sig):
        log.info("Sinyal diblokir (duplikat/cooldown/posisi open).")
        return 0

    h["open"].append(sig)
    save_history(h)
    log.info("SINYAL BARU %s %s RR=1:%.2f", sig["symbol"], sig["direction"], sig["rr"])
    tg(signal_message(sig))
    return 1

# ================= MAIN =================

def main():
    global TELEGRAM

    log.info("==========================================")
    log.info("ALPHAQUANT V6.2 — BTC TAPTE 15m")
    log.info("==========================================")

    if not TOKEN or not CHAT_ID:
        TELEGRAM = False
        missing = []
        if not TOKEN:  missing.append("TELEGRAM_TOKEN")
        if not CHAT_ID: missing.append("CHAT_ID")
        log.warning("Telegram dinonaktifkan. Missing: %s", ", ".join(missing))
    else:
        TELEGRAM = True
        log.info("Telegram aktif.")

    h = load_history()
    moved = archive_legacy(h)
    if moved:
        log.info("Diarsipkan %s trade dari strategi lama.", moved)
        save_history(h)

    log.info(
        "Open: %s | Closed: %s | MaxOpen: %s | RR: 1:%.2f | RunForever: %s",
        len(h["open"]), len(h["closed"]), MAX_OPEN, TARGET_RR, RUN_FOREVER
    )

    today = datetime.now(timezone.utc).date().isoformat()
    if TELEGRAM and h.get("last_online_day") != today:
        h["last_online_day"] = today
        tg(
            "<b>🟢 ALPHAQUANT V6.2 ONLINE</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"Pair     : BTC-USDT-SWAP · 15m\n"
            f"Strategi : T-A-P-T-E\n"
            f"Indikator: EMA13/21, Supertrend, Bollinger MA20, Stochastic 5-3-3\n"
            f"RR min   : 1:{MIN_RR:.1f} | Risiko maks 1% per trade\n"
            "Signal-only · 24 jam non-stop"
        )

    while True:
        started = time.time()
        try:
            log.info("Mulai siklus...")
            closed_n = check_open(h)

            due = (time.time() - float(h.get("last_report_ts", 0))) >= REPORT_EVERY_HOURS * 3600
            if due:
                running_report(h)
                stats_report(h)
                h["last_report_ts"] = time.time()
            elif closed_n:
                stats_report(h)

            scan(h)
            save_history(h)

        except KeyboardInterrupt:
            log.info("Dihentikan.")
            break
        except Exception as e:
            log.exception("ERROR LOOP UTAMA: %s", e)

        elapsed = time.time() - started
        log.info(
            "Siklus selesai %.1fs | History: %s | Open: %s | Closed: %s",
            elapsed, HISTORY_FILE, len(h["open"]), len(h["closed"])
        )

        if not RUN_FOREVER:
            log.info("Mode sekali jalan: selesai.")
            break

        sleep_for = max(10, SCAN_INTERVAL - elapsed)
        log.info("Tidur %.1f detik...", sleep_for)
        time.sleep(sleep_for)

if __name__ == "__main__":
    main()






