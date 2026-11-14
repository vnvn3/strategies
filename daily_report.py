"""
daily_report.py — گزارش روزانه عملکرد ربات.
خلاصه‌ای از سیگنال‌های ارسالی در ۲۴ ساعت گذشته را به کانال تلگرام می‌فرستد.

اجرا:
    python daily_report.py

در GitHub Actions روزانه ساعت ۲۳:۳۰ ایران (۲۰:۰۰ UTC) اجرا می‌شود.
"""
import os
import sys
import json
import html
from datetime import datetime, timezone, timedelta
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import telegram_sender as tg

IRAN_TZ = timezone(timedelta(hours=3, minutes=30))
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(BASE_DIR, "signal_state.json")


def load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"sent": {}, "log": []}


def main():
    state = load_state()
    log = state.get("log", [])
    now = time_now = time_now_ts = None
    cutoff = datetime.now(IRAN_TZ).timestamp() - 24 * 3600  # آخرین ۲۴ ساعت
    recent = [r for r in log if r.get("ts", 0) >= cutoff]
    today = datetime.now(IRAN_TZ).strftime("%Y-%m-%d")

    if not recent:
        msg = (f"📊 <b>گزارش روزانه ربات سیگنال</b>\n"
               f"🗓 تاریخ: {today}\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"📭 هیچ سیگنالی در ۲۴ ساعت گذشته ارسال نشده است.\n"
               f"━━━━━━━━━━━━━━━━\n"
               f"⚠️ <i>این ممکن است به دلیل خارج بودن بازار از جلسه فعال یا نبود ستاپ معتبر باشد.</i>")
        tg.send_message(msg)
        print("📭 گزارش: بدون سیگنال.")
        return

    # آمار
    n = len(recent)
    buys = [r for r in recent if r['side'] == 'buy']
    sells = [r for r in recent if r['side'] == 'sell']
    by_symbol = Counter(r['symbol'] for r in recent)
    by_strategy = Counter(r['strategy'] for r in recent)

    # ستاپ برتر نماد
    top_sym = by_symbol.most_common(5)
    top_strat = by_strategy.most_common(5)

    # محاسبه RR هر سیگنال
    rrs = []
    for r in recent:
        risk = abs(r['entry'] - r['stop']) / r['entry'] * 100 if r['entry'] > 0 else 0
        reward = abs(r['tp'] - r['entry']) / r['entry'] * 100 if r['entry'] > 0 else 0
        if risk > 0:
            rrs.append(reward / risk)

    avg_rr = sum(rrs) / len(rrs) if rrs else 0

    # ساخت پیام
    lines = [
        f"📊 <b>گزارش روزانه ربات سیگنال</b>",
        f"🗓 تاریخ: {today}",
        f"━━━━━━━━━━━━━━━━",
        f"📈 <b>کل سیگنال‌های ارسالی:</b> {n}",
        f"🟢 خرید (LONG): {len(buys)} | 🔴 فروش (SHORT): {len(sells)}",
        f"📐 میانگین RR: 1:{avg_rr:.2f}",
        f"",
        f"🏆 <b>۵ نماد برتر:</b>",
    ]
    for sym, cnt in top_sym:
        lines.append(f"   • {html.escape(sym)}: {cnt} سیگنال")
    lines.append("")
    lines.append("⚙️ <b>۵ استراتژی برتر:</b>")
    for strat, cnt in top_strat:
        lines.append(f"   • {html.escape(strat)}: {cnt} سیگنال")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("📋 <b>آخرین ۵ سیگنال:</b>")
    for r in recent[-5:]:
        side = "🟢" if r['side'] == 'buy' else "🔴"
        lines.append(f"   {side} {html.escape(r['symbol'])} | {html.escape(r['strategy'])} | {r['time_iran']}")
    lines.append("")
    lines.append("━━━━━━━━━━━━━━━━")
    lines.append("⚠️ <i>این گزارش فقط آمار سیگنال است؛ نتیجه معاملات را نشان نمی‌دهد.</i>")

    msg = "\n".join(lines)
    if tg.send_message(msg):
        print(f"✅ گزارش روزانه ارسال شد: {n} سیگنال.")
    else:
        print("❌ ارسال گزارش ناموفق.")


if __name__ == "__main__":
    main()
