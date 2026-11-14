"""
indicators.py — اندیکاتورهای فنی مورد نیاز استراتژی‌ها.
همه خروجی‌ها numpy array هستند. ورودی کندل‌ها به صورت لیست dict یا DataFrame.
طراحی شده برای محیط GitHub Actions: سبک، بدون وابستگی سنگین به pandas.
"""
import numpy as np


# ==========================================================
# ابزارهای پایه
# ==========================================================
def to_arrays(candles):
    """candles: list of dict with o/h/l/c/v or list of [ts,o,h,l,c,v,...].
    بازمی‌گرداند: arrays of open, high, low, close, volume."""
    if len(candles) == 0:
        return (np.array([]),)*5
    if isinstance(candles[0], dict):
        o = np.array([float(c['open']) for c in candles])
        h = np.array([float(c['high']) for c in candles])
        l = np.array([float(c['low']) for c in candles])
        c = np.array([float(c['close']) for c in candles])
        v = np.array([float(c.get('volume', 0)) for c in candles])
    else:
        # OKX format: [ts, o, h, l, c, vol, ...]
        o = np.array([float(c[1]) for c in candles])
        h = np.array([float(c[2]) for c in candles])
        l = np.array([float(c[3]) for c in candles])
        c = np.array([float(c[4]) for c in candles])
        v = np.array([float(c[5]) if len(c) > 5 else 0 for c in candles])
    return o, h, l, c, v


def sma(arr, n):
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < n:
        return out
    csum = np.cumsum(arr)
    out[n-1:] = (csum[n-1:] - np.concatenate(([0], csum[:-n]))) / n
    return out


def ema(arr, n):
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < n:
        return out
    alpha = 2.0 / (n + 1)
    # seed with SMA of first n
    out[n-1] = arr[:n].mean()
    for i in range(n, len(arr)):
        out[i] = alpha * arr[i] + (1 - alpha) * out[i-1]
    return out


def rsi(close, n):
    """Wilder's RSI."""
    out = np.full_like(close, np.nan, dtype=float)
    if len(close) < n + 1:
        return out
    delta = np.diff(close)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    # Wilder smoothing
    avg_g = gain[:n].mean()
    avg_l = loss[:n].mean()
    out[n] = 100.0 if avg_l == 0 else 100 - 100/(1 + avg_g/avg_l) if avg_l > 0 else 100.0
    for i in range(n, len(delta)):
        avg_g = (avg_g * (n-1) + gain[i]) / n
        avg_l = (avg_l * (n-1) + loss[i]) / n
        if avg_l == 0:
            out[i+1] = 100.0
        else:
            rs = avg_g / avg_l
            out[i+1] = 100 - 100/(1 + rs)
    return out


def true_range(high, low, close):
    pc = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - pc),
        np.abs(low - pc)
    ])
    return tr


def atr(high, low, close, n=14):
    tr = true_range(high, low, close)
    out = np.full_like(tr, np.nan, dtype=float)
    if len(tr) < n:
        return out
    # Wilder's smoothing
    out[n-1] = tr[:n].mean()
    for i in range(n, len(tr)):
        out[i] = (out[i-1] * (n-1) + tr[i]) / n
    return out


def bollinger(close, n=20, k=2.0):
    mid = sma(close, n)
    std = np.full_like(close, np.nan, dtype=float)
    if len(close) >= n:
        for i in range(n-1, len(close)):
            std[i] = close[i-n+1:i+1].std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    return upper, mid, lower


def keltner(close, high, low, n=20, mult=2.0, atr_n=14):
    mid = ema(close, n)
    a = atr(high, low, close, atr_n)
    upper = mid + mult * a
    lower = mid - mult * a
    return upper, mid, lower


def macd(close, fast=12, slow=26, sig=9):
    ef = ema(close, fast)
    es = ema(close, slow)
    macd_line = ef - es
    signal = ema(macd_line[~np.isnan(macd_line)], sig) if np.any(~np.isnan(macd_line)) else np.array([])
    # align signal back
    sig_full = np.full_like(macd_line, np.nan, dtype=float)
    first_valid = np.argmax(~np.isnan(macd_line))
    if len(signal) > 0:
        sig_full[first_valid + len(signal) - 1 - (len(signal)-1): first_valid + len(signal)] = signal
    hist = macd_line - sig_full
    return macd_line, sig_full, hist


def stochastic(high, low, close, n=14, d_n=3):
    ll = np.full_like(close, np.nan, dtype=float)
    hh = np.full_like(close, np.nan, dtype=float)
    k = np.full_like(close, np.nan, dtype=float)
    if len(close) < n:
        return k, ll
    for i in range(n-1, len(close)):
        ll[i] = low[i-n+1:i+1].min()
        hh[i] = high[i-n+1:i+1].max()
        if hh[i] > ll[i]:
            k[i] = 100 * (close[i] - ll[i]) / (hh[i] - ll[i])
    d = sma(k[~np.isnan(k)], d_n) if np.any(~np.isnan(k)) else np.array([])
    d_full = np.full_like(close, np.nan, dtype=float)
    valid_k = np.where(~np.isnan(k))[0]
    if len(d) > 0 and len(valid_k) >= len(d):
        d_full[valid_k[len(valid_k)-len(d):]] = d
    return k, d_full


def vwap_intraday(high, low, close, volume):
    """VAP بازنشانی‌شده در هر روز. فرض: کندل‌های متوالی از یک روز."""
    tp = (high + low + close) / 3.0
    cum_pv = np.cumsum(tp * volume)
    cum_v = np.cumsum(volume)
    vwap = np.where(cum_v > 0, cum_pv / cum_v, tp)
    return vwap


def donchian(high, low, n=20):
    hh = np.full_like(high, np.nan, dtype=float)
    ll = np.full_like(low, np.nan, dtype=float)
    for i in range(n-1, len(high)):
        hh[i] = high[i-n+1:i+1].max()
        ll[i] = low[i-n+1:i+1].min()
    return hh, ll


def supertrend(high, low, close, atr_n=10, mult=3.0):
    """Supertrend — برمی‌گرداند (st_array, dir_array: +1 up / -1 down)."""
    a = atr(high, low, close, atr_n)
    hl2 = (high + low) / 2.0
    upper = hl2 + mult * a
    lower = hl2 - mult * a
    n = len(close)
    st = np.full(n, np.nan, dtype=float)
    dir_ = np.ones(n, dtype=int)
    for i in range(1, n):
        if np.isnan(a[i]):
            continue
        prev = st[i-1]
        if np.isnan(prev):
            dir_[i] = 1 if close[i] >= lower[i] else -1
        else:
            if close[i] > prev:
                dir_[i] = 1
            elif close[i] < prev:
                dir_[i] = -1
            else:
                dir_[i] = dir_[i-1]
        if dir_[i] == 1:
            new = lower[i]
            if not np.isnan(prev) and new < prev:
                new = prev
        else:
            new = upper[i]
            if not np.isnan(prev) and new > prev:
                new = prev
        st[i] = new
    return st, dir_


def adx(high, low, close, n=14):
    """ADX + DI+/DI-."""
    up = np.diff(high, prepend=high[0])
    dn = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(high, low, close, n)
    plus_di = 100 * np.where(a > 0, plus_dm / a, 0)
    minus_di = 100 * np.where(a > 0, minus_dm / a, 0)
    dx = 100 * np.abs(plus_di - minus_di) / np.where((plus_di + minus_di) > 0, plus_di + minus_di, 1)
    adx_arr = np.full_like(dx, np.nan, dtype=float)
    if len(dx) >= n:
        adx_arr[n-1] = dx[:n].mean()
        for i in range(n, len(dx)):
            adx_arr[i] = (adx_arr[i-1] * (n-1) + dx[i]) / n
    return adx_arr, plus_di, minus_
