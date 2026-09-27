"""日线信号计算：S1–S4、目标仓位、止损、BTC 联动、资金权重。

策略定义（与回测一致，所有币统一参数）：
  S1  EMA(12) > EMA(26) 持有
  S2  肯特纳：收盘 > EMA(20) + 1.5×ATR(10) 入场，收盘 < EMA(20) 离场
  S3  PBX 瀑布线 4/6/12：p1>p2>p3 且收盘>p1 入场，收盘 < p3 离场
  S4  K 线面积：最近 20 根 (收盘−SMA20)/ATR20 的均值 > 0 持有
  目标仓位 = 四个信号的平均（0 / 25% / 50% / 75% / 100%）
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import numpy as np
import pandas as pd

import exchanges


# ---------------------------------------------------------------- 指标（与 TradingView ta.* 口径一致）

def sma(s, n): return s.rolling(n).mean()
def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def rma(s, n): return s.ewm(alpha=1 / n, adjust=False).mean()


def atr(df, n):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)


def run_state(entry, exit_):
    """有状态信号：入场条件成立置 1，离场条件成立置 0，其余保持。"""
    st, out = 0, []
    for e, x in zip(entry.fillna(False), exit_.fillna(False)):
        if e:
            st = 1
        elif x:
            st = 0
        out.append(st)
    return pd.Series(out, entry.index)


def signals(df, p):
    c = df.close
    e_fast, e_slow = ema(c, p["s1_fast"]), ema(c, p["s1_slow"])
    s1 = (e_fast > e_slow).astype(int)
    mid = ema(c, p["s2_n"])
    upper = mid + p["s2_k"] * atr(df, p["s2_atr"])
    s2 = run_state(c > upper, c < mid)
    pb = lambda n: (ema(c, n) + sma(c, 2 * n) + sma(c, 4 * n)) / 3
    a, b, cc = p["s3"]
    p1, p2, p3 = pb(a), pb(b), pb(cc)
    s3 = run_state((p1 > p2) & (p2 > p3) & (c > p1), c < p3)
    area = ((c - sma(c, p["s4_n"])) / atr(df, p["s4_n"])).rolling(p["s4_n"]).sum() / p["s4_n"]
    s4 = (area > 0).astype(int)
    return dict(e_fast=e_fast, e_slow=e_slow, mid=mid, upper=upper, p1=p1, p3=p3, area=area,
                s1=s1, s2=s2, s3=s3, s4=s4, pos=(s1 + s2 + s3 + s4) / 4)


# ---------------------------------------------------------------- 单个币

def _pct(a, b):
    return float(a / b - 1) if b else None


def _f(x):
    return None if x is None or pd.isna(x) else float(x)


def coin_card(coin, meta, src, df, btc_ret, btc_pos, cfg):
    p, risk = cfg["strategy"], cfg["risk"]
    s = signals(df, p)
    c = df.close
    last = c.iloc[-1]
    pos = s["pos"]
    held = pos > 0
    # 当前持仓段：天数、入场价、最高收盘、止损价
    days_in, stop, peak, entry_px = 0, None, None, None
    if held.iloc[-1]:
        seg = c[c.index > held[::-1].idxmin()] if (~held).any() else c
        days_in = len(seg)
        peak = float(seg.max())
        stop = peak * (1 - risk["stop_drawdown"])
        entry_px = float(df.open.loc[seg.index[0]])
    flips = []
    for k in ["s1", "s2", "s3", "s4"]:
        ch = s[k].diff().iloc[-7:]
        for t, v in ch[ch != 0].dropna().items():
            flips.append({"s": k.upper(), "date": t.strftime("%m-%d"), "dir": "买入" if v > 0 else "卖出"})
    r = c.pct_change()
    j = pd.concat([r, btc_ret], axis=1).dropna()
    j365, j90 = j.iloc[-365:], j.iloc[-90:]
    corr365 = float(j365.corr().iloc[0, 1]) if len(j365) > 60 else None
    corr90 = float(j90.corr().iloc[0, 1]) if len(j90) > 30 else None
    beta = float(np.cov(j365.iloc[:, 0], j365.iloc[:, 1])[0, 1] / j365.iloc[:, 1].var()) if len(j365) > 60 else None
    agree_df = pd.concat([held, btc_pos], axis=1).dropna().iloc[-365:]
    agree = float((agree_df.iloc[:, 0] == agree_df.iloc[:, 1]).mean()) if len(agree_df) else None
    tail = df.iloc[-cfg["dashboard"]["chart_days"]:]
    grade = meta["grade"]
    lev = 0.0 if grade == "C" else float(cfg["leverage"].get(coin, cfg["leverage"]["default"]))
    bt = meta.get("backtest", {})
    return {
        "coin": coin, "grade": grade, "src": src[0], "sym": src[1],
        "okx": bool(meta.get("okx")), "hl": bool(meta.get("hyperliquid")),
        "date": df.index[-1].strftime("%Y-%m-%d"), "price": float(last),
        "chg1": _pct(last, c.iloc[-2]), "chg7": _pct(last, c.iloc[-8]), "chg30": _pct(last, c.iloc[-31]),
        "votes": [int(s[k].iloc[-1]) for k in ["s1", "s2", "s3", "s4"]],
        "pos": float(pos.iloc[-1]), "pos_prev": float(pos.iloc[-2]),
        "days_in": days_in, "entry": entry_px, "peak": peak, "stop": stop,
        "stop_dist": _pct(stop, last) if stop else None,      # 止损价相对现价，正常为负
        "stopped": bool(stop and last < stop),               # 收盘已跌破止损：应空仓，等信号重新入场
        "levels": {
            "ema12": _f(s["e_fast"].iloc[-1]), "ema26": _f(s["e_slow"].iloc[-1]),
            "kc_upper": _f(s["upper"].iloc[-1]), "kc_mid": _f(s["mid"].iloc[-1]),
            "pbx_fast": _f(s["p1"].iloc[-1]), "pbx_slow": _f(s["p3"].iloc[-1]),
            "area": _f(s["area"].iloc[-1]),
        },
        "flips": flips, "lev": lev, "vol60": float(r.iloc[-60:].std() * np.sqrt(365)),
        "btc": {"corr365": corr365, "corr90": corr90, "beta": beta, "agree": agree},
        "bt": {"L1_cagr": bt.get("cagr"), "L1_max_dd": bt.get("max_dd"), "L1_sharpe": bt.get("sharpe"),
               "bh_cagr": bt.get("bh_cagr"), "bh_max_dd": bt.get("bh_max_dd"), "r3_cagr": bt.get("r3_cagr"),
               "r3_bh_cagr": bt.get("r3_bh_cagr"), "hold_days": bt.get("hold_days"),
               "worst_mae": bt.get("worst_mae"), "days": bt.get("days")},
        "chart": {
            "t": [d.strftime("%Y-%m-%d") for d in tail.index],
            "c": [float(x) for x in tail.close],
            "e26": [_f(x) for x in s["e_slow"].loc[tail.index]],
            "p": [float(x) for x in pos.loc[tail.index]],
        },
    }


# ---------------------------------------------------------------- 全部币

def allocate(cards, risk):
    """按 60 日波动率倒数分配权重（B 级乘以系数，单币上限），再套用强跟随组和总敞口上限。
    返回 {coin: (weight, exposure)}，exposure 为名义敞口占总资金的比例。"""
    trad = [c for c in cards if c["grade"] in ("A", "B")]
    inv = {c["coin"]: (risk["b_grade_weight"] if c["grade"] == "B" else 1.0) / c["vol60"]
           for c in trad if c["vol60"] > 0}
    w = {k: v / sum(inv.values()) for k, v in inv.items()} if inv else {}
    cap = risk["weight_cap"]
    for _ in range(10):
        over = {k: v for k, v in w.items() if v > cap}
        if not over:
            break
        free = sum(v - cap for v in over.values())
        rest = {k: v for k, v in w.items() if v < cap}
        for k in over:
            w[k] = cap
        tot = sum(rest.values())
        for k, v in rest.items():
            w[k] = v + free * v / tot
    exp = {c["coin"]: 0.0 if c["stopped"] else w.get(c["coin"], 0.0) * c["pos"] * c["lev"] for c in cards}
    strong = [c["coin"] for c in cards if (c["btc"]["corr365"] or 0) >= risk["strong_follow_corr"]]
    tot = sum(exp[k] for k in strong)
    if tot > risk["strong_follow_cap"]:
        for k in strong:
            exp[k] *= risk["strong_follow_cap"] / tot
    tot = sum(exp.values())
    if tot > risk["total_exposure_cap"]:
        for k in exp:
            exp[k] *= risk["total_exposure_cap"] / tot
    return {c["coin"]: (w.get(c["coin"], 0.0), exp[c["coin"]]) for c in cards}


def compute(cfg, coins, sources):
    """计算全部币种，返回 {"summary", "coins"}。sources 来自 exchanges.resolve_sources。"""
    def load(coin):
        """按优先级取第一个历史足够（≥ min_history 根）的交易所；都不够时取历史最长的。"""
        best, errs = None, []
        for src in sources.get(coin, []):
            try:
                df = exchanges.klines(*src, days=days)
            except Exception as e:
                errs.append(f"{src[0]}：{e}")
                continue
            if best is None or len(df) > len(best[1]):
                best = (src, df)
            if len(df) >= min_hist:
                break
        return best, errs

    days = cfg["dashboard"]["history_days"]
    min_hist = cfg["dashboard"].get("min_history", 300)
    btc_best, _ = load("BTC")
    if not btc_best:
        raise RuntimeError("BTC 日线拉取失败")
    btc = btc_best[1]
    btc_ret = btc.close.pct_change()
    btc_pos = signals(btc, cfg["strategy"])["pos"] > 0

    def one(coin):
        if not sources.get(coin):
            return {"coin": coin, "error": "OKX、币安、Hyperliquid 都没有这个币"}
        best, errs = (btc_best, []) if coin == "BTC" else load(coin)
        if not best:
            return {"coin": coin, "error": "K 线拉取失败：" + "；".join(errs)}
        src, df = best
        if len(df) < 120:
            return {"coin": coin, "error": f"{src[0]} 只有 {len(df)} 根日线，至少需要 120 根"}
        return coin_card(coin, coins[coin], src, df, btc_ret, btc_pos, cfg)

    with ThreadPoolExecutor(4) as ex:
        cards = list(ex.map(one, coins))
    ok = [c for c in cards if "error" not in c]
    for k, (w, e) in allocate(ok, cfg["risk"]).items():
        c = next(x for x in ok if x["coin"] == k)
        c["weight"], c["exposure"] = w, e
    # 我的组合：只在精选币种之间重新分配
    pcfg = cfg.get("portfolio", {}) or {}
    picks = [str(x).upper() for x in pcfg.get("coins", [])]
    by = {c["coin"]: c for c in ok}
    chosen = [by[k] for k in picks if k in by and by[k]["grade"] in ("A", "B")]
    alloc = allocate(chosen, cfg["risk"])
    portfolio = {
        "capital": float(pcfg.get("capital", 10000)),
        "first_batch": float(pcfg.get("first_batch", 0.5)),
        "gap_buffer": float(pcfg.get("gap_buffer", 0.10)),
        "coins": [{"coin": k, "weight": alloc[k][0], "exposure": alloc[k][1]} for k in picks if k in alloc],
        "missing": [k for k in picks if k not in alloc],
    }
    btc_card = next((c for c in ok if c["coin"] == "BTC"), None)
    summary = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_date": btc.index[-1].strftime("%Y-%m-%d"),
        "btc_pos": btc_card["pos"] if btc_card else None,
        "btc_price": float(btc.close.iloc[-1]),
        "n": len(ok), "long": sum(c["pos"] > 0 for c in ok),
        "full": sum(c["pos"] == 1 for c in ok),
        "exposure": sum(c["exposure"] for c in ok),
        "exposure_cap": cfg["risk"]["total_exposure_cap"],
        "errors": [c for c in cards if "error" in c],
        "portfolio": portfolio,
    }
    return {"summary": summary, "coins": ok}
