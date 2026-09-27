"""交易所行情：确定每个币的数据来源，拉取日线 K 线和实时价格。

每个币只用一个交易所的数据，优先级：OKX USDT 永续 > 币安 USDT 现货 > Hyperliquid 永续。
全部使用公开行情接口，不需要 API key。
"""
import time
import pandas as pd
import requests

OKX = "https://www.okx.com"
BINANCE = "https://data-api.binance.vision"   # 币安公开行情域名，美国服务器也能访问
HL = "https://api.hyperliquid.xyz/info"

S = requests.Session()
S.headers["User-Agent"] = "trend-monitor/1.0"


def _get(url, **kw):
    for i in range(3):
        try:
            r = S.get(url, timeout=15, **kw)
            if r.status_code == 429:
                time.sleep(1 + i)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 2:
                raise
            time.sleep(1 + i)


def _post(url, body):
    for i in range(3):
        try:
            r = S.post(url, json=body, timeout=20)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if i == 2:
                raise
            time.sleep(1 + i)


def hl_name(coin, hl_names):
    """Hyperliquid 上部分币以 1000 个为单位，名字带 k 前缀（kPEPE、kBONK…）。"""
    if coin in hl_names:
        return coin
    if "k" + coin in hl_names:
        return "k" + coin
    return None


def resolve_sources(coins):
    """返回 {coin: [(source, symbol), ...]}，按 OKX > 币安 > Hyperliquid 排序的候选列表。"""
    okx = {i["instId"] for i in _get(f"{OKX}/api/v5/public/instruments", params={"instType": "SWAP"})["data"]
           if i["settleCcy"] == "USDT" and i["state"] == "live"}
    bn = {s["symbol"] for s in _get(f"{BINANCE}/api/v3/exchangeInfo", params={"permissions": "SPOT"})["symbols"]
          if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"}
    hl = {u["name"] for u in _post(HL, {"type": "meta"})["universe"] if not u.get("isDelisted")}
    out = {}
    for c in coins:
        cands = []
        if f"{c}-USDT-SWAP" in okx:
            cands.append(("okx", f"{c}-USDT-SWAP"))
        if f"{c}USDT" in bn:
            cands.append(("binance", f"{c}USDT"))
        if hl_name(c, hl):
            cands.append(("hyperliquid", hl_name(c, hl)))
        out[c] = cands
    return out


def _frame(rows):
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("time").astype("int64"), unit="ms")
    df = df.astype(float).sort_index()
    return df[~df.index.duplicated()]


def klines(source, symbol, days=420):
    """最近 days 天的 UTC 日线，只保留已收盘的 K 线。"""
    since = int((pd.Timestamp.now("UTC") - pd.Timedelta(days=days)).timestamp() * 1000)
    if source == "okx":
        rows, after = [], ""
        while True:
            batch = _get(f"{OKX}/api/v5/market/history-candles",
                         params={"instId": symbol, "bar": "1Dutc", "limit": 100, "after": after})["data"]
            if not batch:
                break
            rows += [x[:6] for x in batch if x[8] == "1"]          # x[8] = confirm，1 表示已收盘
            after = batch[-1][0]
            if int(after) <= since or len(batch) < 100:
                break
            time.sleep(0.11)                                       # OKX 限频 20 次 / 2 秒
        df = _frame(rows)
    elif source == "binance":
        rows, start = [], since
        while True:
            batch = _get(f"{BINANCE}/api/v3/klines",
                         params={"symbol": symbol, "interval": "1d", "startTime": start, "limit": 1000})
            if not batch:
                break
            rows += [x[:6] for x in batch]
            start = batch[-1][0] + 1
            if len(batch) < 1000:
                break
        df = _frame(rows)
    elif source == "hyperliquid":
        batch = _post(HL, {"type": "candleSnapshot", "req": {"coin": symbol, "interval": "1d",
                                                             "startTime": since, "endTime": int(time.time() * 1000)}})
        df = _frame([[x["t"], x["o"], x["h"], x["l"], x["c"], x["v"]] for x in batch])
    else:
        raise ValueError(source)
    today = pd.Timestamp.now("UTC").tz_localize(None).normalize()
    return df[df.index < today]                                    # 丢弃当天未收盘的 K 线


def prices():
    """每个交易所一次请求拿全部最新价。返回 ({(source, symbol): price}, [错误信息])。"""
    out, errs = {}, []
    try:
        for x in _get(f"{OKX}/api/v5/market/tickers", params={"instType": "SWAP"})["data"]:
            out[("okx", x["instId"])] = float(x["last"])
    except Exception as e:
        errs.append(f"OKX: {e}")
    try:
        for x in _get(f"{BINANCE}/api/v3/ticker/price"):
            out[("binance", x["symbol"])] = float(x["price"])
    except Exception as e:
        errs.append(f"币安: {e}")
    try:
        for k, v in _post(HL, {"type": "allMids"}).items():
            out[("hyperliquid", k)] = float(v)
    except Exception as e:
        errs.append(f"Hyperliquid: {e}")
    return out, errs
