"""
signal_bot.py — ربات سیگنال کریپتو (نسخه ساده) برای GitHub Actions.
اسکن چندنمادی از OKX، اجرای استراتژی‌های موفق، ارسال سیگنال به یک کانال تلگرام.

متغیرهای محیطی (در GitHub Secrets):
    BOT_TOKEN    — توکن ربات تلگرام
    CHANNEL_ID   — شناسه کانال (مثلاً @mychannel یا -100123456)
    INTERVAL     — اختیاری: 1/3/5/15/30/60/240/D (پیش‌فرض 5)
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from strategies import STRATEGY_FUNCS, DEFAULT_PARAMS

# ==========================================================
# تنظیمات
# ==========================================================
INTERVAL = os.environ.get("INTERVAL", "5")
KLINE_LIMIT = 300
MAX_WORKERS = 6
MAX_RETRIES = 3
RATE_LIMIT_PER_SEC = 12
MAX_CHARTS_IN_GRID = 4
MAX_SIGNALS_PER_RUN = 8
COOLDOWN_MIN = 30  # جلوگیری از سیگنال تکراری

# جلسه فعال ایران (۹ تا ۲۳)
ACTIVE_START_HOUR = 9
ACTIVE_END_HOUR = 23
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

BAR_MAP = {"1": "1m", "3": "3m", "5": "5m", "15": "15m",
           "30": "30m", "60": "1H", "240": "4H", "D": "1Dutc"}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "strategies_config.json")
WATCHLIST_PATH = os.path.join(BASE_DIR, "watchlist.json")
STATE_PATH = os.path.join(BASE_DIR, "signal_state.json")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0"})

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
    return ACTIVE_START_HOUR <= datetime.now(IRAN_TZ).hour < ACTIVE_END_HOUR


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def save_json(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ ذخیره ناموفق {path}: {e}")


def load_state():
    return load_json(STATE_PATH, {"sent": {}})


def already_sent(state, key):
    sent = state.get("sent", {})
    if key in sent and time.time() - sent[key] < COOLDOWN_MIN * 60:
        return True
    return False


def mark_sent(state, key):
    state.setdefault("sent", {})[key] = time.time()
    cutoff = time.time() - 7200
    state["sent"] = {k: v for k, v in state["sent"].items() if v > cutoff}


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
        data = {"chat_id": chat_id, "caption": caption[:1000], "parse_mode": "HTML"}
        r = requests.post(url, data=data, files=files, timeout=30)
        if r.status_code != 200:
            print(f"❌ sendPhoto {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"❌ photo error: {e}")
        return False


# ==========================================================
# دریافت کندل از OKX
# ==========================================================
def fetch_candles(inst_id):
    """دریافت کندل‌های بسته‌شده از OKX. خروجی: list [ts, o, h, l, c, vol] یا None."""
    bar = BAR_MAP.get(INTERVAL, "5m")
    url = (f"https://www.okx.com/api/v5/market/candles"
           f"?instId={inst_id}&bar={bar}&limit={KLINE_LIMIT}")
    for attempt in range(MAX_RETRIES):
        try:
            rate_limit()
            r = session.get(url, timeout=10)
            if r.status_code == 429:
                time.sleep((2 ** attempt) + random.uniform(0.3, 1.0))
                continue
            if r.status_code != 200:
                return None
            data = r.json()
            if data.get("code") != "0":
                return None
            raw = data.get("data", [])
            # فقط کندل‌های بسته‌شده (confirm=1)، به ترتیب زمانی صعودی
            closed = [c for c in raw if c and len(c) > 5 and str(c[-1]) == "1"]
            closed.reverse()
            return closed if len(closed) >= 30 else None
        except requests.exceptions.RequestException:
            time.sleep(1 + attempt)
    return None


# ==========================================================
# اجرای استراتژی‌ها روی یک نماد
# ==========================================================
def run_strategies_for_symbol(sym_cfg, strat_configs):
    name = sym_cfg['name']
    inst_id = sym_cfg.get('okx_inst', name + "-USDT-SWAP")
    allowed = sym_cfg.get('strategies', list(STRATEGY_FUNCS.keys()))
    candles = fetch_candles(inst_id)
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
# چارت کندلی ۲×۲
# ==========================================================
def generate_combined_chart(signals_top):
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
            candles = sig.get('_candles')
            if candles is None:
                ax.axis("off"); continue
            subset = candles[-60:]
            if len(subset) < 5:
                ax.axis("off"); continue
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
            si = len(subset) - 1
            ax.add_patch(Rectangle(
                (si - 0.4, min(opens[si], closes[si])),
                0.8, max(abs(closes[si] - opens[si]), 1e-9),
                facecolor="none", edgecolor="#1565c0", linewidth=2.0, zorder=4
            ))
            side = sig['side']
            arrow = "▲ LONG" if side == 'buy' else "▼ SHORT"
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
    if p >= 1000: return f"{p:,.2f}"
    if p >= 1: return f"{p:.4f}"
    if p >= 0.01: return f"{p:.6f}"
    return f"{p:.8f}"


def build_signal_message(sig):
    side_emoji = "🟢 خرید (LONG)" if sig['side'] == 'buy' else "🔴 فروش (SHORT)"
    now_iran = datetime.now(IRAN_TZ).strftime("%Y-%m-%d %H:%M")
    bt = sig.get('backtest', {})
    bt_str = ""
    if bt:
        bt_str = (f"\n📊 <b>بک‌تست:</b> وین‌ریت {bt.get('win_rate','—')}٪ | "
                  f"PF {bt.get('pf','—')} | بازده {bt.get('return','—')}٪ | DD {bt.get('dd','—')}٪")
    risk_pct = abs(sig['entry'] - sig['stop']) / sig['entry'] * 100 if sig['entry'] else 0
    reward_pct = abs(sig['tp'] - sig['entry']) / sig['entry'] * 100 if sig['entry'] else 0
    rr_actual = reward_pct / risk_pct if risk_pct > 0 else 0
    return (
        f"🚨 <b>سیگنال معاملاتی</b>\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💱 <b>نماد:</b> {html.escape(sig['symbol'])}\n"
        f"🎯 <b>جهت:</b> {side_emoji}\n"
        f"⏱ <b>تایم‌فریم:</b> {sig['interval']} دقیقه\n"
        f"⚙️ <b>استراتژی:</b> {html.escape(sig['strategy'])}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ورود:</b> <code>{fmt_price(sig['entry'])}</code>\n"
        f"🛑 <b>حد ضرر (SL):</b> <code>{fmt_price(sig['stop'])}</code> ({risk_pct:.2f}%)\n"
        f"🎯 <b>حد سود (TP):</b> <code>{fmt_price(sig['tp'])}</code> ({reward_pct:.2f}%)\n"
        f"📐 RR = 1:{rr_actual:.1f}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📝 {html.escape(sig.get('info',''))}{bt_str}\n"
        f"🕐 {now_iran} (تهران)\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>بر اساس بک‌تست تولید شده؛ سرمایه‌گذاری نیست.</i>"
    )


def build_summary_caption(signals_top, total_count, scanned):
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
    return "\n".join(lines)


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
        print(f"⏸️ خارج از بازه فعال ({ACTIVE_START_HOUR}-{ACTIVE_END_HOUR} ایران).")
        return

    strat_configs = load_json(CONFIG_PATH)
    watchlist = load_json(WATCHLIST_PATH)
    symbols = watchlist.get('symbols', [])
    if not symbols:
        print("❌ watchlist خالی است.")
        return
    print(f"🔍 اسکن {len(symbols)} نماد | تایم {INTERVAL}m")

    state = load_state()
    t0 = time.time()
    all_signals = []
    failed = 0

    def worker(sym_cfg):
        try:
            return run_strategies_for_symbol(sym_cfg, strat_configs)
        except Exception:
            return []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(worker, s): s for s in symbols}
        for fut in concurrent.futures.as_completed(futures):
            try:
                sigs = fut.result()
            except Exception:
                failed += 1; continue
            all_signals.extend(sigs)

    elapsed = time.time() - t0
    print(f"⏱ زمان اسکن: {elapsed:.1f}s | ناموفق: {failed} | سیگنال خام: {len(all_signals)}")

    if not all_signals:
        print("ℹ️ هیچ سیگنالی یافت نشد.")
        save_json(STATE_PATH, state)
        return

    # فیلتر تکرار
    fresh = []
    for sig in all_signals:
        key = f"{sig['symbol']}|{sig['strategy']}|{sig['side']}"
        if not already_sent(state, key):
            fresh.append(sig)
        else:
            print(f"⏭️ تکراری (cooldown): {key}")
    print(f"✅ سیگنال تازه: {len(fresh)}")

    if not fresh:
        print("ℹ️ همه سیگنال‌ها تکراری بودند.")
        save_json(STATE_PATH, state)
        return

    fresh = fresh[:MAX_SIGNALS_PER_RUN]
    top = fresh[:MAX_CHARTS_IN_GRID]
    extra = fresh[MAX_CHARTS_IN_GRID:]

    # گرفتن کندل برای چارت (تبدیل SOLUSDT -> SOL-USDT-SWAP)
    for sig in top:
        base = sig['symbol'].replace("USDT", "")
        inst_id = f"{base}-USDT-SWAP"
        sig['_candles'] = fetch_candles(inst_id)

    # ارسال پیام جداگانه برای هر سیگنال
    sent_count = 0
    for sig in fresh:
        key = f"{sig['symbol']}|{sig['strategy']}|{sig['side']}"
        msg = build_signal_message(sig)
        if send_telegram_message(msg):
            mark_sent(state, key)
            sent_count += 1
            print(f"✅ ارسال شد: {sig['symbol']} {sig['strategy']} {sig['side']}")
            time.sleep(1.5)

    save_json(STATE_PATH, state)

    # چارت خلاصه ۲×۲
    if top:
        time.sleep(1.0)
        buf = generate_combined_chart(top)
        if buf:
            caption = build_summary_caption(top, len(fresh), len(symbols))
            if not send_telegram_photo(buf, caption):
                send_telegram_message(caption)

    # پیام تکمیلی
    if extra:
        time.sleep(1.0)
        send_telegram_message(build_extra_message(extra, len(fresh)))

    print(f"\n🏁 پایان: {sent_count}/{len(fresh)} سیگنال ارسال شد.")


if __name__ == "__main__":
    main()
