"""
telegram_sender.py — ارسال پیام/عکس به تلگرام با پشتیبانی از چند کانال.

فرمت متغیر محیطی CHANNELS:
    CHANNELS=token1:chat_id1,token2:chat_id2,...
یا فرمت ساده قدیمی:
    BOT_TOKEN=token
    CHANNEL_ID=chat_id
"""
import os
import time
import html
import requests


def parse_channels():
    """خواندن پیکربندی کانال‌ها از متغیرهای محیطی.
    بازمی‌گرداند: list of (token, chat_id) — حداقل یک تا.”
    """
    channels = []
    # فرمت جدید: CHANNELS=tok1:chat1,tok2:chat2
    multi = os.environ.get("CHANNELS", "")
    if multi:
        for part in multi.split(","):
            part = part.strip()
            if ":" in part:
                tok, chat = part.split(":", 1)
                tok = tok.strip(); chat = chat.strip()
                if tok and chat:
                    channels.append((tok, chat))
    # فرمت قدیمی: BOT_TOKEN + CHANNEL_ID (پشتیبان عقبی)
    if not channels:
        tok = os.environ.get("BOT_TOKEN", "")
        chat = os.environ.get("CHANNEL_ID", "")
        if tok and chat:
            channels.append((tok, chat))
    return channels


def _send_one(token, chat_id, text=None, photo_buf=None, caption=None,
              parse_mode="HTML", timeout=30):
    """ارسال به یک کانال. اگر photo_buf داده شد → sendPhoto، در غیر این‌صورت sendMessage."""
    try:
        if photo_buf is not None:
            url = f"https://api.telegram.org/bot{token}/sendPhoto"
            files = {"photo": ("chart.png", photo_buf, "image/png")}
            data = {"chat_id": chat_id, "caption": caption or "",
                    "parse_mode": parse_mode}
            r = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text or "",
                       "parse_mode": parse_mode}
            r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            print(f"❌ تلگرام {chat_id} {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"❌ شبکه تلگرام ({chat_id}): {e}")
        return False


def send_message(text, channels=None):
    """ارسال پیام متنی به همه کانال‌ها. اگر text طولانی است → چندتایی."""
    if channels is None:
        channels = parse_channels()
    if not channels:
        print("🚨 هیچ کانالی تنظیم نشده! CHANNELS یا BOT_TOKEN/CHANNEL_ID را ست کنید.")
        return False
    MAX = 4000
    chunks = []
    if len(text) <= MAX:
        chunks = [text]
    else:
        lines = text.split("\n")
        cur = ""
        for ln in lines:
            if len(cur) + len(ln) + 1 > MAX:
                chunks.append(cur); cur = ""
            cur += ln + "\n"
        if cur.strip():
            chunks.append(cur)
    ok = True
    for tok, chat in channels:
        for i, chunk in enumerate(chunks):
            ok &= _send_one(tok, chat, text=chunk)
            if i < len(chunks) - 1:
                time.sleep(0.8)  # جلوگیری از flood
    return ok


def send_photo(photo_buf, caption="", channels=None):
    """ارسال عکس با caption به همه کانال‌ها. caption حداکثر 1024 کاراکتر."""
    if channels is None:
        channels = parse_channels()
    if not channels:
        print("🚨 هیچ کانالی تنظیم نشده!")
        return False
    if len(caption) > 1000:
        caption = caption[:997] + "..."
    ok = True
    for tok, chat in channels:
        ok &= _send_one(tok, chat, photo_buf=photo_buf, caption=caption)
    return ok
