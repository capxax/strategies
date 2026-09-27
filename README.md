# 趋势信号监控

加密货币日线趋势策略的实时监控：网页仪表盘 + Telegram 通知。

对 `coins.yaml` 里的每个币计算四个趋势信号（EMA 交叉、肯特纳通道、PBX 瀑布线、K 线面积），
给出目标仓位、止损价、建议杠杆和资金权重，盘中实时跟踪价格，出现仓位变化或风险时推送到 Telegram。

## 快速开始

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml     # 填写 Telegram 配置（不填也能运行，通知只打印到日志）
python monitor.py --test-telegram      # 检查 Telegram 配置
python monitor.py                      # 打开 http://127.0.0.1:8765/
```

启动后约 30 秒完成第一次日线计算，网页每 15 秒自动刷新。

其他用法：
```bash
python monitor.py --config other.yaml     # 指定配置文件
python monitor.py --snapshot out.html     # 计算一次，生成可离线打开的静态页面
```

## 文件

| 文件 | 作用 |
|---|---|
| `monitor.py` | 主程序：网页服务、实时价格、每日重算、Telegram 推送和命令 |
| `signals.py` | 指标与信号计算、止损、BTC 联动、资金分配 |
| `exchanges.py` | 行情数据：确定数据来源、拉取日线和实时价格 |
| `template.html` | 仪表盘页面 |
| `config.example.yaml` | 配置模板（复制为 `config.yaml` 使用） |
| `coins.yaml` | 监控的币种名单、评级和回测结果 |
| `trend-monitor.service` | systemd 服务示例 |

## 行情数据来源

每个币只从一个交易所取数，优先级：**OKX USDT 永续 > 币安 USDT 现货 > Hyperliquid 永续**。

- 某个币在 OKX 上的日线少于 `dashboard.min_history`（默认 300 根，一般是上线较晚的合约），自动改用币安，依此类推。
- 实时价格用和日线相同的交易所，保证口径一致。
- 全部使用公开行情接口，不需要 API key。币安使用公开行情域名 `data-api.binance.vision`，美国服务器也能访问。
- 当前名单的 61 个币：52 个用 OKX，6 个用币安，3 个用 Hyperliquid。每个币的数据来源显示在卡片上。

## 更新频率

| 内容 | 频率 |
|---|---|
| 实时价格、盘中预警 | 每 `monitor.price_interval` 秒（默认 30），每个交易所一次批量请求 |
| 日线信号、目标仓位、止损价 | 每天 UTC `monitor.daily_at`（默认 00:05）；启动时也会算一次 |
| 币种名单 | 每次日线重算时重新读取 `coins.yaml` |

仓位信号只看**日线收盘**；盘中价格只用于预警，不改变目标仓位。

## 仪表盘内容

- **顶部汇总**：BTC 趋势状态、持仓币数量、市场宽度、建议总敞口、今日调仓，以及需要关注的事项（点击跳到对应币）。
- **每个币一张卡片**：
  - 实时价格与盘中涨跌，1/7/30 日涨跌
  - S1–S4 四个信号的投票和目标仓位
  - 180 天走势图：绿色底色为持仓期，蓝线为 EMA26，红色虚线为止损价
  - 持仓天数、入场价、最高收盘、止损价与距离
  - 下一个触发价位（空仓时为入场位，持仓时为离场位）
  - 与 BTC 的相关性、β、信号一致率
  - 回测表现、建议杠杆、权重、敞口
  - 盘中预警
- **筛选排序**：评级、仓位状态、BTC 联动程度、搜索、多种排序。

## Telegram

### 配置
1. 在 Telegram 找 **@BotFather**，发送 `/newbot` 创建机器人，把得到的 token 填到 `telegram.bot_token`。
2. 给机器人发一条任意消息（群组则把机器人拉进群后发一条消息）。
3. 浏览器打开 `https://api.telegram.org/bot<token>/getUpdates`，找到 `"chat":{"id":...}`，填到 `telegram.chat_ids`。
4. 运行 `python monitor.py --test-telegram`，收到测试消息即成功。

### 推送内容（同一事件每个 UTC 日只推一次，重启不会重复推送）

| 通知 | 时机 | 配置开关 |
|---|---|---|
| 📊 日线收盘报告：BTC 状态、持仓数、总敞口、当天的入场 / 加仓 / 减仓 / 离场 | 每天 `daily_at` 后 | `monitor.daily_report` |
| 🛑 止损触发：收盘价跌破止损价（持仓以来最高收盘 × 0.75），应清仓 | 日线收盘 | 始终推送 |
| 🛑 盘中跌破止损价 | 盘中 | `alerts.stop_break` |
| ⚠️ 接近止损（默认 5% 以内） | 盘中 | `alerts.near_stop` |
| 📈 空仓的币盘中突破入场位（收盘站稳会加仓） | 盘中 | `alerts.entry_watch` |
| 📉 持仓的币盘中跌破离场位（收盘不收回会减仓） | 盘中 | `alerts.exit_watch` |
| ⚠️ 数据异常、计算失败、实时价格连续拉取失败 | 出错时 | 始终推送 |

### 命令
- `/status`：汇总
- `/pos`：持仓、敞口、止损价与距离
- `/c BTC`：单个币的详细状态
- `/alerts`：当前盘中预警
- `/help`：帮助

机器人只响应 `chat_ids` 里的对话。

## 修改币种

编辑 `coins.yaml`。每个币的格式：
```yaml
SOL:
  grade: A              # A 推荐交易 / B 可交易但权重减半 / C 只观察
  okx: true             # 仅用于显示在哪些交易所可交易
  hyperliquid: true
  backtest: {cagr: 1.21, max_dd: -0.58, ...}   # 可省略，页面对应位置显示“—”
```
`BTC` 必须保留（用于计算 BTC 联动和市场状态）。修改后在下一次日线重算时生效，想立即生效请重启服务。

## 部署到服务器

1. 用 systemd 常驻：修改 `trend-monitor.service` 里的用户和目录，然后
   ```bash
   sudo cp trend-monitor.service /etc/systemd/system/
   sudo systemctl enable --now trend-monitor
   journalctl -u trend-monitor -f
   ```
2. 服务默认只监听 `127.0.0.1`。从外网访问有两种方式：
   - 推荐 SSH 隧道：`ssh -L 8765:127.0.0.1:8765 服务器`，然后在本机打开 http://127.0.0.1:8765/
   - 或者把 `server.host` 改成 `0.0.0.0`，并设置 `server.access_token`，访问 `http://服务器IP:8765/?token=xxx`。
     这是明文 HTTP，建议放在 Nginx + HTTPS 后面。
3. `.monitor_state.json` 记录上次的仓位和已发送的通知，用来避免重复推送；删除即可重置。

## 风险提示

- 信号和参数来自历史回测（日线、只做多），过去的表现不代表未来。
- 建议杠杆：BTC 2x，ETH / BNB / HYPE 1.5x，其余 1x，一律逐仓；可在 `config.yaml` 的 `leverage` 中修改。
- 本程序只做监控和提醒，不会下单。
