"""常用技术指标（与 TradingView/Pine 的 ta.* 口径对齐）。"""
import numpy as np
import pandas as pd


def sma(s, n): return s.rolling(n).mean()
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rma(s, n): return s.ewm(alpha=1 / n, adjust=False).mean()  # Pine ta.rma / Wilder


def rsi(s, n=14):
    d = s.diff()
    up, dn = rma(d.clip(lower=0), n), rma(-d.clip(upper=0), n)
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def macd(s, fast=12, slow=26, signal=9):
    line = ema(s, fast) - ema(s, slow)
    sig = ema(line, signal)
    return line, sig, line - sig


def bbands(s, n=20, k=2.0):
    mid, sd = sma(s, n), s.rolling(n).std(ddof=0)
    return mid + k * sd, mid, mid - k * sd


def true_range(df):
    pc = df.close.shift()
    return pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)


def atr(df, n=14): return rma(true_range(df), n)


def adx(df, n=14):
    up, dn = df.high.diff(), -df.low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), df.index)
    ndm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), df.index)
    tr = rma(true_range(df), n)
    pdi, ndi = 100 * rma(pdm, n) / tr, 100 * rma(ndm, n) / tr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return rma(dx, n), pdi, ndi


def stoch(df, k=14, d=3, smooth=3):
    ll, hh = df.low.rolling(k).min(), df.high.rolling(k).max()
    kk = sma(100 * (df.close - ll) / (hh - ll), smooth)
    return kk, sma(kk, d)


def donchian(df, n=20): return df.high.rolling(n).max(), df.low.rolling(n).min()


def supertrend(df, n=10, mult=3.0):
    """返回方向序列：1 多头，-1 空头。"""
    a = atr(df, n).values
    hl2 = ((df.high + df.low) / 2).values
    c = df.close.values
    ub, lb = hl2 + mult * a, hl2 - mult * a
    fu, fl = ub.copy(), lb.copy()
    dirn = np.ones(len(c))
    for i in range(1, len(c)):
        fu[i] = ub[i] if (ub[i] < fu[i-1] or c[i-1] > fu[i-1]) else fu[i-1]
        fl[i] = lb[i] if (lb[i] > fl[i-1] or c[i-1] < fl[i-1]) else fl[i-1]
        if dirn[i-1] == 1:
            dirn[i] = -1 if c[i] < fl[i] else 1
        else:
            dirn[i] = 1 if c[i] > fu[i] else -1
    dirn[np.isnan(a)] = 0
    return pd.Series(dirn, df.index)


def vwap_rolling(df, n=20):
    tp = (df.high + df.low + df.close) / 3
    return (tp * df.volume).rolling(n).sum() / df.volume.rolling(n).sum()


def crossover(a, b): return (a > b) & (a.shift() <= b.shift())
def crossunder(a, b): return (a < b) & (a.shift() >= b.shift())
