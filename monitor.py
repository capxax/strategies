"""趋势信号监控：实时网页仪表盘 + Telegram 通知。

  python monitor.py                         # 读取 config.yaml，打开 http://127.0.0.1:8765/
  python monitor.py --config my.yaml
  python monitor.py --test-telegram         # 发一条测试消息后退出
  python monitor.py --snapshot out.html     # 计算一次，生成静态页面后退出

  - 启动时和每天 monitor.daily_at（UTC）重算日线信号
  - 每 monitor.price_interval 秒批量拉取实时价格，计算盘中预警
  - Telegram 推送（同一事件每个 UTC 日只推一次）：日报、止损、盘中预警、系统异常
  - Telegram 命令：/status /pos /c BTC /alerts /help
"""
import argparse, copy, json, logging, os, sys, threading, time, traceback
from datetime import datetime, timezone, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import requests
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import exchanges  # noqa: E402
import signals  # noqa: E402
import trader  # noqa: E402

STATE_FILE = os.path.join(HERE, ".monitor_state.json")
DEFAULT_CONFIG = os.path.join(HERE, "config.yaml")
COINS_FILE = os.path.join(HERE, "coins.yaml")


# ---------------------------------------------------------------- 配置

def _merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v
    return base


# 内置默认配置（与 config.example.yaml 一致）。用户配置里没写的项使用这里的值。
DEFAULTS = {'telegram': {'bot_token': '', 'chat_ids': []},
 'server': {'host': '127.0.0.1', 'port': 8765, 'access_token': ''},
 'monitor': {'price_interval': 30,
             'daily_at': '00:05',
             'near_stop': 0.05,
             'alerts': {'stop_break': True, 'near_stop': True, 'entry_watch': True, 'exit_watch': True},
             'daily_report': True},
 'dashboard': {'history_days': 420, 'min_history': 300, 'chart_days': 180, 'refresh_seconds': 15},
 'universe': {'auto_update': True, 'update_days': 90, 'top': 40, 'core': 12, 'min_days': 365},
 'portfolio': {'capital': 100, 'first_batch': 0.5, 'gap_buffer': 0.1, 'coins': 'auto'},
 'trading': {'enabled': False,
             'dry_run': True,
             'capital': 100,
             'coins': [],
             'rebalance_threshold': 0.2,
             'min_trade_usdt': 5,
             'protective_buffer': 0.1,
             'max_drawdown': 0.35,
             'retry_minutes': 10,
             'log_dir': 'logs',
             'okx': {'api_key': '', 'api_secret': '', 'passphrase': '', 'demo': False}},
 'strategy': {'s1_fast': 12,
              's1_slow': 26,
              's2_n': 20,
              's2_k': 1.5,
              's2_atr': 10,
              's3': [4, 6, 12],
              's4_n': 20},
 'risk': {'stop_drawdown': 0.25,
          'weight_cap': 0.25,
          'b_grade_weight': 0.5,
          'strong_follow_corr': 0.7,
          'strong_follow_cap': 0.6,
          'total_exposure_cap': 1.5},
 'leverage': {'default': 1, 'BTC': 2, 'ETH': 1.5, 'BNB': 1.5, 'HYPE': 1.5}}


def load_config(path, required=False):
    """内置默认值 + 用户配置文件。path 为相对路径时，先找当前目录，再找程序目录。"""
    cfg = copy.deepcopy(DEFAULTS)
    cands = [path] if os.path.isabs(path) else list(dict.fromkeys([os.path.abspath(path), os.path.join(HERE, path)]))
    found = next((p for p in cands if os.path.exists(p)), None)
    if found:
        try:
            _merge(cfg, yaml.safe_load(open(found, encoding="utf-8")) or {})
        except yaml.YAMLError as e:
            sys.exit(f"配置文件 {found} 格式错误：{e}")
        log(f"使用配置文件 {found}")
    elif required:
        sys.exit(f"找不到配置文件 {path}（查找过：{'、'.join(cands)}）")
    else:
        log(f"没有找到 {path}，使用内置默认配置")
    return cfg


def load_coins():
    coins = yaml.safe_load(open(COINS_FILE, encoding="utf-8")) or {}
    coins = {k.upper(): v for k, v in coins.items() if (v or {}).get("grade") in ("A", "B", "C")}
    if "BTC" not in coins:
        raise ValueError("coins.yaml 必须包含 BTC（用于计算 BTC 联动和市场状态）")
    return coins


# ---------------------------------------------------------------- 工具

def now_utc():
    return datetime.now(timezone.utc)


LOGGER = logging.getLogger("trend")


def setup_logging(cfg):
    """日志同时输出到终端和 logs/monitor.log（5MB 轮转，保留 5 个）。"""
    from logging.handlers import RotatingFileHandler
    d = cfg["trading"].get("log_dir") or "logs"
    d = d if os.path.isabs(d) else os.path.join(HERE, d)
    os.makedirs(d, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fmt.converter = time.gmtime                       # 日志时间统一用 UTC
    LOGGER.handlers.clear()
    for h in (logging.StreamHandler(sys.stdout),
              RotatingFileHandler(os.path.join(d, "monitor.log"), maxBytes=5_000_000, backupCount=5, encoding="utf-8")):
        h.setFormatter(fmt)
        LOGGER.addHandler(h)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False


def log(*a):
    msg = " ".join(str(x) for x in a)
    if LOGGER.handlers:
        LOGGER.info(msg)
    else:
        print(now_utc().strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def fp(v):
    if v is None:
        return "—"
    return f"{v:,.0f}" if v >= 1000 else f"{v:.2f}" if v >= 100 else f"{v:.4g}"


def pc(v):
    return "—" if v is None else f"{v*100:+.1f}%"


def render_page(payload=None, refresh=15):
    """把数据嵌入页面模板。payload 为 None 时页面从 /api/data 拉取并定时刷新。"""
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    data_js = "null" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    html = tpl.replace("/*__DATA__*/null", data_js).replace("/*__REFRESH__*/15000", str(int(refresh * 1000)))
    return ('<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
            '</head>\n<body style="margin:0">\n' + html + '\n</body>\n</html>\n')


# ---------------------------------------------------------------- Telegram

class Telegram:
    def __init__(self, token, chat_ids):
        self.token = (token or "").strip()
        self.chats = [str(c).strip() for c in (chat_ids or []) if str(c).strip()]
        self.enabled = bool(self.token and self.chats)
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


# ---------------------------------------------------------------- 监控核心

FLAG_ICON = {"stop_break": "🛑", "near_stop": "⚠️", "entry_watch": "📈", "exit_watch": "📉"}


class Monitor:
    def __init__(self, tg, cfg):
        self.tg, self.cfg = tg, cfg
        self.lock = threading.Lock()
        self.payload = None
        self.last_prices = None
        self.last_compute = None
        self.errors = []
        self.trade_attempts = {}
        self.state = self._load_state()

    def _load_state(self):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            return {"positions": {}, "stopped": {}, "sent": {}, "data_date": None}

    def _save_state(self):
        if getattr(self, "readonly", False):
            return
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
        old = (now_utc() - timedelta(days=3)).strftime("%Y-%m-%d")
        for d in [d for d in self.state["sent"] if d < old]:
            del self.state["sent"][d]
        self._save_state()
        self.tg.send(text)

    # ---- 日线
    def recompute(self):
        log("开始计算日线信号…")
        t0 = time.time()
        coins = load_coins()                       # 每次重新读取，编辑 coins.yaml 后次日自动生效
        sources = exchanges.resolve_sources(coins)
        payload = signals.compute(self.cfg, coins, sources)
        with self.lock:
            self.payload = payload
            self.last_compute = now_utc()
        s = payload["summary"]
        srcs = {}
        for c in payload["coins"]:
            srcs[c["src"]] = srcs.get(c["src"], 0) + 1
        log(f"日线信号完成：{s['n']} 个币（" + "、".join(f"{k} {v}" for k, v in srcs.items()) +
            f"），用时 {time.time()-t0:.0f}s，数据截至 {s['data_date']}")
        if s["errors"]:
            bad = "\n".join(f"{e['coin']}：{e['error']}" for e in s["errors"][:15])
            log("以下币种未能计算：", bad.replace("\n", "；"))
            self._once("err-daily-" + ",".join(e["coin"] for e in s["errors"]),
                       f"⚠️ <b>部分币种数据异常</b>\n{bad}")
        self._daily_events(payload)
        if self.last_prices:
            self.apply_prices(self.last_prices)
        self.maybe_trade()

    def _daily_events(self, p):
        s, coins = p["summary"], p["coins"]
        if self.state.get("data_date") == s["data_date"]:
            return                                 # 这根日线已经通知过（例如服务重启）
        first_run = not self.state.get("positions")
        up, down, stop = [], [], []
        for c in coins:
            old = self.state["positions"].get(c["coin"])
            new = 0.0 if c["stopped"] else c["pos"]
            tag = f"{c['coin']}（{c['grade']}级 {c['lev']:g}x）" if c["lev"] else f"{c['coin']}（C级 观察）"
            if c["stopped"] and not self.state["stopped"].get(c["coin"], False):
                stop.append(f"🛑 {tag} 收盘 {fp(c['price'])} 跌破止损 {fp(c['stop'])}，应清仓")
            elif old is not None and new > old:
                up.append(f"🟢 {tag} {'入场' if old == 0 else '加仓'} {old:.0%} → {new:.0%}　收盘 {fp(c['price'])}")
            elif old is not None and new < old:
                down.append(f"🔻 {tag} {'离场' if new == 0 else '减仓'} {old:.0%} → {new:.0%}　收盘 {fp(c['price'])}")
            self.state["positions"][c["coin"]] = new
            self.state["stopped"][c["coin"]] = c["stopped"]
        self.state["data_date"] = s["data_date"]
        self._save_state()
        if first_run:
            self.tg.send(f"✅ <b>监控已启动</b>\n数据截至 {s['data_date']} 收盘，共 {s['n']} 个币，"
                         f"其中 {s['long']} 个持仓。之后每天收盘后推送仓位变化。")
            return
        if not self.cfg["monitor"]["daily_report"] and not (up or down or stop):
            return
        head = (f"📊 <b>{s['data_date']} 日线收盘</b>\nBTC {fp(s['btc_price'])}，BTC 目标仓位 {s['btc_pos']:.0%}\n"
                f"持仓币 {s['long']}/{s['n']}，建议总敞口 {s['exposure']:.0%}")
        body = []
        for title, lines in (("止损", stop), ("加仓 / 入场", up), ("减仓 / 离场", down)):
            if lines:
                body += ["", f"<b>{title}</b>"] + lines
        self.tg.send(head + "\n" + "\n".join(body or ["", "今日无仓位变化。"]))

    # ---- 盘中
    def apply_prices(self, prices):
        mcfg = self.cfg["monitor"]
        with self.lock:
            self.last_prices = prices                # 日线还没算完时先存下，算完后立即套用
            p = self.payload
            if not p:
                return
            for c in p["coins"]:
                live = prices.get((c["src"], c["sym"]))
                c["live"] = live
                flags = []
                if live:
                    c["live_chg"] = live / c["price"] - 1
                    c["live_stop_dist"] = None if c["stopped"] or not c["stop"] else c["stop"] / live - 1
                if live and not c["stopped"]:              # 已止损离场的币等信号重新入场，不再预警
                    held, lv = c["pos"] > 0, c["levels"]
                    if held and c["stop"] and live < c["stop"]:
                        flags.append(("stop_break", f"盘中跌破止损 {fp(c['stop'])}"))
                    elif held and c["stop"] and live < c["stop"] * (1 + mcfg["near_stop"]):
                        flags.append(("near_stop", f"距止损 {pc(c['stop'] / live - 1)}"))
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
                if not mcfg["alerts"].get(f["k"], True):
                    continue
                self._once(f"{f['k']}-{c['coin']}",
                           f"{FLAG_ICON[f['k']]} <b>{c['coin']}</b> {fp(c['live'])}（盘中 {pc(c.get('live_chg'))}）\n{f['t']}"
                           + ("\n以收盘价确认为准。" if f["k"] in ("entry_watch", "exit_watch") else ""))

    def price_loop(self):
        fails = 0
        while True:
            try:
                prices, errs = exchanges.prices()
                if prices:
                    self.apply_prices(prices)
                self.errors = errs
                fails = fails + 1 if errs else 0
                if fails == 10:
                    self._once("err-prices", "⚠️ <b>实时价格拉取连续失败</b>\n" + "\n".join(errs))
            except Exception:
                log("价格循环异常", traceback.format_exc())
            time.sleep(self.cfg["monitor"]["price_interval"])

    def maybe_trade(self):
        """每根日线调仓一次；有失败时每 retry_minutes 分钟重试，最多 3 次。"""
        t = self.cfg["trading"]
        if not t.get("enabled") or getattr(self, "readonly", False) or not self.payload:
            return
        day = self.payload["summary"]["data_date"]
        if trader.load_state().get("data_date") == day and not t.get("dry_run", True):
            return
        att = self.trade_attempts.setdefault(day, {"n": 0, "last": 0.0})
        if att["n"] >= (1 if t.get("dry_run", True) else 4):
            return
        if att["n"] and time.time() - att["last"] < float(t["retry_minutes"]) * 60:
            return
        att["n"] += 1
        att["last"] = time.time()
        try:
            text, ok = trader.rebalance(self.payload, self.cfg, notify=self.tg.send, force=False)
            if not ok and att["n"] >= 4:
                self.tg.send(f"❌ <b>自动交易重试 3 次仍有失败</b>，今天不再重试，请检查。可手动运行 trader.py --once --live。")
        except Exception as e:
            log("自动交易失败", traceback.format_exc())
            self.tg.send(f"❌ <b>自动交易失败</b>（第 {att['n']} 次）\n{e}")

    def maybe_update_universe(self):
        """coins.yaml 超过 universe.update_days 天就按流动性重建，并推送名单变化。"""
        u = self.cfg["universe"]
        if not u.get("auto_update") or getattr(self, "readonly", False):
            return
        import rank_coins
        gen = rank_coins.generated_date()
        if gen and (now_utc().date() - gen).days < int(u["update_days"]):
            return
        log("候选名单已过期（生成于 %s），开始按流动性重建…" % gen)
        res = rank_coins.rebuild(self.cfg, u["top"], u["core"], u["min_days"], write=True, verbose=False)
        j = lambda xs: "、".join(xs) or "无"
        self.tg.send(f"🔄 <b>候选名单已按流动性更新</b>（上次 {gen or '未知'}）\n"
                     f"核心 {len(res['core'])} 个：{j(res['core'])}\n"
                     f"核心新进：{j(res['core_added'])}\n核心移出：{j(res['core_removed'])}\n"
                     f"候选新进：{j([c for c in res['added'] if c not in res['core']])}\n"
                     f"移出名单：{j(res['removed'])}"
                     + ("\n\n自动交易会在下一次调仓时平掉移出名单的币。" if self.cfg["trading"].get("enabled") else ""))
        self.payload = None                            # 触发用新名单重算

    def daily_loop(self):
        hh, mm = map(int, str(self.cfg["monitor"]["daily_at"]).split(":"))
        while True:
            try:
                try:
                    self.maybe_update_universe()
                except Exception:
                    log("名单更新失败", traceback.format_exc())
                    self._once("err-universe", "⚠️ <b>候选名单自动更新失败</b>，继续使用旧名单。详见服务日志。")
                n = now_utc()
                target = n.replace(hour=hh, minute=mm, second=0, microsecond=0)
                expected = (n - timedelta(days=1 if n >= target else 2)).strftime("%Y-%m-%d")
                have = self.payload["summary"]["data_date"] if self.payload else None
                if have is None or have < expected:
                    self.recompute()
                else:
                    self.maybe_trade()
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
            p = copy.deepcopy(self.payload)
        p["summary"]["server"] = {
            "tg": self.tg.enabled, "errors": self.errors,
            "last_compute": self.last_compute.strftime("%Y-%m-%d %H:%M UTC") if self.last_compute else None,
            "interval": self.cfg["monitor"]["price_interval"],
        }
        return p

    def command(self, text):
        p = self.snapshot()
        if not p:
            return "数据还在计算中，请稍后再试。"
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()
        s, coins = p["summary"], p["coins"]
        if cmd == "/bot":
            if not self.cfg["trading"].get("enabled"):
                return "自动交易未启用（config.yaml 的 trading.enabled）。"
            try:
                acct = trader.status(self.cfg)
            except Exception as e:
                acct = f"读取账户失败：{e}"
            last = trader.load_state().get("last_report") or "还没有执行过调仓。"
            return f"<pre>{acct}</pre>\n\n<b>最近一次调仓</b>\n{last}"
        if cmd in ("/start", "/help"):
            return ("/status 汇总\n/pos 当前持仓与止损\n/c BTC 查看单个币\n/alerts 盘中预警\n/bot 自动交易账户与最近一次调仓\n"
                    "仓位信号基于日线收盘，每天 UTC " + str(self.cfg["monitor"]["daily_at"]) + " 更新；盘中价格每 "
                    f"{self.cfg['monitor']['price_interval']} 秒刷新。")
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
                return f"没有 {args[0].upper()}（只监控 coins.yaml 里的币种）"
            v = "".join("✅" if x else "▫️" for x in c["votes"])
            b, t, lv = c["btc"], c["bt"], c["levels"]
            num = lambda x, f="{:.2f}": "—" if x is None else f.format(x)
            return (f"<b>{c['coin']}</b> {c['grade']}级 {('%gx 逐仓' % c['lev']) if c['lev'] else '不交易'}（{c['src']}）\n"
                    f"现价 {fp(c.get('live') or c['price'])}（盘中 {pc(c.get('live_chg'))}），收盘 {fp(c['price'])}\n"
                    f"信号 S1–S4 {v} 目标仓位 {c['pos']:.0%}{'（已止损）' if c['stopped'] else ''}\n"
                    f"持仓 {c['days_in']} 天，止损 {fp(c['stop'])}（{pc(c.get('live_stop_dist'))}）\n"
                    f"肯特纳 上轨 {fp(lv['kc_upper'])} / 中线 {fp(lv['kc_mid'])}，PBX 慢线 {fp(lv['pbx_slow'])}\n"
                    f"BTC 相关 {num(b['corr365'])}，β {num(b['beta'])}\n"
                    f"回测 年化 {num(t['L1_cagr'], '{:.0%}')} 回撤 {num(t['L1_max_dd'], '{:.0%}')}，近3年 {num(t['r3_cagr'], '{:.0%}')}"
                    + ("\n盘中：" + "；".join(f["t"] for f in c["live_flags"]) if c.get("live_flags") else ""))
        return "未知命令，发送 /help 查看。"


# ---------------------------------------------------------------- 网页

def make_handler(mon, cfg):
    page = render_page(None, cfg["dashboard"]["refresh_seconds"]).encode("utf-8")
    token = str(cfg["server"].get("access_token") or "")

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
            if u.path == "/health":
                return self._send(200, b"ok", "text/plain")
            if token and parse_qs(u.query).get("token", [""])[0] != token:
                return self._send(403, "需要正确的 ?token= 参数".encode(), "text/plain; charset=utf-8")
            if u.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if u.path == "/api/data":
                snap = mon.snapshot()
                if not snap:
                    return self._send(503, b'{"loading":true}', "application/json")
                return self._send(200, json.dumps(snap, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            self._send(404, b"not found", "text/plain")
    return H


def main():
    ap = argparse.ArgumentParser(description="趋势信号监控")
    ap.add_argument("--config", default=DEFAULT_CONFIG)
    ap.add_argument("--test-telegram", action="store_true", help="发一条测试消息后退出")
    ap.add_argument("--snapshot", metavar="FILE", help="计算一次，生成静态 HTML 后退出")
    args = ap.parse_args()
    cfg = load_config(args.config, required=args.config != DEFAULT_CONFIG)
    setup_logging(cfg)
    tg = Telegram(cfg["telegram"].get("bot_token"), cfg["telegram"].get("chat_ids"))
    if args.test_telegram:
        if not tg.enabled:
            sys.exit("config.yaml 里的 telegram.bot_token 或 chat_ids 没有填写")
        tg.send("✅ 趋势信号监控：Telegram 通知测试成功")
        return
    mon = Monitor(tg, cfg)
    if args.snapshot:
        mon.tg, mon.readonly = Telegram(None, None), True
        mon.state = {"positions": {}, "stopped": {}, "sent": {}, "data_date": None}
        mon.recompute()
        prices, _ = exchanges.prices()
        mon.apply_prices(prices)
        open(args.snapshot, "w", encoding="utf-8").write(render_page(mon.snapshot()))
        log("已生成", args.snapshot)
        return
    # 单实例锁：同一目录只允许一个 monitor 运行，防止重复下单
    import fcntl
    lock = open(os.path.join(HERE, ".monitor.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("已经有一个 monitor.py 在运行（.monitor.lock 被占用），不能重复启动。")
    if not tg.enabled:
        log("telegram 未配置，通知只打印到日志")
    threading.Thread(target=mon.daily_loop, daemon=True).start()
    threading.Thread(target=mon.price_loop, daemon=True).start()
    threading.Thread(target=tg.poll, args=(mon.command,), daemon=True).start()
    host, port = cfg["server"]["host"], int(cfg["server"]["port"])
    srv = ThreadingHTTPServer((host, port), make_handler(mon, cfg))
    tok = cfg["server"].get("access_token")
    log(f"仪表盘：http://{host}:{port}/" + (f"?token={tok}" if tok else ""))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
