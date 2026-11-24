# -*- coding: utf-8 -*-
"""日线级预测 · pooled 机器学习 walk-forward。

用全市场103只票的 OHLCV 特征做 pooled 训练，让模型学"通用量价→次日涨跌"规律，
严格按日期 walk-forward（训练集日期永远 < 预测日期），评估四只目标票的真实准确率。
这是日线方向预测的极限挑战：如果 pooled 也上不去 55%，则证明单票日线接近随机。
"""
import numpy as np
from backtest_rules import _get_kline, parse_klines, UNIVERSE
from sklearn.ensemble import HistGradientBoostingClassifier

TARGETS = ['600611', '600702', '000568', '603605']
WARMUP = 250
RETRAIN_DAYS = 15


def make_features(data):
    closes = np.array([d['close'] for d in data], dtype=float)
    opens = np.array([d['open'] for d in data], dtype=float)
    highs = np.array([d['high'] for d in data], dtype=float)
    lows = np.array([d['low'] for d in data], dtype=float)
    vols = np.array([d['volume'] for d in data], dtype=float)
    n = len(data)
    feats = []
    for i in range(n):
        f = []
        c, o, h, l, v = closes[i], opens[i], highs[i], lows[i], vols[i]
        for p in (1, 2, 3, 5, 10, 20):
            f.append((c / closes[i - p] - 1) if i >= p else 0.0)
        for p in (5, 10, 20):
            f.append((c / closes[i - p + 1:i + 1].mean() - 1) if i >= p else 0.0)
        if i >= 20:
            ma5 = closes[i - 4:i + 1].mean()
            ma10 = closes[i - 9:i + 1].mean()
            ma20 = closes[i - 19:i + 1].mean()
            f.append(1.0 if ma5 > ma10 > ma20 else (-1.0 if ma5 < ma10 < ma20 else 0.0))
        else:
            f.append(0.0)
        for p in (5, 20):
            f.append((v / (vols[i - p + 1:i + 1].mean() + 1e-9) - 1) if i >= p else 0.0)
        body = (c - o) / o if o > 0 else 0
        upper = (h - max(o, c)) / o if o > 0 else 0
        lower = (min(o, c) - l) / o if o > 0 else 0
        rng = (h - l) / o if o > 0 else 0
        f.extend([body, upper, lower, rng])
        for p in (5, 20):
            if i >= p + 1:
                rets = closes[i - p + 1:i + 1] / closes[i - p:i] - 1
                f.append(float(np.std(rets)))
            else:
                f.append(0.0)
        if i >= 20:
            lo = lows[i - 19:i + 1].min()
            hi = highs[i - 19:i + 1].max()
            f.append((c - lo) / (hi - lo + 1e-9))
            f.append(c / highs[i - 19:i + 1].max() - 1)
            f.append(c / lows[i - 19:i + 1].min() - 1)
        else:
            f.extend([0.0, 0.0, 0.0])
        streak = 0
        j = i
        while j >= 1 and (closes[j] - closes[j - 1]) * (closes[i] - closes[i - 1]) > 0:
            streak += 1
            j -= 1
        f.append(float(streak))
        feats.append(f)
    return np.array(feats, dtype=float)


def main():
    print("pooled 日线预测 · 全市场训练 · 严格按日期 walk-forward")
    print("=" * 78)
    samples = []  # (date, features, label, code)
    for code in UNIVERSE:
        symbol = ('sh' if code.startswith(('6', '9')) else 'sz') + code
        name, kl = _get_kline(symbol, days=800)
        data = parse_klines(kl)
        if len(data) < WARMUP + 30:
            continue
        X = make_features(data)
        closes = np.array([d['close'] for d in data], dtype=float)
        for i in range(WARMUP, len(data) - 1):
            label = 1 if closes[i + 1] > closes[i] else 0
            samples.append((data[i]['date'], X[i], label, code))
    print(f"样本总数: {len(samples)}（{len(UNIVERSE)}只票，各约{len(data) if 'data' in dir() else '?'}天）")
    samples.sort(key=lambda s: s[0])

    # 按日期切分 walk-forward
    dates = sorted(set(s[0] for s in samples))
    retrain_dates = dates[::RETRAIN_DAYS]
    model = None
    trained_upto = None
    # 目标票统计
    tgt = {c: {'hit': 0, 'n': 0} for c in TARGETS}
    all_hit = all_n = 0
    # 置信度分桶: 概率区间 -> [n, 实际上涨次数]
    buckets = [(0, 0.45), (0.45, 0.48), (0.48, 0.52), (0.52, 0.55), (0.55, 0.6), (0.6, 0.65), (0.65, 1.01)]
    prob_stat = {b: [0, 0] for b in buckets}
    train_X, train_y = [], []
    # 逐日推进
    for di, dt in enumerate(dates):
        # 当日样本
        day_samps = [s for s in samples if s[0] == dt]
        if not day_samps:
            continue
        # 需要重训吗
        need_retrain = (model is None) or (dt in retrain_dates)
        if need_retrain:
            # 重建训练集：所有日期 < dt 的样本
            train_X = [s[1] for s in samples if s[0] < dt]
            train_y = [s[2] for s in samples if s[0] < dt]
            if len(train_X) < 500:
                continue
            model = HistGradientBoostingClassifier(
                max_iter=200, max_depth=6, learning_rate=0.08,
                min_samples_leaf=30, random_state=42)
            model.fit(np.array(train_X, dtype=float), np.array(train_y, dtype=int))
        # 预测当日样本
        for s in day_samps:
            if s[3] not in TARGETS:
                continue
            p_up = model.predict_proba(np.array([s[1]], dtype=float))[0][1]
            actual = s[2]
            pred = 1 if p_up >= 0.5 else 0
            if pred == actual:
                tgt[s[3]]['hit'] += 1
            tgt[s[3]]['n'] += 1
            if pred == actual:
                all_hit += 1
            all_n += 1
            for b in buckets:
                if b[0] <= p_up < b[1]:
                    prob_stat[b][0] += 1
                    if actual == 1:
                        prob_stat[b][1] += 1
                    break
    print("\n四只目标票 pooled 模型准确率:")
    for c in TARGETS:
        if tgt[c]['n']:
            print(f"  {c}: {tgt[c]['hit']}/{tgt[c]['n']} = {tgt[c]['hit'] / tgt[c]['n'] * 100:.1f}%")
    print(f"\n目标票汇总: {all_hit}/{all_n} = {all_hit / all_n * 100:.1f}%")
    print("\n置信度分桶(概率越高,次日真实上涨率应越高):")
    for b in buckets:
        n, wins = prob_stat[b]
        if n > 0:
            print(f"  概率 {b[0]:.2f}-{b[1]:.2f}: n={n:<4} 次日上涨率={wins / n * 100:.1f}%")


if __name__ == "__main__":
    main()
