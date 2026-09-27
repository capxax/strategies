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
import argparse, base64, hashlib, hmac, json, os, sys, time
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
STATE_FILE = os.path.join(HERE, ".trader_state.json")
TAG = "tm"            # 保护止损单的 algoClOrdId 前缀，只撤 bot 自己挂的单


def log(*a):
    print(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "[trader]", *a, flush=True)


class OKXError(Exception):
    pass


class OKX:
    BASE = "https://www.okx.com"

    def __init__(self, key, secret, passphrase, demo=False):
        self.key, self.secret, self.passphrase, self.demo = key, secret, passphrase, demo
        self.s = requests.Session()

    @property
    def has_keys(self):
        return bool(self.key and self.secret and self.passphrase)

    def req(self, method, path, params=None, body=None, auth=True):
        if params:
            path = path + "?" + "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        data = json.dumps(body) if body is not None else ""
        headers = {"Content-Type": "application/json"}
        if auth:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
                f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
            sign = base64.b64encode(hmac.new(self.secret.encode(), (ts + method + path + data).encode(),
                                             hashlib.sha256).digest()).decode()
            headers.update({"OK-ACCESS-KEY": self.key, "OK-ACCESS-SIGN": sign,
                            "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": self.passphrase})
        if self.demo:
            headers["x-simulated-trading"] = "1"
        for i in range(3):
            try:
                r = self.s.request(method, self.BASE + path, data=data or None, headers=headers, timeout=15)
                j = r.json()
                break
            except (requests.RequestException, ValueError) as e:
                if i == 2:
                    raise OKXError(f"{method} {path} 网络错误：{e}")
                time.sleep(1 + i)
        if j.get("code") != "0":
            detail = ""
            if isinstance(j.get("data"), list) and j["data"] and isinstance(j["data"][0], dict):
                detail = j["data"][0].get("sMsg") or ""
            raise OKXError(f"{path} 失败：{j.get('code')} {j.get('msg')} {detail}".strip())
        return j["data"]

    # 公共
    def instruments(self):
        return {i["instId"]: i for i in self.req("GET", "/api/v5/public/instruments",
                                                 {"instType": "SWAP"}, auth=False)}

    def tickers(self):
        return {x["instId"]: float(x["last"]) for x in self.req("GET", "/api/v5/market/tickers",
                                                               {"instType": "SWAP"}, auth=False)}

    # 账户
    def config(self):
        return self.req("GET", "/api/v5/account/config")[0]

    def usdt_equity(self):
        d = self.req("GET", "/api/v5/account/balance", {"ccy": "USDT"})[0]
        for x in d.get("details", []):
            if x["ccy"] == "USDT":
                return float(x.get("eq") or 0), float(x.get("availEq") or x.get("availBal") or 0)
        return 0.0, 0.0

    def positions(self):
        out = {}
        for p in self.req("GET", "/api/v5/account/positions", {"instType": "SWAP"}):
            sz = float(p.get("pos") or 0)
            if sz and p.get("mgnMode") == "isolated":
                out[p["instId"]] = {"sz": sz, "avgPx": float(p.get("avgPx") or 0),
                                    "lever": p.get("lever"), "posSide": p.get("posSide"),
                                    "upl": float(p.get("upl") or 0), "liqPx": float(p.get("liqPx") or 0)}
        return out

    def set_leverage(self, inst, lever, pos_side=None):
        body = {"instId": inst, "lever": str(lever), "mgnMode": "isolated"}
        if pos_side:
            body["posSide"] = pos_side
        return self.req("POST", "/api/v5/account/set-leverage", body=body)

    def market(self, inst, side, sz, reduce_only=False, pos_side=None):
        body = {"instId": inst, "tdMode": "isolated", "side": side, "ordType": "market", "sz": sz}
        if pos_side:
            body["posSide"] = pos_side
        elif reduce_only:
            body["reduceOnly"] = True
        return self.req("POST", "/api/v5/trade/order", body=body)[0]

    def pending_stops(self):
        return [a for a in self.req("GET", "/api/v5/trade/orders-algo-pending",
                                    {"ordType": "conditional", "instType": "SWAP"})
                if (a.get("algoClOrdId") or "").startswith(TAG)]

    def cancel_algos(self, algos):
        if algos:
            self.req("POST", "/api/v5/trade/cancel-algos",
                     body=[{"algoId": a["algoId"], "instId": a["instId"]} for a in algos])

    def place_stop(self, inst, sz, trigger, pos_side=None):
        body = {"instId": inst, "tdMode": "isolated", "side": "sell", "ordType": "conditional", "sz": sz,
                "slTriggerPx": trigger, "slOrdPx": "-1", "slTriggerPxType": "last",
                "algoClOrdId": f"{TAG}{inst.split('-')[0]}{int(time.time())}"[:32]}
        if pos_side:
            body["posSide"] = pos_side
        else:
            body["reduceOnly"] = True
        return self.req("POST", "/api/v5/trade/order-algo", body=body)


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


# ---------------------------------------------------------------- 调仓

def plan(payload, cfg, ex, equity=None, positions=None):
    """计算目标张数和需要执行的订单（不下单）。"""
    t = cfg["trading"]
    ins, px = ex.instruments(), ex.tickers()
    capital = float(t["capital"])
    if equity is not None:
        capital = min(capital, equity)
    positions = positions or {}
    by = {c["coin"]: c for c in payload["coins"]}
    pf = {p["coin"]: p for p in payload["summary"]["portfolio"]["coins"]}
    coins = [str(c).upper() for c in (t.get("coins") or list(pf))]
    rows = []
    for coin in coins:
        inst = f"{coin}-USDT-SWAP"
        c, p, i = by.get(coin), pf.get(coin), ins.get(inst)
        cur = positions.get(inst, {}).get("sz", 0.0)
        row = {"coin": coin, "inst": inst, "cur": cur, "target": 0.0, "action": None, "note": ""}
        rows.append(row)
        if not i or i.get("state") != "live":
            row["note"] = "OKX 没有这个 USDT 永续"
            continue
        if not c or not p:
            row["note"] = "不在组合或暂无信号数据"
            if cur:
                row.update(action="sell", sz=cur, reduce=True, note=row["note"] + "，平掉已有仓位")
            continue
        ct, lot, mn, tick = float(i["ctVal"]), float(i["lotSz"]), float(i["minSz"]), float(i["tickSz"])
        last = px.get(inst) or c.get("live") or c["price"]
        notional = capital * p["exposure"]
        tgt = _floor(notional / (ct * last), lot) if notional > 0 else 0.0
        if notional > 0 and tgt < mn:
            row["note"] = f"目标 {notional:.1f}U 不够最小一张（{mn * ct * last:.1f}U），跳过"
            tgt = 0.0
        row.update(target=tgt, notional=notional, price=last, lev=c["lev"], stop=c.get("stop"),
                   stopped=c["stopped"], pos=c["pos"], lot=lot, tick=tick, ct=ct)
        diff = tgt - cur
        if tgt == 0 and cur > 0:
            row.update(action="sell", sz=cur, reduce=True,
                       note="止损离场" if c["stopped"] else ("信号离场" if c["pos"] == 0 else row["note"] or "目标为 0"))
        elif diff > 0:
            thr = max(float(t["min_trade_usdt"]), float(t["rebalance_threshold"]) * notional)
            if cur == 0 or diff * ct * last >= thr:
                row.update(action="buy", sz=_floor(diff, lot))
        elif diff < 0:
            thr = max(float(t["min_trade_usdt"]), float(t["rebalance_threshold"]) * notional)
            if -diff * ct * last >= thr:
                row.update(action="sell", sz=_floor(-diff, lot), reduce=True)
        if row["action"] and row.get("sz", 0) <= 0:
            row["action"] = None
    return capital, rows


def rebalance(payload, cfg, live=False, notify=None, force=True):
    """执行一次调仓，返回报告文本。live=False 时按 dry_run 配置决定是否只模拟。
    force=False 时，同一根日线只执行一次（monitor 自动调用时使用，防止重启后重复下单）。"""
    t = cfg["trading"]
    dry = (not live) and bool(t.get("dry_run", True))
    ex = client(cfg)
    st = load_state()
    day = payload["summary"]["data_date"]
    if not force and not dry and st.get("data_date") == day:
        return None
    mode = "模拟盘" if ex.demo else "实盘"
    head = f"🤖 <b>自动交易 {'（dry-run，未下单）' if dry else mode}</b>"
    equity = positions = None
    pos_mode = "net_mode"
    if ex.has_keys:
        acc = ex.config()
        pos_mode = acc.get("posMode", "net_mode")
        if acc.get("acctLv") == "1":
            raise OKXError("OKX 账户模式为“简单模式”，不能交易永续。请在 OKX 设置里切换到“单币种保证金”或以上模式。")
        equity, _ = ex.usdt_equity()
        positions = ex.positions()
    elif not dry:
        raise OKXError("config.yaml 里 trading.okx 的 api_key / api_secret / passphrase 没有填写")
    ps = "long" if pos_mode == "long_short_mode" else None
    capital, rows = plan(payload, cfg, ex, equity, positions)
    lines = [f"本金 {capital:.2f}U" + (f"（账户权益 {equity:.2f}U）" if equity is not None else "（未连接账户，按配置本金计算）")]
    errors = []
    # 先减仓再加仓，释放保证金
    for row in sorted([r for r in rows if r["action"]], key=lambda r: r["action"] != "sell"):
        sz = _fmt(row["sz"], row.get("lot", 0.01))
        desc = f"{'🔻 卖出' if row['action'] == 'sell' else '🟢 买入'} {row['coin']} {sz} 张" \
               + (f" ≈ {row['sz'] * row['ct'] * row['price']:.1f}U" if row.get("ct") else "") \
               + (f"（{row['note']}）" if row["note"] else "")
        if dry:
            lines.append(desc)
            continue
        try:
            if row["action"] == "buy":
                lev = row["lev"]
                try:
                    ex.set_leverage(row["inst"], lev, ps)
                except OKXError:
                    lev = max(1, int(lev))            # 不支持小数杠杆时向下取整，保证金更充足
                    ex.set_leverage(row["inst"], lev, ps)
                ex.market(row["inst"], "buy", sz, pos_side=ps)
            else:
                ex.market(row["inst"], "sell", sz, reduce_only=True, pos_side=ps)
            lines.append(desc + " ✅")
        except OKXError as e:
            errors.append(f"{row['coin']}：{e}")
            lines.append(desc + " ❌")
    skipped = [f"{r['coin']}：{r['note']}" for r in rows if not r["action"] and r["note"]]
    # 保护止损单
    if not dry and ex.has_keys:
        time.sleep(1)
        positions = ex.positions()
        try:
            old = ex.pending_stops()
            ex.cancel_algos(old)
        except OKXError as e:
            errors.append(f"撤旧止损单失败：{e}")
        buf = float(t["protective_buffer"])
        for r in rows:
            p = positions.get(r["inst"])
            if not p or not r.get("stop"):
                continue
            trig = r["stop"] * (1 - buf)
            if p.get("liqPx"):
                trig = max(trig, p["liqPx"] * 1.05)
            try:
                ex.place_stop(r["inst"], _fmt(p["sz"], r["lot"]), _px(trig, r["tick"]), ps)
            except OKXError as e:
                errors.append(f"{r['coin']} 保护止损单：{e}")
        st["managed"] = sorted(set(st.get("managed", [])) | set(positions))
    held = [r for r in rows if (r["target"] or r["cur"])]
    lines.append("")
    lines.append("<b>目标持仓</b>")
    for r in held:
        if r.get("ct"):
            lines.append(f"{r['coin']} {_fmt(r['target'], r['lot'])} 张 ≈ {r['target'] * r['ct'] * r['price']:.1f}U"
                         f"（{r['lev']:g}x，止损 {r['stop']:.6g}）" if r.get("stop") else
                         f"{r['coin']} {_fmt(r['target'], r['lot'])} 张")
    if skipped:
        lines += ["", "<b>跳过</b>"] + skipped
    if errors:
        lines += ["", "<b>错误</b>"] + errors
    if not any(r["action"] for r in rows):
        lines.insert(1, "今日无需调仓。")
    text = head + "\n" + "\n".join(lines)
    st["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    st["last_report"] = text
    if not dry:
        st["data_date"] = day
        save_state(st)
    log(text.replace("\n", " | "))
    if notify:
        notify(text)
    return text


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
    cfg = monitor.load_config(args.config)
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
        print(rebalance(payload, cfg, live=args.live).replace("<b>", "").replace("</b>", ""))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
