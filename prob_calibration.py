# -*- coding: utf-8 -*-
"""概率校准: 把 pooled ML 模型输出的概率校准到真实上涨频率。
诊断指标: 可靠性曲线(reliability curve)、Brier score、ECE(期望校准误差)。
方法: IsotonicRegression(单调非参数校准), walk-forward 收集 (预测概率, 真实标签)。"""
import numpy as np
from pooled_ml_forecast import make_features, TARGETS, WARMUP, RETRAIN_DAYS
from backtest_rules import _get_kline, parse_klines, UNIVERSE
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.isotonic import IsotonicRegression

def brier(p, y):
    return float(np.mean((np.array(p) - np.array(y)) ** 2))

def ece(p, y, n_bins=10):
    p = np.array(p); y = np.array(y)
    bins = np.linspace(0, 1, n_bins + 1)
    total = len(p); err = 0.0
    for i in range(n_bins):
        mask = (p >= bins[i]) & (p < bins[i + 1])
        if mask.sum() == 0:
            continue
        frac = mask.sum() / total
        err += frac * abs(p[mask].mean() - y[mask].mean())
    return float(err)

def reliability(p, y, n_bins=10):
    p = np.array(p); y = np.array(y)
    bins = np.linspace(0, 1, n_bins + 1)
    out = []
    for i in range(n_bins):
        mask = (p >= bins[i]) & (p < bins[i + 1])
        if mask.sum() == 0:
            continue
        out.append(((bins[i] + bins[i + 1]) / 2, mask.sum(), float(y[mask].mean())))
    return out

def main():
    print("概率校准 · pooled ML · walk-forward")
    print("=" * 78)
    samples = []
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
    samples.sort(key=lambda s: s[0])
    dates = sorted(set(s[0] for s in samples))
    retrain_dates = set(dates[::RETRAIN_DAYS])

    model = None
    pred_probs = []   # (date, p_up, actual)
    for dt in dates:
        day_samps = [s for s in samples if s[0] == dt]
        if not day_samps:
            continue
        if model is None or dt in retrain_dates:
            train_X = np.array([s[1] for s in samples if s[0] < dt], dtype=float)
            train_y = np.array([s[2] for s in samples if s[0] < dt], dtype=int)
            if len(train_X) < 500:
                continue
            model = HistGradientBoostingClassifier(
                max_iter=200, max_depth=6, learning_rate=0.08,
                min_samples_leaf=30, random_state=42)
            model.fit(train_X, train_y)
        for s in day_samps:
            if s[3] not in TARGETS:
                continue
            p_up = float(model.predict_proba(np.array([s[1]], dtype=float))[0][1])
            pred_probs.append((s[0], p_up, s[2]))

    p = np.array([x[1] for x in pred_probs])
    y = np.array([x[2] for x in pred_probs])
    print(f"预测样本: {len(p)}  真实上涨率基线: {y.mean()*100:.1f}%")

    print("\n=== 原始模型可靠性曲线(分10桶) ===")
    for mid, n, freq in reliability(p, y):
        bar = '█' * int(freq * 40)
        print(f"  预测概率≈{mid:.2f}: n={n:<4} 实际上涨率={freq*100:5.1f}% {bar}")

    print(f"\n原始 Brier score = {brier(p, y):.4f}")
    print(f"原始 ECE = {ece(p, y):.4f}")

    # isotonic 校准
    iso = IsotonicRegression(out_of_bounds='clip', y_min=0.0, y_max=1.0)
    iso.fit(p, y)
    p_cal = iso.predict(p)

    print("\n=== Isotonic 校准后可靠性曲线 ===")
    for mid, n, freq in reliability(p_cal, y):
        bar = '█' * int(freq * 40)
        print(f"  校准概率≈{mid:.2f}: n={n:<4} 实际上涨率={freq*100:5.1f}% {bar}")

    print(f"\n校准后 Brier score = {brier(p_cal, y):.4f}")
    print(f"校准后 ECE = {ece(p_cal, y):.4f}")

    # 校准函数的形态: 原始概率 -> 校准概率 的几个锚点
    print("\n=== 校准映射(原始概率→校准概率) ===")
    for x in (0.30, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        print(f"  原始 {x:.2f} → 校准 {float(iso.predict([x])[0]):.3f}")

if __name__ == "__main__":
    main()
