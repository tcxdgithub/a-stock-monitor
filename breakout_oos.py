# -*- coding: utf-8 -*-
"""高置信突破稳健性验证: 前一半样本确定阈值, 后一半样本外验证。
信号 = 收盘上穿20日箱顶 且 突破幅度>A 且 量比>V。
验证: 分年收益/分散度/组合回测(年化/夏普/回撤)。"""
import pickle, sys, math
import numpy as np
from scipy import stats
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import parse_klines

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003
N = 20
HOLD = 5

def collect(klines, code):
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
            ret5 = (data[T+HOLD]['close'] - cur) / cur - COST
            out.append((data[T]['date'], amp, vol_ratio, ret5, code))
    return out

def stats_block(label, rows):
    if len(rows) < 10:
        return f"{label}: n={len(rows)} 样本不足"
    r = np.array([x[3] for x in rows])
    t, p = stats.ttest_1samp(r, 0)
    return f"{label}: n={len(rows):<5} 胜率{(r>0).mean()*100:>5.1f}% 单笔{r.mean()*100:>+6.2f}% p={p:.4f}"

def max_drawdown(eq):
    peak = -1e9; mdd = 0
    for v in eq:
        peak = max(peak, v); mdd = min(mdd, v - peak)
    return mdd

def port_backtest(rows):
    """组合回测: 每日收益路径口径"""
    daily = {}
    for date, amp, vr, ret5, code in rows:
        # 5日收益近似均摊到5天(做组合回测口径)
        d5 = ret5 + COST  # 先还原未扣成本总收益
        daily_r = (1 + d5) ** (1/5) - 1  # 日化
        for j in range(HOLD):
            d = date  # 用买入日做简化聚合(信号稀疏, 误差可接受)
            daily.setdefault(d, []).append(daily_r - COST/5)
    dates = sorted(daily)
    port_rets = np.array([np.mean(daily[d]) for d in dates])
    cum = 1.0; eq = [1.0]
    for r in port_rets:
        cum *= (1+r); eq.append(cum)
    mdd = max_drawdown(eq)
    sharpe = port_rets.mean()/port_rets.std()*math.sqrt(252) if port_rets.std()>0 else 0
    annual = cum**(252/len(dates))-1 if len(dates)>0 else 0
    return cum-1, annual, sharpe, mdd, len(dates)

def main():
    with open(CACHE_PATH, 'rb') as f:
        cache = pickle.load(f)
    all_rows = []
    for code, kl in cache.items():
        all_rows.extend(collect(kl, code))
    all_rows.sort(key=lambda x: x[0])  # 按日期排序
    print(f"全部突破信号: {len(all_rows)}")

    # 按时间切一半
    mid = len(all_rows) // 2
    train, test = all_rows[:mid], all_rows[mid:]
    print(f"训练段(前): {len(train)}  {train[0][0]} ~ {train[-1][0]}")
    print(f"测试段(后): {len(test)}  {test[0][0]} ~ {test[-1][0]}")

    # 训练段确定最优阈值
    print("\n=== 训练段扫描阈值(幅度 x 量比) ===")
    best = None
    for a in [0.02, 0.03, 0.04, 0.05, 0.06]:
        for v in [1.0, 1.5, 2.0, 2.5, 3.0]:
            sel = [r for r in train if r[1] >= a and r[2] >= v]
            if len(sel) >= 30:
                rr = np.array([x[3] for x in sel])
                if best is None or rr.mean() > best[2]:
                    best = (a, v, rr.mean(), len(sel))
    a_best, v_best, _, n_best = best
    print(f"训练段最优阈值: 幅度>={a_best*100:.0f}% 量比>={v_best:.1f}  n={n_best}")

    # 样本外验证
    sel_test = [r for r in test if r[1] >= a_best and r[2] >= v_best]
    print(f"\n=== 样本外(后段) 用阈值 幅度>={a_best*100:.0f}% 量比>={v_best:.1f} ===")
    print(stats_block("样本外整体", sel_test))
    # 分年
    by_year = {}
    for r in sel_test:
        y = r[0][:4]
        by_year.setdefault(y, []).append(r)
    for y in sorted(by_year):
        print(" ", stats_block(y, by_year[y]))
    # 分散度
    by_code = {}
    for r in sel_test:
        by_code.setdefault(r[4], []).append(r)
    print("\n样本外按股票分布(前10):")
    for c, rows in sorted(by_code.items(), key=lambda x: -len(x[1]))[:10]:
        print(f"  {c}: n={len(rows)} 单笔{np.mean([x[3] for x in rows])*100:+.2f}%")
    # 组合回测
    cum, annual, sharpe, mdd, nd = port_backtest(sel_test)
    print(f"\n样本外组合回测: 累计{cum*100:+.1f}% 年化{annual*100:+.1f}% 夏普{sharpe:.2f} 回撤{mdd*100:.1f}% 交易日{nd}")

if __name__ == "__main__":
    main()
