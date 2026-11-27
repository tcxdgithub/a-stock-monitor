# -*- coding: utf-8 -*-
"""风险调整后收益 + 统计检验 + 收益归因 + 压力测试(正确组合口径)。
策略: 日线突破20日箱顶买入(收盘), 持有5个交易日。全市场103只票。
组合回测: 按日历日聚合所有活跃信号的当日个股收益(等权), 得到组合日收益序列。"""
import pickle, sys, math
import numpy as np
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import parse_klines

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003  # 双边成本0.3%(分摊到5天)
N = 20
HOLD = 5

def build_signals(klines):
    """返回 [(date_list[5], daily_rets[5])], 每个信号持有5日的日期与日收益"""
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
            dates = []
            dret = []
            for j in range(1, HOLD + 1):
                dates.append(data[T+j]['date'])
                dret.append((data[T+j]['close'] - data[T+j-1]['close']) / data[T+j-1]['close'])
            out.append((dates, dret))
    return out

def max_drawdown(equity):
    peak = -1e9; mdd = 0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd

def bootstrap_pvalue(rets, n_boot=10000):
    rets = np.array(rets)
    obs = rets.mean()
    centered = rets - obs
    rng = np.random.default_rng(42)
    cnt = 0
    for _ in range(n_boot):
        b = rng.choice(centered, size=len(centered), replace=True)
        if b.mean() >= obs:
            cnt += 1
    return cnt / n_boot

def main():
    with open(CACHE_PATH, 'rb') as f:
        cache = pickle.load(f)

    # 收集信号
    all_sigs = []
    for code, klines in cache.items():
        all_sigs.extend(build_signals(klines))
    print(f"信号总数: {len(all_sigs)}")

    # 单笔收益(每信号5日累计收益, 扣成本)
    single_rets = []
    for dates, dret in all_sigs:
        tot = 1.0
        for r in dret:
            tot *= (1 + r)
        single_rets.append(tot - 1 - COST)
    rets = np.array(single_rets)

    win_rate = float((rets > 0).mean())
    wins = rets[rets > 0]; losses = rets[rets <= 0]
    avg_win = wins.mean() if len(wins) else 0
    avg_loss = losses.mean() if len(losses) else 0
    pl_ratio = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    mean_ret = float(rets.mean())
    std_ret = float(rets.std())

    print("\n=== 单笔收益统计(扣成本) ===")
    print(f"胜率: {win_rate*100:.1f}%")
    print(f"平均单笔(5日): {mean_ret*100:+.2f}%  标准差 {std_ret*100:.2f}%")
    print(f"平均盈利: {avg_win*100:+.2f}%  平均亏损: {avg_loss*100:+.2f}%  盈亏比: {pl_ratio:.2f}")

    print("\n=== Bootstrap 显著性(原假设: 平均收益=0) ===")
    pval = bootstrap_pvalue(single_rets)
    rng = np.random.default_rng(7)
    boot_means = [rng.choice(rets, size=len(rets), replace=True).mean() for _ in range(5000)]
    lo, hi = np.percentile(boot_means, [2.5, 97.5])
    print(f"p值 = {pval:.4f}  {'显著>0' if pval < 0.05 else '不显著'}")
    print(f"95%置信区间: [{lo*100:+.2f}%, {hi*100:+.2f}%]")

    # === 组合日收益回测(正确口径) ===
    daily_port = {}  # date -> [当日所有活跃信号的日收益]
    for dates, dret in all_sigs:
        for d, r in zip(dates, dret):
            daily_port.setdefault(d, []).append(r)
    port_dates = sorted(daily_port)
    port_rets = [np.mean(daily_port[d]) for d in port_dates]
    port_rets = np.array(port_rets)
    # 组合累计
    cum = 1.0; equity = [1.0]
    for r in port_rets:
        cum *= (1 + r)
        equity.append(cum)
    mdd = max_drawdown(equity)
    n_days = len(port_rets)
    sharpe = port_rets.mean() / port_rets.std() * math.sqrt(252) if port_rets.std() > 0 else 0
    annual = (cum ** (252 / n_days) - 1) if n_days > 0 else 0
    print("\n=== 组合回测(每日等权, 未扣单笔成本分摊) ===")
    print(f"活跃交易日: {n_days}天  累计收益: {(cum-1)*100:+.1f}%  年化: {annual*100:+.1f}%")
    print(f"组合日收益均值: {port_rets.mean()*100:+.3f}%  夏普(年化): {sharpe:.2f}")
    print(f"最大回撤: {mdd*100:.1f}%")

    # === 收益归因: 组合日收益 vs 上证指数日收益 ===
    print("\n=== 收益归因(alpha/beta, 组合日收益口径) ===")
    try:
        from backtest_rules import _get_kline
        _, idx_kl = _get_kline("sh000001", 800)
        idx_data = parse_klines(idx_kl)
        idx_map = {d['date']: d['close'] for d in idx_data}
        idx_dates = sorted(idx_map)
        idx_ret = {idx_dates[i]: (idx_map[idx_dates[i]] - idx_map[idx_dates[i-1]]) / idx_map[idx_dates[i-1]]
                   for i in range(1, len(idx_dates))}
        pairs = [(idx_ret[d], r) for d, r in zip(port_dates, port_rets) if d in idx_ret]
        if pairs:
            X = np.array([p[0] for p in pairs]); Y = np.array([p[1] for p in pairs])
            beta = float(np.cov(X, Y)[0, 1] / np.var(X))
            alpha = float(Y.mean() - beta * X.mean())
            print(f"对齐天数: {len(pairs)}  大盘日收益均值 {X.mean()*100:+.3f}%  策略 {Y.mean()*100:+.3f}%")
            print(f"市场beta = {beta:.3f}  日alpha = {alpha*100:+.3f}%")
            print(f"结论: {'有真实alpha(跑赢大盘)' if alpha > 0 else '无alpha(收益来自beta, 未跑赢大盘)'}")
    except Exception as e:
        print(f"指数数据获取失败: {e}")

    # === 市场状态分段 ===
    print("\n=== 市场状态分段(信号日大盘20日涨幅) ===")
    try:
        idx_chg20 = {}
        for k, d in enumerate(idx_dates):
            if k >= 20:
                idx_chg20[d] = (idx_map[d] - idx_map[idx_dates[k-20]]) / idx_map[idx_dates[k-20]]
        buckets = {'熊(< -5%)': [], '震荡(-5%~5%)': [], '牛(> +5%)': []}
        for dates, dret in all_sigs:
            buy_date = dates[0]
            chg = idx_chg20.get(buy_date)
            if chg is None:
                continue
            tot = 1.0
            for r in dret:
                tot *= (1 + r)
            r5 = tot - 1 - COST
            if chg < -0.05:
                buckets['熊(< -5%)'].append(r5)
            elif chg > 0.05:
                buckets['牛(> +5%)'].append(r5)
            else:
                buckets['震荡(-5%~5%)'].append(r5)
        for name, arr in buckets.items():
            if arr:
                a = np.array(arr)
                print(f"  {name}: n={len(a)}  胜率={(a>0).mean()*100:.1f}%  平均{a.mean()*100:+.2f}%")
    except Exception as e:
        print(f"分段失败: {e}")

    # === 压力测试 ===
    print("\n=== 压力测试(最差5笔单笔收益) ===")
    worst = sorted(zip([s[0][0] for s in all_sigs], single_rets), key=lambda x: x[1])[:5]
    for d, r in worst:
        print(f"  {d}: {r*100:+.2f}%")

if __name__ == "__main__":
    main()
