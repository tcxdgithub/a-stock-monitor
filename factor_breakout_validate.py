# -*- coding: utf-8 -*-
"""突破N周高 参数稳健性验证(门槛3): 换突破周期/持有期/分年, 确认是否过拟合。"""
import pickle, sys, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')
from factor_funnel import build_weekly

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003

def collect(weeks, N, HOLD):
    highs = [w['high'] for w in weeks]
    closes = [w['close'] for w in weeks]
    n = len(weeks)
    out = []
    for i in range(N, n-HOLD):
        if closes[i] > max(highs[i-N:i]):
            ret = (closes[i+HOLD] - closes[i])/closes[i] - COST
            out.append((weeks[i]['date'], ret))
    return out

def stat(arr):
    if len(arr) < 10: return f"n={len(arr)}不足"
    a = np.array(arr)
    t,p = stats.ttest_1samp(a, 0)
    return f"n={len(a):<4} {a.mean()*100:>+6.2f}% p={p:.4f}"

def main():
    with open(CACHE_PATH,'rb') as f:
        cache = pickle.load(f)
    print("=== 参数稳健性: 突破N周高 ===")
    print(f"{'突破周期':<8} {'持有期':<6} {'结果':<24}")
    print('-'*50)
    for N in [12, 16, 20, 24, 30]:
        for HOLD in [2, 3, 4, 5, 6]:
            rows = []
            for code, kl in cache.items():
                rows.extend(collect(build_weekly(kl), N, HOLD))
            print(f"{N}周       {HOLD}周    {stat([r for _,r in rows])}")

    print("\n=== 分年验证(突破20周高, 持有4周) ===")
    rows = []
    for code, kl in cache.items():
        rows.extend(collect(build_weekly(kl), 20, 4))
    by_year = {}
    for d, r in rows:
        by_year.setdefault(d[:4], []).append(r)
    for y in sorted(by_year):
        print(f"  {y}: {stat(by_year[y])}")

    print("\n=== 组合回测(突破20周高, 持有4周) ===")
    daily = {}
    for code, kl in cache.items():
        for d, r in collect(build_weekly(kl), 20, 4):
            daily.setdefault(d, []).append(r)
    dates = sorted(daily)
    port = np.array([np.mean(daily[d]) for d in dates])
    cum = 1.0; eq = [1.0]; peak = 1.0; mdd = 0
    for r in port:
        cum *= (1+r); eq.append(cum)
        peak = max(peak, cum); mdd = min(mdd, cum-peak)
    sharpe = port.mean()/port.std()*math.sqrt(52) if port.std()>0 else 0
    annual = cum**(52/len(dates))-1 if len(dates)>0 else 0
    print(f"  周线交易日{len(dates)} 累计{cum-1:+.1%} 年化{annual:+.1%} 夏普{sharpe:.2f} 回撤{mdd:.1%}")

if __name__ == "__main__":
    main()
