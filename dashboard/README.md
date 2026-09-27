# 趋势信号看板（实时监控 + Telegram 通知）

按币种实时显示 `backtest/BOT_SPEC.md` 中 S1–S4 的信号、目标仓位、止损价、BTC 联动和回测表现，
并通过 Telegram 推送仓位变化和风险预警。只包含 A/B/C 级币种（D 级不显示）。

## 快速开始

```bash
pip install -r backtest/requirements.txt
cp dashboard/.env.example dashboard/.env      # 填写 Telegram 配置（不填也能跑，通知只打印到日志）
python dashboard/monitor.py                    # 打开 http://127.0.0.1:8765/
python dashboard/monitor.py --test-telegram    # 只发一条测试消息，检查 Telegram 配置
```

启动后约 1–2 分钟完成第一次日线计算（之后有缓存会更快），页面每 15 秒自动刷新。

## 更新频率

| 内容 | 频率 |
|---|---|
| 实时价格、盘中预警 | 每 30 秒（`PRICE_INTERVAL`），币安 / OKX / Hyperliquid 各一次批量请求 |
| 日线信号、目标仓位、止损价 | 每天 UTC 00:05（`DAILY_AT`）重算；服务启动时也会算一次 |

仓位信号只看**日线收盘**，这和回测、bot 保持一致。盘中价格只用来预警，不改变目标仓位。

## Telegram 通知

### 配置
1. 在 Telegram 里找 **@BotFather**，发送 `/newbot`，按提示起名，得到 token，填入 `TG_BOT_TOKEN`。
2. 给你的机器人发一条任意消息（群组则把机器人拉进群并发一条消息）。
3. 打开 `https://api.telegram.org/bot<你的token>/getUpdates`，在返回内容里找到 `"chat":{"id":...}`，填入 `TG_CHAT_ID`。
   多个接收人用逗号分隔。
4. 运行 `python dashboard/monitor.py --test-telegram`，收到测试消息即配置成功。

### 会推送什么（同一事件每个 UTC 日只推一次）

| 类型 | 时机 | 内容 |
|---|---|---|
| 📊 日线收盘报告 | 每天 UTC 00:05 后 | BTC 状态、持仓数、总敞口，以及当天所有入场 / 加仓 / 减仓 / 离场 |
| 🛑 止损触发 | 日线收盘 | 收盘价跌破止损价（持仓以来最高收盘 × 0.75），应清仓 |
| 🛑 盘中跌破止损 | 盘中 | 现价低于止损价 |
| ⚠️ 接近止损 | 盘中 | 现价距止损价不到 5%（`NEAR_STOP`） |
| 📈 盘中突破入场位 | 盘中 | 空仓的币突破肯特纳上轨，收盘站稳会加仓 |
| 📉 盘中跌破离场位 | 盘中 | 持仓的币跌破肯特纳中线或 PBX 慢线，收盘不收回会减仓 |
| ⚠️ 系统告警 | 出错时 | 日线计算失败、实时价格连续拉取失败 |

### 命令（在和机器人的对话里发送）
- `/status`：汇总
- `/pos`：当前持仓、敞口、止损价与距离
- `/c BTC`：单个币的详细状态
- `/alerts`：当前盘中预警
- `/help`：帮助

机器人只响应 `TG_CHAT_ID` 里配置的对话。

## 部署到服务器

1. 用 systemd 保持常驻：修改 `trend-monitor.service` 里的用户和路径，然后
   ```bash
   sudo cp dashboard/trend-monitor.service /etc/systemd/system/
   sudo systemctl enable --now trend-monitor
   journalctl -u trend-monitor -f      # 查看日志
   ```
2. 默认只监听 `127.0.0.1`。如果要从外网访问：
   - 推荐用 SSH 隧道：`ssh -L 8765:127.0.0.1:8765 你的服务器`，然后在本机打开 http://127.0.0.1:8765/
   - 或者 `--host 0.0.0.0` 并在 `.env` 设置 `DASH_TOKEN`，访问 `http://服务器IP:8765/?token=xxx`。
     这种方式是明文 HTTP，建议放在 Nginx + HTTPS 后面。
3. 如果服务器在美国，`api.binance.com` 会返回 451。本服务只用币安的公开行情域名 `data-api.binance.vision`，不受影响。

## 文件

| 文件 | 作用 |
|---|---|
| `monitor.py` | 常驻服务：网页、实时价格、日线重算、Telegram 推送与命令 |
| `build.py` | 信号计算（`compute()`），也可单独运行生成静态快照 `index.html` |
| `template.html` | 页面模板；有内嵌数据时显示静态快照，否则从 `/api/data` 拉取实时数据 |
| `.env.example` | 配置模板 |
| `trend-monitor.service` | systemd 服务示例 |

`.monitor_state.json` 记录上一次的仓位和已发送的通知，用来在重启后避免重复推送，删除即可重置。
币种名单来自 `backtest/results/universe.csv`，重跑 `backtest/universe.py` 后重启服务即可生效。
