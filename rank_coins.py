"""按流动性重建候选名单 coins.yaml。建议每季度运行一次。

  python rank_coins.py                 # 前 40 名写入 coins.yaml，前 12 名为核心
  python rank_coins.py --top 30 --core 10
  python rank_coins.py --dry-run       # 只打印，不写文件

选币规则（滚动检验见 README）：
  1. 币安 USDT 现货里，最近 30 天平均日成交额（美元）排名，排除稳定币、包装币、黄金代币、杠杆代币
  2. 至少有 365 天日线历史
  3. 前 --core 名为 A（核心，进入“我的组合”），其余为 B（候选，资金分配时权重减半）

每个币附带一组回测数据（全历史、默认参数、1x、只做多、含 25% 收盘止损和手续费/资金费）。
这是样本内结果，只用来了解这个币的历史特征，不用于选币。
"""
import argparse, os, re, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import numpy as np
import pandas as pd
import requests
import yaml

import exchanges
import signals

HERE = os.path.dirname(os.path.abspath(__file__))
EXCLUDE = set("USDC FDUSD TUSD DAI USDP EUR AEUR USDE BUSD XUSD USD1 PYUSD RLUSD BFUSD USDS EURI "
              "WBTC WBETH STETH BNSOL PAXG XAUT".split())
FEE, FUND = 0.0006, 0.11 / 365


def binance_daily(symbol, start="2017-08-01"):
    rows, t = [], int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
    while True:
        batch = exchanges._get(f"{exchanges.BINANCE}/api/v3/klines",
                               params={"symbol": symbol, "interval": "1d", "startTime": t, "limit": 1000})
        if not batch:
            break
        rows += [r[:6] + [r[7]] for r in batch]
        t = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    df = pd.DataFrame(rows, columns=["t", "open", "high", "low", "close", "volume", "qv"])
    df.index = pd.to_datetime(df.pop("t"), unit="ms")
    df = df.astype(float)
    return df[df.index < pd.Timestamp.now("UTC").tz_localize(None).normalize()]


def backtest(df, cfg):
    """1x、按信号持仓、收盘价从最高点回落 stop_drawdown 止损（止损后等信号归零）。"""
    pos = signals.signals(df, cfg["strategy"])["pos"].values
    cl, n, stop = df.close.values, len(df), cfg["risk"]["stop_drawdown"]
    r, held, peak, stopped = np.zeros(n), 0.0, 0.0, False
    trades, e0, eq, days = [], None, 1.0, 0
    for k in range(1, n):
        tgt = 0.0 if stopped else pos[k - 1]
        if stopped and pos[k - 1] == 0:
            stopped = False
        if tgt > 0 and held == 0:
            peak, e0, days = cl[k - 1], eq, 0
        cost = abs(tgt - held) * FEE
        held = tgt
        r[k] = held * (cl[k] / cl[k - 1] - 1) - cost - held * FUND
        if held > 0:
            days += 1
            peak = max(peak, cl[k])
            if cl[k] < peak * (1 - stop):
                stopped = True
                r[k] -= held * FEE
                held = 0.0
        eq *= 1 + r[k]
        if e0 is not None and held == 0:
            trades.append((eq / e0 - 1, days))
            e0 = None
    R = pd.Series(r, df.index)
    bh = df.close.pct_change().fillna(0)

    def st(x):
        e = (1 + x).cumprod()
        yrs = len(x) / 365
        return (e.iloc[-1] ** (1 / yrs) - 1, (e / e.cummax() - 1).min(),
                x.mean() / x.std() * np.sqrt(365) if x.std() > 0 else 0)
    c, d, s = st(R)
    bc, bd, _ = st(bh)
    r3 = R.iloc[-1095:]
    b3 = bh.iloc[-1095:]
    t = np.array([x[0] for x in trades]) if trades else np.array([0.0])
    rd = lambda v: round(float(v), 4)
    return {"cagr": rd(c), "max_dd": rd(d), "sharpe": rd(s), "bh_cagr": rd(bc), "bh_max_dd": rd(bd),
            "r3_cagr": rd(st(r3)[0]), "r3_bh_cagr": rd(st(b3)[0]),
            "win_rate": rd((t > 0).mean()), "trades": len(trades),
            "hold_days": float(np.median([x[1] for x in trades])) if trades else None,
            "worst_trade": rd(t.min()), "days": len(df)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--core", type=int, default=12)
    ap.add_argument("--min-days", type=int, default=365)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    cfg = yaml.safe_load(open(os.path.join(HERE, "config.example.yaml"), encoding="utf-8"))
    if os.path.exists(os.path.join(HERE, "config.yaml")):
        import monitor
        cfg = monitor.load_config(os.path.join(HERE, "config.yaml"))

    info = exchanges._get(f"{exchanges.BINANCE}/api/v3/exchangeInfo", params={"permissions": "SPOT"})
    bases = sorted({s["baseAsset"] for s in info["symbols"]
                    if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"} - EXCLUDE)
    bases = [b for b in bases if not re.search(r"(UP|DOWN|BULL|BEAR)$", b)]
    # 24 小时成交额先粗筛前 120，再用 30 天均值精排
    tick = {x["symbol"]: float(x["quoteVolume"]) for x in exchanges._get(f"{exchanges.BINANCE}/api/v3/ticker/24hr")}
    pre = sorted(bases, key=lambda b: -tick.get(b + "USDT", 0))[:120]
    print(f"币安 USDT 现货 {len(bases)} 个，按 24h 成交额粗筛 {len(pre)} 个，计算 30 天均值…")

    def vol30(b):
        try:
            k = exchanges._get(f"{exchanges.BINANCE}/api/v3/klines",
                               params={"symbol": b + "USDT", "interval": "1d", "limit": 31})
            return b, float(np.mean([float(x[7]) for x in k[:-1]])), None
        except Exception as e:
            return b, 0.0, str(e)
    with ThreadPoolExecutor(8) as ex:
        v = {b: q for b, q, _ in ex.map(vol30, pre)}
    ranked = sorted(pre, key=lambda b: -v[b])

    print("拉取历史并回测…")
    def hist(b):
        try:
            return b, binance_daily(b + "USDT")
        except Exception:
            return b, None
    chosen, dfs = [], {}
    with ThreadPoolExecutor(6) as ex:
        for b, df in ex.map(hist, ranked[:args.top + 30]):
            dfs[b] = df
    for b in ranked:
        df = dfs.get(b)
        if df is not None and len(df) >= args.min_days:
            chosen.append(b)
        if len(chosen) >= args.top:
            break
    if "BTC" not in chosen:
        chosen = ["BTC"] + chosen[:-1]

    okx = {i["instId"] for i in exchanges._get(f"{exchanges.OKX}/api/v5/public/instruments",
                                              params={"instType": "SWAP"})["data"] if i["state"] == "live"}
    hl = {u["name"] for u in exchanges._post(exchanges.HL, {"type": "meta"})["universe"] if not u.get("isDelisted")}
    out = {}
    for i, b in enumerate(chosen):
        out[b] = {"grade": "A" if i < args.core else "B", "liq_rank": i + 1,
                  "volume_30d_musd": round(v[b] / 1e6, 1),
                  "okx": f"{b}-USDT-SWAP" in okx, "hyperliquid": exchanges.hl_name(b, hl) is not None,
                  "backtest": backtest(dfs[b], cfg)}
        bt = out[b]["backtest"]
        print(f"{i+1:>2} {b:<6} {'核心' if i < args.core else '候选'} 30天日均 {v[b]/1e6:>8.1f}M  "
              f"历史 {bt['days']:>4} 天  回测年化 {bt['cagr']:+.0%} 回撤 {bt['max_dd']:.0%} 胜率 {bt['win_rate']:.0%}")
    if args.dry_run:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    hdr = f"""# 监控币种名单（{today} 由 rank_coins.py 生成，建议每季度重新生成一次）
# 选币规则：币安 USDT 现货最近 30 天日均成交额排名，排除稳定币/包装币，至少 {args.min_days} 天历史
# grade: A = 核心（流动性前 {args.core}，进入“我的组合”）；B = 候选（权重减半）；C = 只观察（手动添加）
# liq_rank: 流动性排名；volume_30d_musd: 30 天日均成交额（百万美元）
# okx / hyperliquid: 是否有 USDT 永续
# backtest: 全历史、默认参数、1x、只做多、含 25% 收盘止损和手续费/资金费的回测。
#   这是样本内结果，只反映历史特征，不用于选币（滚动检验表明回测排名对下一年没有预测力）。
#   win_rate 胜率；worst_trade 单笔最差（含止损）；hold_days 持仓天数中位数；r3_* 近 3 年
"""
    open(os.path.join(HERE, "coins.yaml"), "w", encoding="utf-8").write(
        hdr + yaml.safe_dump(out, allow_unicode=True, sort_keys=False, default_flow_style=None, width=220))
    print(f"\n已写入 coins.yaml：{len(out)} 个币，核心 {args.core} 个：" + " ".join(chosen[:args.core]))
    print("把 config.yaml 的 portfolio.coins 改成上面的核心名单，然后重启 monitor.py。")


if __name__ == "__main__":
    main()
