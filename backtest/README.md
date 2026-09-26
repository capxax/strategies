# 策略回测验证框架

仓库里有 5800 多个策略，其中 91% 是 PineScript，无法直接在 Python 中运行。
这里从中提炼出 **15 个最常见的策略族**，用 Python 实现代表版本，
再用真实行情（Yahoo 股票 / Binance 加密货币）做**样本外**检验，判断哪类思路真正有效。
`catalog.py` 可以把仓库中的全部策略按关键词归入这些族（覆盖约 92%）。

## 安装
```bash
pip install -r requirements.txt
```

## 使用
```bash
# 股票（美股/港股/A股均可用 Yahoo 代码）
python run.py --source yahoo --symbols SPY QQQ AAPL MSFT NVDA 0700.HK 600519.SS --start 2012-01-01

# 币安现货，4 小时线，允许做空
python run.py --source binance --symbols BTCUSDT ETHUSDT SOLUSDT BNBUSDT --interval 4h --start 2021-01-01 --short

# 只测部分策略
python run.py --source binance --symbols BTCUSDT --only supertrend donchian_breakout

# 离线自测（模拟数据）
python run.py --source synthetic --symbols A B C

# 策略归类统计
python catalog.py
```
输出位于 `reports/`：`report_*.md`（汇总结论）、`summary_*.csv`、`detail_*.csv`（每个标的、每个策略的明细）。

## 验证方法（防止过拟合）
1. **无未来函数**：在 bar 收盘时产生信号，下一根 bar 开盘成交；计入手续费和滑点。
2. **样本内/样本外**：前 60% 数据用网格搜索参数（以夏普为目标），后 40% 用选定参数检验，只看样本外结果。
3. **对比基准**：与同期买入持有比较夏普和最大回撤。
4. **跨标的一致性**：一个策略在 ≥60% 的标的上有效才判为“稳健”。`decay` 列表示样本外夏普减样本内夏普，越负越说明过拟合。

## 文件
| 文件 | 作用 |
|---|---|
| `data.py` | Yahoo / Binance（分页拉取、备用域名）/ 模拟数据，带 CSV 缓存 |
| `indicators.py` | SMA/EMA/RMA、RSI、MACD、布林带、ATR、ADX、KD、唐奇安、SuperTrend、VWAP（口径与 Pine `ta.*` 一致） |
| `strategies.py` | 15 个策略族；新增策略只需加一个 `@strategy` 装饰函数 |
| `engine.py` | 向量化回测，统计年化、夏普、最大回撤、Calmar、胜率、盈亏比、持仓占比 |
| `run.py` | 批量寻优、样本外验证、生成报告 |
| `catalog.py` | 仓库策略归类 |

## 局限
- 只实现了各族的代表逻辑，没有逐个翻译 5800 个 Pine 脚本；某个族的结论只说明这类思路是否可行。
- 归类靠关键词，是启发式的。像 “atr / 止损” 这样的通用词会让 `ema_atr_trail` 被高估。
- 日线股票数据使用 Yahoo 复权价；Binance 只取现货，不含资金费率。
