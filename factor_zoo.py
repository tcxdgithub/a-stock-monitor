# -*- coding: utf-8 -*-
"""日线因子动物园: 16个价量因子, 算横截面Spearman IC + 5分位分层收益(扣成本)。
输出: 每个因子的 IC均值/ICIR/t值/分层Q1-Q5收益差, 全样本 vs 前半 vs 后半。"""
import pickle, sys, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import parse_klines

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003
HOLD = 5

def compute_factors(data):
    """对单只票的日线序列, 返回 (dates, factor_matrix dict, fwd_ret5)"""
    n = len(data)
    closes = np.array([d['close'] for d in data])
    highs = np.array([d['high'] for d in data])
    lows = np.array([d['low'] for d in data])
    vols = np.array([d['volume'] for d in data])
    dates = [d['date'] for d in data]
    # 日收益
    ret = np.zeros(n); ret[1:] = closes[1:]/closes[:-1] - 1
    factors = {k: np.full(n, np.nan) for k in [
        'mom_1','mom_5','mom_20','mom_60','rev_1','rev_5',
        'vol_20','vol_chg','amp_20','vr_5','vr_20','vol_chg_20',
        'hl_dist','rsi_14','ma_dist','ma_slope']}
    for i in range(n):
        if i >= 1: factors['mom_1'][i] = closes[i]/closes[i-1] - 1
        if i >= 5: factors['mom_5'][i] = closes[i]/closes[i-5] - 1
        if i >= 20: factors['mom_20'][i] = closes[i]/closes[i-20] - 1
        if i >= 60: factors['mom_60'][i] = closes[i]/closes[i-60] - 1
        if i >= 1: factors['rev_1'][i] = -factors['mom_1'][i]
        if i >= 5: factors['rev_5'][i] = -factors['mom_5'][i]
        if i >= 20:
            factors['vol_20'][i] = np.std(ret[i-19:i+1])
            factors['vol_chg'][i] = factors['vol_20'][i] / factors['vol_20'][i-20] - 1 if i>=40 else np.nan
            factors['amp_20'][i] = np.mean((highs[i-19:i+1]-lows[i-19:i+1])/closes[i-19:i+1])
            factors['vr_20'][i] = vols[i] / np.mean(vols[i-19:i+1])
            factors['vol_chg_20'][i] = np.mean(vols[i-4:i+1])/np.mean(vols[i-19:i-4]) - 1
            ma20 = np.mean(closes[i-19:i+1])
            factors['ma_dist'][i] = closes[i]/ma20 - 1
            ma20_prev = np.mean(closes[i-24:i-4])
            factors['ma_slope'][i] = ma20/ma20_prev - 1 if i>=24 else np.nan
        if i >= 5:
            factors['vr_5'][i] = vols[i] / np.mean(vols[i-4:i+1])
        if i >= 250:
            factors['hl_dist'][i] = closes[i]/np.max(highs[i-249:i+1]) - 1
        if i >= 14:
            # RSI(14)
            diff = np.diff(closes[i-14:i+1])
            gain = np.where(diff>0, diff, 0).mean()
            loss = np.where(diff<0, -diff, 0).mean()
            factors['rsi_14'][i] = 100 - 100/(1 + gain/(loss+1e-9))
    fwd = np.full(n, np.nan)
    fwd[:n-HOLD] = closes[HOLD:]/closes[:n-HOLD] - 1 - COST
    return dates, factors, fwd

def main():
    with open(CACHE_PATH,'rb') as f:
        cache = pickle.load(f)
    # 构建面板: date -> {code -> (factor_vals, fwd)}
    panel = {}
    for code, kl in cache.items():
        dates, factors, fwd = compute_factors(parse_klines(kl))
        for i, d in enumerate(dates):
            if np.isnan(fwd[i]):
                continue
            panel.setdefault(d, {})[code] = ({k: factors[k][i] for k in factors}, fwd[i])
    all_dates = sorted(panel)
    mid = len(all_dates)//2
    print(f"面板: {len(all_dates)}个交易日, 每日约{len(panel[all_dates[0]])}只票\n")

    def ic_series(dates_sub):
        """返回 {factor: [每日横截面Spearman IC]}"""
        out = {k: [] for k in ['mom_1','mom_5','mom_20','mom_60','rev_1','rev_5','vol_20','vol_chg','amp_20','vr_5','vr_20','vol_chg_20','hl_dist','rsi_14','ma_dist','ma_slope']}
        for d in dates_sub:
            day = panel[d]
            codes = list(day.keys())
            if len(codes) < 20: continue
            for fk in out:
                xs = [day[c][0][fk] for c in codes]
                ys = [day[c][1] for c in codes]
                valid = [(x,y) for x,y in zip(xs,ys) if not np.isnan(x)]
                if len(valid) < 20: continue
                xs = [v[0] for v in valid]; ys = [v[1] for v in valid]
                ic, _ = stats.spearmanr(xs, ys)
                if not np.isnan(ic): out[fk].append(ic)
        return out

    def summarize(ics):
        print(f"{'因子':<12} {'日IC均值':>8} {'ICIR':>6} {'t值':>6} {'p值':>7}")
        print('-'*45)
        res = {}
        for fk, arr in ics.items():
            if len(arr) < 30:
                res[fk] = None; continue
            arr = np.array(arr)
            t, p = stats.ttest_1samp(arr, 0)
            res[fk] = (arr.mean(), arr.mean()/arr.std()*math.sqrt(len(arr)), t, p, len(arr))
            print(f"{fk:<12} {arr.mean()*100:>+7.3f}% {arr.mean()/arr.std()*math.sqrt(len(arr)):>6.2f} {t:>+6.2f} {p:>7.4f}")
        return res

    print("=== 全样本 IC ===")
    ics_all = ic_series(all_dates)
    res_all = summarize(ics_all)

    print("\n=== 前半样本 IC ===")
    ics_train = ic_series(all_dates[:mid])
    summarize(ics_train)

    print("\n=== 后半样本 IC ===")
    ics_test = ic_series(all_dates[mid:])
    res_test = summarize(ics_test)

    # 一致性: 前半IC符号 vs 后半IC符号
    print("\n=== 前半→后半符号一致性(过拟合检测) ===")
    flip = 0
    for fk in res_all:
        if res_all[fk] is None: continue
        sign_all = 1 if res_all[fk][0] > 0 else -1
        tr = ics_train.get(fk, []); te = ics_test.get(fk, [])
        if len(tr)>=30 and len(te)>=30:
            s_tr = 1 if np.mean(tr)>0 else -1
            s_te = 1 if np.mean(te)>0 else -1
            mark = '翻转' if s_tr != s_te else '一致'
            if s_tr != s_te: flip += 1
            print(f"  {fk:<12} 前半{s_tr:+d} 后半{s_te:+d}  {mark}")
    print(f"\n16个因子中方向翻转: {flip}个")

if __name__ == "__main__":
    main()
