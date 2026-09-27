"""分析自动交易记录（logs/ 目录）。

  python analyze.py                  # 汇总全部记录
  python analyze.py --mode live      # 只看实盘（live / demo / dry）
  python analyze.py --since 2026-10-01

输出：权益变化、各币已实现盈亏、手续费、滑点（成交价相对下单时参考价）、交易次数与胜率。
"""
import argparse, json, os
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(HERE, "logs"))
    ap.add_argument("--mode", default=None, help="live / demo / dry")
    ap.add_argument("--since", default=None)
    a = ap.parse_args()
    pd.set_option("display.width", 200)

    tp = os.path.join(a.dir, "trades.csv")
    ep = os.path.join(a.dir, "equity.csv")
    if not os.path.exists(tp):
        return print(f"没有交易记录：{tp}")
    t = pd.read_csv(tp, parse_dates=["time"])
    if a.mode:
        t = t[t["mode"] == a.mode]
    if a.since:
        t = t[t["time"] >= a.since]
    ok = t[t["state"] == "filled"].copy()
    print(f"订单 {len(t)} 笔，成交 {len(ok)} 笔，失败 {int((t['state'] == 'error').sum())} 笔，"
          f"dry-run {int((t['state'] == 'dry-run').sum())} 笔")
    if len(ok):
        for col in ["fee", "pnl", "fill_px", "est_price", "fill_notional"]:
            ok[col] = pd.to_numeric(ok[col], errors="coerce")
        # 滑点：买入成交价高于参考价、卖出低于参考价为不利（正数 = 吃亏）
        sign = ok["side"].map({"buy": 1, "sell": -1})
        ok["slip_bp"] = (ok["fill_px"] / ok["est_price"] - 1) * sign * 1e4
        by = ok.groupby("coin").agg(成交笔数=("coin", "size"), 成交额U=("fill_notional", "sum"),
                                     已实现盈亏U=("pnl", "sum"), 手续费U=("fee", "sum"),
                                     平均滑点bp=("slip_bp", "mean"))
        by.loc["合计"] = [by["成交笔数"].sum(), by["成交额U"].sum(), by["已实现盈亏U"].sum(),
                        by["手续费U"].sum(), ok["slip_bp"].mean()]
        print("\n各币汇总（手续费为负数表示支出）")
        print(by.round(3).to_string())
        closes = ok[(ok["side"] == "sell") & ok["reason"].isin(["止损离场", "信号离场", "移出名单"])]
        if len(closes):
            print(f"\n平仓 {len(closes)} 次，盈利 {int((closes['pnl'] > 0).sum())} 次，"
                  f"胜率 {(closes['pnl'] > 0).mean():.0%}；按原因：")
            print(closes.groupby("reason").pnl.agg(["count", "sum", "mean"]).round(3).to_string())
    if os.path.exists(ep):
        e = pd.read_csv(ep, parse_dates=["time"])
        if a.mode:
            e = e[e["mode"] == a.mode]
        if a.since:
            e = e[e["time"] >= a.since]
        e = e.groupby("data_date").last()
        if len(e):
            first, last = e["equity"].iloc[0], e["equity"].iloc[-1]
            print(f"\n权益：{first:.2f} → {last:.2f}（{last / first - 1:+.1%}），"
                  f"最大回撤 {(e['equity'] / e['equity'].cummax() - 1).min():.1%}，记录 {len(e)} 天")
            print(e[["equity", "positions", "notional", "upl", "drawdown"]].tail(15).round(3).to_string())
    dp = os.path.join(a.dir, "decisions.jsonl")
    if os.path.exists(dp):
        recs = [json.loads(l) for l in open(dp, encoding="utf-8") if l.strip()]
        errs = [(r["data_date"], x) for r in recs for x in r.get("errors", [])]
        print(f"\n调仓决策记录 {len(recs)} 次，其中出错 {len(errs)} 条" + ("，最近：" if errs else ""))
        for d, x in errs[-5:]:
            print(" ", d, x)


if __name__ == "__main__":
    main()
