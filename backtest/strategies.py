"""仓库中最常见的策略族的 Python 复刻。

每个策略返回目标仓位序列：1 多 / 0 空仓 / -1 空（在该 bar 收盘时决定，
引擎在下一根 bar 执行，避免未来函数）。
keywords 用于 catalog.py 把仓库里的 .md 策略归类到对应族。
"""
import numpy as np
import pandas as pd
from indicators import *

REGISTRY = {}


def strategy(name, keywords, grid):
    def deco(fn):
        REGISTRY[name] = dict(fn=fn, keywords=keywords, grid=grid,
                              defaults={k: v[0] for k, v in grid.items()})
        return fn
    return deco


def hold(entry_long, exit_long, entry_short=None, exit_short=None):
    """把进出场事件转成持仓状态（类似 strategy.entry / strategy.close）。"""
    ev = pd.Series(np.nan, entry_long.index)
    ev[exit_long.fillna(False)] = 0
    if entry_short is not None:
        if exit_short is not None:
            ev[exit_short.fillna(False) & (ev != 1)] = 0
        ev[entry_short.fillna(False)] = -1
    ev[entry_long.fillna(False)] = 1
    return ev.ffill().fillna(0)


def allow_short(pos, short):
    return pos if short else pos.clip(lower=0)


@strategy("ma_cross", ["均线交叉", "双均线", "moving average crossover", "sma cross", "golden cross", "金叉"],
          {"fast": [10, 20, 50], "slow": [50, 100, 200]})
def ma_cross(df, fast=20, slow=50, short=False):
    f, s = sma(df.close, fast), sma(df.close, slow)
    return allow_short(pd.Series(np.where(f > s, 1, -1), df.index).where(s.notna(), 0), short)


@strategy("ema_cross", ["ema", "双ema", "指数移动平均"],
          {"fast": [9, 12, 21], "slow": [21, 26, 55]})
def ema_cross(df, fast=12, slow=26, short=False):
    f, s = ema(df.close, fast), ema(df.close, slow)
    return allow_short(pd.Series(np.where(f > s, 1, -1), df.index), short)


@strategy("triple_ema", ["三均线", "triple", "均线带", "ribbon"],
          {"a": [5, 10], "b": [20, 30], "c": [50, 100]})
def triple_ema(df, a=10, b=20, c=50, short=False):
    e1, e2, e3 = ema(df.close, a), ema(df.close, b), ema(df.close, c)
    return hold((e1 > e2) & (e2 > e3), e1 < e2, (e1 < e2) & (e2 < e3), e1 > e2) if short \
        else hold((e1 > e2) & (e2 > e3), e1 < e2)


@strategy("rsi_reversion", ["rsi", "相对强弱", "超卖", "oversold"],
          {"n": [14, 7, 2], "lo": [30, 25, 10], "hi": [70, 75, 90]})
def rsi_reversion(df, n=14, lo=30, hi=70, short=False):
    r = rsi(df.close, n)
    return hold(crossover(r, pd.Series(lo, df.index)), r > hi,
                crossunder(r, pd.Series(hi, df.index)) if short else None, r < lo if short else None)


@strategy("rsi_trend_filter", ["rsi", "ema", "趋势过滤", "trend filter", "动量"],
          {"n": [2, 3, 14], "lo": [10, 20, 30], "trend": [200, 100]})
def rsi_trend_filter(df, n=2, lo=10, trend=200, short=False):
    """Connors 风格：大趋势向上时买入短期超卖，价格回到短均线上方离场。"""
    up = df.close > sma(df.close, trend)
    r = rsi(df.close, n)
    return hold(up & (r < lo), (df.close > sma(df.close, 5)) | ~up)


@strategy("macd_cross", ["macd", "histogram", "柱状"],
          {"fast": [12, 8], "slow": [26, 21], "signal": [9, 5]})
def macd_cross(df, fast=12, slow=26, signal=9, short=False):
    line, sig, _ = macd(df.close, fast, slow, signal)
    return allow_short(pd.Series(np.where(line > sig, 1, -1), df.index), short)


@strategy("bb_reversion", ["布林", "bollinger", "均值回归", "mean reversion"],
          {"n": [20, 10], "k": [2.0, 1.5, 2.5]})
def bb_reversion(df, n=20, k=2.0, short=False):
    u, m, l = bbands(df.close, n, k)
    return hold(crossover(df.close, l), df.close > m,
                crossunder(df.close, u) if short else None, df.close < m if short else None)


@strategy("bb_breakout", ["布林", "bollinger", "突破", "breakout", "通道"],
          {"n": [20, 50], "k": [2.0, 1.5]})
def bb_breakout(df, n=20, k=2.0, short=False):
    u, m, l = bbands(df.close, n, k)
    return hold(df.close > u, df.close < m,
                df.close < l if short else None, df.close > m if short else None)


@strategy("donchian_breakout", ["唐奇安", "donchian", "海龟", "turtle", "突破", "breakout", "高点"],
          {"entry": [20, 55], "exit": [10, 20]})
def donchian_breakout(df, entry=20, exit=10, short=False):
    hh, ll = donchian(df, entry)
    xh, xl = donchian(df, exit)
    return hold(df.close > hh.shift(), df.close < xl.shift(),
                df.close < ll.shift() if short else None, df.close > xh.shift() if short else None)


@strategy("supertrend", ["supertrend", "超级趋势", "atr"],
          {"n": [10, 14, 7], "mult": [3.0, 2.0, 4.0]})
def supertrend_strat(df, n=10, mult=3.0, short=False):
    return allow_short(supertrend(df, n, mult), short)


@strategy("ema_atr_trail", ["atr", "止损", "trailing", "追踪", "stop"],
          {"n": [50, 20], "atr_n": [14], "mult": [3.0, 2.0]})
def ema_atr_trail(df, n=50, atr_n=14, mult=3.0, short=False):
    """价格站上 EMA 入场，ATR 吊灯追踪止损离场。"""
    e, a, c = ema(df.close, n).values, atr(df, atr_n).values, df.close.values
    pos, stop, out = 0, 0.0, np.zeros(len(c))
    for i in range(len(c)):
        if np.isnan(a[i]):
            continue
        if pos == 0 and c[i] > e[i]:
            pos, stop = 1, c[i] - mult * a[i]
        elif pos == 1:
            stop = max(stop, c[i] - mult * a[i])
            if c[i] < stop:
                pos = 0
        out[i] = pos
    return pd.Series(out, df.index)


@strategy("adx_ma", ["adx", "dmi", "趋势强度"],
          {"fast": [20, 10], "slow": [50, 30], "th": [20, 25]})
def adx_ma(df, fast=20, slow=50, th=20, short=False):
    a, _, _ = adx(df)
    f, s = ema(df.close, fast), ema(df.close, slow)
    strong = a > th
    return hold(strong & (f > s), f < s, strong & (f < s) if short else None, f > s if short else None)


@strategy("stoch_cross", ["随机指标", "stochastic", "kdj"],
          {"k": [14, 9], "lo": [20, 30], "hi": [80, 70]})
def stoch_cross(df, k=14, lo=20, hi=80, short=False):
    kk, dd = stoch(df, k)
    return hold(crossover(kk, dd) & (kk < lo), crossunder(kk, dd) & (kk > hi),
                crossunder(kk, dd) & (kk > hi) if short else None,
                crossover(kk, dd) & (kk < lo) if short else None)


@strategy("vwap_rsi", ["vwap", "成交量加权"],
          {"n": [20, 50], "rsi_n": [14], "th": [50, 55]})
def vwap_rsi(df, n=20, rsi_n=14, th=50, short=False):
    v, r = vwap_rolling(df, n), rsi(df.close, rsi_n)
    return hold((df.close > v) & (r > th), (df.close < v) | (r < 100 - th))


@strategy("multi_confirm", ["多指标", "multi-indicator", "综合", "multiple indicators", "组合"],
          {"trend": [50, 100], "rsi_th": [50, 55]})
def multi_confirm(df, trend=50, rsi_th=50, short=False):
    """EMA 趋势 + MACD 动量 + RSI 过滤 三重确认，任一失效离场。"""
    t = df.close > ema(df.close, trend)
    line, sig, _ = macd(df.close)
    r = rsi(df.close)
    return hold(t & (line > sig) & (r > rsi_th), ~t | (line < sig))
