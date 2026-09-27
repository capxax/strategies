"""实时监控服务：网页仪表盘 + Telegram 通知。

  python dashboard/monitor.py                 # 默认 http://127.0.0.1:8765
  python dashboard/monitor.py --port 8080 --host 0.0.0.0

配置（环境变量，或 dashboard/.env 文件，格式 KEY=VALUE）：
  TG_BOT_TOKEN     Telegram 机器人 token（找 @BotFather 创建）
  TG_CHAT_ID       接收通知的 chat id（私聊填自己的 id，群组填群 id，多个用逗号分隔）
  DASH_TOKEN       可选。设置后网页需要 ?token=xxx 才能访问（部署到公网时务必设置）
  PRICE_INTERVAL   实时价格刷新间隔秒数，默认 30
  NEAR_STOP        距止损多少以内发预警，默认 0.05（5%）
  DAILY_AT         日线重算时间（UTC，HH:MM），默认 00:05

做了什么：
  - 启动时和每天 DAILY_AT 计算日线信号（与 BOT_SPEC 一致，数据为已收盘 K 线）
  - 每 PRICE_INTERVAL 秒批量拉取币安 / OKX / Hyperliquid 最新价，计算盘中状态
  - Telegram 通知（同一事件每天只发一次）：
      日线：仓位变化（加仓/减仓/入场/离场）、止损触发、每日汇总
      盘中：跌破止损价、接近止损、空仓币突破入场位、持仓币跌破离场位（盘中预警，以收盘确认为准）
      系统：数据拉取失败
  - Telegram 命令：/status 汇总  /pos 当前持仓  /c BTC 查看单个币  /alerts 今日盘中预警  /help
"""
import argparse, json, os, sys, threading, time, traceback
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import build  # noqa: E402

STATE_FILE = os.path.join(HERE, ".monitor_state.json")


def load_env():
    path = os.path.join(HERE, ".env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def now_utc():
    return datetime.now(timezone.utc)


def log(*a):
    print(now_utc().strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def fp(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if v >= 1000 else f"{v:.2f}" if v >= 100 else f"{v:.4g}"


def pc(v):
    return "—" if v is None else f"{v*100:+.1f}%"


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, chat_ids):
        self.token = token
        self.chats = [c.strip() for c in (chat_ids or "").split(",") if c.strip()]
        self.enabled = bool(token and self.chats)
        self.offset = None

    def send(self, text, chat=None):
        if not self.enabled:
            log("[TG 未配置]", text.replace("\n", " | ")[:300])
            return
        for cid in ([chat] if chat else self.chats):
            for part in [text[i:i + 3900] for i in range(0, len(text), 3900)]:
                try:
                    r = requests.post(f"https://api.telegram.org/bot{self.token}/sendMessage", timeout=15,
                                      json={"chat_id": cid, "text": part, "parse_mode": "HTML",
                                            "disable_web_page_preview": True})
                    if r.status_code != 200:
                        log("TG 发送失败", r.status_code, r.text[:200])
                except requests.RequestException as e:
                    log("TG 发送异常", e)

    def poll(self, handler):
        """长轮询接收命令，只响应配置里的 chat。"""
        if not self.enabled:
            return
        while True:
            try:
                r = requests.get(f"https://api.telegram.org/bot{self.token}/getUpdates", timeout=40,
                                 params={"timeout": 30, "offset": self.offset})
                for u in r.json().get("result", []):
                    self.offset = u["update_id"] + 1
                    msg = u.get("message") or u.get("channel_post") or {}
                    cid = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if cid in self.chats and text.startswith("/"):
                        try:
                            self.send(handler(text), chat=cid)
                        except Exception as e:
                            self.send(f"命令出错：{e}", chat=cid)
            except Exception as e:
                log("TG 轮询异常", e)
                time.sleep(5)


# ---------------------------------------------------------------- 实时价格

def fetch_prices():
    """每个交易所一次请求拿全部最新价。返回 {(src, sym): price}。"""
    out, errs = {}, []
    try:
        r = requests.get("https://data-api.binance.vision/api/v3/ticker/price", timeout=15)
        for x in r.json():
            out[("binance", x["symbol"])] = float(x["price"])
    except Exception as e:
        errs.append(f"币安: {e}")
    try:
        r = requests.get("https://www.okx.com/api/v5/market/tickers", params={"instType": "SWAP"}, timeout=15)
        for x in r.json().get("data", []):
            out[("okx", x["instId"])] = float(x["last"])
    except Exception as e:
        errs.append(f"OKX: {e}")
    try:
        r = requests.post("https://api.hyperliquid.xyz/info", json={"type": "allMids"}, timeout=15)
        for k, v in r.json().items():
            out[("hyperliquid", k)] = float(v)
    except Exception as e:
        errs.append(f"Hyperliquid: {e}")
    return out, errs


# ---------------------------------------------------------------- 监控核心

class Monitor:
    def __init__(self, tg, cfg):
        self.tg, self.cfg = tg, cfg
        self.lock = threading.Lock()
        self.payload = None
        self.last_prices = None
        self.last_compute = None
        self.errors = []
        self.state = self._load_state()

    # 状态持久化：上一次的仓位、已发送的通知键
    def _load_state(self):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            return {"positions": {}, "stopped": {}, "sent": {}, "data_date": None}

    def _save_state(self):
        try:
            json.dump(self.state, open(STATE_FILE, "w"))
        except Exception as e:
            log("保存状态失败", e)

    def _once(self, key, text):
        """同一 key 每个 UTC 日只发一次。"""
        day = now_utc().strftime("%Y-%m-%d")
        sent = self.state["sent"].setdefault(day, [])
        if key in sent:
            return
        sent.append(key)
        for d in list(self.state["sent"]):
            if d < (now_utc() - timedelta(days=3)).strftime("%Y-%m-%d"):
                del self.state["sent"][d]
        self._save_state()
        self.tg.send(text)

    # ---- 日线
    def recompute(self):
        log("开始计算日线信号…")
        t0 = time.time()
        payload = build.compute()
        with self.lock:
            prev = self.payload
            self.payload = payload
            self.last_compute = now_utc()
        log(f"日线信号完成：{len(payload['coins'])} 个币，用时 {time.time()-t0:.0f}s，数据截至 {payload['summary']['data_date']}")
        if payload["summary"]["errors"]:
            bad = ", ".join(e["coin"] for e in payload["summary"]["errors"][:10])
            self._once(f"err-daily-{bad}", f"⚠️ <b>日线数据异常</b>\n以下币种未能计算：{bad}")
        self._daily_events(payload)
        if self.last_prices:
            self.apply_prices(self.last_prices)

    def _daily_events(self, p):
        s, coins = p["summary"], p["coins"]
        if self.state.get("data_date") == s["data_date"]:
            return  # 这根日线已经通知过（例如服务重启）
        first_run = not self.state.get("positions")
        lines_up, lines_down, lines_stop = [], [], []
        for c in coins:
            old = self.state["positions"].get(c["coin"])
            new = 0.0 if c["stopped"] else c["pos"]
            was_stopped = self.state["stopped"].get(c["coin"], False)
            tag = f"{c['coin']}（{c['grade']}级 {c['lev']:g}x）" if c["lev"] else f"{c['coin']}（C级 观察）"
            if c["stopped"] and not was_stopped:
                lines_stop.append(f"🛑 {tag} 收盘 {fp(c['price'])} 跌破止损 {fp(c['stop'])}，应清仓")
            elif old is not None and new > old:
                verb = "入场" if old == 0 else "加仓"
                lines_up.append(f"🟢 {tag} {verb} {old:.0%} → {new:.0%}　收盘 {fp(c['price'])}")
            elif old is not None and new < old:
                verb = "离场" if new == 0 else "减仓"
                lines_down.append(f"🔻 {tag} {verb} {old:.0%} → {new:.0%}　收盘 {fp(c['price'])}")
            self.state["positions"][c["coin"]] = new
            self.state["stopped"][c["coin"]] = c["stopped"]
        self.state["data_date"] = s["data_date"]
        self._save_state()
        if first_run:
            self.tg.send(f"✅ <b>监控已启动</b>\n数据截至 {s['data_date']} 收盘，共 {s['n']} 个币，"
                         f"其中 {s['long']} 个持仓。之后每天收盘后推送仓位变化。")
            return
        head = f"📊 <b>{s['data_date']} 日线收盘</b>\nBTC {fp(s['btc_price'])}，" \
               f"BTC 目标仓位 {s['btc_pos']:.0%}\n持仓币 {s['long']}/{s['n']}，建议总敞口 {s['exposure']:.0%}"
        body = []
        if lines_stop:
            body += ["", "<b>止损</b>"] + lines_stop
        if lines_up:
            body += ["", "<b>加仓 / 入场</b>"] + lines_up
        if lines_down:
            body += ["", "<b>减仓 / 离场</b>"] + lines_down
        if not body:
            body = ["", "今日无仓位变化。"]
        self.tg.send(head + "\n" + "\n".join(body))

    # ---- 盘中
    def apply_prices(self, prices):
        with self.lock:
            p = self.payload
            if not p:
                return
            for c in p["coins"]:
                live = prices.get((c["src"], c["sym"]))
                c["live"] = live
                flags = []
                if live and c["stopped"]:
                    c["live_chg"] = live / c["price"] - 1
                    c["live_stop_dist"] = None           # 已止损离场，等信号重新入场，不再发盘中预警
                elif live:
                    c["live_chg"] = live / c["price"] - 1
                    c["live_stop_dist"] = (c["stop"] / live - 1) if c["stop"] else None
                    held = c["pos"] > 0 and not c["stopped"]
                    lv = c["levels"]
                    if held and c["stop"] and live < c["stop"]:
                        flags.append(("stop_break", f"盘中跌破止损 {fp(c['stop'])}"))
                    elif held and c["stop"] and live < c["stop"] * (1 + self.cfg["near_stop"]):
                        flags.append(("near_stop", f"距止损 {pc(c['stop']/live-1)}"))
                    if c["pos"] < 1 and not c["votes"][1] and lv["kc_upper"] and live > lv["kc_upper"]:
                        flags.append(("entry_watch", f"突破肯特纳上轨 {fp(lv['kc_upper'])}，收盘站稳将加仓"))
                    if c["votes"][1] and lv["kc_mid"] and live < lv["kc_mid"]:
                        flags.append(("exit_watch", f"跌破肯特纳中线 {fp(lv['kc_mid'])}，收盘不收回将减仓"))
                    if c["votes"][2] and lv["pbx_slow"] and live < lv["pbx_slow"]:
                        flags.append(("exit_watch", f"跌破 PBX 慢线 {fp(lv['pbx_slow'])}"))
                c["live_flags"] = [{"k": k, "t": t} for k, t in flags]
            self.last_prices = prices
            p["summary"]["live_at"] = now_utc().strftime("%Y-%m-%d %H:%M:%S UTC")
            coins = list(p["coins"])
        for c in coins:
            for f in c["live_flags"]:
                icon = {"stop_break": "🛑", "near_stop": "⚠️", "entry_watch": "📈", "exit_watch": "📉"}[f["k"]]
                self._once(f"{f['k']}-{c['coin']}", f"{icon} <b>{c['coin']}</b> {fp(c['live'])}（盘中 {pc(c.get('live_chg'))}）\n{f['t']}"
                           + ("\n以收盘价确认为准。" if f["k"] in ("entry_watch", "exit_watch") else ""))

    def price_loop(self):
        fails = 0
        while True:
            try:
                prices, errs = fetch_prices()
                if prices:
                    self.apply_prices(prices)
                self.errors = errs
                fails = fails + 1 if errs else 0
                if fails == 10:
                    self._once("err-prices", "⚠️ <b>实时价格拉取连续失败</b>\n" + "\n".join(errs))
            except Exception:
                log("价格循环异常", traceback.format_exc())
            time.sleep(self.cfg["price_interval"])

    def daily_loop(self):
        hh, mm = map(int, self.cfg["daily_at"].split(":"))
        while True:
            try:
                n = now_utc()
                target = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
                expected = (n - timedelta(days=1 if n >= target else 2)).strftime("%Y-%m-%d")
                have = self.payload["summary"]["data_date"] if self.payload else None
                if have is None or have < expected:
                    self.recompute()
            except Exception:
                log("日线计算异常", traceback.format_exc())
                self._once("err-compute", "⚠️ <b>日线计算失败</b>，5 分钟后重试。详见服务日志。")
                time.sleep(300)
                continue
            time.sleep(60)

    # ---- 查询
    def snapshot(self):
        with self.lock:
            if not self.payload:
                return None
            p = json.loads(json.dumps(self.payload))
        p["summary"]["server"] = {
            "tg": self.tg.enabled, "errors": self.errors,
            "last_compute": self.last_compute.strftime("%Y-%m-%d %H:%M UTC") if self.last_compute else None,
            "interval": self.cfg["price_interval"],
        }
        return p

    def command(self, text):
        p = self.snapshot()
        if not p:
            return "数据还在计算中，请稍后再试。"
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        s, coins = p["summary"], p["coins"]
        if cmd in ("/start", "/help"):
            return ("/status 汇总\n/pos 当前持仓与止损\n/c BTC 查看单个币\n/alerts 盘中预警\n"
                    "仓位信号基于日线收盘，每天 UTC 00:05 更新；盘中价格每 "
                    f"{self.cfg['price_interval']} 秒刷新。")
        if cmd == "/status":
            return (f"📊 数据截至 {s['data_date']} 收盘，实时价 {s.get('live_at', '—')}\n"
                    f"BTC {fp(s['btc_price'])}，目标仓位 {s['btc_pos']:.0%}\n"
                    f"持仓币 {s['long']}/{s['n']}（满仓 {s['full']}），建议总敞口 {s['exposure']:.0%}\n"
                    f"盘中预警 {sum(bool(c.get('live_flags')) for c in coins)} 个")
        if cmd == "/pos":
            held = sorted([c for c in coins if c["pos"] > 0 and c["lev"] and not c["stopped"]], key=lambda c: -c["exposure"])
            lines = [f"{c['coin']:<6} {c['pos']:.0%} {c['lev']:g}x 敞口{c['exposure']:.1%} 现价{fp(c.get('live') or c['price'])} "
                     f"止损{fp(c['stop'])}({pc(c.get('live_stop_dist'))})" for c in held]
            return f"<b>持仓 {len(held)} 个</b>\n<pre>" + "\n".join(lines) + "</pre>"
        if cmd == "/alerts":
            lines = [f"{c['coin']}: " + "；".join(f["t"] for f in c["live_flags"]) for c in coins if c.get("live_flags")]
            return "<b>盘中预警</b>\n" + ("\n".join(lines) if lines else "暂无")
        if cmd == "/c" and args:
            c = next((x for x in coins if x["coin"] == args[0].upper()), None)
            if not c:
                return f"没有 {args[0].upper()}（只监控 A/B/C 级币种）"
            v = "".join("✅" if x else "▫️" for x in c["votes"])
            b, t, lv = c["btc"], c["bt"], c["levels"]
            return (f"<b>{c['coin']}</b> {c['grade']}级 {('%gx 逐仓' % c['lev']) if c['lev'] else '不交易'}\n"
                    f"现价 {fp(c.get('live') or c['price'])}（盘中 {pc(c.get('live_chg'))}），收盘 {fp(c['price'])}\n"
                    f"信号 S1–S4 {v} 目标仓位 {c['pos']:.0%}{'（已止损）' if c['stopped'] else ''}\n"
                    f"持仓 {c['days_in']} 天，止损 {fp(c['stop'])}（{pc(c.get('live_stop_dist'))}）\n"
                    f"肯特纳 上轨 {fp(lv['kc_upper'])} / 中线 {fp(lv['kc_mid'])}，PBX 慢线 {fp(lv['pbx_slow'])}\n"
                    f"BTC 相关 {b['corr365']:.2f}，β {b['beta']:.2f}\n"
                    f"回测 年化 {t['L1_cagr']:.0%} 回撤 {t['L1_max_dd']:.0%}，近3年 {t['r3_cagr']:.0%}"
                    + ("\n盘中：" + "；".join(f["t"] for f in c["live_flags"]) if c.get("live_flags") else ""))
        return "未知命令，发送 /help 查看。"


# ---------------------------------------------------------------- 网页

def make_handler(mon, token):
    page = build.wrap(build.render(None)).encode("utf-8")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if token and parse_qs(u.query).get("token", [""])[0] != token:
                return self._send(403, "需要正确的 ?token= 参数".encode(), "text/plain; charset=utf-8")
            if u.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path == "/api/data":
                snap = mon.snapshot()
                if not snap:
                    return self._send(503, b'{"loading":true}', "application/json")
                return self._send(200, json.dumps(snap, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            if u.path == "/health":
                return self._send(200, b"ok", "text/plain")
            self._send(404, b"not found", "text/plain")
    return H


def main():
    load_env()
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--test-telegram", action="store_true", help="发一条测试消息后退出")
    args = ap.parse_args()
    cfg = {"price_interval": int(os.environ.get("PRICE_INTERVAL", 30)),
           "near_stop": float(os.environ.get("NEAR_STOP", 0.05)),
           "daily_at": os.environ.get("DAILY_AT", "00:05")}
    tg = Telegram(os.environ.get("TG_BOT_TOKEN"), os.environ.get("TG_CHAT_ID"))
    if args.test_telegram:
        tg.send("✅ 趋势信号看板：Telegram 通知测试成功")
        return
    if not tg.enabled:
        log("未配置 TG_BOT_TOKEN / TG_CHAT_ID，通知只打印到日志")
    mon = Monitor(tg, cfg)
    threading.Thread(target=mon.daily_loop, daemon=True).start()
    threading.Thread(target=mon.price_loop, daemon=True).start()
    threading.Thread(target=tg.poll, args=(mon.command,), daemon=True).start()
    token = os.environ.get("DASH_TOKEN")
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(mon, token))
    log(f"仪表盘：http://{args.host}:{args.port}/" + (f"?token={token}" if token else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
