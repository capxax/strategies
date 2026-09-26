"""行情数据：Yahoo(股票/指数/ETF)、Binance(现货K线)、synthetic(离线自测)。本地CSV缓存。"""
import os, time
import numpy as np
import pandas as pd

CACHE = os.path.join(os.path.dirname(__file__), ".cache")
COLS = ["open", "high", "low", "close", "volume"]


def _cached(key, fn):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, key.replace("/", "_") + ".csv")
    if os.path.exists(path):
        return pd.read_csv(path, index_col=0, parse_dates=True)
    df = fn()
    df.to_csv(path)
    return df


def yahoo(symbol, start="2015-01-01", end=None, interval="1d"):
    import yfinance as yf

    def fetch():
        df = yf.download(symbol, start=start, end=end, interval=interval,
                         auto_adjust=True, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[COLS].dropna()
        if df.empty:
            raise ValueError(f"Yahoo 无数据: {symbol}")
        return df
    return _cached(f"yahoo_{symbol}_{interval}_{start}_{end}", fetch)


BINANCE_URLS = ["https://api.binance.com", "https://data-api.binance.vision"]


def binance(symbol="BTCUSDT", start="2019-01-01", end=None, interval="1d"):
    import requests

    def fetch():
        t0 = int(pd.Timestamp(start, tz="UTC").timestamp() * 1000)
        t1 = int(pd.Timestamp(end or pd.Timestamp.utcnow(), tz="UTC").timestamp() * 1000) \
            if end else int(time.time() * 1000)
        rows, err = [], None
        for base in BINANCE_URLS:
            try:
                cur = t0
                while cur < t1:
                    r = requests.get(base + "/api/v3/klines", timeout=15, params={
                        "symbol": symbol, "interval": interval,
                        "startTime": cur, "endTime": t1, "limit": 1000})
                    r.raise_for_status()
                    batch = r.json()
                    if not batch:
                        break
                    rows += batch
                    cur = batch[-1][0] + 1
                    if len(batch) < 1000:
                        break
                break
            except Exception as e:  # 换备用域名
                rows, err = [], e
        if not rows:
            raise RuntimeError(f"Binance 拉取失败: {err}")
        df = pd.DataFrame(rows).iloc[:, :6]
        df.columns = ["time"] + COLS
        df.index = pd.to_datetime(df.pop("time"), unit="ms")
        return df.astype(float)
    return _cached(f"binance_{symbol}_{interval}_{start}_{end}", fetch)


def synthetic(symbol="SYN", n=2500, seed=0):
    """带趋势切换和波动聚集的随机游走，仅用于离线验证框架。"""
    rng = np.random.default_rng(abs(hash(symbol)) % 2**32 + seed)
    regime = np.repeat(rng.normal(0, 0.0015, n // 100 + 1), 100)[:n]
    vol = 0.01 + 0.01 * np.abs(np.sin(np.arange(n) / 150))
    ret = regime + rng.normal(0, 1, n) * vol
    close = 100 * np.exp(np.cumsum(ret))
    op = np.r_[close[0], close[:-1]]
    hi = np.maximum(op, close) * (1 + np.abs(rng.normal(0, vol / 2)))
    lo = np.minimum(op, close) * (1 - np.abs(rng.normal(0, vol / 2)))
    idx = pd.date_range("2016-01-01", periods=n, freq="D")
    return pd.DataFrame({"open": op, "high": hi, "low": lo, "close": close,
                         "volume": rng.lognormal(10, 0.5, n)}, index=idx)


def load(source, symbol, **kw):
    return {"yahoo": yahoo, "binance": binance, "synthetic": synthetic}[source](symbol, **kw)
