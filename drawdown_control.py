# -*- coding: utf-8 -*-
"""回撤控制验证(正确口径): 突破20日箱顶持有5日, 加回撤控制。
方案: ①基线 ②熊市过滤(大盘20日<-5%不交易) ③个股止损(跌破买入日箱底) ④过滤+止损。
组合回测: 按日历日聚合所有活跃信号的当日个股收益(等权), 得到组合日收益序列。"""
import pickle, sys, math
import numpy as np
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import parse_klines, _get_kline

CACHE_PATH = '/home/user/a-stock-monitor/data/kline_cache.pkl'
COST = 0.003
N = 20
HOLD = 5

def build_signals(klines):
    """返回 [(buy_date, buy_price, stop_price, holding[(date, close, low)])]"""
    data = parse_klines(klines)
    n = len(data)
    if n < N + HOLD + 2:
        return []
    out = []
    for T in range(N + 1, n - HOLD):
        win = data[T-N:T]
        box_top = max(d['high'] for d in win)
        box_bot = min(d['low'] for d in win)
        cur = data[T]['close']
        prev = data[T-1]['close']
        pwin = data[T-1-N:T-1]
        ptop = max(d['high'] for d in pwin)
        if prev <= ptop and cur > box_top:
            holding = [(data[T+j]['date'], data[T+j]['close'], data[T+j]['low']) for j in range(1, HOLD + 1)]
            out.append((data[T]['date'], cur, box_bot, holding))
    return out

def daily_path(buy_price, stop_price, holding, use_stop):
    """返回 [(date, daily_ret)] 持有期每日收益路径(含止损)"""
    out = []
    prev_close = buy_price
    stopped = False
    for i, (d, c, low) in enumerate(holding):
        if use_stop and not stopped and low < stop_price:
            r = (stop_price - prev_close) / prev_close
            out.append((d, r))
            stopped = True
            break
        r = (c - prev_close) / prev_close
        out.append((d, r))
        prev_close = c
    # 成本分摊到第一天
    if out:
        d0, r0 = out[0]
        out[0] = (d0, r0 - COST)
    return out

def max_drawdown(equity):
    peak = -1e9; mdd = 0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v - peak)
    return mdd

def evaluate(sigs, bear_filter, use_stop, idx_chg20):
    """返回 (信号数, 胜率, 平均单笔, 盈亏比, 累计, 年化, 夏普, 最大回撤)"""
    daily = {}       # date -> [当日活跃信号收益]
    single = []      # 单笔收益
    for buy_date, buy_price, stop_price, holding in sigs:
        if bear_filter:
            chg = idx_chg20.get(buy_date)
            if chg is not None and chg < -0.05:
                continue
        path = daily_path(buy_price, stop_price, holding, use_stop)
        if not path:
            continue
        # 单笔收益 = 路径累计
        tot = 1.0
        for _, r in path:
            tot *= (1 + r)
        single.append(tot - 1)
        for d, r in path:
            daily.setdefault(d, []).append(r)

    if not single:
        return None
    single = np.array(single)
    win = float((single > 0).mean())
    wins = single[single > 0]; losses = single[single <= 0]
    avg_win = wins.mean() if len(wins) else 0
    avg_loss = losses.mean() if len(losses) else 0
    pl = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    port_dates = sorted(daily)
    port_rets = np.array([np.mean(daily[d]) for d in port_dates])
    cum = 1.0; equity = [1.0]
    for r in port_rets:
        cum *= (1 + r)
        equity.append(cum)
    mdd = max_drawdown(equity)
    sharpe = port_rets.mean() / port_rets.std() * math.sqrt(252) if port_rets.std() > 0 else 0
    annual = cum ** (252 / len(port_dates)) - 1 if len(port_dates) > 0 else 0
    return (len(single), win, float(single.mean()), pl, cum - 1, annual, sharpe, mdd)

def main():
    with open(CACHE_PATH, 'rb') as f:
        cache = pickle.load(f)
    _, idx_kl = _get_kline("sh000001", 800)
    idx_data = parse_klines(idx_kl)
    idx_map = {d['date']: d['close'] for d in idx_data}
    idx_dates = sorted(idx_map)
    idx_chg20 = {}
    for k, d in enumerate(idx_dates):
        if k >= 20:
            idx_chg20[d] = (idx_map[d] - idx_map[idx_dates[k-20]]) / idx_map[idx_dates[k-20]]

    sigs = []
    for code, klines in cache.items():
        sigs.extend(build_signals(klines))
    print(f"信号总数: {len(sigs)}")

    plans = [
        ('①基线(无控制)', False, False),
        ('②熊市过滤', True, False),
        ('③个股止损', False, True),
        ('④过滤+止损', True, True),
    ]
    print(f"\n{'方案':<14} {'信号':>5} {'胜率':>6} {'单笔':>7} {'盈亏比':>6} {'年化':>7} {'夏普':>6} {'回撤':>8}")
    print('-' * 72)
    for name, bf, st in plans:
        m = evaluate(sigs, bf, st, idx_chg20)
        if m:
            n, win, mean, pl, cum, annual, sharpe, mdd = m
            print(f"{name:<14} {n:>5} {win*100:>5.1f}% {mean*100:>+6.2f}% {pl:>6.2f} {annual*100:>+6.1f}% {sharpe:>6.2f} {mdd*100:>7.1f}%")

if __name__ == "__main__":
    main()
