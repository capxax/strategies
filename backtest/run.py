"""批量验证：样本内网格寻优 -> 样本外检验 -> 与买入持有对比 -> 跨标的汇总。

例:
  python run.py --source yahoo   --symbols SPY QQQ AAPL MSFT 0700.HK 600519.SS --start 2012-01-01
  python run.py --source binance --symbols BTCUSDT ETHUSDT SOLUSDT --interval 4h --start 2021-01-01
  python run.py --source synthetic --symbols A B C        # 离线自测
"""
import argparse, itertools, os, sys
import numpy as np
import pandas as pd
import data
from engine import backtest, metrics, buy_hold, periods_per_year
from strategies import REGISTRY
import strategies_digest  # noqa: F401  注册 FMZ digest 策略


def valid(p):
    for a, b in [("fast", "slow"), ("a", "b"), ("b", "c"), ("exit", "entry"), ("lo", "hi")]:
        if a in p and b in p and p[a] >= p[b]:
            return False
    return True


def grid(spec):
    keys = list(spec)
    for vals in itertools.product(*spec.values()):
        p = dict(zip(keys, vals))
        if valid(p):
            yield p


def evaluate(df, name, params, short, fee, ppy):
    pos = REGISTRY[name]["fn"](df, short=short, **params)
    ret, eq, held = backtest(df, pos, fee=fee)
    return metrics(ret, eq, held, ppy)


def run_symbol(df, sym, args, ppy):
    split = int(len(df) * args.train)
    warm = 250  # 指标预热，样本外也带上前面的数据计算指标
    is_df, oos_df = df.iloc[:split], df.iloc[max(0, split - warm):]
    oos_start = df.index[split]
    bh_is, bh_oos = buy_hold(is_df, ppy), buy_hold(df.loc[oos_start:], ppy)
    rows = []
    for name, spec in REGISTRY.items():
        if (args.only and name not in args.only) or not name.startswith(args.prefix):
            continue
        best, best_p = -np.inf, None
        for p in grid(spec["grid"]):
            m = evaluate(is_df, name, p, args.short, args.fee, ppy)
            score = m["sharpe"] if m["trades"] >= args.min_trades else -np.inf
            if score > best:
                best, best_p = score, p
        if best_p is None:
            continue
        m_is = evaluate(is_df, name, best_p, args.short, args.fee, ppy)
        pos = REGISTRY[name]["fn"](oos_df, short=args.short, **best_p).loc[oos_start:]
        ret, eq, held = backtest(oos_df.loc[oos_start:], pos, fee=args.fee)
        m_oos = metrics(ret, eq, held, ppy)
        rows.append({
            "symbol": sym, "strategy": name, "params": best_p,
            "is_sharpe": m_is["sharpe"], "oos_sharpe": m_oos["sharpe"],
            "oos_cagr": m_oos["cagr"], "oos_max_dd": m_oos["max_dd"],
            "oos_calmar": m_oos["calmar"], "oos_trades": m_oos["trades"],
            "oos_win_rate": m_oos["win_rate"], "oos_pf": m_oos["profit_factor"],
            "oos_exposure": m_oos["exposure"],
            "bh_oos_sharpe": bh_oos["sharpe"], "bh_oos_cagr": bh_oos["cagr"],
            "bh_oos_max_dd": bh_oos["max_dd"],
        })
    return rows


def verdict(g, min_sharpe):
    """有效 = 样本外夏普为正且不差于买入持有，或同等夏普下回撤显著更小。"""
    beat = (g.oos_sharpe > min_sharpe) & (
        (g.oos_sharpe >= g.bh_oos_sharpe) |
        ((g.oos_sharpe >= 0.8 * g.bh_oos_sharpe) & (g.oos_max_dd > 0.6 * g.bh_oos_max_dd)))
    return beat


def fmt(x, pct=False):
    if pd.isna(x):
        return "-"
    return f"{x:.1%}" if pct else f"{x:.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="yahoo", choices=["yahoo", "binance", "synthetic"])
    ap.add_argument("--symbols", nargs="+", default=["SPY", "QQQ", "AAPL"])
    ap.add_argument("--start", default="2012-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--interval", default="1d")
    ap.add_argument("--train", type=float, default=0.6, help="样本内比例")
    ap.add_argument("--fee", type=float, default=None, help="单边费率，默认股票0.0005/币安0.001")
    ap.add_argument("--short", action="store_true", help="允许做空（股票默认只做多）")
    ap.add_argument("--min-trades", type=int, default=5)
    ap.add_argument("--min-sharpe", type=float, default=0.3)
    ap.add_argument("--only", nargs="*", help="只跑指定策略")
    ap.add_argument("--prefix", default="", help="只跑名字以此开头的策略，如 fmz_")
    ap.add_argument("--out", default="reports")
    args = ap.parse_args()
    if args.fee is None:
        args.fee = 0.0005 if args.source == "yahoo" else 0.001

    rows = []
    for sym in args.symbols:
        try:
            kw = {} if args.source == "synthetic" else dict(start=args.start, end=args.end, interval=args.interval)
            df = data.load(args.source, sym, **kw)
        except Exception as e:
            print(f"[跳过] {sym}: {e}", file=sys.stderr)
            continue
        ppy = periods_per_year(df.index)
        print(f"{sym}: {len(df)} bars {df.index[0].date()} ~ {df.index[-1].date()}", file=sys.stderr)
        rows += run_symbol(df, sym, args, ppy)
    if not rows:
        sys.exit("没有可用数据")

    res = pd.DataFrame(rows)
    res["useful"] = verdict(res, args.min_sharpe)
    res["decay"] = res.oos_sharpe - res.is_sharpe
    os.makedirs(args.out, exist_ok=True)
    tag = f"{args.source}_{args.interval}{'_' + args.prefix.strip('_') if args.prefix else ''}"
    res.to_csv(os.path.join(args.out, f"detail_{tag}.csv"), index=False)

    summ = res.groupby("strategy").agg(
        symbols=("symbol", "count"), useful_ratio=("useful", "mean"),
        oos_sharpe=("oos_sharpe", "median"), bh_sharpe=("bh_oos_sharpe", "median"),
        oos_cagr=("oos_cagr", "median"), oos_max_dd=("oos_max_dd", "median"),
        bh_max_dd=("bh_oos_max_dd", "median"), decay=("decay", "median"),
        trades=("oos_trades", "median"),
    ).sort_values(["useful_ratio", "oos_sharpe"], ascending=False)
    summ.to_csv(os.path.join(args.out, f"summary_{tag}.csv"))

    lines = [f"# 回测验证报告 ({args.source}, {args.interval})", "",
             f"- 标的: {', '.join(res.symbol.unique())}",
             f"- 样本内 {args.train:.0%} 网格寻优(按夏普) → 样本外 {1-args.train:.0%} 检验；单边费率 {args.fee}，做空: {args.short}",
             f"- 判定“有用”: 样本外夏普 > {args.min_sharpe} 且 ≥ 买入持有，或夏普接近持有(≥80%)但最大回撤明显更小",
             "- decay = 样本外夏普 − 样本内夏普，越负说明过拟合越严重", "",
             "| 策略 | 有效标的占比 | OOS夏普(中位) | 持有夏普 | OOS年化 | OOS回撤 | 持有回撤 | decay | 交易数 | 结论 |",
             "|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in summ.iterrows():
        tagv = "✅ 稳健" if r.useful_ratio >= 0.6 else ("⚠️ 部分有效" if r.useful_ratio >= 0.3 else "❌ 无效")
        lines.append(f"| {name} | {r.useful_ratio:.0%} | {fmt(r.oos_sharpe)} | {fmt(r.bh_sharpe)} | "
                     f"{fmt(r.oos_cagr, 1)} | {fmt(r.oos_max_dd, 1)} | {fmt(r.bh_max_dd, 1)} | "
                     f"{fmt(r.decay)} | {r.trades:.0f} | {tagv} |")
    report = "\n".join(lines) + "\n"
    with open(os.path.join(args.out, f"report_{tag}.md"), "w") as f:
        f.write(report)
    print(report)


if __name__ == "__main__":
    main()
