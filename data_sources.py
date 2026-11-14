"""
data_sources.py — لایه انتزاع منابع داده (OKX و Binance).
هر منبع یک کلاس با متدهای یکسان: get_top_symbols() و fetch_candles().
"""
import time
import random
import threading
import requests

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})

_rate_lock = threading.Lock()
_last_times = []

# مپ تایم‌فریم به فرمت هر صرافی
OKX_BAR_MAP = {"1": "1m", "3": "3m", "5": "5m", "15": "15m",
               "30": "30m", "60": "1H", "240": "4H", "D": "1Dutc"}
BINANCE_BAR_MAP = {"1": "1m", "3": "3m", "5": "5m", "15": "15m",
                   "30": "30m", "60": "1h", "240": "4h", "D": "1d"}


def rate_limit(max_per_sec=12):
    with _rate_lock:
        now = time.time()
        _last_times[:] = [t for t in _last_times if now - t < 1.0]
        if len(_last_times) >= max_per_sec:
            sleep_for = 1.0 - (now - _last_times[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        _last_times.append(time.time())


def to_standard(raw, source):
    """تبدیل خروجی صرافی به فرمت استاندارد: [ts_ms, o, h, l, c, vol, confirm]."""
    out = []
    if source == 'okx':
        # OKX: [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        for c in raw:
            if len(c) < 7:
                continue
            confirm = str(c[-1]) == "1"
            out.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]),
                        float(c[4]), float(c[5]), confirm])
    elif source == 'binance':
        # Binance: [open_time, o, h, l, c, vol, close_time, quote_vol, trades, ...]
        for c in raw:
            if len(c) < 6:
                continue
            # کندل بسته اگر close_time گذشته باشد
            confirm = True  # Binance klines همگی بسته‌اند مگر آخرین
            out.append([int(c[0]), float(c[1]), float(c[2]), float(c[3]),
                        float(c[4]), float(c[5]), confirm])
    return out


# ==========================================================
# OKX
# ==========================================================
class OKXSource:
    name = 'okx'
    base_url = 'https://www.okx.com/api/v5'

    def __init__(self, interval='5', kline_limit=300, max_retries=3):
        self.interval = interval
        self.kline_limit = kline_limit
        self.max_retries = max_retries

    def get_top_symbols(self, top_n=200, min_vol_usd=2_000_000):
        try:
            url = f"{self.base_url}/market/tickers?instType=SWAP"
            r = session.get(url, timeout=15)
            data = r.json()
            if data.get("code") != "0":
                print(f"⚠️ OKX tickers خطا: {data.get('msg')}")
                return []
            rows = []
            for t in data.get("data", []):
                inst = t.get("instId", "")
                if not inst.endswith("-USDT-SWAP"):
                    continue
                try:
                    last = float(t.get("last") or 0)
                    vol_ccy = float(t.get("volCcy24h") or 0)
                    vol_usd = vol_ccy * last
                except ValueError:
                    continue
                if vol_usd < min_vol_usd:
                    continue
                rows.append((inst, vol_usd))
            rows.sort(key=lambda x: x[1], reverse=True)
            return [inst for inst, _ in rows[:top_n]]
        except Exception as e:
            print(f"❌ OKX get_top_symbols: {e}")
            return []

    def fetch_candles(self, inst_id):
        bar = OKX_BAR_MAP.get(self.interval, "5m")
        url = (f"{self.base_url}/market/candles"
               f"?instId={inst_id}&bar={bar}&limit={self.kline_limit}")
        for attempt in range(self.max_retries):
            try:
                rate_limit()
                r = session.get(url, timeout=10)
                if r.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0.3, 1.0)
                    time.sleep(wait); continue
                if r.status_code != 200:
                    return None
                data = r.json()
                if data.get("code") != "0":
                    return None
                raw = data.get("data", [])
                std = to_standard(raw, 'okx')
                std.reverse()  # OKX نزولی برمی‌گرداند
                return std
            except requests.exceptions.RequestException:
                time.sleep(1 + attempt)
        return None

    def symbol_to_inst(self, symbol):
        """BTCUSDT -> BTC-USDT-SWAP"""
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            return f"{base}-USDT-SWAP"
        return symbol


# ==========================================================
# Binance
# ==========================================================
class BinanceSource:
    name = 'binance'
    base_url = 'https://api.binance.com'

    def __init__(self, interval='5', kline_limit=300, max_retries=3):
        self.interval = interval
        self.kline_limit = kline_limit
        self.max_retries = max_retries

    def get_top_symbols(self, top_n=200, min_vol_usd=2_000_000):
        try:
            # 24hr ticker — قیمت و حجم همه نمادها
            url = f"{self.base_url}/api/v3/ticker/24hr"
            r = session.get(url, timeout=20)
            data = r.json()
            if not isinstance(data, list):
                return []
            rows = []
            for t in data:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                if sym in ("USDCUSDT", "BUSDUSDT", "TUSDUSDT", "FDUSDUSDT", "PAXGUSDT"):
                    continue  # استیبل‌کوین‌ها و طلا را حذف
                try:
                    vol_usd = float(t.get("quoteVolume") or 0)
                except ValueError:
                    continue
                if vol_usd < min_vol_usd:
                    continue
                rows.append((sym, vol_usd))
            rows.sort(key=lambda x: x[1], reverse=True)
            return [s for s, _ in rows[:top_n]]
        except Exception as e:
            print(f"❌ Binance get_top_symbols: {e}")
            return []

    def fetch_candles(self, inst_id):
        """inst_id برای Binance همان نماد است: BTCUSDT"""
        bar = BINANCE_BAR_MAP.get(self.interval, "5m")
        url = (f"{self.base_url}/api/v3/klines"
               f"?symbol={inst_id}&interval={bar}&limit={self.kline_limit}")
        for attempt in range(self.max_retries):
            try:
                rate_limit()
                r = session.get(url, timeout=10)
                if r.status_code == 429:
                    wait = (2 ** attempt) + random.uniform(0.3, 1.0)
                    time.sleep(wait); continue
                if r.status_code != 200:
                    return None
                raw = r.json()
                std = to_standard(raw, 'binance')
                # Binance صعودی برمی‌گرداند — به جز آخرین کندل که ممکن است باز باشد
                if std:
                    std[-1] = std[-1][:6] + [True]  # برای بات فقط کندل‌های بسته می‌خواهیم
                # حذف آخرین (کندل در حال تشکیل)
                if len(std) > 1:
                    std = std[:-1]
                return std
            except requests.exceptions.RequestException:
                time.sleep(1 + attempt)
        return None

    def symbol_to_inst(self, symbol):
        return symbol  # Binance نماد همان inst است


# ==========================================================
# Factory
# ==========================================================
def get_source(name, interval='5', kline_limit=300):
    if name == 'binance':
        return BinanceSource(interval, kline_limit)
    return OKXSource(interval, kline_limit)  # پیش‌فرض OKX
