# -*- coding: utf-8 -*-
"""分层收益 + 多重检验校正: 重点验证低振幅/低波动因子做多是否可交易。
每天按因子值5分位, 算每档未来5日收益(扣0.3%成本)。多空=Q1-Q5。做多只看Q1绝对收益。"""
import pickle, sys, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import parse_klines
from factor_zoo import compute_factors

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'

def panel():
    with open(CACHE_PATH,'rb') as f:
        cache = pickle.load(f)
    p = {}
    for code, kl in cache.items():
        dates, factors, fwd = compute_factors(parse_klines(kl))
        for i, d in enumerate(dates):
            if np.isnan(fwd[i]):
                continue
            p.setdefault(d, {})[code] = ({k: factors[k][i] for k in factors}, fwd[i])
    return p

def layer_ret(p, factor, dates):
    """按因子值每天分5档, 返回 {Q1:[ret],...,Q5:[ret]} 及多空序列"""
    buckets = {q: [] for q in range(1,6)}
    ls = []  # 多空 Q1-Q5 每日
    for d in dates:
        day = p[d]
        rows = [(day[c][0][factor], day[c][1]) for c in day if not np.isnan(day[c][0][factor])]
        if len(rows) < 20: continue
        rows.sort(key=lambda x: x[0])
        n = len(rows)
        for q in range(1,6):
            lo = (q-1)*n//5; hi = q*n//5
            if hi > lo:
                buckets[q].extend([r[1] for r in rows[lo:hi]])
        q1 = [r[1] for r in rows[:n//5]]
        q5 = [r[1] for r in rows[-n//5:]]
        ls.append(np.mean(q1) - np.mean(q5))
    return buckets, ls

def main():
    p = panel()
    all_dates = sorted(p)
    mid = len(all_dates)//2
    train_dates, test_dates = all_dates[:mid], all_dates[mid:]
    print(f"训练段 {train_dates[0]}~{train_dates[-1]} ({len(train_dates)}日)")
    print(f"测试段 {test_dates[0]}~{test_dates[-1]} ({len(test_dates)}日)\n")

    for factor in ['amp_20','vol_20','vr_20','vol_chg_20']:
        print(f"=== {factor} 分层收益(扣成本) ===")
        for label, dates in [('全样本', all_dates), ('训练段', train_dates), ('测试段', test_dates)]:
            buckets, ls = layer_ret(p, factor, dates)
            q1 = np.array(buckets[1]); q5 = np.array(buckets[5])
            ls = np.array(ls)
            t_ls, p_ls = stats.ttest_1samp(ls, 0)
            t_q1, p_q1 = stats.ttest_1samp(q1, 0)
            print(f"  {label:<4} Q1做多{q1.mean()*100:>+6.2f}%(p={p_q1:.4f})  Q5{q5.mean()*100:>+6.2f}%  多空Q1-Q5{ls.mean()*100:>+6.2f}%(p={p_ls:.4f})  n={len(q1)}")
        print()

    # 多重检验校正: 全因子做多Q1收益的 Bonferroni 检验
    print("=== 多重检验校正(Bonferroni) ===")
    all_factors = ['mom_1','mom_5','mom_20','mom_60','rev_1','rev_5','vol_20','vol_chg','amp_20','vr_5','vr_20','vol_chg_20','hl_dist','rsi_14','ma_dist','ma_slope']
    # 对每个因子, 测试段做多方向 = 训练段确定的方向
    results = []
    for fk in all_factors:
        # 训练段确定方向(全样本IC符号)
        b_tr, ls_tr = layer_ret(p, fk, train_dates)
        q1_tr = np.array(b_tr[1]); q5_tr = np.array(b_tr[5])
        # 方向: 如果Q1 > Q5 做多低值(做多Q1), 否则做多高值(做多Q5)
        long_is_q1 = q1_tr.mean() > q5_tr.mean()
        # 测试段
        b_te, _ = layer_ret(p, fk, test_dates)
        long_rets = np.array(b_te[1] if long_is_q1 else b_te[5])
        t_stat, p_val = stats.ttest_1samp(long_rets, 0)
        results.append((fk, long_is_q1, long_rets.mean()*100, p_val, len(long_rets)))
    # 排序按p值
    results.sort(key=lambda x: x[3])
    print(f"{'因子':<12} {'做多端':>6} {'测试段做多收益':>12} {'p值':>7} {'Bonferroni':>10} {'n':>6}")
    print('-'*60)
    m = len(results)
    for fk, is_q1, ret, p, n in results:
        bonf = p * m
        mark = '存活' if bonf < 0.05 else '死亡'
        side = 'Q1低值' if is_q1 else 'Q5高值'
        print(f"{fk:<12} {side:>6} {ret:>+11.2f}% {p:>7.4f} {bonf:>10.4f} {n:>6} {mark}")
    print(f"\n{m}个因子, 多重检验校正后存活: {sum(1 for r in results if r[3]*m < 0.05)}个")

if __name__ == "__main__":
    main()
