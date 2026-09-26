"""向量化回测引擎 + 绩效指标。"""
import numpy as np
import pandas as pd


def periods_per_year(idx):
    d = pd.Series(idx).diff().median()
    if d >= pd.Timedelta(days=1):
        # 股票约 252 天，加密货币 365 天：用样本密度估计
        span = (idx[-1] - idx[0]).days or 1
        return round(len(idx) / span * 365)
    return pd.Timedelta(days=365) / d


def backtest(df, pos, fee=0.001, slippage=0.0005):
    """pos 在 bar t 收盘产生，于 t+1 开盘成交：t+1 的收益 = 开->收 用新仓位 + 前收->开 用旧仓位。"""
    pos = pos.reindex(df.index).fillna(0).clip(-1, 1)
    held = pos.shift().fillna(0)          # t 开盘后持有的仓位
    prev = pos.shift(2).fillna(0)         # t 开盘前持有的仓位
    gap = df.open / df.close.shift() - 1
    intra = df.close / df.open - 1
    ret = (prev * gap.fillna(0) + held * intra)
    cost = (held - prev).abs() * (fee + slippage)
    ret = ret - cost
    equity = (1 + ret).cumprod()
    return ret, equity, held


def trades(held, ret):
    """按持仓段切分交易，返回每笔收益。"""
    seg = (held != held.shift()).cumsum()
    out = []
    for _, g in ret.groupby(seg):
        if held.loc[g.index[0]] != 0:
            out.append((1 + g).prod() - 1)
    return np.array(out)


def metrics(ret, equity, held, ppy):
    n = len(ret)
    years = n / ppy
    total = equity.iloc[-1] - 1
    cagr = equity.iloc[-1] ** (1 / years) - 1 if years > 0 and equity.iloc[-1] > 0 else -1
    sd = ret.std()
    sharpe = ret.mean() / sd * np.sqrt(ppy) if sd > 0 else 0.0
    dd = equity / equity.cummax() - 1
    t = trades(held, ret)
    wins, losses = t[t > 0].sum(), -t[t < 0].sum()
    return {
        "total_return": total, "cagr": cagr, "sharpe": sharpe,
        "max_dd": dd.min(), "calmar": cagr / -dd.min() if dd.min() < 0 else np.nan,
        "trades": len(t), "win_rate": (t > 0).mean() if len(t) else np.nan,
        "profit_factor": wins / losses if losses > 0 else np.nan,
        "exposure": (held != 0).mean(),
    }


def buy_hold(df, ppy):
    ret = df.close.pct_change().fillna(0)
    return metrics(ret, (1 + ret).cumprod(), pd.Series(1, df.index), ppy)
