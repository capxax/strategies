"""OKX + Hyperliquid 全部 USDT 永续币种的日线回测 + 杠杆分析。

币种 = OKX USDT 永续 ∪ Hyperliquid 永续（按币名去重，重复的只测一次）。
数据优先级：币安现货（历史最长）> OKX 永续 > Hyperliquid 永续。
策略：BOT_SPEC.md 的 S1–S4 平均仓位，所有币统一使用默认参数（不按币寻优，避免过拟合）。

python universe.py            -> reports/universe.csv + reports/universe.md
"""
import json, os, sys, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import pandas as pd
import requests
import data
from strategies import REGISTRY as R
import strategies_digest  # noqa: F401

START = "2018-01-01"
MIN_DAYS = 500            # 少于此天数不评估
FEE = 0.0006              # 永续 taker 约 0.05% + 滑点
FUNDING_YR = 0.11         # 多头平均资金费 ≈ 0.01%/8h，按持仓名义价值计
MM = 0.01                 # 维持保证金率（近似）
LEVELS = [1, 2, 3, 5]


def universe():
    okx = requests.get("https://www.okx.com/api/v5/public/instruments", params={"instType": "SWAP"}, timeout=20).json()["data"]
    okx = {i["ctValCcy"]: i["instId"] for i in okx if i["settleCcy"] == "USDT" and i["state"] == "live"}
    hl = requests.post("https://api.hyperliquid.xyz/info", json={"type": "meta"}, timeout=20).json()["universe"]
    hl = {u["name"]: u["maxLeverage"] for u in hl if not u.get("isDelisted")}
    bn = requests.get("https://data-api.binance.vision/api/v3/exchangeInfo", params={"permissions": "SPOT"}, timeout=30).json()
    bn = {s["baseAsset"] for s in bn["symbols"] if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    coins = {}
    for c in okx:
        coins[c] = dict(coin=c, okx=True, hl=False, hl_name=None, hl_maxlev=None)
    for n, lev in hl.items():
        base = n[1:] if n.startswith("k") and n[1:].isupper() else n   # kPEPE = 1000 PEPE
        d = coins.setdefault(base, dict(coin=base, okx=False))
        d.update(hl=True, hl_name=n, hl_maxlev=lev)
    for c, d in coins.items():
        d["cands"] = ([("binance", c + "USDT")] if c in bn else []) + \
                     ([("okx", okx[c])] if d["okx"] else []) + ([("hyperliquid", d["hl_name"])] if d["hl"] else [])
        d["src"], d["sym"] = d["cands"][0]
    return list(coins.values())


def _load(src, sym):
    try:
        df = data.load(src, sym, start=START)
        return df[df.index < pd.Timestamp.now('UTC').tz_localize(None).normalize()]  # 丢弃未收盘的当日K线
    except Exception:
        return None


def load(d):
    """按优先级取数据；主来源历史不足时尝试其他交易所，取历史最长的。"""
    best = None
    for src, sym in d["cands"]:
        df = _load(src, sym)
        if df is not None and (best is None or len(df) > len(best[2])):
            best = (src, sym, df)
        if best and len(best[2]) >= MIN_DAYS:
            break
    if best is None:
        return None
    d["src"], d["sym"] = best[0], best[1]
    return best[2]


def position(df):
    subs = [R["ema_cross"]["fn"](df, 12, 26), R["fmz_keltner"]["fn"](df, 20, 1.5),
            R["fmz_pbx"]["fn"](df, 4, 6, 12), R["fmz_kline_area"]["fn"](df, 20, 0.0)]
    return sum(subs) / 4


def simulate(df, pos, lev):
    """永续做多，每日按 lev×pos 调整名义仓位；用当日最低价检查爆仓。"""
    held = pos.shift().fillna(0) * lev
    prev = pos.shift(2).fillna(0) * lev
    gap = (df.open / df.close.shift() - 1).fillna(0)
    intra = df.close / df.open - 1
    worst = df.low / df.open - 1
    ret = prev * gap + held * intra - (held - prev).abs() * FEE - held * FUNDING_YR / 365
    bust = (prev * gap + held * worst) <= -(1 - MM * max(held.max(), 1))
    eq = (1 + ret).cumprod()
    liq = bust.any()
    if liq:
        first = bust.idxmax()
        eq[first:] = 0.0
    return ret, eq, liq


def stats(ret, eq):
    n = len(ret); yrs = n / 365
    end = eq.iloc[-1]
    cagr = end ** (1 / yrs) - 1 if end > 0 else -1.0
    sd = ret.std()
    return dict(cagr=cagr, sharpe=ret.mean() / sd * np.sqrt(365) if sd > 0 else 0,
                max_dd=(eq / eq.cummax() - 1).min())


def trade_mae(df, pos):
    """每笔持仓（仓位>0 的连续段）从入场价到期间最低价的最大不利波动。"""
    held = pos.shift().fillna(0) > 0
    seg = (held != held.shift()).cumsum()
    maes, durs = [], []
    for _, g in df[held].groupby(seg[held]):
        entry = g.open.iloc[0]
        maes.append(g.low.min() / entry - 1)
        durs.append(len(g))
    return (min(maes) if maes else 0.0), (np.median(durs) if durs else 0), len(maes)


def evaluate(d):
    df = load(d)
    if df is None or len(df) < 30:
        return {**d, "status": "无数据"}
    days = len(df)
    base = {**d, "days": days, "first": df.index[0].date()}
    if days < MIN_DAYS:
        return {**base, "status": "历史不足"}
    pos = position(df)
    bh = df.close.pct_change().fillna(0)
    bh_eq = (1 + bh).cumprod()
    out = {**base, "status": "ok"}
    out.update({f"bh_{k}": v for k, v in stats(bh, bh_eq).items()})
    for L in LEVELS:
        ret, eq, liq = simulate(df, pos, L)
        s = stats(ret, eq)
        out.update({f"L{L}_{k}": v for k, v in s.items()}); out[f"L{L}_liq"] = liq
    # 最近 3 年（统一窗口，便于比较）
    recent = df.index >= df.index[-1] - pd.Timedelta(days=3 * 365)
    r1, e1, _ = simulate(df, pos, 1)
    rr = r1[recent]; out.update({f"r3_{k}": v for k, v in stats(rr, (1 + rr).cumprod()).items()})
    br = bh[recent]; out.update({f"r3_bh_{k}": v for k, v in stats(br, (1 + br).cumprod()).items()})
    mae, dur, n = trade_mae(df, pos)
    out.update(worst_mae=mae, hold_days=dur, trades=n, exposure=(pos > 0).mean(),
               vol=bh.std() * np.sqrt(365))
    # 杠杆建议：① 最差一笔不利波动 ×1.5 仍不爆仓；② 1x 最大回撤放大后不超过 50%；③ 不超过 3x
    safe_liq = 0.9 / (abs(mae) * 1.5) if mae < 0 else 3
    safe_dd = 0.5 / abs(out["L1_max_dd"]) if out["L1_max_dd"] < 0 else 3
    lev = max(1.0, min(3.0, safe_liq, safe_dd, d.get("hl_maxlev") or 99))
    out["lev_rec"] = np.floor(lev * 2) / 2
    return out


def verdict(r):
    if r["status"] != "ok":
        return r["status"]
    good = (r["L1_sharpe"] > 0.5) and (r["L1_sharpe"] >= r["bh_sharpe"] or r["L1_max_dd"] > 0.7 * r["bh_max_dd"]) \
        and r["L1_cagr"] > 0.05
    recent_ok = r["r3_sharpe"] > 0.3 and r["r3_cagr"] > 0
    if good and recent_ok and r["days"] >= 1000:
        return "A 推荐"
    if good and recent_ok:
        return "B 可用(历史较短)"
    if r["L1_cagr"] > 0 and r["L1_max_dd"] > -0.6:
        return "C 观察"
    return "D 不适用"


def main():
    coins = universe()
    print(f"币种: {len(coins)}  (来源 " + ", ".join(f"{k}:{v}" for k, v in
          pd.Series([c['src'] for c in coins]).value_counts().items()) + ")", file=sys.stderr)
    fast = [c for c in coins if c["src"] != "okx"]
    slow = [c for c in coins if c["src"] == "okx"]
    with ThreadPoolExecutor(8) as ex1, ThreadPoolExecutor(2) as ex2:
        rows = list(ex1.map(evaluate, fast)) + list(ex2.map(evaluate, slow))
    for r in rows:
        r.pop("cands", None)
    df = pd.DataFrame(rows)
    df["grade"] = df.apply(verdict, axis=1)
    os.makedirs("reports", exist_ok=True)
    df.sort_values(["grade", "L1_sharpe"], ascending=[True, False]).to_csv("reports/universe.csv", index=False)
    print(df.grade.value_counts().to_string())


if __name__ == "__main__":
    main()
