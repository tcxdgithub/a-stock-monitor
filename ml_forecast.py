# -*- coding: utf-8 -*-
"""日线级预测：机器学习前向验证。

只用 OHLCV（价格+成交量）提取特征，梯度提升树预测 T+1 涨跌，
严格 walk-forward（训练窗口永远在预测窗口之前）验证真实样本外准确率。
目标：挑战日线方向预测的极限，用数据说话，看能逼近多少。
"""
import numpy as np
from backtest_rules import _get_kline, parse_klines
from sklearn.ensemble import HistGradientBoostingClassifier

CODES = ['600611', '600702', '000568', '603605']
WARMUP = 250        # 首次训练所需最少K线
RETRAIN = 15        # 每15个交易日重训一次模型


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
        # 收益率(1/2/3/5/10/20日)
        for p in (1, 2, 3, 5, 10, 20):
            f.append((c / closes[i - p] - 1) if i >= p else 0.0)
        # 均线偏离
        for p in (5, 10, 20):
            f.append((c / closes[i - p + 1:i + 1].mean() - 1) if i >= p else 0.0)
        # 均线排列
        if i >= 20:
            ma5 = closes[i - 4:i + 1].mean()
            ma10 = closes[i - 9:i + 1].mean()
            ma20 = closes[i - 19:i + 1].mean()
            f.append(1.0 if ma5 > ma10 > ma20 else (-1.0 if ma5 < ma10 < ma20 else 0.0))
        else:
            f.append(0.0)
        # 量比(5/20日)
        for p in (5, 20):
            f.append((v / (vols[i - p + 1:i + 1].mean() + 1e-9) - 1) if i >= p else 0.0)
        # K线形态(实体/上影/下影/振幅)
        body = (c - o) / o if o > 0 else 0
        upper = (h - max(o, c)) / o if o > 0 else 0
        lower = (min(o, c) - l) / o if o > 0 else 0
        rng = (h - l) / o if o > 0 else 0
        f.extend([body, upper, lower, rng])
        # 波动率(5/20日)
        for p in (5, 20):
            if i >= p + 1:
                rets = closes[i - p + 1:i + 1] / closes[i - p:i] - 1
                f.append(float(np.std(rets)))
            else:
                f.append(0.0)
        # 20日相对位置
        if i >= 20:
            lo = lows[i - 19:i + 1].min()
            hi = highs[i - 19:i + 1].max()
            f.append((c - lo) / (hi - lo + 1e-9))
        else:
            f.append(0.0)
        # 距20日最高/最低
        if i >= 20:
            f.append(c / highs[i - 19:i + 1].max() - 1)
            f.append(c / lows[i - 19:i + 1].min() - 1)
        else:
            f.extend([0.0, 0.0])
        # 连续涨跌天数
        streak = 0
        j = i
        while j >= 1 and (closes[j] - closes[j - 1]) * (closes[i] - closes[i - 1]) > 0:
            streak += 1
            j -= 1
        f.append(float(streak))
        feats.append(f)
    return np.array(feats, dtype=float)


def main():
    print("日线级预测 · 机器学习 walk-forward 前向验证")
    print("模型: HistGradientBoosting | 特征: OHLCV提取" + str(make_features(parse_klines(_get_kline('sh600611', 300)[1]))[0].shape[0]) + "维")
    print("=" * 78)
    all_true, all_total = 0, 0
    per_stock = {}
    for code in CODES:
        symbol = ('sh' if code.startswith(('6', '9')) else 'sz') + code
        name, kl = _get_kline(symbol, days=800)
        data = parse_klines(kl)
        if len(data) < WARMUP + 30:
            print(code, "数据不足", len(data))
            continue
        closes = np.array([d['close'] for d in data], dtype=float)
        X = make_features(data)
        y = np.array([1 if closes[i + 1] > closes[i] else 0 for i in range(len(data) - 1)], dtype=int)
        correct = total = 0
        # walk-forward: 从 WARMUP 开始，每 RETRAIN 天重训，预测当天之后的每一天
        model = None
        for t in range(WARMUP, len(data) - 1):
            if model is None or (t - WARMUP) % RETRAIN == 0:
                model = HistGradientBoostingClassifier(
                    max_iter=200, max_depth=5, learning_rate=0.08,
                    min_samples_leaf=20, random_state=42)
                model.fit(X[:t], y[:t])
            pred = model.predict(X[t:t + 1])[0]
            actual = y[t]
            if pred == actual:
                correct += 1
            total += 1
        acc = correct / total * 100
        per_stock[code] = (name, correct, total, acc)
        all_true += correct
        all_total += total
        print(f"{name} {code}: 次日方向准确率 {correct}/{total} = {acc:.1f}%")
    print("=" * 78)
    print(f"四只票汇总: {all_true}/{all_total} = {all_true / all_total * 100:.1f}%")
    print(f"基准(随机): 50% | 基准(全预测涨): 四票次日上涨占比待算")


if __name__ == "__main__":
    main()
