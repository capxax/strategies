"""OKX 自动交易：按“我的组合”的目标仓位调仓（USDT 永续、逐仓、只做多）。

  python trader.py --once            # 按 config.yaml 执行一次（dry_run 为 true 时只打印订单）
  python trader.py --once --live     # 忽略 dry_run，真实下单（仍需 trading.enabled: true）
  python trader.py --status          # 查看账户、持仓和保护止损单
  python trader.py --close-all       # 平掉 bot 管理的全部仓位并撤掉保护止损单

monitor.py 在 trading.enabled 为 true 时，每天日线重算后自动调用一次 rebalance()。

流程：
  1. 本金 = min(trading.capital, 账户 USDT 权益)
  2. 每个币目标名义价值 = 本金 × 组合敞口（已含目标仓位、杠杆、强跟随组和总敞口上限）
     → 换算成合约张数，按最小下单量和步长向下取整，不够最小一张的币跳过
  3. 与当前持仓比较，差额超过阈值才下单；止损离场或目标为 0 的币全部平仓
  4. 逐仓、市价单；加仓前设置杠杆
  5. 每个持仓挂一张保护止损单（止损价再往下 protective_buffer），只防盘中暴跌；
     正常止损按日线收盘价由 bot 执行（与回测一致）
"""
import argparse, json, os, sys, time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN


HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
STATE_FILE = os.path.join(HERE, ".trader_state.json")
TAG = "tm"            # 保护止损单的 algoClOrdId 前缀，只撤 bot 自己挂的单


class OKXError(Exception):
    pass


class OKX:
    """OKX 官方 SDK（python-okx）的薄封装：统一检查返回码，出错抛 OKXError。"""

    def __init__(self, key, secret, passphrase, demo=False):
        import okx.Account as Account
        import okx.MarketData as MarketData
        import okx.PublicData as PublicData
        import okx.Trade as Trade
        self.key, self.secret, self.passphrase, self.demo = key, secret, passphrase, demo
        flag = "1" if demo else "0"                       # 1 = 模拟盘，0 = 实盘
        k = (key or "-1", secret or "-1", passphrase or "-1")
        self.public = PublicData.PublicAPI(flag=flag, debug=False)
        self.market = MarketData.MarketAPI(flag=flag, debug=False)
        # use_server_time=True：用 OKX 服务器时间签名，本机时钟不准也不会被拒（50102）
        self.account = Account.AccountAPI(*k, True, flag, debug=False)
        self.trade = Trade.TradeAPI(*k, True, flag, debug=False)

    @property
    def has_keys(self):
        return bool(self.key and self.secret and self.passphrase)

    @staticmethod
    def _call(name, fn, *a, **kw):
        for i in range(3):
            try:
                j = fn(*a, **kw)
                break
            except Exception as e:                        # 网络错误、HTTP 错误
                if i == 2:
                    raise OKXError(f"{name} 请求失败：{e}")
                time.sleep(1 + i)
        if str(j.get("code")) != "0":
            detail = ""
            if isinstance(j.get("data"), list) and j["data"] and isinstance(j["data"][0], dict):
                detail = j["data"][0].get("sMsg") or ""
            raise OKXError(f"{name} 失败：{j.get('code')} {j.get('msg')} {detail}".strip())
        return j["data"]

    # 公共行情
    def instruments(self):
        return {i["instId"]: i for i in self._call("获取合约信息", self.public.get_instruments, instType="SWAP")}

    def tickers(self):
        return {x["instId"]: float(x["last"]) for x in self._call("获取行情", self.market.get_tickers, instType="SWAP")
                if x.get("last")}

    # 账户
    def config(self):
        return self._call("读取账户配置", self.account.get_account_config)[0]

    def usdt_equity(self):
        d = self._call("读取余额", self.account.get_account_balance, ccy="USDT")[0]
        for x in d.get("details", []):
            if x["ccy"] == "USDT":
                return float(x.get("eq") or 0), float(x.get("availEq") or x.get("availBal") or 0)
        return 0.0, 0.0

    def positions(self):
        out = {}
        for p in self._call("读取持仓", self.account.get_positions, instType="SWAP"):
            sz = float(p.get("pos") or 0)
            if sz and p.get("mgnMode") == "isolated":
                out[p["instId"]] = {"sz": sz, "avgPx": float(p.get("avgPx") or 0),
                                    "lever": p.get("lever"), "posSide": p.get("posSide"),
                                    "upl": float(p.get("upl") or 0), "liqPx": float(p.get("liqPx") or 0)}
        return out

    def set_leverage(self, inst, lever, pos_side=None):
        return self._call(f"{inst} 设置杠杆", self.account.set_leverage, lever=str(lever), mgnMode="isolated",
                          instId=inst, posSide=pos_side or "")

    # 交易
    def market(self, inst, side, sz, reduce_only=False, pos_side=None):
        kw = {"instId": inst, "tdMode": "isolated", "side": side, "ordType": "market", "sz": sz}
        if pos_side:
            kw["posSide"] = pos_side
        elif reduce_only:
            kw["reduceOnly"] = "true"
        return self._call(f"{inst} 下单", self.trade.place_order, **kw)[0]

    def order_detail(self, inst, ord_id):
        return self._call(f"{inst} 查询订单", self.trade.get_order, instId=inst, ordId=ord_id)[0]

    def pending_stops(self):
        return [a for a in self._call("读取止损单", self.trade.order_algos_list, ordType="conditional", instType="SWAP")
                if (a.get("algoClOrdId") or "").startswith(TAG)]

    def cancel_algos(self, algos):
        if algos:
            self._call("撤销止损单", self.trade.cancel_algo_order,
                       [{"algoId": a["algoId"], "instId": a["instId"]} for a in algos])

    def place_stop(self, inst, sz, trigger, pos_side=None):
        kw = {"instId": inst, "tdMode": "isolated", "side": "sell", "ordType": "conditional", "sz": sz,
              "slTriggerPx": trigger, "slOrdPx": "-1", "slTriggerPxType": "last",
              "algoClOrdId": f"{TAG}{inst.split('-')[0]}{int(time.time())}"[:32]}
        if pos_side:
            kw["posSide"] = pos_side
        else:
            kw["reduceOnly"] = "true"
        return self._call(f"{inst} 挂保护止损单", self.trade.place_algo_order, **kw)


# ---------------------------------------------------------------- 工具

def _floor(x, step):
    s = Decimal(str(step))
    return float((Decimal(str(x)) / s).to_integral_value(rounding=ROUND_DOWN) * s)


def _fmt(x, step):
    """按步长的小数位输出字符串（OKX 要求）。"""
    d = max(0, -Decimal(str(step)).normalize().as_tuple().exponent)
    return f"{x:.{d}f}"


def _px(x, tick):
    return _fmt(_floor(x, tick), tick)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except Exception:
        return {"managed": [], "last_run": None, "last_report": None}


def save_state(st):
    json.dump(st, open(STATE_FILE, "w"), ensure_ascii=False, indent=1)


def client(cfg):
    t = cfg["trading"]
    ok = t.get("okx", {}) or {}
    return OKX(ok.get("api_key", ""), ok.get("api_secret", ""), ok.get("passphrase", ""), bool(ok.get("demo")))


# ---------------------------------------------------------------- 记录

import csv
import logging

logger = logging.getLogger("trend")   # 与 monitor.py 共用，写入 logs/monitor.log

TRADE_COLS = ["time", "data_date", "mode", "coin", "inst", "side", "reason", "sz", "est_price", "est_notional",
              "lever", "ord_id", "state", "fill_sz", "fill_px", "fill_notional", "fee", "pnl", "error"]
EQUITY_COLS = ["time", "data_date", "mode", "equity", "avail", "positions", "notional", "upl", "equity_peak",
               "drawdown", "capital_used"]


def log_dir(cfg):
    d = cfg["trading"].get("log_dir") or "logs"
    d = d if os.path.isabs(d) else os.path.join(HERE, d)
    os.makedirs(d, exist_ok=True)
    return d


def _append_csv(path, cols, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow(row)


def journal_trade(cfg, row):
    _append_csv(os.path.join(log_dir(cfg), "trades.csv"), TRADE_COLS, row)


def journal_equity(cfg, row):
    _append_csv(os.path.join(log_dir(cfg), "equity.csv"), EQUITY_COLS, row)


def journal_decision(cfg, rec):
    with open(os.path.join(log_dir(cfg), "decisions.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def log(*a):
    msg = " ".join(str(x) for x in a)
    if logger.handlers:
        logger.info("[trader] " + msg)
    else:
        print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "[trader]", msg, flush=True)


# ---------------------------------------------------------------- 调仓

def plan(payload, cfg, ex, equity=None, positions=None, managed=None):
    """计算目标张数和需要执行的订单（不下单）。"""
    t = cfg["trading"]
    ins, px = ex.instruments(), ex.tickers()
    capital = float(t["capital"])
    if equity is not None:
        capital = min(capital, equity)
    positions = positions or {}
    day = payload["summary"]["data_date"]
    by = {c["coin"]: c for c in payload["coins"]}
    pf = {p["coin"]: p for p in payload["summary"]["portfolio"]["coins"]}
    # 数据异常：拉取失败、或最后一根日线不是最新（交易所数据延迟）→ 保持现有仓位，不做任何操作
    bad = {e["coin"]: e["error"] for e in payload["summary"].get("errors", [])}
    bad.update({k: f"数据只到 {c['date']}，不是最新的 {day}" for k, c in by.items() if c["date"] != day})
    bad.update({k: "暂无数据" for k in payload["summary"]["portfolio"].get("missing", []) if k not in bad})
    coins = [str(c).upper() for c in (t.get("coins") or list(pf) + [k for k in payload["summary"]["portfolio"].get("missing", [])])]
    for inst in managed or []:                    # 以前建过仓、现在不在名单里的币（例如季度更新被移出）
        coin = inst.split("-")[0]
        if coin not in coins:
            coins.append(coin)
    rows = []
    for coin in dict.fromkeys(coins):
        inst = f"{coin}-USDT-SWAP"
        c, p, i = by.get(coin), pf.get(coin), ins.get(inst)
        cur = positions.get(inst, {}).get("sz", 0.0)
        row = {"coin": coin, "inst": inst, "cur": cur, "target": 0.0, "action": None, "note": "",
               "lot": float(i["lotSz"]) if i else 0.01, "tick": float(i["tickSz"]) if i else 0.0001,
               "ct": float(i["ctVal"]) if i else None, "price": px.get(inst)}
        rows.append(row)
        if not i or i.get("state") != "live":
            row["note"] = "OKX 没有这个 USDT 永续"
            continue
        if coin in bad:
            row.update(target=cur, note=f"数据异常，保持现有仓位（{bad[coin]}）")
            continue
        if not c or not p:
            row["note"] = "已移出名单"
            if cur:
                row.update(action="sell", sz=cur, reason="移出名单", note="已移出名单，平掉已有仓位")
            continue
        ct, lot, mn = row["ct"], row["lot"], float(i["minSz"])
        last = row["price"] or c.get("live") or c["price"]
        # 止损或信号离场时目标一律为 0（不依赖上游的敞口计算，双重保险）
        notional = 0.0 if (c["stopped"] or c["pos"] <= 0) else capital * p["exposure"]
        tgt = _floor(notional / (ct * last), lot) if notional > 0 else 0.0
        if notional > 0 and tgt < mn:
            row["note"] = f"目标 {notional:.1f}U 不够最小一张（{mn * ct * last:.1f}U），跳过"
            tgt = 0.0
        row.update(target=tgt, notional=notional, price=last, lev=c["lev"], stop=c.get("stop"),
                   stopped=c["stopped"], pos=c["pos"], votes=c["votes"], min_sz=mn)
        diff = tgt - cur
        thr = max(float(t["min_trade_usdt"]), float(t["rebalance_threshold"]) * notional)
        if tgt == 0 and cur > 0:
            reason = "止损离场" if c["stopped"] else ("信号离场" if c["pos"] == 0 else "目标不足最小一张")
            row.update(action="sell", sz=cur, reason=reason, note=reason)
        elif diff > 0 and (cur == 0 or diff * ct * last >= thr):
            row.update(action="buy", sz=_floor(diff, lot), reason="入场" if cur == 0 else "加仓")
        elif diff < 0 and -diff * ct * last >= thr:
            row.update(action="sell", sz=_floor(-diff, lot), reason="减仓")
        if row["action"] and row.get("sz", 0) <= 0:
            row["action"] = None
    return capital, rows


def _fill(ex, inst, ord_id):
    """查询成交结果（市价单通常 1 秒内成交）。"""
    d = {}
    for _ in range(5):
        time.sleep(0.6)
        try:
            d = ex.order_detail(inst, ord_id)
        except OKXError:
            continue
        if d.get("state") in ("filled", "canceled", "mmp_canceled"):
            return d
    return d


def rebalance(payload, cfg, live=False, notify=None, force=True):
    """执行一次调仓。返回 (报告文本, 是否全部成功)。
    live=False 时按 dry_run 配置决定是否只模拟；force=False 时同一根日线只成功执行一次。"""
    t = cfg["trading"]
    dry = (not live) and bool(t.get("dry_run", True))
    ex = client(cfg)
    st = load_state()
    day = payload["summary"]["data_date"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if not force and not dry and st.get("data_date") == day:
        return None, True
    mode = "dry" if dry else ("demo" if ex.demo else "live")
    head = f"🤖 <b>自动交易 {'（dry-run，未下单）' if dry else ('模拟盘' if ex.demo else '实盘')}</b>"
    equity = avail = positions = None
    pos_mode = "net_mode"
    if ex.has_keys:
        acc = ex.config()
        pos_mode = acc.get("posMode", "net_mode")
        if acc.get("acctLv") == "1":
            raise OKXError("OKX 账户模式为“简单模式”，不能交易永续。请在 OKX 设置里切换到“单币种保证金”或以上模式。")
        equity, avail = ex.usdt_equity()
        positions = ex.positions()
    elif not dry:
        raise OKXError("config.yaml 里 trading.okx 的 api_key / api_secret / passphrase 没有填写")
    ps = "long" if pos_mode == "long_short_mode" else None
    capital, rows = plan(payload, cfg, ex, equity, positions, st.get("managed"))

    # 账户熔断：权益从历史最高回撤超过 max_drawdown，停止加仓（减仓、平仓照常）
    halted = False
    if equity is not None and not dry:
        st["equity_peak"] = max(float(st.get("equity_peak") or 0), equity)
        dd = 1 - equity / st["equity_peak"] if st["equity_peak"] else 0
        if dd >= float(t["max_drawdown"]):
            halted = True
    lines = [f"本金 {capital:.2f}U" + (f"（账户权益 {equity:.2f}U，可用 {avail:.2f}U）" if equity is not None
                                      else "（未连接账户，按配置本金计算）")]
    if halted:
        lines.append(f"⛔ 账户回撤 {dd:.0%} ≥ {float(t['max_drawdown']):.0%}，熔断：暂停加仓，只执行减仓和平仓。"
                     "人工确认后删除 .trader_state.json 里的 equity_peak 可解除。")
    errors = []
    sells = [r for r in rows if r["action"] == "sell"]
    buys = [r for r in rows if r["action"] == "buy"]
    for r in buys if halted else []:
        r.update(action=None, note="账户熔断，暂停加仓")
    buys = [] if halted else buys

    def execute(row):
        sz = _fmt(row["sz"], row["lot"])
        est = row["sz"] * row["ct"] * row["price"] if row.get("ct") and row.get("price") else None
        desc = f"{'🔻 卖出' if row['action'] == 'sell' else '🟢 买入'} {row['coin']} {sz} 张" \
               + (f" ≈ {est:.1f}U" if est else "") + f"（{row.get('reason') or row['note']}）"
        rec = {"time": now, "data_date": day, "mode": mode, "coin": row["coin"], "inst": row["inst"],
               "side": row["action"], "reason": row.get("reason"), "sz": sz, "est_price": row.get("price"),
               "est_notional": round(est, 4) if est else None, "lever": row.get("lev")}
        if dry:
            journal_trade(cfg, {**rec, "state": "dry-run"})
            return desc
        try:
            if row["action"] == "buy":
                lev = row["lev"]
                try:
                    ex.set_leverage(row["inst"], lev, ps)
                except OKXError:
                    lev = max(1, int(lev))            # 不支持小数杠杆时向下取整
                    ex.set_leverage(row["inst"], lev, ps)
                rec["lever"] = lev
                res = ex.market(row["inst"], "buy", sz, pos_side=ps)
            else:
                res = ex.market(row["inst"], "sell", sz, reduce_only=True, pos_side=ps)
            d = _fill(ex, row["inst"], res.get("ordId"))
            fsz = float(d.get("accFillSz") or 0)
            fpx = float(d.get("avgPx") or 0)
            rec.update(ord_id=res.get("ordId"), state=d.get("state"), fill_sz=fsz, fill_px=fpx,
                       fill_notional=round(fsz * row["ct"] * fpx, 4) if row.get("ct") else None,
                       fee=d.get("fee"), pnl=d.get("pnl"))
            journal_trade(cfg, rec)
            if d.get("state") != "filled":
                errors.append(f"{row['coin']}：订单状态 {d.get('state')}，成交 {fsz} 张")
                return desc + f" ⚠️ {d.get('state')}"
            return desc + f" ✅ 成交价 {fpx:g}"
        except OKXError as e:
            journal_trade(cfg, {**rec, "state": "error", "error": str(e)})
            errors.append(f"{row['coin']}：{e}")
            return desc + " ❌"

    # 先减仓再加仓，释放保证金
    for r in sells:
        lines.append(execute(r))
    if buys and not dry and ex.has_keys:
        _, avail = ex.usdt_equity()                    # 卖出后刷新可用保证金
    for r in buys:
        if avail is not None:
            lev_m = max(1, int(r["lev"]))            # 按取整后的杠杆估算保证金（偏保守）
            per = r["ct"] * r["price"] / lev_m * 1.02  # 每张需要的保证金（含手续费余量）
            need = r["sz"] * per
            if need > avail:                          # 保证金不够：按可用资金缩小，仍不够最小一张则跳过
                sz = _floor(avail / per, r["lot"])
                if sz < r.get("min_sz", r["lot"]):
                    r.update(action=None, note=f"可用保证金 {avail:.1f}U 不足（需要 {need:.1f}U），跳过")
                    errors.append(f"{r['coin']}：{r['note']}")
                    continue
                r.update(sz=sz, reason=r["reason"] + "（保证金不足，已缩小）")
                need = sz * per
            avail -= need
        lines.append(execute(r))

    # 保护止损单：撤掉 bot 旧单，按最新持仓重新挂
    if not dry and ex.has_keys:
        time.sleep(1)
        positions = ex.positions()
        # 需要重挂的：有持仓且有止损价的币；没有持仓的币旧单全部撤掉；
        # 有持仓但本次没有止损价的币（例如数据异常）保留原来的保护止损单
        renew = {r["inst"] for r in rows if r["inst"] in positions and r.get("stop")}
        try:
            old = ex.pending_stops()
            ex.cancel_algos([a for a in old if a["instId"] in renew or a["instId"] not in positions])
            kept = {a["instId"] for a in old if a["instId"] not in renew and a["instId"] in positions}
        except OKXError as e:
            errors.append(f"撤旧止损单失败：{e}")
            kept = set()
        for r in rows:
            if r["inst"] in positions and r["inst"] not in renew:
                r["protect"] = "保留原单" if r["inst"] in kept else None
                if r["inst"] not in kept and r["inst"] in {x["inst"] for x in rows if x["note"].startswith("数据异常")}:
                    errors.append(f"{r['coin']}：数据异常且没有保护止损单，请手动检查")
        buf = float(t["protective_buffer"])
        for r in rows:
            p = positions.get(r["inst"])
            if not p or r["inst"] not in renew:
                continue
            trig = r["stop"] * (1 - buf)
            if p.get("liqPx"):
                trig = max(trig, p["liqPx"] * 1.05)
            try:
                ex.place_stop(r["inst"], _fmt(p["sz"], r["lot"]), _px(trig, r["tick"]), ps)
                r["protect"] = trig
            except OKXError as e:
                errors.append(f"{r['coin']} 保护止损单：{e}")
        st["managed"] = sorted(r["inst"] for r in rows if r["inst"] in positions)
        eq2, av2 = ex.usdt_equity()
        notional = sum(positions[r["inst"]]["sz"] * (r["ct"] or 0) * (r["price"] or 0)
                       for r in rows if r["inst"] in positions)
        journal_equity(cfg, {"time": now, "data_date": day, "mode": mode, "equity": round(eq2, 4),
                             "avail": round(av2, 4), "positions": len(st["managed"]),
                             "notional": round(notional, 4), "upl": round(sum(p["upl"] for p in positions.values()), 4),
                             "equity_peak": round(st.get("equity_peak") or eq2, 4),
                             "drawdown": round(1 - eq2 / st["equity_peak"], 4) if st.get("equity_peak") else 0,
                             "capital_used": round(capital, 4)})

    journal_decision(cfg, {"time": now, "data_date": day, "mode": mode, "capital": capital, "equity": equity,
                           "halted": halted, "errors": errors,
                           "rows": [{k: r.get(k) for k in ("coin", "cur", "target", "action", "sz", "reason", "note",
                                                           "pos", "votes", "stopped", "stop", "protect", "price",
                                                           "notional", "lev")} for r in rows]})
    held = [r for r in rows if r["target"] > 0 and r.get("ct") and r.get("price")]
    lines += ["", "<b>目标持仓</b>"]
    for r in held:
        lines.append(f"{r['coin']} {_fmt(r['target'], r['lot'])} 张 ≈ {r['target'] * r['ct'] * r['price']:.1f}U"
                     + (f"（{r['lev']:g}x，止损 {r['stop']:.6g}）" if r.get("stop") and r.get("lev") else ""))
    skipped = [f"{r['coin']}：{r['note']}" for r in rows if not r["action"] and r["note"]]
    if skipped:
        lines += ["", "<b>跳过 / 保持</b>"] + skipped
    if errors:
        lines += ["", "<b>错误</b>"] + errors
    if not sells and not buys:
        lines.insert(1, "今日无需调仓。")
    text = head + "\n" + "\n".join(lines)
    st["last_run"] = now + " UTC"
    st["last_report"] = text
    if not dry:
        if not errors:
            st["data_date"] = day                     # 全部成功才标记完成；有错误时 monitor 会稍后重试
        save_state(st)
    log(text.replace("\n", " | "))
    if notify:
        notify(text)
    return text, not errors


def status(cfg):
    ex = client(cfg)
    if not ex.has_keys:
        return "未配置 OKX API key"
    eq, avail = ex.usdt_equity()
    acc = ex.config()
    pos = ex.positions()
    stops = {a["instId"]: a for a in ex.pending_stops()}
    lines = [f"账户：{'模拟盘' if ex.demo else '实盘'}  模式 acctLv={acc.get('acctLv')} posMode={acc.get('posMode')}",
             f"USDT 权益 {eq:.2f}，可用 {avail:.2f}", f"逐仓持仓 {len(pos)} 个："]
    for k, p in pos.items():
        s = stops.get(k)
        lines.append(f"  {k} {p['sz']} 张 均价 {p['avgPx']} 杠杆 {p['lever']} 浮盈 {p['upl']:.2f} 强平价 {p['liqPx']}"
                     + (f" 保护止损 {s.get('slTriggerPx')}" if s else " 无保护止损"))
    return "\n".join(lines)


def close_all(cfg):
    ex = client(cfg)
    st = load_state()
    pos = ex.positions()
    ps = "long" if ex.config().get("posMode") == "long_short_mode" else None
    ex.cancel_algos(ex.pending_stops())
    out = []
    for inst in st.get("managed", []):
        if inst in pos:
            ex.market(inst, "sell", str(pos[inst]["sz"]), reduce_only=True, pos_side=ps)
            out.append(inst)
    st["managed"] = []
    save_state(st)
    return "已平仓：" + (", ".join(out) if out else "无")


def main():
    import monitor, exchanges, signals
    ap = argparse.ArgumentParser(description="OKX 自动交易")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--once", action="store_true", help="计算信号并执行一次调仓")
    ap.add_argument("--live", action="store_true", help="忽略 dry_run 真实下单")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--close-all", action="store_true")
    args = ap.parse_args()
    cfg = monitor.load_config(args.config, required=args.config != monitor.DEFAULT_CONFIG)
    monitor.setup_logging(cfg)
    if args.status:
        print(status(cfg))
        return
    if args.close_all:
        print(close_all(cfg))
        return
    if args.once:
        if args.live and not cfg["trading"].get("enabled"):
            sys.exit("trading.enabled 为 false，拒绝真实下单。确认无误后在 config.yaml 打开它。")
        coins = monitor.load_coins()
        payload = signals.compute(cfg, coins, exchanges.resolve_sources(coins))
        text, _ = rebalance(payload, cfg, live=args.live)
        print(text.replace("<b>", "").replace("</b>", ""))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
