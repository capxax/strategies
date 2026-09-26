"""FMZ digest (https://www.fmz.com/digest) 中可用 K 线回测的交易策略。

每个策略注释里标了 digest-topic 编号。实现遵循文章及该策略的经典定义，
信号在收盘确认、下根开盘成交（由 engine 保证）。
"""
import numpy as np
import pandas as pd
from indicators import *
from strategies import strategy, hold, allow_short

K = []  # digest 专用关键词不参与仓库归类


@strategy("fmz_dual_thrust", K, {"n": [4, 2, 6], "k1": [0.5, 0.3, 0.7], "k2": [0.5, 0.3, 0.7]})
def dual_thrust(df, n=4, k1=0.5, k2=0.5, short=False):
    """#9872 Dual Thrust：开盘价 ± k×Range 突破。"""
    hh, lc = df.high.rolling(n).max().shift(), df.close.rolling(n).min().shift()
    hc, ll = df.close.rolling(n).max().shift(), df.low.rolling(n).min().shift()
    rng = np.maximum(hh - lc, hc - ll)
    up, dn = df.open + k1 * rng, df.open - k2 * rng
    return hold(df.close > up, df.close < dn, df.close < dn if short else None, df.close > up if short else None)


@strategy("fmz_thermostat", K, {"n": [20, 30], "ci": [20, 30], "k": [0.5, 1.0]})
def thermostat(df, n=20, ci=20, k=0.5, short=False):
    """#9891 #6050 恒温器：CI 高=趋势模式(通道突破)，CI 低=震荡模式(均值回归)。"""
    ch = (df.close - df.close.shift(ci)).abs() / (df.high.rolling(ci).max() - df.low.rolling(ci).min()) * 100
    trend = ch > 30
    m, a = sma(df.close, n), atr(df, 10)
    hh, ll = df.high.rolling(n).max().shift(), df.low.rolling(n).min().shift()
    u, _, l = bbands(df.close, n, 2)
    el = (trend & (df.close > hh)) | (~trend & (df.close < m - k * a))
    xl = (trend & (df.close < m)) | (~trend & (df.close > m + k * a))
    es = (trend & (df.close < ll)) | (~trend & (df.close > m + k * a))
    xs = (trend & (df.close > m)) | (~trend & (df.close < m - k * a))
    return hold(el, xl, es if short else None, xs if short else None)


@strategy("fmz_atr_channel", K, {"n": [20, 50], "k": [1.0, 2.0, 0.5]})
def atr_channel(df, n=20, k=1.0, short=False):
    """#6051 #9892 ATR 通道：均线 ± k×ATR 突破，回到均线离场。"""
    m, a = sma(df.close, n), atr(df, n)
    return hold(df.close > m + k * a, df.close < m, df.close < m - k * a if short else None, df.close > m if short else None)


@strategy("fmz_keltner", K, {"n": [20, 50], "k": [1.5, 2.0, 1.0]})
def keltner(df, n=20, k=1.5, short=False):
    """#4114 肯特纳通道升级版：EMA ± k×ATR，EMA 回穿离场。"""
    m, a = ema(df.close, n), atr(df, 10)
    return hold(df.close > m + k * a, df.close < m, df.close < m - k * a if short else None, df.close > m if short else None)


@strategy("fmz_range_break", K, {"n": [5, 10], "k": [0.3, 0.5, 0.7]})
def range_break(df, n=5, k=0.5, short=False):
    """#4083 RangeBreak：开盘价 ± k×平均振幅。"""
    rng = (df.high - df.low).rolling(n).mean().shift()
    up, dn = df.open + k * rng, df.open - k * rng
    return hold(df.close > up, df.close < dn, df.close < dn if short else None, df.close > up if short else None)


@strategy("fmz_box", K, {"n": [20, 40, 60]})
def box(df, n=20, short=False):
    """#9890 #4080 箱体理论：突破箱顶买入，跌破箱底离场。"""
    hh, ll = df.high.rolling(n).max().shift(), df.low.rolling(n).min().shift()
    return hold(df.close > hh, df.close < ll, df.close < ll if short else None, df.close > hh if short else None)


@strategy("fmz_rsi2", K, {"lo": [10, 5, 15], "trend": [200, 100]})
def rsi2(df, lo=10, trend=200, short=False):
    """#6074 RSI2 均值回归：长均线上方 RSI(2) 超卖买入，站上 5 日均线卖出。"""
    up, r = df.close > sma(df.close, trend), rsi(df.close, 2)
    return hold(up & (r < lo), df.close > sma(df.close, 5))


@strategy("fmz_bias", K, {"n": [20, 10, 30], "th": [5, 8, 3]})
def bias(df, n=20, th=5, short=False):
    """#5827 乖离率 BIAS：负乖离过大买入，回到均线卖出。"""
    b = (df.close / sma(df.close, n) - 1) * 100
    return hold(b < -th, b > 0, b > th if short else None, b < 0 if short else None)


@strategy("fmz_emv", K, {"n": [14, 20], "m": [9, 5]})
def emv(df, n=14, m=9, short=False):
    """#5833 简易波动 EMV：EMV 上穿 0 买入，下穿卖出。"""
    mid = (df.high + df.low) / 2
    br = (df.volume / df.volume.rolling(n).mean()) / (df.high - df.low).replace(0, np.nan)
    e = sma(mid.diff() / br, n)
    return allow_short(pd.Series(np.where(sma(e, m) > 0, 1, -1), df.index), short)


@strategy("fmz_alligator", K, {"scale": [1, 2]})
def alligator(df, scale=1, short=False):
    """#5566 鳄鱼线：唇>齿>颚 多头排列持有，失去排列离场。"""
    mid = (df.high + df.low) / 2
    jaw = rma(mid, 13 * scale).shift(8)
    teeth = rma(mid, 8 * scale).shift(5)
    lips = rma(mid, 5 * scale).shift(3)
    return hold((lips > teeth) & (teeth > jaw) & (df.close > lips), lips < teeth,
                ((lips < teeth) & (teeth < jaw) & (df.close < lips)) if short else None, (lips > teeth) if short else None)


def psar(df, af0=0.02, step=0.02, mx=0.2):
    h, l = df.high.values, df.low.values
    sar, up, af, ep = l[0], True, af0, h[0]
    out = np.zeros(len(h))
    for i in range(1, len(h)):
        sar = sar + af * (ep - sar)
        if up:
            sar = min(sar, l[i-1], l[i-2] if i > 1 else l[i-1])
            if l[i] < sar:
                up, sar, ep, af = False, ep, l[i], af0
            elif h[i] > ep:
                ep, af = h[i], min(af + step, mx)
        else:
            sar = max(sar, h[i-1], h[i-2] if i > 1 else h[i-1])
            if h[i] > sar:
                up, sar, ep, af = True, ep, h[i], af0
            elif l[i] < ep:
                ep, af = l[i], min(af + step, mx)
        out[i] = 1 if up else -1
    return pd.Series(out, df.index)


@strategy("fmz_sar", K, {"af": [0.02, 0.01], "mx": [0.2, 0.1]})
def sar(df, af=0.02, mx=0.2, short=False):
    """#4376 抛物线 SAR 转向。"""
    return allow_short(psar(df, af, af, mx), short)


@strategy("fmz_double_ema", K, {"fast": [9, 12], "slow": [21, 26], "trend": [200, 100]})
def magic_double_ema(df, fast=9, slow=21, trend=200, short=False):
    """#9732 “神奇双 EMA”：长 EMA 过滤方向，快慢 EMA 金叉进场、死叉离场。"""
    f, s, t = ema(df.close, fast), ema(df.close, slow), ema(df.close, trend)
    return hold(crossover(f, s) & (df.close > t), crossunder(f, s),
                (crossunder(f, s) & (df.close < t)) if short else None, crossover(f, s) if short else None)


@strategy("fmz_momentum", K, {"n": [20, 60, 120]})
def momentum(df, n=20, short=False):
    """#9873 #9889 价格动量 / 相对强弱：N 期收益为正持有。"""
    mom = df.close / df.close.shift(n) - 1
    return allow_short(pd.Series(np.where(mom > 0, 1, -1), df.index).where(mom.notna(), 0), short)


@strategy("fmz_pbx", K, {"a": [4, 6], "b": [6, 9], "c": [9, 12]})
def pbx(df, a=4, b=6, c=9, short=False):
    """#9887 PBX 瀑布线：价格站上最慢瀑布线且短线多头排列。"""
    def pb(n): return (ema(df.close, n) + sma(df.close, 2 * n) + sma(df.close, 4 * n)) / 3
    p1, p2, p3 = pb(a), pb(b), pb(c)
    return hold((p1 > p2) & (p2 > p3) & (df.close > p1), df.close < p3,
                ((p1 < p2) & (p2 < p3) & (df.close < p1)) if short else None, (df.close > p3) if short else None)


@strategy("fmz_kline_area", K, {"n": [20, 40], "th": [0.0, 0.5]})
def kline_area(df, n=20, th=0.0, short=False):
    """#10261 K 线面积：均线上方面积占比（带符号）超过阈值做多。"""
    m = sma(df.close, n)
    area = ((df.close - m) / atr(df, n)).rolling(n).sum() / n
    return hold(area > th, area < -th, area < -th if short else None, area > th if short else None)


@strategy("fmz_shadow", K, {"n": [20], "r": [2.0, 1.5, 3.0]})
def shadow(df, n=20, r=2.0, short=False):
    """#4306 #9739 K 线影线/抄底：近 N 期低位出现长下影线买入，跌破该 K 最低或 N 期后离场。"""
    body = (df.close - df.open).abs()
    lower = np.minimum(df.open, df.close) - df.low
    sig = (lower > r * body) & (lower > atr(df, 14) * 0.5) & (df.low <= df.low.rolling(n).min())
    exit_ = (df.close > df.high.rolling(n).max().shift()) | (df.close < df.low.rolling(5).min().shift())
    return hold(sig, exit_)


@strategy("fmz_psy", K, {"n": [12, 24], "lo": [25, 33], "hi": [75, 67]})
def psy(df, n=12, lo=25, hi=75, short=False):
    """#10263 PSY 心理线：上涨天数占比过低买入，过高卖出。"""
    p = (df.close.diff() > 0).rolling(n).mean() * 100
    return hold(p < lo, p > hi, p > hi if short else None, p < lo if short else None)


@strategy("fmz_zdzb", K, {"n": [125], "m1": [5], "m2": [20]})
def zdzb(df, n=125, m1=5, m2=20, short=False):
    """#5900 筑底指标 ZDZB：上涨/下跌天数比的快线上穿慢线。"""
    up = (df.close >= df.close.shift()).rolling(n).sum()
    dn = (df.close < df.close.shift()).rolling(n).sum().replace(0, np.nan)
    a = up / dn
    b, d = sma(a, m1), sma(a, m2)
    return hold(crossover(b, d), crossunder(b, d))


@strategy("fmz_bb_dca", K, {"n": [20], "k": [2.0, 2.5], "layers": [3, 5]})
def bb_reversal_dca(df, n=20, k=2.0, layers=3, short=False):
    """#11002 D-Man V3 布林带反转 DCA：每次收盘低于下轨加一层(最多 layers 层)，回到中轨全平。仓位 0~1。"""
    u, m, l = bbands(df.close, n, k)
    c, lv, mv = df.close.values, l.values, m.values
    out, lay = np.zeros(len(c)), 0
    for i in range(len(c)):
        if np.isnan(lv[i]):
            continue
        if c[i] < lv[i] and lay < layers:
            lay += 1
        elif lay and c[i] > mv[i]:
            lay = 0
        out[i] = lay / layers
    return pd.Series(out, df.index)


@strategy("fmz_vol_reversion", K, {"n": [20], "z": [2.0, 1.5, 2.5], "hold_n": [5, 3]})
def vol_reversion(df, n=20, z=2.0, hold_n=5, short=False):
    """#10689 逆势短线均值回归：单根收益低于 -z 倍波动率买入，持有 hold_n 根或反弹离场。"""
    r = df.close.pct_change()
    sig = r < -z * r.rolling(n).std().shift()
    c, s = df.close.values, sig.values
    out, left = np.zeros(len(c)), 0
    for i in range(len(c)):
        if s[i]:
            left = hold_n
        elif left:
            left -= 1
        out[i] = 1 if left else 0
    return pd.Series(out, df.index)


@strategy("fmz_dca", K, {"n": [7, 30]})
def dca(df, n=7, short=False):
    """#10505 DCA 定投：每 n 根等额买入，不卖出。仓位 = 已投入份数/总份数（近似资金逐步入场）。"""
    k = np.arange(len(df)) // n + 1
    return pd.Series(k / k[-1], df.index)


@strategy("fmz_grid", K, {"n": [60, 120], "levels": [5, 10]})
def grid(df, n=60, levels=5, short=False):
    """#9294 #9841 #10721 网格（K 线近似）：在 n 期区间内价格越低仓位越高，线性分层，出区间上沿清仓。"""
    hh, ll = df.high.rolling(n).max().shift(), df.low.rolling(n).min().shift()
    frac = ((hh - df.close) / (hh - ll)).clip(0, 1)
    return (np.floor(frac * levels) / levels).fillna(0)


@strategy("fmz_turtle", K, {"entry": [20, 55], "exit": [10, 20], "risk": [2.0]})
def turtle(df, entry=20, exit=10, risk=2.0, short=False):
    """#9847 唐奇安 + 2N 止损（海龟系统一）。"""
    hh, ll = df.high.rolling(entry).max().shift(), df.low.rolling(entry).min().shift()
    xl = df.low.rolling(exit).min().shift()
    c, a, h, x = df.close.values, atr(df, 20).values, hh.values, xl.values
    out, pos, stop = np.zeros(len(c)), 0, 0.0
    for i in range(len(c)):
        if np.isnan(h[i]) or np.isnan(a[i]):
            continue
        if not pos and c[i] > h[i]:
            pos, stop = 1, c[i] - risk * a[i]
        elif pos and (c[i] < x[i] or c[i] < stop):
            pos = 0
        out[i] = pos
    return pd.Series(out, df.index)
