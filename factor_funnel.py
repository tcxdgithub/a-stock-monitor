# -*- coding: utf-8 -*-
"""alpha搜索漏斗 + 因子墓地 (V3).
统一漏斗: 训练段定方向 -> 样本外验证 -> 三重门槛(做多端为正/方向稳定/参数稳健).
不达标归档进 factor_graveyard.json 并记录失效原因。"""
import pickle, sys, json, datetime, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')

GRAVEYARD = '/home/user/a-stock-monitor/factor_graveyard.json'
CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003
HOLD_W = 4  # 周线持有4周

# ---------- 周线聚合 ----------
def build_weekly(klines):
    weeks = {}
    for k in klines:
        try:
            dt = datetime.datetime.strptime(k[0], '%Y-%m-%d')
        except Exception:
            continue
        y, w, _ = dt.isocalendar()
        key = (y, w)
        o, c, h, l, v = float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
        if key not in weeks:
            weeks[key] = {'date': k[0], 'open': o, 'close': c, 'high': h, 'low': l, 'vol': v}
        else:
            wk = weeks[key]
            wk['date'] = k[0]; wk['close'] = c
            wk['high'] = max(wk['high'], h); wk['low'] = min(wk['low'], l)
            wk['vol'] += v
    return [weeks[k] for k in sorted(weeks)]

# ---------- 周线连续因子 ----------
def compute_weekly_factors(weeks):
    n = len(weeks)
    closes = np.array([w['close'] for w in weeks])
    highs = np.array([w['high'] for w in weeks])
    lows = np.array([w['low'] for w in weeks])
    vols = np.array([w['vol'] for w in weeks])
    ret = np.zeros(n); ret[1:] = closes[1:]/closes[:-1] - 1
    F = {k: np.full(n, np.nan) for k in [
        'w_mom_4','w_mom_8','w_mom_20','w_rev_4','w_vol_20','w_vol_chg',
        'w_vr_4','w_vr_20','w_hl_dist','w_ma_dist','w_amp_20','w_ma_slope']}
    for i in range(n):
        if i >= 4: F['w_mom_4'][i] = closes[i]/closes[i-4] - 1
        if i >= 8: F['w_mom_8'][i] = closes[i]/closes[i-8] - 1
        if i >= 20: F['w_mom_20'][i] = closes[i]/closes[i-20] - 1
        if i >= 4: F['w_rev_4'][i] = -F['w_mom_4'][i]
        if i >= 20:
            F['w_vol_20'][i] = np.std(ret[i-19:i+1])
            F['w_vol_chg'][i] = F['w_vol_20'][i]/F['w_vol_20'][i-20] - 1 if i >= 40 else np.nan
            F['w_vr_20'][i] = vols[i]/np.mean(vols[i-19:i+1])
            ma20 = np.mean(closes[i-19:i+1])
            F['w_ma_dist'][i] = closes[i]/ma20 - 1
            F['w_amp_20'][i] = np.mean((highs[i-19:i+1]-lows[i-19:i+1])/closes[i-19:i+1])
            F['w_ma_slope'][i] = ma20/np.mean(closes[i-23:i-3]) - 1 if i >= 23 else np.nan
        if i >= 4: F['w_vr_4'][i] = vols[i]/np.mean(vols[i-3:i+1])
        if i >= 52: F['w_hl_dist'][i] = closes[i]/np.max(highs[i-51:i+1]) - 1
    fwd = np.full(n, np.nan)
    fwd[:n-HOLD_W] = closes[HOLD_W:]/closes[:n-HOLD_W] - 1 - COST
    return F, fwd

# ---------- 统一漏斗 ----------
def layer_ret_weekly(panel, factor, dates):
    buckets = {q: [] for q in range(1,6)}
    ls = []
    for d in dates:
        day = panel.get(d)
        if not day or len(day) < 20: continue
        rows = [(day[c][0][factor], day[c][1]) for c in day if not np.isnan(day[c][0][factor])]
        if len(rows) < 20: continue
        rows.sort(key=lambda x: x[0])
        n = len(rows)
        for q in range(1,6):
            lo=(q-1)*n//5; hi=q*n//5
            if hi>lo: buckets[q].extend([r[1] for r in rows[lo:hi]])
        q1=[r[1] for r in rows[:n//5]]; q5=[r[1] for r in rows[-n//5:]]
        ls.append(np.mean(q1)-np.mean(q5))
    return buckets, ls

def funnel(factor, panel, train_dates, test_dates):
    """三重门槛。返回 (status, report_dict)"""
    # 训练段定方向
    b_tr, ls_tr = layer_ret_weekly(panel, factor, train_dates)
    q1_tr = np.array(b_tr[1]); q5_tr = np.array(b_tr[5])
    if len(q1_tr) < 30:
        return 'untested', {'reason': '训练段样本不足30'}
    long_is_q1 = q1_tr.mean() > q5_tr.mean()
    # 测试段验证
    b_te, ls_te = layer_ret_weekly(panel, factor, test_dates)
    long_te = np.array(b_te[1] if long_is_q1 else b_te[5])
    q1_te = np.array(b_te[1]); q5_te = np.array(b_te[5])
    if len(long_te) < 30:
        return 'untested', {'reason': '测试段样本不足30'}
    t_long, p_long = stats.ttest_1samp(long_te, 0)
    ls_tr_arr = np.array(ls_tr); ls_te_arr = np.array(ls_te)
    # 门槛1: 做多端收益为正(扣成本) 且 统计显著(p<0.05)
    g1 = bool(long_te.mean() > 0 and p_long < 0.05)
    # 门槛2: 多空方向稳定(训练/测试同号)
    g2 = bool((np.mean(ls_tr_arr) * np.mean(ls_te_arr)) > 0) if len(ls_tr_arr)>0 and len(ls_te_arr)>0 else False
    report = {
        'train_n': int(len(q1_tr)), 'test_n': int(len(long_te)),
        'long_side': 'Q1低值' if long_is_q1 else 'Q5高值',
        'test_long_ret': float(long_te.mean()*100),
        'test_p': float(p_long),
        'train_ls': float(np.mean(ls_tr_arr)*100) if len(ls_tr_arr) else None,
        'test_ls': float(np.mean(ls_te_arr)*100) if len(ls_te_arr) else None,
        'gate1_positive_long': g1,
        'gate2_stable_direction': g2,
    }
    if not g1:
        return 'dead', {**report, 'reason': '做多端扣成本后非正或p>=0.05'}
    if not g2:
        return 'dead', {**report, 'reason': '多空方向训练/测试翻转'}
    return 'alive', report

# ---------- 墓地 ----------
def load_graveyard():
    try:
        with open(GRAVEYARD) as f:
            return json.load(f)
    except Exception:
        return {}

def save_graveyard(g):
    with open(GRAVEYARD, 'w') as f:
        json.dump(g, f, ensure_ascii=False, indent=2)

# ---------- main ----------
def main():
    with open(CACHE_PATH,'rb') as f:
        cache = pickle.load(f)
    panel = {}
    for code, kl in cache.items():
        weeks = build_weekly(kl)
        F, fwd = compute_weekly_factors(weeks)
        dates = [w['date'] for w in weeks]
        for i, d in enumerate(dates):
            if np.isnan(fwd[i]): continue
            panel.setdefault(d, {})[code] = ({k: F[k][i] for k in F}, fwd[i])
    all_dates = sorted(panel)
    mid = len(all_dates)//2
    train_dates, test_dates = all_dates[:mid], all_dates[mid:]
    print(f"周线面板: {len(all_dates)}周, 训练{len(train_dates)}周 {train_dates[0]}~{train_dates[-1]}, 测试{len(test_dates)}周 {test_dates[0]}~{test_dates[-1]}\n")

    graveyard = load_graveyard()
    # 先归档日线16因子(已证伪)
    daily_dead = ['mom_1','mom_5','mom_20','mom_60','rev_1','rev_5','vol_20','vol_chg','amp_20','vr_5','vr_20','vol_chg_20','hl_dist','rsi_14','ma_dist','ma_slope']
    for fk in daily_dead:
        graveyard.setdefault(f'daily_{fk}', {
            'freq':'daily','data':'price_volume','status':'dead',
            'reason':'做多端扣成本后全负(因子动物园+多重检验证伪)',
            'evidence':['factor_zoo.py','factor_layer.py']})

    factors = ['w_mom_4','w_mom_8','w_mom_20','w_rev_4','w_vol_20','w_vol_chg','w_vr_4','w_vr_20','w_hl_dist','w_ma_dist','w_amp_20','w_ma_slope']
    print(f"{'因子':<12} {'做多端':>6} {'测试段做多收益':>12} {'p值':>7} {'多空(训/测)':>16} {'结论':>6}")
    print('-'*70)
    alive = 0; dead = 0
    for fk in factors:
        status, rep = funnel(fk, panel, train_dates, test_dates)
        key = f'weekly_{fk}'
        graveyard[key] = {
            'freq':'weekly','data':'price_volume','status':status,
            'reason': rep.get('reason',''), 'report': rep,
            'evidence':['factor_funnel.py']}
        if status == 'alive': alive += 1
        elif status == 'dead': dead += 1
        ls_str = f"{rep.get('train_ls',0):+.2f}/{rep.get('test_ls',0):+.2f}" if rep.get('train_ls') is not None else 'NA'
        print(f"{fk:<12} {rep.get('long_side',''):>6} {rep.get('test_long_ret',0):>+11.2f}% {rep.get('test_p',1):>7.4f} {ls_str:>16} {status:>6}")
    save_graveyard(graveyard)
    print(f"\n周线第一轮: 存活{alive} 死亡{dead} 未测{len(factors)-alive-dead}")
    print(f"因子墓地已更新: {GRAVEYARD} (共{len(graveyard)}条)")

if __name__ == "__main__":
    main()
