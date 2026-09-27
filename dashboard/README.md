# 趋势信号看板

按币种显示 BOT_SPEC 中 S1–S4 的实时信号、目标仓位、止损价、BTC 联动和回测表现。只包含 A/B/C 级币种。

```bash
pip install -r backtest/requirements.txt
python dashboard/build.py          # 生成 dashboard/index.html，浏览器直接打开
```

- 每天 UTC 00:05 之后运行一次（日线收盘后），例如 crontab：`5 0 * * * cd /path/to/strategies && python dashboard/build.py`
- 币种名单和回测数据来自 `backtest/results/universe.csv`，重跑 `backtest/universe.py` 后看板会自动使用新名单。
- `template.html` 是页面模板，`build.py` 把数据以 JSON 形式嵌入模板。
