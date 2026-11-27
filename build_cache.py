# -*- coding: utf-8 -*-
"""一次性下载103只票K线, 存成pickle缓存, 后续统计/归因/压力测试脚本复用, 避免重复下载限流。"""
import pickle, sys, time
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import _get_kline, UNIVERSE

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'

cache = {}
for i, code in enumerate(UNIVERSE):
    symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
    try:
        name, kl = _get_kline(symbol, 800)
    except Exception:
        kl = []
    cache[code] = kl
    if (i + 1) % 10 == 0:
        print(f"已下载 {i + 1}/{len(UNIVERSE)}", flush=True)
    time.sleep(0.1)

with open(CACHE_PATH, 'wb') as f:
    pickle.dump(cache, f)
ok = sum(1 for v in cache.values() if len(v) > 200)
print(f"缓存完成: {ok}/{len(UNIVERSE)} 只有效K线")
