"""生成币种监控仪表盘。

每天 UTC 00:05 之后运行一次（和 bot 同一时间），读取最新已收盘日线，计算 BOT_SPEC 中
S1–S4 各自的信号、目标仓位、关键价位、止损价、与 BTC 的联动性，并合并回测评级。

  python dashboard/build.py              -> dashboard/index.html（数据已内嵌，直接用浏览器打开）
  python dashboard/build.py --json out.json

只显示 A/B/C 级币种（D 级不适用，不显示）。
"""
import argparse, json, os, sys, tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
BT = os.path.join(HERE, "..", "backtest")
sys.path.insert(0, BT)
import data  # noqa: E402
from indicators import ema, sma, atr  # noqa: E402

LOOKBACK_DAYS = 420        # 指标预热 300 根 + 图表 120 根
CHART_DAYS = 180
STOP_DRAWDOWN = 0.25       # 持仓后最高收盘价回落 25% 强平（BOT_SPEC 第 5 节）
LEVERAGE = {"BTC": 2.0, "ETH": 1.5, "BNB": 1.5, "HYPE": 1.5}   # 其余 A/B 级 1x，C 级不交易
WEIGHT_CAP = 0.25
STRONG_CAP = 0.60           # BTC 强跟随组合计敞口上限
TOTAL_CAP = 1.50            # 账户总名义敞口上限


def run_state(entry, exit_):
    st, out = 0, []
    for e, x in zip(entry.fillna(False), exit_.fillna(False)):
        if e:
            st = 1
        elif x:
            st = 0
        out.append(st)
    return pd.Series(out, entry.index)


def signals(df):
    c = df.close
    e12, e26 = ema(c, 12), ema(c, 26)
    s1 = (e12 > e26).astype(int)
    mid = ema(c, 20)
    upper = mid + 1.5 * atr(df, 10)
    s2 = run_state(c > upper, c < mid)
    pb = lambda n: (ema(c, n) + sma(c, 2 * n) + sma(c, 4 * n)) / 3
    p1, p2, p3 = pb(4), pb(6), pb(12)
    s3 = run_state((p1 > p2) & (p2 > p3) & (c > p1), c < p3)
    area = ((c - sma(c, 20)) / atr(df, 20)).rolling(20).sum() / 20
    s4 = (area > 0).astype(int)
    pos = (s1 + s2 + s3 + s4) / 4
    return dict(e12=e12, e26=e26, mid=mid, upper=upper, p1=p1, p2=p2, p3=p3, area=area,
                s1=s1, s2=s2, s3=s3, s4=s4, pos=pos)


def fetch(src, sym):
    start = (pd.Timestamp.now("UTC") - pd.Timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    df = data.load(src, sym, start=start)
    today = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    return df[df.index < today]


def pct(a, b):
    return float(a / b - 1) if b else None


def coin_card(row, btc_ret, btc_pos):
    try:
        df = fetch(row["src"], row["sym"])
    except Exception as e:
        return {"coin": row["coin"], "error": str(e)}
    if len(df) < 120:
        return {"coin": row["coin"], "error": "数据不足"}
    s = signals(df)
    c = df.close
    last = c.iloc[-1]
    pos = s["pos"]
    held = pos > 0
    # 当前持仓段起点、持仓天数、段内最高收盘 → 止损价
    days_in, stop, peak, entry_px = 0, None, None, None
    if held.iloc[-1]:
        seg_start = held[::-1].idxmin() if (~held).any() else held.index[0]
        seg = c[c.index > seg_start] if (~held).any() else c
        days_in = len(seg)
        peak = float(seg.max()); stop = peak * (1 - STOP_DRAWDOWN)
        entry_px = float(df.open.loc[seg.index[0]]) if len(seg) else None
    # 近 7 天信号变化
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
    a = pd.concat([held, btc_pos], axis=1).dropna().iloc[-365:]
    agree = float((a.iloc[:, 0] == a.iloc[:, 1]).mean()) if len(a) else None
    tail = df.iloc[-CHART_DAYS:]
    grade = row["grade"][0]
    lev = 0.0 if grade == "C" else LEVERAGE.get(row["coin"], 1.0)
    vol60 = float(r.iloc[-60:].std() * np.sqrt(365))
    f = lambda x: None if x is None or pd.isna(x) else float(x)
    return {
        "coin": row["coin"], "grade": grade, "src": row["src"], "sym": row["sym"],
        "okx": bool(row["okx"]), "hl": bool(row["hl"]),
        "date": df.index[-1].strftime("%Y-%m-%d"), "price": float(last),
        "chg1": pct(last, c.iloc[-2]), "chg7": pct(last, c.iloc[-8]), "chg30": pct(last, c.iloc[-31]),
        "votes": [int(s[k].iloc[-1]) for k in ["s1", "s2", "s3", "s4"]],
        "pos": float(pos.iloc[-1]), "pos_prev": float(pos.iloc[-2]),
        "days_in": days_in, "entry": entry_px, "peak": peak, "stop": stop,
        "stop_dist": pct(stop, last) if stop else None,      # 止损价相对现价，正常为负
        "stopped": bool(stop and last < stop),              # 已跌破止损：bot 应空仓，等信号重新入场
        "levels": {
            "ema12": f(s["e12"].iloc[-1]), "ema26": f(s["e26"].iloc[-1]),
            "kc_upper": f(s["upper"].iloc[-1]), "kc_mid": f(s["mid"].iloc[-1]),
            "pbx_fast": f(s["p1"].iloc[-1]), "pbx_slow": f(s["p3"].iloc[-1]),
            "area": f(s["area"].iloc[-1]),
        },
        "flips": flips, "lev": lev, "vol60": vol60,
        "btc": {"corr365": corr365, "corr90": corr90, "beta": beta, "agree": agree},
        "bt": {k: f(row.get(k)) for k in ["L1_cagr", "L1_max_dd", "L1_sharpe", "bh_cagr", "bh_max_dd",
                                           "r3_cagr", "r3_bh_cagr", "hold_days", "worst_mae", "days"]},
        "chart": {
            "t": [d.strftime("%Y-%m-%d") for d in tail.index],
            "c": [float(x) for x in tail.close],
            "e26": [f(x) for x in s["e26"].loc[tail.index]],
            "p": [float(x) for x in pos.loc[tail.index]],
        },
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=os.path.join(BT, "results", "universe.csv"))
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    ap.add_argument("--json", default=None)
    ap.add_argument("--fragment", default=None, help="另存一份不带 <html> 外壳的页面（用于嵌入/发布）")
    args = ap.parse_args()
    data.CACHE = os.path.join(tempfile.gettempdir(), "dash_cache")   # 实时数据不污染回测缓存

    u = pd.read_csv(args.universe)
    u = u[u.grade.str[0].isin(["A", "B", "C"])]
    btc = fetch("binance", "BTCUSDT")
    btc_ret = btc.close.pct_change()
    btc_pos = signals(btc)["pos"] > 0
    with ThreadPoolExecutor(6) as ex:
        cards = list(ex.map(lambda r: coin_card(r, btc_ret, btc_pos), [r for _, r in u.iterrows()]))
    ok = [c for c in cards if "error" not in c]
    # 建议资金权重：可交易（A/B）币按 60 日波动率倒数，单币上限 25%
    trad = [c for c in ok if c["grade"] in "AB"]
    inv = {c["coin"]: (0.5 if c["grade"] == "B" else 1.0) / c["vol60"] for c in trad if c["vol60"] > 0}  # B 级权重减半
    w = {k: v / sum(inv.values()) for k, v in inv.items()}
    for _ in range(10):
        over = {k: v for k, v in w.items() if v > WEIGHT_CAP}
        if not over:
            break
        free = sum(v - WEIGHT_CAP for v in over.values())
        rest = {k: v for k, v in w.items() if v < WEIGHT_CAP}
        for k in over:
            w[k] = WEIGHT_CAP
        tot = sum(rest.values())
        for k, v in rest.items():
            w[k] = v + free * v / tot
    for c in ok:
        c["weight"] = w.get(c["coin"], 0.0)
        c["exposure"] = 0.0 if c["stopped"] else c["weight"] * c["pos"] * c["lev"]
    # BOT_SPEC 4.2：BTC 强跟随组（相关性 ≥0.70）合计敞口 ≤60%；10：账户总敞口 ≤150%
    strong = [c for c in ok if (c["btc"]["corr365"] or 0) >= 0.7]
    tot_strong = sum(c["exposure"] for c in strong)
    if tot_strong > STRONG_CAP:
        for c in strong:
            c["exposure"] *= STRONG_CAP / tot_strong
    tot = sum(c["exposure"] for c in ok)
    if tot > TOTAL_CAP:
        for c in ok:
            c["exposure"] *= TOTAL_CAP / tot
    btc_card = next((c for c in ok if c["coin"] == "BTC"), None)
    summary = {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "data_date": btc.index[-1].strftime("%Y-%m-%d"),
        "btc_pos": btc_card["pos"] if btc_card else None,
        "btc_price": float(btc.close.iloc[-1]),
        "n": len(ok), "long": sum(c["pos"] > 0 for c in ok),
        "full": sum(c["pos"] == 1 for c in ok),
        "exposure": sum(c["exposure"] for c in ok),
        "errors": [c for c in cards if "error" in c],
    }
    payload = {"summary": summary, "coins": ok}
    if args.json:
        json.dump(payload, open(args.json, "w"), ensure_ascii=False)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__DATA__*/null", json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    if args.fragment:
        open(args.fragment, "w", encoding="utf-8").write(html)
    page = ('<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '</head>\n<body style="margin:0">\n' + html + '\n</body>\n</html>\n')
    open(args.out, "w", encoding="utf-8").write(page)
    print(f"{args.out}: {len(ok)} coins, {summary['long']} long, errors={len(summary['errors'])}")


if __name__ == "__main__":
    main()
