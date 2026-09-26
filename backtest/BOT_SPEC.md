# 加密货币量化 Bot 实现规格

> 本文档写给负责实现 bot 的开发者（或 AI）。所有公式、参数和执行规则都与本目录的回测代码
> （`indicators.py`、`strategies.py`、`strategies_digest.py`、`engine.py`）保持一致。
> 按本文实现后，在同样的数据上应能复现回测结果。遇到歧义时，以上述源码为准。

---

## 0. 回测依据（为什么选这些策略和币种）

- 数据：币安现货日线，2018-01-01 至 2026-09。
- 方法：前 60% 的数据用网格搜索参数（按夏普比率选），后 40%（约 2022 年底之后）做样本外检验，
  单边费率 0.1%，滑点 0.05%，信号在收盘产生、下一根开盘成交。
- 判定“有效”：样本外夏普 > 0.3 且不低于买入持有；或夏普达到持有的 80% 以上、同时最大回撤明显更小。
- 下列数值全部是**样本外**结果。

| 策略 | BTC | ETH | BNB | SOL | ADA | DOGE | XRP | LINK |
|---|---|---|---|---|---|---|---|---|
| S1 EMA 交叉 | ✅ 1.02 | ✅ 0.63 | ✅ 0.90 | ✅ 0.43 | ✅ 0.69 | ✅ 0.78 | ❌ 0.47 | ✅ 0.88 |
| S2 肯特纳 | ✅ 0.88 | ✅ 0.53 | ✅ 0.98 | ✅ 0.37 | ✅ 0.47 | ✅ 0.92 | ✅ 0.82 | ❌ 0.64 |
| S3 PBX 瀑布线 | ✅ 1.03 | ✅ 0.84 | ❌ 0.46 | ✅ 0.38 | ✅ 0.87 | ✅ 0.70 | ✅ 0.82 | ❌ 0.52 |
| S4 K 线面积 | ✅ 0.93 | ✅ 0.72 | ✅ 0.81 | ✅ 0.85 | ❌ 0.30 | ✅ 0.93 | ❌ 0.59 | ❌ 0.66 |
| S5 Dual Thrust | ✅ 0.97 | ✅ 0.51 | ❌ 0.65 | ✅ 0.51 | ✅ 0.53 | ✅ 0.80 | ❌ 0.07 | ❌ 0.01 |
| 买入持有夏普 | 0.92 | 0.50 | 0.76 | 0.27 | 0.29 | 0.56 | 0.81 | 0.73 |
| 买入持有最大回撤 | -53% | -68% | -58% | -76% | -88% | -85% | -72% | -75% |

表中数字为样本外夏普。策略的样本外最大回撤大多在 -24% 到 -60% 之间，明显小于持有。

**结论：**
- 交易币种：BTCUSDT、ETHUSDT、BNBUSDT、DOGEUSDT 为核心；SOLUSDT 可选，只给小仓位。
- 不交易：XRPUSDT、LINKUSDT。
- 策略：S1、S2、S3、S4 组合投票（见第 4 节）。S5 交易太频繁、跨币表现不稳，可以不用或只给很小的权重。

---

## 1. 通用约定（必须严格遵守）

### 1.1 数据
- 交易所：币安现货（Spot），交易对以 USDT 计价。
- K 线：`GET /api/v3/klines?symbol=BTCUSDT&interval=1d&limit=1000`。
  主域名 `api.binance.com` 在部分地区返回 451，这时改用只读行情域名 `data-api.binance.vision`（K 线相同）。
  下单只能走主域名 API（或所在地区可用的币安站点）。
- 字段：open_time, open, high, low, close, volume（取前 6 列，转为 float）。
- **只用已收盘的 K 线计算信号。** 日线 K 线在 UTC 00:00 收盘。拉取 K 线后，丢掉 `open_time` 等于当天的那一根（它还在形成中）。
- 每次至少拉取 **300 根**历史 K 线，给指标预热（最长用到 SMA 48 和 ADX）。

### 1.2 执行时机
- 每天 UTC 00:00 之后几分钟（例如 00:02）运行一次。
- 用截至刚刚收盘的那根日线计算“目标仓位”，然后立即按市价或近似市价把实际仓位调到目标仓位。
  这等价于回测中的“收盘出信号、下一根开盘成交”。
- 不要在 K 线中途根据未收盘的价格触发交易，否则结果会与回测不一致。

### 1.3 仓位模型
- 只做多，现货，不加杠杆。每个策略输出一个目标仓位 `pos ∈ [0, 1]`：0 表示空仓，1 表示用满分配给它的资金。
- 许多策略是**有状态的**（入场条件和出场条件不同，中间保持原仓位），bot 必须**持久化保存每个币、每个策略的当前状态**（文件或数据库），重启后能恢复。
  也可以在每次运行时，用最近 300 根 K 线从头重放一遍状态机得到当前状态。重放的结果是确定的，**推荐用重放**，这样不依赖本地存储。

### 1.4 指标定义（与 TradingView Pine 的 `ta.*` 一致）

```
SMA(x, n)  = x 最近 n 个值的算术平均
EMA(x, n)  : alpha = 2/(n+1)；EMA[0] = x[0]；EMA[t] = alpha*x[t] + (1-alpha)*EMA[t-1]
             （pandas: x.ewm(span=n, adjust=False).mean()）
RMA(x, n)  : Wilder 平滑，alpha = 1/n，递推方式同 EMA（pandas: x.ewm(alpha=1/n, adjust=False).mean()）
TR[t]      = max(high[t]-low[t], |high[t]-close[t-1]|, |low[t]-close[t-1]|)
ATR(n)     = RMA(TR, n)
```

ADX(n)（仅 4h 补充策略使用）：
```
up = high[t]-high[t-1]；dn = low[t-1]-low[t]
+DM = up  若 up>dn 且 up>0，否则 0
-DM = dn  若 dn>up 且 dn>0，否则 0
+DI = 100*RMA(+DM,n)/RMA(TR,n)；-DI = 100*RMA(-DM,n)/RMA(TR,n)
DX  = 100*|+DI - -DI|/(+DI + -DI)；ADX = RMA(DX, n)
```

下面所有公式中，`[t]` 表示刚收盘的那根 K 线，`[t-1]` 是它的前一根。

---

## 2. 策略详细规格（日线）

### S1 — EMA 交叉（趋势跟随）

**思路：** 短期均线在长期均线之上说明处于上升趋势，这时持有；否则空仓。这是最简单、最稳健的一个。

**参数：**

| 币 | fast | slow |
|---|---|---|
| BTCUSDT | 12 | 55 |
| ETHUSDT / BNBUSDT / SOLUSDT | 12 | 21 |
| DOGEUSDT | 12 | 26 |
| 默认（其他币） | 12 | 26 |

**规则（无状态）：**
```
f = EMA(close, fast)[t]
s = EMA(close, slow)[t]
pos = 1 if f > s else 0
```
**特征：** 每个币每年大约 3–8 次交易，胜率低（30–40%），靠少数大波段盈利。

---

### S2 — 肯特纳通道突破（Keltner）

**思路：** 价格有力地突破 “EMA + k 倍 ATR” 的上轨时说明趋势启动，买入；价格跌回 EMA 中线时说明趋势结束，卖出。
入场要求强，出场比较宽松，能拿住趋势。

**参数：**

| 币 | n（EMA 周期） | k |
|---|---|---|
| BTCUSDT | 50 | 1.5 |
| BNBUSDT / SOLUSDT | 50 | 1.0 |
| ETHUSDT | 20 | 1.0 |
| DOGEUSDT | 20 | 2.0 |
| 默认 | 20 | 1.5 |

ATR 周期固定为 10。

**规则（有状态）：**
```
mid   = EMA(close, n)[t]
upper = mid + k * ATR(10)[t]
if state == 0 and close[t] > upper: state = 1   # 入场
elif state == 1 and close[t] < mid: state = 0   # 离场
pos = state
```
同一根 K 线如果同时满足入场和出场条件，**入场优先**（与回测的 `hold()` 一致）。实际上 upper > mid，两者不会同时成立。

---

### S3 — PBX 瀑布线

**思路：** 瀑布线是 EMA 和两条更长 SMA 的平均，比单一均线更平滑。三条瀑布线多头排列、价格站在最快线之上时入场；价格跌破最慢线时离场。

**参数：** a=4, b=6, c=12（BNB 的最优是 4/6/9，但 BNB 上这个策略无效，不建议用在 BNB）。

**定义：**
```
PB(n) = ( EMA(close, n) + SMA(close, 2n) + SMA(close, 4n) ) / 3
p1 = PB(a)[t]; p2 = PB(b)[t]; p3 = PB(c)[t]
```
**规则（有状态）：**
```
if state == 0 and p1 > p2 and p2 > p3 and close[t] > p1: state = 1
elif state == 1 and close[t] < p3: state = 0
pos = state
```
注意 c=12 时要用到 SMA(48)，所以至少需要 48 根 K 线预热，建议 300 根。

---

### S4 — K 线面积（趋势强度）

**思路：** 统计最近 n 根 K 线收盘价在均线上方或下方的累计偏离（用 ATR 标准化）。累计偏离为正，说明多头占优。
它比简单的“价格在均线上方”更抗短期噪音。

**参数：** n = 20，th = 0.0（BNB 用 th = 0.5）。

**定义：**
```
d[i]  = (close[i] - SMA(close, n)[i]) / ATR(n)[i]      # 对每根 K 线计算
area  = ( d[t-n+1] + ... + d[t] ) / n                 # 最近 n 个 d 的平均
```
**规则（有状态）：**
```
if area > th:  state = 1
elif area < -th: state = 0
# th = 0 时等价于 pos = 1 if area > 0 else 0
pos = state
```

---

### S5 — Dual Thrust（可选，权重低）

**思路：** 以当天开盘价为基准，加减过去 n 天振幅的 k 倍作为上下轨。收盘突破上轨买入，跌破下轨卖出。

**参数：** n = 4, k1 = 0.5, k2 = 0.5（BTC、ETH 用 n = 6, k1 = 0.5~0.7, k2 = 0.3）。

**定义**（区间只用 `[t-1]` 及以前的数据，不包含当根）：
```
HH = max(high[t-n .. t-1]);  LC = min(close[t-n .. t-1])
HC = max(close[t-n .. t-1]); LL = min(low[t-n .. t-1])
range = max(HH - LC, HC - LL)
up = open[t] + k1*range
dn = open[t] - k2*range
```
**规则（有状态）：**
```
if close[t] > up: state = 1
elif close[t] < dn: state = 0
pos = state
```
**特征：** 交易次数是 S1 的 3–5 倍，对手续费敏感。

---

## 3. 4 小时补充策略（可选）

### S6 — ADX 过滤均线交叉（4h）
只在 BTCUSDT、ETHUSDT、SOLUSDT 上有效（样本外夏普 0.56 / 0.44 / 0.39，持有是 0.43 / 0.14 / 0.24），BNB 上无效。
年化只有 4–15%，适合作为日线组合之外的小额补充。**只做多。**

**参数：** fast = 20, slow = 50, th = 20（ETH 用 slow = 30, th = 25）。
**执行：** 每根 4h K 线收盘后（UTC 00/04/08/12/16/20 点）运行。
```
adx = ADX(14)[t]
f = EMA(close, fast)[t]; s = EMA(close, slow)[t]
if state == 0 and adx > th and f > s: state = 1
elif state == 1 and f < s: state = 0
pos = state
```
回测里的做空版本在 4h 上表现差，所以不要做空。

---

## 4. 组合与资金分配

### 4.1 每个币内部：多策略投票
对每个币同时计算 S1–S4 的 pos（0 或 1）。

**推荐方案 A（平均仓位）：**
```
coin_pos = mean(pos_S1, pos_S2, pos_S3, pos_S4)    # 结果为 0, 0.25, 0.5, 0.75, 1
```
这相当于每个策略各管该币 1/4 的资金。仓位会平滑变化，单一参数失效的影响被分散。

**可选方案 B（多数表决）：**
```
coin_pos = 1 if sum(pos) >= 2 else 0   # 4 个里至少 2 个看多（或设为 >= 3）
```
方案 B 交易更少，但是全进全出。

排除规则：如果某策略在该币上被第 0 节的表格标为 ❌，就从这个币的投票中去掉，只对剩下的策略取平均。

### 4.2 币种之间：按波动率分配
```
vol_i    = 过去 60 天日收益率的标准差
w_raw_i  = 1 / vol_i
w_i      = w_raw_i / Σ w_raw_j            # 归一化
target_value_i = 总权益 × w_i × coin_pos_i
```
- 也可以用固定权重，例如 BTC 35%、ETH 25%、BNB 20%、DOGE 10%、SOL 10%。
- 单个币的权重上限设为 40%。
- 未分配或空仓的部分以 USDT 持有。

### 4.3 调仓
- 每天运行时，计算 `target_value_i` 与当前持仓市值的差额。
- **只有差额超过总权益的 2%（或 10 USDT）时才下单**，避免频繁的小额交易。
- 下单用市价单，或者用“盘口买一/卖一挂单，1 分钟不成交就改市价”的方式。
- 需要满足币安的最小下单额（通常为 5 USDT）和数量精度（LOT_SIZE、PRICE_FILTER、NOTIONAL），从 `/api/v3/exchangeInfo` 获取。

---

## 5. 风险控制（回测里没有，但实盘必须有）

1. **紧急止损：** 如果某个币持仓从入场后最高价回落超过 25%，而策略还没发出离场信号，立即清仓，直到策略重新发出入场信号再进。这是为了防止黑天鹅，正常情况下很少触发。
2. **组合熔断：** 账户权益从历史最高点回撤超过 35% 时，暂停所有交易，并通知人工复核。
3. **数据校验：** 如果 K 线缺失、时间戳不连续，或最新一根收盘时间不是 UTC 00:00，就跳过本次运行并告警，不要用残缺数据下单。
4. **下单失败重试：** 最多重试 3 次，间隔 2 / 4 / 8 秒；仍失败就告警。
5. **幂等：** 记录每天的运行日期，同一天不重复执行调仓。
6. **API 权限：** 只开启现货交易权限，关闭提现，并绑定 IP 白名单。

---

## 6. 上线流程

1. **复现回测：** 用本目录的 `engine.py` 或自行实现的回测，在 2018 至今的数据上计算 S1–S4 的仓位序列，确认结果与第 0 节表格接近（夏普相差 0.1 以内）。
2. **模拟盘：** 在 dry-run 模式下运行至少 2–4 周，只记录会下的单、不真实下单，确认信号时点和仓位计算正确。
3. **小资金实盘：** 先用计划资金的 10–20%，运行 1–3 个月。
4. **定期复核：** 每季度运行一次
   `python run.py --source binance --symbols BTCUSDT ETHUSDT BNBUSDT SOLUSDT DOGEUSDT --start 2018-01-01`，
   检查各策略在各币上是否仍然“有效”。某个策略在某个币上连续两次判定无效，就把它从该币的投票中移除。

---

## 7. 预期表现与心理准备

- 样本外年化约 10–40%（因币而异），最大回撤 25–60%，胜率 30–45%。
- 利润集中在少数几次大趋势中；在震荡市里会连续小亏，这是正常现象，不要因此手动干预。
- 牛市里收益通常会**低于**直接持有（入场晚、中途可能被洗出），价值主要体现在熊市少亏。
- 过去的结果不代表未来。所有参数都是在样本内优化出来的，存在过拟合风险。

---

## 附录 A：参考伪代码（Python）

```python
import pandas as pd

def ema(s, n): return s.ewm(span=n, adjust=False).mean()
def sma(s, n): return s.rolling(n).mean()
def rma(s, n): return s.ewm(alpha=1/n, adjust=False).mean()
def atr(df, n):
    pc = df.close.shift()
    tr = pd.concat([df.high-df.low, (df.high-pc).abs(), (df.low-pc).abs()], axis=1).max(axis=1)
    return rma(tr, n)

def run_state(entry, exit_):
    """有状态信号：入场优先，返回最后一根的状态。"""
    state = 0
    for e, x in zip(entry, exit_):
        if e: state = 1
        elif x: state = 0
    return state

def s1(df, fast, slow):
    return int(ema(df.close, fast).iloc[-1] > ema(df.close, slow).iloc[-1])

def s2(df, n, k):
    mid = ema(df.close, n); up = mid + k * atr(df, 10)
    return run_state(df.close > up, df.close < mid)

def s3(df, a=4, b=6, c=12):
    pb = lambda n: (ema(df.close, n) + sma(df.close, 2*n) + sma(df.close, 4*n)) / 3
    p1, p2, p3 = pb(a), pb(b), pb(c)
    return run_state((p1 > p2) & (p2 > p3) & (df.close > p1), df.close < p3)

def s4(df, n=20, th=0.0):
    area = ((df.close - sma(df.close, n)) / atr(df, n)).rolling(n).sum() / n
    return run_state(area > th, area < -th)

PARAMS = {
    "BTCUSDT":  dict(s1=(12, 55), s2=(50, 1.5), s3=True,  s4=0.0),
    "ETHUSDT":  dict(s1=(12, 21), s2=(20, 1.0), s3=True,  s4=0.0),
    "BNBUSDT":  dict(s1=(12, 21), s2=(50, 1.0), s3=False, s4=0.5),
    "SOLUSDT":  dict(s1=(12, 21), s2=(50, 1.0), s3=True,  s4=0.0),
    "DOGEUSDT": dict(s1=(12, 26), s2=(20, 2.0), s3=True,  s4=0.0),
}

def coin_position(df, p):
    """df: 已收盘的日线 K 线（至少 300 根），列为 open/high/low/close/volume。"""
    votes = [s1(df, *p["s1"]), s2(df, *p["s2"]), s4(df, th=p["s4"])]
    if p["s3"]:
        votes.append(s3(df))
    return sum(votes) / len(votes)   # 方案 A：0 ~ 1
```

注意：`run_state` 里把 NaN 比较当作 False，指标预热期内不会入场，这和回测一致。
