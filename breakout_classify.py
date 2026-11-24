# -*- coding: utf-8 -*-
"""突破箱顶真假预判：收集全市场突破样本，提取当天特征，看能否区分真/假突破。
特征(突破当天可观察):
  - 突破幅度 = (close-box_top)/box_top
  - 量比 = vol/mean(vol[-20:])
  - 箱体宽度 = (box_top-box_bot)/box_bot
  - 突破前位置 = (昨收-昨箱底)/(昨箱顶-昨箱底)
  - 突破K线实体占比 = (close-open)/(high-low)
标签: 真突破=突破后5日收盘不回箱顶下方; 假突破=回箱。
先分桶看真假率, 再上机器学习 walk-forward 预测。"""
import sys, datetime
import numpy as np
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import _get_kline, UNIVERSE

N = 20

def parse(klines):
    out = []
    for k in klines:
        try:
            datetime.datetime.strptime(k[0], '%Y-%m-%d')
        except Exception:
            continue
        out.append({'date': k[0], 'open': float(k[1]), 'close': float(k[2]),
                    'high': float(k[3]), 'low': float(k[4]), 'vol': float(k[5])})
    return out

samples = []  # (date, features dict, is_true)

def collect(data):
    n = len(data)
    for T in range(N, n):
        win = data[T-N:T]
        box_top = max(d['high'] for d in win)
        box_bot = min(d['low'] for d in win)
        close = data[T]['close']
        prev = data[T-1]
        if T-1 >= N:
            pwin = data[T-1-N:T-1]
            ptop = max(d['high'] for d in pwin)
            pbot = min(d['low'] for d in pwin)
        else:
            ptop = pbot = None
        if ptop is None:
            continue
        # 突破: 昨收<=昨箱顶 且 今收>今箱顶
        if prev['close'] <= ptop and close > box_top:
            # 真/假: 5日内是否回箱顶下方
            is_true = True
            for j in range(1, 6):
                if T + j < n and data[T+j]['close'] < box_top:
                    is_true = False
                    break
            # 特征
            amp = (close - box_top) / box_top
            vol_mean = sum(d['vol'] for d in win) / N
            vol_ratio = data[T]['vol'] / vol_mean if vol_mean > 0 else 1.0
            width = (box_top - box_bot) / box_bot if box_bot > 0 else 0
            pos_prev = (prev['close'] - pbot) / (ptop - pbot) if ptop > pbot else 0.5
            rng = data[T]['high'] - data[T]['low']
            body = (close - data[T]['open']) / rng if rng > 0 else 0
            feat = [amp, vol_ratio, width, pos_prev, body]
            fwd5 = (data[T+5]['close'] - close) / close if T + 5 < n else None
            samples.append((data[T]['date'], feat, 1 if is_true else 0, fwd5))

if __name__ == '__main__':
    for code in UNIVERSE:
        symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
        name, klines = _get_kline(symbol, 800)
        if len(klines) < 100:
            continue
        collect(parse(klines))
    print(f"突破样本总数: {len(samples)}  真突破: {sum(1 for s in samples if s[2]==1)}  假突破: {sum(1 for s in samples if s[2]==0)}")

    # 分桶: 量比
    print("\n=== 按量比分桶 (真突破率) ===")
    for lo, hi, lbl in [(0,1,'缩量(<1)'),(1,1.5,'温和(1-1.5)'),(1.5,2,'放量(1.5-2)'),(2,5,'巨量(>2)')]:
        sub = [s for s in samples if lo <= s[1][1] < hi]
        if sub:
            tr = sum(1 for s in sub if s[2])/len(sub)*100
            print(f"  {lbl}: n={len(sub):<5} 真突破率={tr:.1f}%")
    # 分桶: 突破幅度
    print("\n=== 按突破幅度分桶 ===")
    for lo, hi, lbl in [(0,0.02,'勉强(<2%)'),(0.02,0.05,'温和(2-5%)'),(0.05,0.10,'强势(5-10%)'),(0.10,1,'极强(>10%)')]:
        sub = [s for s in samples if lo <= s[1][0] < hi]
        if sub:
            tr = sum(1 for s in sub if s[2])/len(sub)*100
            print(f"  {lbl}: n={len(sub):<5} 真突破率={tr:.1f}%")
    # 分桶: 突破前位置
    print("\n=== 按突破前位置分桶 ===")
    for lo, hi, lbl in [(0,0.5,'箱底半区(<0.5)'),(0.5,0.8,'中上(0.5-0.8)'),(0.8,1.1,'箱顶附近(>0.8)')]:
        sub = [s for s in samples if lo <= s[1][3] < hi]
        if sub:
            tr = sum(1 for s in sub if s[2])/len(sub)*100
            print(f"  {lbl}: n={len(sub):<5} 真突破率={tr:.1f}%")

    # 机器学习: HistGradientBoosting, 按日期 walk-forward
    print("\n=== 机器学习真假突破预测 (按日期 walk-forward) ===")
    samples.sort(key=lambda s: s[0])
    dates = sorted(set(s[0] for s in samples))
    X = np.array([s[1] for s in samples], dtype=float)
    y = np.array([s[2] for s in samples], dtype=int)
    from sklearn.ensemble import HistGradientBoostingClassifier
    # 按日期切: 训练集=日期早于当前, 测试=当前日期
    correct = total = 0
    retrain_dates = set(dates[::30])
    model = None
    # 预测结果分组: 预测真/假 -> 未来5日收益
    pred_true_rets = []
    pred_false_rets = []
    tp = fp = tn = fn = 0
    for d in dates:
        test_idx = [i for i, s in enumerate(samples) if s[0] == d]
        if not test_idx:
            continue
        train_idx = [i for i, s in enumerate(samples) if s[0] < d]
        if len(train_idx) < 300:
            continue
        if model is None or d in retrain_dates:
            model = HistGradientBoostingClassifier(max_iter=150, max_depth=5, learning_rate=0.1, min_samples_leaf=20, random_state=42)
            model.fit(X[train_idx], y[train_idx])
        pred = model.predict(X[test_idx])
        for i, p in zip(test_idx, pred):
            actual = y[i]
            if p == 1 and actual == 1: tp += 1
            elif p == 1 and actual == 0: fp += 1
            elif p == 0 and actual == 0: tn += 1
            else: fn += 1
            if samples[i][3] is not None:
                if p == 1:
                    pred_true_rets.append(samples[i][3])
                else:
                    pred_false_rets.append(samples[i][3])
            correct += (1 if p == actual else 0)
            total += 1
    print(f"  walk-forward 真假突破准确率: {correct}/{total} = {correct/total*100:.1f}%  (基准: 全预测真突破={sum(y)/len(y)*100:.1f}%)")
    prec = tp/(tp+fp) if tp+fp else 0
    rec = tp/(tp+fn) if tp+fn else 0
    print(f"  预测'真突破'精确率: {prec*100:.1f}%  召回率: {rec*100:.1f}%")
    print(f"\n=== 策略收益验证 ===")
    if pred_true_rets:
        avg = sum(pred_true_rets)/len(pred_true_rets)*100
        win = sum(1 for r in pred_true_rets if r > 0)/len(pred_true_rets)*100
        print(f"  模型预测'真突破'→买入: n={len(pred_true_rets)}  未来5日平均={avg:+.2f}%  上涨率={win:.1f}%")
    if pred_false_rets:
        avg = sum(pred_false_rets)/len(pred_false_rets)*100
        win = sum(1 for r in pred_false_rets if r > 0)/len(pred_false_rets)*100
        print(f"  模型预测'假突破'→不买/规避: n={len(pred_false_rets)}  未来5日平均={avg:+.2f}%  上涨率={win:.1f}%")
