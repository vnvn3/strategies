"""
strategies.py — استراتژی‌های موفق از بک‌تست.
هر تابع سیگنال یک candle جدید را بررسی می‌کند و در صورت فعال‌شدن ورودی،
یک dict با (side, entry, stop, tp, info) برمی‌گرداند.

ورودی: candles (list), params (dict), cur_price (float)
خروجی: None یا dict(side='buy'/'sell', entry=float, stop=float, tp=float, info=str)
"""
import numpy as np
from indicators import (
    to_arrays, ema, rsi, atr, bollinger, keltner, macd, stochastic,
    vwap_intraday, supertrend, adx, sma
)


def _last_valid(arr):
    """آخرین مقدار غیر-NaN."""
    v = arr[~np.isnan(arr)]
    return v[-1] if len(v) > 0 else None


# ==========================================================
# 1. SP2L — اسپایک → لگ دوم
# ==========================================================
def sp2l(candles, p, cur_price):
    """
    اسپایک = ۲-۵ کندل بزرگ هم‌جهت. سپس شکار لگ دوم:
      صعودی: کندل اصلاحی کف کندل قبلی را بزند → خرید
      نزولی: کندل اصلاحی سقف کندل قبلی را بزند → فروش
    SL = پشت کندل شروع اسپایک. RR قابل تنظیم.
    """
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['lookback'] + 6:
        return None
    body = np.abs(c - o)
    avg_body = sma(body, p['lookback'])
    # جستجوی طولانی‌ترین اسپایک معتبر که در i-2 تمام می‌شود (i-1 = اولین کندل اصلاحی، i = کندل لگ دوم)
    i = n - 1
    end = i - 2  # آخرین کندل اسپایک
    if end < p['lookback'] + 1:
        return None
    found = None
    for length in range(p['max_spike_candles'], p['min_spike_candles'] - 1, -1):
        start = end - length + 1
        if start < p['lookback'] + 1:
            continue
        if np.isnan(avg_body[start]):
            continue
        idxs = list(range(start, end + 1))
        buy = all(c[k] > o[k] for k in idxs)
        sell = all(c[k] < o[k] for k in idxs)
        if not (buy or sell):
            continue
        big = all(body[k] >= p['body_mult'] * avg_body[k] for k in idxs)
        if not big:
            continue
        # کندل اصلاحی (end+1 = i-1) باید خلاف جهت باشد
        if buy and not (c[i-1] < o[i-1]):
            continue
        if sell and not (c[i-1] > o[i-1]):
            continue
        # لگ دوم: کندل i کف/سقف کندل قبلی (i-1) را بزند
        if buy:
            if l[i] < l[i-1]:
                found = ('buy', l[start], length, start, end)
                break
        else:
            if h[i] > h[i-1]:
                found = ('sell', h[start], length, start, end)
                break
    if found is None:
        return None
    side, sl_level, length, sp_start, sp_end = found
    entry = cur_price
    if side == 'buy':
        stop = sl_level
        rpu = entry - stop
        if rpu <= 0:
            return None
        tp = entry + p['rr'] * rpu
    else:
        stop = sl_level
        rpu = stop - entry
        if rpu <= 0:
            return None
        tp = entry - p['rr'] * rpu
    return dict(side=side, entry=entry, stop=stop, tp=tp,
                info=f"SP2L spike×{length} | SL پشت کندل شروع")


# ==========================================================
# 2. RSI2 — بازگشت کانرز (RSI دوره ۲)
# ==========================================================
def rsi2(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < 4:
        return None
    r = rsi(c, p['rsi_period'])
    a = atr(h, l, c, p['atr_period'])
    last_rsi = _last_valid(r)
    last_atr = _last_valid(a)
    if last_rsi is None or last_atr is None or last_atr <= 0:
        return None
    entry = cur_price
    if last_rsi < p['buy_threshold']:
        # خرید — اسپایک نزولی موقت
        stop = entry - p['stop_atr_mult'] * last_atr
        tp = entry + p['rr'] * (p['stop_atr_mult'] * last_atr)
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"RSI2={last_rsi:.1f} (oversold)")
    if last_rsi > 100 - p['buy_threshold']:
        stop = entry + p['stop_atr_mult'] * last_atr
        tp = entry - p['rr'] * (p['stop_atr_mult'] * last_atr)
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"RSI2={last_rsi:.1f} (overbought)")
    return None


# ==========================================================
# 3. Bollinger — بازگشت میانگین
# ==========================================================
def bollinger_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['bb_n'] + 2:
        return None
    upper, mid, lower = bollinger(c, p['bb_n'], p['bb_k'])
    a = atr(h, l, c, p['atr_period'])
    if np.isnan(lower[-1]) or np.isnan(upper[-1]) or np.isnan(mid[-1]) or np.isnan(a[-1]):
        return None
    entry = cur_price
    # سیگنال: کندل قبلی باند را لمس/شکست و کندل فعلی برگشت
    if c[-2] <= lower[-2] and c[-1] > c[-2]:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"BB bounce lower (close={c[-1]:.6g})")
    if c[-2] >= upper[-2] and c[-1] < c[-2]:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"BB reject upper (close={c[-1]:.6g})")
    return None


# ==========================================================
# 4. Keltner — بازگشت میانگین با باندهای ATR
# ==========================================================
def keltner_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['kc_n'] + 2:
        return None
    upper, mid, lower = keltner(c, h, l, p['kc_n'], p['kc_mult'], p['atr_period'])
    a = atr(h, l, c, p['atr_period'])
    if np.isnan(lower[-1]) or np.isnan(upper[-1]) or np.isnan(a[-1]):
        return None
    entry = cur_price
    if c[-2] < lower[-2] and c[-1] > c[-2]:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"KC bounce lower")
    if c[-2] > upper[-2] and c[-1] < c[-2]:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"KC reject upper")
    return None


# ==========================================================
# 5. VWAP — بازگشت به VWAP روزانه
# ==========================================================
def vwap_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['vwap_n'] + 2:
        return None
    vw = vwap_intraday(h, l, c, v)
    # انحراف استاندارد تقریبی از قیمت- VWAP
    dev = np.std(c[-p['vwap_n']:] - vw[-p['vwap_n']:])
    a = atr(h, l, c, 14)
    if np.isnan(vw[-1]) or np.isnan(a[-1]) or dev <= 0:
        return None
    lower_band = vw[-1] - p['vwap_dev'] * dev
    upper_band = vw[-1] + p['vwap_dev'] * dev
    entry = cur_price
    if c[-2] <= lower_band and c[-1] > vw[-1] * 0.999:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"VWAP bounce (below {p['vwap_dev']}σ)")
    if c[-2] >= upper_band and c[-1] < vw[-1] * 1.001:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"VWAP reject (above {p['vwap_dev']}σ)")
    return None


# ==========================================================
# 6. MACD + EMA200 — تقاطع هیستوگرام در جهت روند
# ==========================================================
def macd_ema_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['ema_trend'] + p['macd_slow'] + 2:
        return None
    macd_line, sig, hist = macd(c, p['macd_fast'], p['macd_slow'], p['macd_sig'])
    ema200 = ema(c, p['ema_trend'])
    a = atr(h, l, c, 14)
    if np.isnan(hist[-1]) or np.isnan(hist[-2]) or np.isnan(ema200[-1]) or np.isnan(a[-1]):
        return None
    entry = cur_price
    # تقاطع به بالا + قیمت بالای EMA200
    if hist[-2] <= 0 and hist[-1] > 0 and c[-1] > ema200[-1]:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"MACD hist cross↑ (above EMA{p['ema_trend']})")
    if hist[-2] >= 0 and hist[-1] < 0 and c[-1] < ema200[-1]:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"MACD hist cross↓ (below EMA{p['ema_trend']})")
    return None


# ==========================================================
# 7. StochEMA — استوکاستیک + فیلتر روند EMA200
# ==========================================================
def stoch_ema_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['ema_trend'] + p['stoch_n'] + 2:
        return None
    k, d = stochastic(h, l, c, p['stoch_n'], p['stoch_d'])
    ema200 = ema(c, p['ema_trend'])
    a = atr(h, l, c, 14)
    if np.isnan(k[-1]) or np.isnan(k[-2]) or np.isnan(ema200[-1]) or np.isnan(a[-1]):
        return None
    entry = cur_price
    # K زیر آستانه + رو به بالا + قیمت بالای EMA200
    if k[-2] < p['stoch_os'] and k[-1] > k[-2] and c[-1] > ema200[-1]:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"Stoch oversold↑ (K={k[-1]:.1f}, trend↑)")
    if k[-2] > 100 - p['stoch_os'] and k[-1] < k[-2] and c[-1] < ema200[-1]:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"Stoch overbought↓ (K={k[-1]:.1f}, trend↓)")
    return None


# ==========================================================
# 8. NYBreak — شکست محدوده بازگشایی نیویورک
# ==========================================================
def nybreak_sig(candles, p, cur_price):
    """بازه OR = 12:00-12:59 UTC (17:30-18:30 ایران)، ورود بعد از 13:00 UTC."""
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < 20:
        return None
    # زمان‌های کندل (میلی‌ثانیه در OKX)
    ts_list = [int(candle[0]) for candle in candles] if not isinstance(candles[0], dict) else None
    if ts_list is None:
        return None
    # گروه‌بندی روزانه
    from datetime import datetime, timezone
    days = {}
    for idx, ts in enumerate(ts_list):
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        day = dt.date()
        hr = dt.hour
        days.setdefault(day, {'or_high': -1, 'or_low': 1e18, 'bars': []})
        if p['or_start_hour_utc'] <= hr < p['or_end_hour_utc']:
            days[day]['or_high'] = max(days[day]['or_high'], h[idx])
            days[day]['or_low'] = min(days[day]['or_low'], l[idx])
    # کندل فعلی
    dt_now = datetime.fromtimestamp(ts_list[-1] / 1000, tz=timezone.utc)
    today = dt_now.date()
    hr_now = dt_now.hour
    if today not in days or days[today]['or_high'] < 0:
        return None
    if hr_now < p['entry_after_hour_utc']:
        return None
    orh = days[today]['or_high']
    orl = days[today]['or_low']
    a = atr(h, l, c, 14)
    if np.isnan(a[-1]):
        return None
    entry = cur_price
    # شکست بالای OR
    if c[-1] > orh and c[-2] <= orh:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"NY OR breakout↑ (OR={orh:.6g})")
    if c[-1] < orl and c[-2] >= orl:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"NY OR breakout↓ (OR={orl:.6g})")
    return None


# ==========================================================
# 9. Donchian — شکست کانال
# ==========================================================
def donchian_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['dc_n'] + 2:
        return None
    hh, ll = __import__('indicators').donchian(h, l, p['dc_n'])
    a = atr(h, l, c, 14)
    if np.isnan(hh[-2]) or np.isnan(ll[-2]) or np.isnan(a[-1]):
        return None
    entry = cur_price
    if c[-1] > hh[-2]:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"Donchian breakout↑ ({p['dc_n']}-bar high)")
    if c[-1] < ll[-2]:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"Donchian breakout↓ ({p['dc_n']}-bar low)")
    return None


# ==========================================================
# 10. AsianBRK — شکست محدوده سشن آسیا
# ==========================================================
def asian_brk_sig(candles, p, cur_price):
    """بازه آسیا = 00:00-05:59 UTC، شکست بعد از 06:00 UTC."""
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < 20:
        return None
    ts_list = [int(candle[0]) for candle in candles] if not isinstance(candles[0], dict) else None
    if ts_list is None:
        return None
    from datetime import datetime, timezone
    days = {}
    for idx, ts in enumerate(ts_list):
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
        day = dt.date(); hr = dt.hour
        days.setdefault(day, {'ar_high': -1, 'ar_low': 1e18})
        if 0 <= hr < 6:
            days[day]['ar_high'] = max(days[day]['ar_high'], h[idx])
            days[day]['ar_low'] = min(days[day]['ar_low'], l[idx])
    dt_now = datetime.fromtimestamp(ts_list[-1] / 1000, tz=timezone.utc)
    today = dt_now.date(); hr_now = dt_now.hour
    if today not in days or days[today]['ar_high'] < 0 or hr_now < 6:
        return None
    arh = days[today]['ar_high']; arl = days[today]['ar_low']
    a = atr(h, l, c, 14)
    if np.isnan(a[-1]):
        return None
    entry = cur_price
    if c[-1] > arh and c[-2] <= arh:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"Asian range breakout↑ (R={arh:.6g})")
    if c[-1] < arl and c[-2] >= arl:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"Asian range breakout↓ (R={arl:.6g})")
    return None


# ==========================================================
# 11. Supertrend — برگشت روند
# ==========================================================
def supertrend_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['st_atr'] + 3:
        return None
    st, dir_ = supertrend(h, l, c, p['st_atr'], p['st_mult'])
    a = atr(h, l, c, 14)
    if dir_[-3] == 0 or dir_[-1] == 0 or np.isnan(a[-1]):
        return None
    # برگشت روند
    if dir_[-3] == -1 and dir_[-1] == 1:
        entry = cur_price
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"Supertrend flip↑ ({p['st_atr']}/{p['st_mult']})")
    if dir_[-3] == 1 and dir_[-1] == -1:
        entry = cur_price
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"Supertrend flip↓ ({p['st_atr']}/{p['st_mult']})")
    return None


# ==========================================================
# 12. ADX_DI — ورود در روند قدرتمند
# ==========================================================
def adx_di_sig(candles, p, cur_price):
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['adx_n'] * 3:
        return None
    adx_arr, plus_di, minus_di = adx(h, l, c, p['adx_n'])
    a = atr(h, l, c, 14)
    if np.isnan(adx_arr[-1]) or np.isnan(adx_arr[-2]) or np.isnan(a[-1]):
        return None
    entry = cur_price
    # ADX قوی + DI+ از DI- سبقت می‌گیرد
    if adx_arr[-1] > p['adx_min'] and plus_di[-2] <= minus_di[-2] and plus_di[-1] > minus_di[-1]:
        stop = entry - p['stop_atr_mult'] * a[-1]
        tp = entry + p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='buy', entry=entry, stop=stop, tp=tp,
                    info=f"ADX={adx_arr[-1]:.0f} DI+>DI- (strong trend↑)")
    if adx_arr[-1] > p['adx_min'] and minus_di[-2] <= plus_di[-2] and minus_di[-1] > plus_di[-1]:
        stop = entry + p['stop_atr_mult'] * a[-1]
        tp = entry - p['rr'] * (p['stop_atr_mult'] * a[-1])
        return dict(side='sell', entry=entry, stop=stop, tp=tp,
                    info=f"ADX={adx_arr[-1]:.0f} DI->DI+ (strong trend↓)")
    return None


# ==========================================================
# 13. OB Reject — رسیدن به اوردر بلاک + ریجکت با سایه
# ==========================================================
def find_order_blocks(o, h, l, c, p):
    """شناسایی Order Block های معتبر.
    Bullish OB = آخرین کندل نزولی (با بدنه بزرگ) قبل از impulse صعودی قوی.
    Bearish OB = آخرین کندل صعودی (با بدنه بزرگ) قبل از impulse نزولی قوی.
    بازمی‌گرداند: list of dict(type='bull'/'bear', idx, top, bottom, mid, body, fresh).
    """
    n = len(c)
    body = np.abs(c - o)
    avg_body = sma(body, p['lookback'])
    obs = []
    max_age = p.get('max_ob_age', 60)
    start_search = max(p['lookback'] + p['min_impulse_candles'] + 1, n - max_age)
    i = n - 2  # کندل فعلی برای reject بررسی می‌شود، نه OB
    while i >= start_search:
        if np.isnan(avg_body[i]):
            i -= 1; continue
        is_bear = c[i] < o[i]   # نزولی → کاندیدای Bullish OB
        is_bull = c[i] > o[i]   # صعودی → کاندیدای Bearish OB
        if not (is_bear or is_bull):
            i -= 1; continue
        # شرط بدنه بزرگ OB
        if body[i] < p['ob_body_mult'] * avg_body[i]:
            i -= 1; continue
        # بررسی impulse بعد از OB
        imp_start = i + 1
        imp_end = min(i + p['min_impulse_candles'], n - 1)
        if imp_end - imp_start + 1 < p['min_impulse_candles']:
            i -= 1; continue
        if is_bear:  # Bullish OB → impulse صعودی
            impulse_bars = c[imp_start:imp_end+1] - o[imp_start:imp_end+1]
            if not np.all(impulse_bars > 0):
                i -= 1; continue
            move = c[imp_end] - o[imp_start]
            if move < p['impulse_atr_mult'] * avg_body[i]:
                i -= 1; continue
            ob_type = 'bull'
        else:  # Bearish OB → impulse نزولی
            impulse_bars = c[imp_start:imp_end+1] - o[imp_start:imp_end+1]
            if not np.all(impulse_bars < 0):
                i -= 1; continue
            move = o[imp_start] - c[imp_end]
            if move < p['impulse_atr_mult'] * avg_body[i]:
                i -= 1; continue
            ob_type = 'bear'
        # ناحیه OB = بدنه کندل OB
        ob_top = max(o[i], c[i])
        ob_bottom = min(o[i], c[i])
        ob_mid = (ob_top + ob_bottom) / 2.0
        # چک Fresh: قیمت نباید قبل از کندل فعلی به OB نفوذ کرده باشد
        fresh = True
        for j in range(imp_end + 1, n - 1):
            if ob_type == 'bull' and l[j] < ob_top:
                fresh = False; break
            if ob_type == 'bear' and h[j] > ob_bottom:
                fresh = False; break
        obs.append({'type': ob_type, 'idx': i, 'top': ob_top,
                    'bottom': ob_bottom, 'mid': ob_mid,
                    'body': float(body[i]), 'fresh': fresh})
        # جلوگیری از OB های همپوشان: پرش به قبل از OB
        i -= 1
    return obs


def ob_reject_sig(candles, p, cur_price):
    """استراتژی OB Reject — رسیدن قیمت به OB معتبر + ریجکت با سایه حداقل 50%.

    سناریوی کامل:
    1. OB معتبر شناسایی شود (آخرین کندل خلاف جهت با بدنه بزرگ، قبل از impulse قوی).
    2. قیمت به ناحیه OB برگردد (low/high به OB نفوذ کند).
    3. کندل reject کند:
       - سایه در جهت OB ≥ min_shadow_pct (پیش‌فرض 50%) طول کندل.
       - close به سمت صحیح برگردد (بالای mid برای buy، زیر mid برای sell).
       - کندل در جهت معامله بسته شود.
    4. ورود در close. SL = پشت shadow. TP = RR.
    """
    o, h, l, c, v = to_arrays(candles)
    n = len(c)
    if n < p['lookback'] + p['min_impulse_candles'] + 3:
        return None
    a = atr(h, l, c, p['atr_period'])
    if np.isnan(a[-1]) or a[-1] <= 0:
        return None
    obs = find_order_blocks(o, h, l, c, p)
    if not obs:
        return None
    # کندل فعلی برای بررسی reject
    i = n - 1
    candle_range = h[i] - l[i]
    if candle_range <= 0:
        return None
    body_i = abs(c[i] - o[i])
    upper_shadow = h[i] - max(o[i], c[i])
    lower_shadow = min(o[i], c[i]) - l[i]
    entry = cur_price
    # اولویت با OB تازه (fresh)
    obs_sorted = sorted(obs, key=lambda x: (not x['fresh'], -x['idx']))
    for ob in obs_sorted:
        if ob['type'] == 'bull':
            # قیمت باید به OB نفوذ کرده باشد
            if l[i] > ob['top']:
                continue
            # سایه پایین ≥ min_shadow_pct طول کندل
            if lower_shadow < p['min_shadow_pct'] * candle_range:
                continue
            # close باید بالای mid OB برگردد
            if c[i] < ob['mid']:
                continue
            # کندل باید صعودی بسته شود
            if c[i] <= o[i]:
                continue
            # SL پشت shadow پایین
            stop = l[i]
            risk = entry - stop
            # حداقل ریسک = 0.3 ATR
            if risk < 0.3 * a[-1]:
                stop = entry - 0.5 * a[-1]
                risk = entry - stop
            if risk <= 0:
                continue
            tp = entry + p['rr'] * risk
            sh_pct = lower_shadow / candle_range * 100
            fresh_tag = "fresh" if ob['fresh'] else "revisit"
            return dict(side='buy', entry=entry, stop=stop, tp=tp,
                        info=f"OB reject↑ (shadow={sh_pct:.0f}%, {fresh_tag}, OB@{ob['mid']:.6g})")
        else:  # bear OB
            if h[i] < ob['bottom']:
                continue
            if upper_shadow < p['min_shadow_pct'] * candle_range:
                continue
            if c[i] > ob['mid']:
                continue
            if c[i] >= o[i]:
                continue
            stop = h[i]
            risk = stop - entry
            if risk < 0.3 * a[-1]:
                stop = entry + 0.5 * a[-1]
                risk = stop - entry
            if risk <= 0:
                continue
            tp = entry - p['rr'] * risk
            sh_pct = upper_shadow / candle_range * 100
            fresh_tag = "fresh" if ob['fresh'] else "revisit"
            return dict(side='sell', entry=entry, stop=stop, tp=tp,
                        info=f"OB reject↓ (shadow={sh_pct:.0f}%, {fresh_tag}, OB@{ob['mid']:.6g})")
    return None


# ==========================================================
# Registry
# ==========================================================
STRATEGY_FUNCS = {
    'SP2L':       sp2l,
    'RSI2':       rsi2,
    'Bollinger':  bollinger_sig,
    'Keltner':    keltner_sig,
    'VWAP':       vwap_sig,
    'MACD_EMA':   macd_ema_sig,
    'StochEMA':   stoch_ema_sig,
    'NYBreak':    nybreak_sig,
    'Donchian':   donchian_sig,
    'AsianBRK':   asian_brk_sig,
    'Supertrend': supertrend_sig,
    'ADX_DI':     adx_di_sig,
    'OB':         ob_reject_sig,
}

# پارامترهای پیش‌فرض هر استراتژی (در صورت نبود در strategies_config.json)
DEFAULT_PARAMS = {
    'SP2L':       dict(lookback=20, min_spike_candles=2, max_spike_candles=5,
                       body_mult=2.0, gap_required=False, rr=1.5),
    'RSI2':       dict(rsi_period=2, buy_threshold=10, exit_rsi=50,
                       atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'Bollinger':  dict(bb_n=20, bb_k=2.0, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'Keltner':    dict(kc_n=20, kc_mult=2.0, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'VWAP':       dict(vwap_n=30, vwap_dev=2.0, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'MACD_EMA':   dict(macd_fast=12, macd_slow=26, macd_sig=9, ema_trend=200,
                       atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'StochEMA':   dict(stoch_n=14, stoch_d=3, stoch_os=20, ema_trend=200,
                       atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'NYBreak':    dict(or_start_hour_utc=12, or_end_hour_utc=13,
                       entry_after_hour_utc=13, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'Donchian':   dict(dc_n=20, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'AsianBRK':   dict(atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'Supertrend': dict(st_atr=10, st_mult=3.0, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'ADX_DI':     dict(adx_n=14, adx_min=25, atr_period=14, stop_atr_mult=1.5, rr=2.0),
    'OB':         dict(lookback=20, ob_body_mult=1.5, min_impulse_candles=3,
                       impulse_atr_mult=2.0, min_shadow_pct=0.5, max_ob_age=60,
                       atr_period=14, stop_atr_mult=1.5, rr=2.0),
}
