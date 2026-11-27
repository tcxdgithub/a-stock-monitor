# -*- coding: utf-8 -*-
"""突破信号提纯: 按"突破幅度 x 量比"分层, 找扣成本后仍显著为正的信号层。
突破信号 = 收盘价上穿20日箱顶。统计每层: 样本数/胜率/单笔(扣0.3%成本)/t值/p值。"""
import pickle, sys, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import parse_klines

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003
N = 20
HOLD = 5

def collect(klines):
    """返回 [(amp, vol_ratio, ret5)] amp=突破幅度, vol_ratio=量比, ret5=扣成本5日收益"""
    data = parse_klines(klines)
    n = len(data)
    if n < N + HOLD + 2:
        return []
    out = []
    for T in range(N + 1, n - HOLD):
        win = data[T-N:T]
        box_top = max(d['high'] for d in win)
        cur = data[T]['close']
        prev = data[T-1]['close']
        pwin = data[T-1-N:T-1]
        ptop = max(d['high'] for d in pwin)
        if prev <= ptop and cur > box_top:
            amp = (cur - ptop) / ptop if ptop > 0 else 0
            vol_ratio = data[T]['volume'] / (np.mean([d['volume'] for d in win]) + 1e-9)
            exit_price = data[T+HOLD]['close']
            ret5 = (exit_price - cur) / cur - COST
            out.append((amp, vol_ratio, ret5))
    return out

def summarize(label, rets):
    rets = np.array(rets)
    n = len(rets)
    if n < 10:
        return f"{label:<22} n={n:<5} (样本不足)"
    win = (rets > 0).mean() * 100
    mean = rets.mean() * 100
    t, p = stats.ttest_1samp(rets, 0)
    # 符号说明: p<0.05显著
    return f"{label:<22} n={n:<5} 胜率{win:>5.1f}% 单笔{mean:>+6.2f}% t={t:>+5.2f} p={p:.4f}"

def main():
    with open(CACHE_PATH, 'rb') as f:
        cache = pickle.load(f)
    all_rows = []
    for code, kl in cache.items():
        all_rows.extend(collect(kl))
    amp = np.array([r[0] for r in all_rows])
    vr = np.array([r[1] for r in all_rows])
    ret = np.array([r[2] for r in all_rows])
    print(f"突破信号总数: {len(all_rows)}\n")

    print("=== 按突破幅度分层 ===")
    for lo, hi in [(0, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1.0)]:
        m = (amp >= lo) & (amp < hi)
        print(summarize(f"幅度[{lo*100:.0f}%~{hi*100:.0f}%)", ret[m]))

    print("\n=== 按量比分层 ===")
    for lo, hi in [(0, 1.0), (1.0, 1.5), (1.5, 2.0), (2.0, 10.0)]:
        m = (vr >= lo) & (vr < hi)
        print(summarize(f"量比[{lo:.1f}~{hi:.1f})", ret[m]))

    print("\n=== 交叉分层(幅度 x 量比) ===")
    for a_lo, a_hi in [(0, 0.03), (0.03, 0.05), (0.05, 1.0)]:
        for v_lo, v_hi in [(0, 1.5), (1.5, 10.0)]:
            m = (amp >= a_lo) & (amp < a_hi) & (vr >= v_lo) & (vr < v_hi)
            print(summarize(f"幅{a_lo*100:.0f}-{a_hi*100:.0f}% 量{v_lo:.1f}-{v_hi:.1f}", ret[m]))

if __name__ == "__main__":
    main()
