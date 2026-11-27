# -*- coding: utf-8 -*-
"""周线事件因子漏斗验证: 突破20周高/跌破20周低/金叉/死叉, 训练段vs测试段。"""
import pickle, sys, json, datetime, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')
from factor_funnel import build_weekly, load_graveyard, save_graveyard, GRAVEYARD

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003
HOLD = 4

def ema(vals, n):
    k = 2/(n+1); out=[]; e=None
    for v in vals:
        e = v if e is None else v*k + e*(1-k)
        out.append(e)
    return out

def collect_events(weeks):
    """返回 {event_name: [(date, ret4)]}"""
    closes = [w['close'] for w in weeks]
    highs = [w['high'] for w in weeks]
    lows = [w['low'] for w in weeks]
    n = len(weeks)
    if n < 40: return {}
    ema12 = ema(closes, 12); ema26 = ema(closes, 26)
    dif = [ema12[i]-ema26[i] for i in range(n)]
    dea = ema(dif, 9)
    out = {'突破20周高': [], '跌破20周低': [], '金叉': [], '死叉': []}
    for i in range(20, n-HOLD):
        c = closes[i]
        box_top = max(highs[i-20:i])
        box_bot = min(lows[i-20:i])
        ret4 = (closes[i+HOLD] - c)/c - COST
        if c > max(highs[i-20:i]):
            out['突破20周高'].append((weeks[i]['date'], ret4))
        if c < min(lows[i-20:i]):
            out['跌破20周低'].append((weeks[i]['date'], ret4))
        if dif[i-1] <= dea[i-1] and dif[i] > dea[i]:
            out['金叉'].append((weeks[i]['date'], ret4))
        if dif[i-1] >= dea[i-1] and dif[i] < dea[i]:
            out['死叉'].append((weeks[i]['date'], ret4))
    return out

def main():
    with open(CACHE_PATH,'rb') as f:
        cache = pickle.load(f)
    all_events = {k: [] for k in ['突破20周高','跌破20周低','金叉','死叉']}
    for code, kl in cache.items():
        ev = collect_events(build_weekly(kl))
        for k in ev:
            all_events[k].extend(ev[k])
    # 按日期切分
    for k in all_events:
        all_events[k].sort(key=lambda x: x[0])
    all_dates = sorted(set(d for ev in all_events.values() for d,_ in ev))
    mid_date = all_dates[len(all_dates)//2]
    print(f"事件切分点: {mid_date}\n")

    graveyard = load_graveyard()
    print(f"{'事件':<10} {'训练段':>18} {'测试段':>18} {'结论':>6}")
    print('-'*70)
    for k in ['突破20周高','跌破20周低','金叉','死叉']:
        ev = all_events[k]
        tr = [r for d,r in ev if d < mid_date]
        te = [r for d,r in ev if d >= mid_date]
        def stat(arr, label):
            if len(arr) < 10: return f"n={len(arr)}不足"
            a = np.array(arr)
            t,p = stats.ttest_1samp(a, 0)
            return f"n={len(a)} {a.mean()*100:+.2f}% p={p:.3f}"
        s_tr = stat(tr, 'train'); s_te = stat(te, 'test')
        # 判定: 测试段做多收益>0且p<0.05, 且训练/测试同号
        status = 'dead'
        if len(tr)>=10 and len(te)>=10:
            tr_a = np.array(tr); te_a = np.array(te)
            _, p_te = stats.ttest_1samp(te_a, 0)
            same_sign = (tr_a.mean() * te_a.mean()) > 0
            if te_a.mean() > 0 and p_te < 0.05 and same_sign:
                status = 'alive'
        graveyard[f'weekly_event_{k}'] = {
            'freq':'weekly','data':'price_volume','status':status,
            'reason': f'训练{s_tr} 测试{s_te}',
            'evidence':['factor_events.py']}
        print(f"{k:<10} {s_tr:>18} {s_te:>18} {status:>6}")
    save_graveyard(graveyard)
    print(f"\n因子墓地已更新(共{len(graveyard)}条)")

if __name__ == "__main__":
    main()
