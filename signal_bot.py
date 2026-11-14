"""
signal_bot.py — ربات سیگنال کریپتو برای GitHub Actions.
اسکن چندنمادی از OKX، اجرای استراتژی‌های موفق، ارسال سیگنال به کانال تلگرام.

اجرا:
    python signal_bot.py

متغیرهای محیطی (در GitHub Secrets):
    BOT_TOKEN    — توکن ربات تلگرام
    CHANNEL_ID   — شناسه کانال (مثلاً @mychannel یا -100123456)
    SCAN_MODE    — اختیاری: "once" (پیش‌فرض) یا "live"

GitHub Actions این اسکریپت را هر ۵ دقیقه اجرا می‌کند.
"""
import os
import sys
import json
import html
import io
import time
import random
import threading
import concurrent.futures
from datetime import datetime, timezone, timedelta
from math import ceil

import requests
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

# فونت فارسی (در صورت موجود بودن)
try:
    import matplotlib.font_manager as fm
    for fp in ['/usr/share/fonts/truetype/noto-serif-sc/NotoSerifSC-Regular.ttf',
               '/usr/share/fonts/truetype/chinese/NotoSansSC-Regular.ttf']:
        if os.path.exists(fp):
            fm.fontManager.addfont(fp); break
except Exception:
    pass

# ماژول‌های محلی
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategies import STRATEGY_FUNCS, DEFAULT_PARAMS

# ==========================================================
# تنظیمات
# ==========================================================
INTERVAL = os.environ.get("INTERVAL", "5")  # 1/3/5/15/30/60/240/D
KLINE_LIMIT = 300                            # تعداد کندل برای تحلیل
TOP_N_SYMBOLS = 200                          # حداکثر نماد برای اسکن
MIN_VOL_USD_24H = 2_000_000                  # حداقل حجم 24h برای فیلتر
MAX_WORKERS = 6                              # تعداد thread همزمان
REQUEST_TIMEOUT = 10
MAX_RETRIES = 3
RATE_LIMIT_PER_SEC = 12                      # محدوده OKX
TELEGRAM_MAX_LEN = 4000
PHOTO_CAPTION_MAX = 1000
MAX_CHARTS_IN_GRID = 4                       # ۲×۲
MAX_SIGNALS_PER_RUN = 8                      # حداکثر سیگنال در هر اجرا

# جلسه فعال ایران (۹ تا ۲۳) = 05:30-19:30 UTC
ACTIVE_START_HOUR_IRAN = 9
ACTIVE_END_HOUR_IRAN = 23
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

BAR_MAP = {"1": "1m", "3": "3m", "5": "5m", "15": "15m",
           "30": "30m", "60": "1H", "240": "4H", "D": "1Dutc"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "strategies_config.json")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
STATE_PATH = os.path.join(BASE_DIR, "signal_state.json")  # جلوگیری از سیگنال تکراری

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"})

_rate_lock = threading.Lock()
_last_times = []


def rate_limit():
    with _rate_lock:
        now = time.time()
        _last_times[:] = [t for t in _last_times if now - t < 1.0]
        if len(_last_times) >= RATE_LIMIT_PER_SEC:
            sleep_for = 1.0 - (now - _last_times[0])
            if sleep_for > 0:
                time.sleep(sleep_for)
        _last_times.append(time.time())


# ==========================================================
# ابزارها
# ==========================================================
def is_within_active_hours():
    """جلسه ایران ۹-۲۳."""
    return ACTIVE_START_HOUR_IRAN <= datetime.now(IRAN_TZ).hour < ACTIVE_END_HOUR_IRAN


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ خطا در خواندن {path}: {e}")
        return {}


def load_state():
    """حالت برای جلوگیری از ارسال سیگنال تکراری در یک بازه."""
    if not os.path.exists(STATE_PATH):
        return {"sent": {}}
    return load_json(STATE_PATH)


def save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ ذخیره state ناموفق: {e}")


def already_sent(state, key, cooldown_min=30):
    """آیا همین سیگنال در ۳۰ دقیقه گذشته ارسال شده؟"""
    sent = state.get("sent", {})
    if key in sent:
        last_ts = sent[key]
        if time.time() - last_ts < cooldown_min * 60:
            return True
    return False


def mark_sent(state, key):
    state.setdefault("sent", {})[key] = time.time()
    # پاکسازی کلیدهای قدیمی‌تر از ۲ ساعت
    cutoff = time.time() - 7200
    state["sent"] = {k: v for k, v in state["sent"].items() if v > cutoff}


def safe_div(a, b, default=0.0):
    try:
        if b == 0 or b is None:
            return default
        return a / b
    except (TypeError, ZeroDivisionError):
        return default


# ==========================================================
# تلگرام
# ==========================================================
def send_telegram_message(message):
    token = os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("CHANNEL_ID")
    if not token or not chat_id:
        print("🚨 BOT_TOKEN یا CHANNEL_ID تنظیم نشده!")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": message,
                                     "parse_mode": "HTML"}, timeout=15)
        if r.status_code != 200:
            print(f"❌ تلگرام {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"❌ خطای شبکه تلگرام: {e}")
        return False


def send_telegram_photo(photo_buf, caption):
    token = os.environ.get("BOT_TOKEN")
    chat_id = os.environ.get("CHANNEL_ID")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        files = {"photo": ("chart.png", photo_buf, "image/png")}
        data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
        r = requests.post(url, data=data, files=files, timeout=30)
        if r.status_code != 200:
            print(f"❌ sendPhoto {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"❌ photo error: {e}")
        return False


def send_long_message(text):
    lines, chunk, ok = text.split("\n"), "", True
    for line in lines:
        if len(chunk) + len(line) + 1 > TELEGRAM_MAX_LEN:
            ok &= send_telegram_message(chunk)
            chunk = ""
            time.sleep(1)
        chunk += line + "\n"
    if chunk.strip():
        ok &= send_telegram_message(chunk)
    return ok


# ==========================================================
# دریافت نمادها و کندل‌ها از OKX
# ==========================================================
def get_top_symbols():
    """دریافت لیست نمادهای پرحجم از OKX (SWAP = فیوچرز)."""
    try:
        url = "https://www.okx.com/api/v5/market/tickers?instType=SWAP"
        r = session.get(url, timeout=15)
        data = r.json()
        if data.get("code") != "0":
            print(f"⚠️ خطا در دریافت لیست نمادها: {data.get('msg')}")
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
            if vol_usd < MIN_VOL_USD_24H:
                continue
            rows.append((inst, vol_usd))
        rows.sort(key=lambda x: x[1], reverse=True)
        selected = [inst for inst, _ in rows[:TOP_N_SYMBOLS]]
        print(f"📋 {len(selected)} نماد معتبر از OKX دریافت شد.")
        return selected
    except Exception as e:
        print(f"❌ خطا در get_top_symbols: {e}")
        return []


def fetch_candles(inst_id):
    """دریافت کندل‌ها از OKX. خروجی: لیست [ts, o, h, l, c, vol, ...] یا None."""
    bar = BAR_MAP.get(INTERVAL, "5m")
    url = (f"https://www.okx.com/api/v5/market/candles"
           f"?instId={inst_id}&bar={bar}&limit={KLINE_LIMIT}")
    for attempt in range(MAX_RETRIES):
        try:
            rate_limit()
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            if r.status_code == 429:
                wait = (2 ** attempt) + random.uniform(0.3, 1.0)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "0":
                return None
            return data.get("data", [])
        except requests.exceptions.RequestException:
            time.sleep(1 + attempt)
    return None


def get_closed_candles(inst_id):
    """فقط کندل‌های بسته‌شده (confirm=1) به ترتیب زمانی صعودی."""
    raw = fetch_candles(inst_id)
    if not raw:
        return None
    closed = [c for c in raw if c and len(c) > 5 and str(c[-1]) == "1"]
    closed.reverse()  # OKX نزولی برمی‌گرداند
    return closed if len(closed) >= 30 else None


# ==========================================================
# اجرای استراتژی‌ها روی یک نماد
# ==========================================================
def run_strategies_for_symbol(sym_cfg, strat_configs):
    """اجرای استراتژی‌های فعال روی یک نماد. برمی‌گرداند: list of signal dicts."""
    name = sym_cfg['name']
    okx_inst = sym_cfg.get('okx_inst', name + "-USDT-SWAP")
    allowed = sym_cfg.get('strategies', list(STRATEGY_FUNCS.keys()))
    candles = get_closed_candles(okx_inst)
    if not candles:
        return []
    last_price = float(candles[-1][4])
    signals = []
    for strat_name in allowed:
        if strat_name not in STRATEGY_FUNCS:
            continue
        cfg = strat_configs.get('strategies', {}).get(strat_name, {})
        if not cfg.get('enabled', True):
            continue
        params = {k: v for k, v in cfg.items() if k not in ('enabled', 'backtest')}
        # merge با پیش‌فرض‌ها
        merged = dict(DEFAULT_PARAMS.get(strat_name, {}))
        merged.update(params)
        try:
            sig = STRATEGY_FUNCS[strat_name](candles, merged, last_price)
        except Exception as e:
            print(f"❌ {name}/{strat_name}: {type(e).__name__}: {e}")
            continue
        if sig:
            sig['symbol'] = name
            sig['strategy'] = strat_name
            sig['interval'] = INTERVAL
            sig['backtest'] = cfg.get('backtest', {})
            signals.append(sig)
    return signals


# ==========================================================
# چارت کندلی
# ==========================================================
def generate_combined_chart(signals_top):
    """ساخت چارت ۲×۲ از سیگنال‌های برتر. خروجی: BytesIO یا None."""
    try:
        n = min(len(signals_top), MAX_CHARTS_IN_GRID)
        if n == 0:
            return None
        cols = 2
        rows = ceil(n / cols)
        fig, axes = plt.subplots(rows, cols, figsize=(12, 5 * rows), dpi=100)
        if rows * cols == 1:
            axes = [axes]
        elif rows == 1 or cols == 1:
            axes = list(axes)
        else:
            axes = axes.flatten()

        for idx in range(n):
            ax = axes[idx]
            sig = signals_top[idx]
            # دوباره کندل‌ها را بگیریم برای همین نماد
            candles = sig.get('_candles')
            if candles is None:
                ax.axis("off")
                continue
            subset = candles[-60:]
            if len(subset) < 5:
                ax.axis("off")
                continue
            opens = [float(c[1]) for c in subset]
            highs = [float(c[2]) for c in subset]
            lows = [float(c[3]) for c in subset]
            closes = [float(c[4]) for c in subset]
            for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
                color = "#26a69a" if c >= o else "#ef5350"
                ax.plot([i, i], [l, h], color="#333", linewidth=0.6, zorder=2)
                ax.add_patch(Rectangle(
                    (i - 0.3, min(o, c)), 0.6, max(abs(c - o), 1e-9),
                    facecolor=color, edgecolor="#333", linewidth=0.4, zorder=3
                ))
            # نشانگر نقطه ورود روی آخرین کندل
            si = len(subset) - 1
            ax.add_patch(Rectangle(
                (si - 0.4, min(opens[si], closes[si])),
                0.8, max(abs(closes[si] - opens[si]), 1e-9),
                facecolor="none", edgecolor="#1565c0", linewidth=2.0, zorder=4
            ))
            side = sig['side']
            arrow = "▲" if side == 'buy' else "▼"
            col = "#26a69a" if side == 'buy' else "#ef5350"
            ax.set_title(
                f"{sig['symbol']} {arrow} {sig['strategy']} | {sig['entry']:.6g}",
                fontsize=10, fontweight="bold", color=col
            )
            ax.grid(alpha=0.25, linestyle="--")
            ax.set_xlim(-1, len(subset))
            ax.tick_params(labelsize=8)

        for idx in range(n, len(axes)):
            axes[idx].axis("off")

        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"❌ chart: {type(e).__name__}: {e}")
        return None


# ==========================================================
# ساخت پیام سیگنال
# ==========================================================
def fmt_price(p):
    if p >= 1000:
        return f"{p:,.2f}"
    if p >= 1:
        return f"{p:.4f}"
    if p >= 0.01:
        return f"{p:.6f}"
    return f"{p:.8f}"


def build_signal_message(sig):
    """ساخت پیام تلگرام برای یک سیگنال."""
    side_emoji = "🟢 خرید (LONG)" if sig['side'] == 'buy' else "🔴 فروش (SHORT)"
    now_iran = datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M")
    bt = sig.get('backtest', {})
    bt_str = ""
    if bt:
        bt_str = (f"\n📊 <b>بک‌تست:</b> وین‌ریت {bt.get('win_rate','—')}٪ | "
                  f"PF {bt.get('pf','—')} | بازده {bt.get('return','—')}٪ | DD {bt.get('dd','—')}٪")
    rr_str = f"RR ≈ {sig.get('rr_hint','2:1')}"
    msg = (
        f"🚨 <b>سیگنال معاملاتی</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💱 <b>نماد:</b> {html.escape(sig['symbol'])}\n"
        f"🎯 <b>جهت:</b> {side_emoji}\n"
        f"⏱ <b>تایم‌فریم:</b> {sig['interval']} دقیقه\n"
        f"⚙️ <b>استراتژی:</b> {html.escape(sig['strategy'])}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ورود:</b> <code>{fmt_price(sig['entry'])}</code>\n"
        f"🛑 <b>حد ضرر (SL):</b> <code>{fmt_price(sig['stop'])}</code>\n"
        f"🎯 <b>حد سود (TP):</b> <code>{fmt_price(sig['tp'])}</code>\n"
        f"📐 {rr_str}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📝 {html.escape(sig.get('info',''))}{bt_str}\n"
        f"🕐 {now_iran} (تهران)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>این سیگنال بر اساس بک‌تست تولید شده و سرمایه‌گذاری نیست.</i>"
    )
    return msg


def build_summary_caption(signals_top, total_count, scanned):
    """caption کوتاه برای عکس چارت."""
    now_iran = datetime.now(IRAN_TZ).strftime("%H:%M")
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    lines = [
        f"🚨 <b>اسکنر کریپتو | تایم {INTERVAL} دقیقه</b>",
        f"🕐 {now_iran} ایران | اسکن {scanned} نماد | یافت: {total_count} سیگنال",
        "",
    ]
    for i, sig in enumerate(signals_top):
        medal = medals[i] if i < len(medals) else "•"
        side = "🟢" if sig['side'] == 'buy' else "🔴"
        lines.append(
            f"{medal} <b>{html.escape(sig['symbol'])}</b> {side} {html.escape(sig['strategy'])} | "
            f"<code>{fmt_price(sig['entry'])}</code>"
        )
    caption = "\n".join(lines)
    if len(caption) > PHOTO_CAPTION_MAX:
        caption = caption[:PHOTO_CAPTION_MAX - 3] + "..."
    return caption


def build_extra_message(extra, total_count):
    lines = [f"📋 <b>سایر سیگنال‌ها</b> ({len(extra)} از {total_count}):", ""]
    for sig in extra:
        side = "🟢" if sig['side'] == 'buy' else "🔴"
        lines.append(
            f"• <b>{html.escape(sig['symbol'])}</b> {side} {html.escape(sig['strategy'])} | "
            f"<code>{fmt_price(sig['entry'])}</code>"
        )
    return "\n".join(lines)


# ==========================================================
# main
# ==========================================================
def main():
    if not is_within_active_hours():
        print(f"⏸️ خارج از بازه فعال ({ACTIVE_START_HOUR_IRAN}-{ACTIVE_END_HOUR_IRAN} ایران).")
        return

    strat_configs = load_json(CONFIG_PATH)
    watchlist = load_json(WATCHLIST_PATH)
    symbols = watchlist.get('symbols', [])
    if not symbols:
        print("❌ watchlist خالی است. استفاده از فهرست پرحجم OKX.")
        okx_insts = get_top_symbols()
        symbols = [{'name': inst.replace("-USDT-SWAP", ""), 'okx_inst': inst,
                    'strategies': list(STRATEGY_FUNCS.keys())}
                   for inst in okx_insts]
    print(f"🔍 اسکن {len(symbols)} نماد | تایم {INTERVAL}m | استراتژی‌ها فعال")

    state = load_state()
    t0 = time.time()
    all_signals = []
    failed = 0

    def worker(sym_cfg):
        try:
            return run_strategies_for_symbol(sym_cfg, strat_configs)
        except Exception as e:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(worker, s): s for s in symbols}
        for fut in concurrent.futures.as_completed(futures):
            try:
                sigs = fut.result()
            except Exception as e:
                failed += 1
                continue
            all_signals.extend(sigs)

    elapsed = time.time() - t0
    print(f"⏱ زمان اسکن: {elapsed:.1f}s | ناموفق: {failed} | سیگنال خام: {len(all_signals)}")

    if not all_signals:
        print("ℹ️ هیچ سیگنالی یافت نشد.")
        return

    # فیلتر تکرار (cooldown)
    fresh = []
    for sig in all_signals:
        key = f"{sig['symbol']}|{sig['strategy']}|{sig['side']}"
        if not already_sent(state, key, cooldown_min=30):
            fresh.append(sig)
        else:
            print(f"⏭️ تکراری (cooldown): {key}")
    print(f"✅ سیگنال تازه: {len(fresh)}")

    if not fresh:
        print("ℹ️ همه سیگنال‌ها تکراری بودند.")
        return

    # محدودیت تعداد
    fresh = fresh[:MAX_SIGNALS_PER_RUN]
    top = fresh[:MAX_CHARTS_IN_GRID]
    extra = fresh[MAX_CHARTS_IN_GRID:]

    # گرفتن کندل برای چارت
    for sig in top:
        okx_inst = sig['symbol'] + "-USDT-SWAP"
        sig['_candles'] = get_closed_candles(okx_inst)

    # ارسال پیام‌های جداگانه برای هر سیگنال (با جزئیات کامل)
    sent_count = 0
    for sig in fresh:
        key = f"{sig['symbol']}|{sig['strategy']}|{sig['side']}"
        msg = build_signal_message(sig)
        if send_telegram_message(msg):
            mark_sent(state, key)
            sent_count += 1
            print(f"✅ ارسال شد: {sig['symbol']} {sig['strategy']} {sig['side']}")
            time.sleep(1.5)  # جلوگیری از spam تلگرام

    save_state(state)

    # ارسال چارت خلاصه برای ۴ سیگنال برتر
    if top:
        time.sleep(1.0)
        buf = generate_combined_chart(top)
        if buf:
            caption = build_summary_caption(top, len(fresh), len(symbols))
            if send_telegram_photo(buf, caption):
                print(f"📊 چارت خلاصه ارسال شد.")
            else:
                send_long_message(caption)

    # پیام تکمیلی اگر سیگنال بیشتر از ۴ است
    if extra:
        time.sleep(1.0)
        send_long_message(build_extra_message(extra, len(fresh)))

    print(f"\n🏁 پایان: {sent_count}/{len(fresh)} سیگنال ارسال شد.")


if __name__ == "__main__":
    main()
