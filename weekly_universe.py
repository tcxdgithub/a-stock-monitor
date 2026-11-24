# -*- coding: utf-8 -*-
"""周线趋势全市场验证：跑 UNIVERSE 全部股票，汇总周线状态/事件信号的未来收益与超额。
重点看：多头/空头排列、突破20周高/跌破20周低 在全市场是否普遍成立。"""
import sys, datetime
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import _get_kline, UNIVERSE

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
            wk['date'] = k[0]
            wk['close'] = c
            wk['high'] = max(wk['high'], h)
            wk['low'] = min(wk['low'], l)
            wk['vol'] += v
    return [weeks[k] for k in sorted(weeks)]

def ema_series(vals, n):
    k = 2 / (n + 1)
    out, e = [], None
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

# 全局汇总桶: 信号名 -> {fwd: [收益list]}
G_STAT = {}
G_EVENT = {}
G_BASE = {1: [], 2: [], 4: []}  # 全市场基准收益

def add(bucket, name, fwd, r):
    bucket.setdefault(name, {}).setdefault(fwd, []).append(r)

def analyze(weeks):
    closes = [w['close'] for w in weeks]
    highs = [w['high'] for w in weeks]
    lows = [w['low'] for w in weeks]
    n = len(weeks)
    if n < 40:
        return
    ma20 = [sum(closes[i-19:i+1])/20 if i >= 19 else None for i in range(n)]
    ema12 = ema_series(closes, 12)
    ema26 = ema_series(closes, 26)
    dif = [ema12[i] - ema26[i] for i in range(n)]
    dea = ema_series(dif, 9)

    for i in range(20, n):
        c = closes[i]
        m20 = ma20[i]
        m20_prev = ma20[i-1] if i > 0 else None
        if m20 is None or m20_prev is None:
            continue
        if c > m20 and m20 > m20_prev:
            state = '多头'
        elif c < m20 and m20 < m20_prev:
            state = '空头'
        else:
            state = '震荡'
        for fwd in (1, 2, 4):
            if i + fwd < n:
                r = (closes[i+fwd] - c) / c
                add(G_STAT, state, fwd, r)
                G_BASE[fwd].append(r)
        # 事件
        if i >= 1:
            if dif[i-1] <= dea[i-1] and dif[i] > dea[i]:
                for fwd in (1, 2, 4):
                    if i + fwd < n:
                        add(G_EVENT, '金叉', fwd, (closes[i+fwd]-c)/c)
            elif dif[i-1] >= dea[i-1] and dif[i] < dea[i]:
                for fwd in (1, 2, 4):
                    if i + fwd < n:
                        add(G_EVENT, '死叉', fwd, (closes[i+fwd]-c)/c)
        hh20 = max(highs[max(0, i-20):i])
        ll20 = min(lows[max(0, i-20):i])
        if c > hh20:
            for fwd in (1, 2, 4):
                if i + fwd < n:
                    add(G_EVENT, '突破20周高', fwd, (closes[i+fwd]-c)/c)
        if c < ll20:
            for fwd in (1, 2, 4):
                if i + fwd < n:
                    add(G_EVENT, '跌破20周低', fwd, (closes[i+fwd]-c)/c)

if __name__ == '__main__':
    done = 0
    for code in UNIVERSE:
        symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
        try:
            name, klines = _get_kline(symbol, 800)
        except Exception:
            continue
        if len(klines) < 200:
            continue
        weeks = build_weekly(klines)
        analyze(weeks)
        done += 1
        if done % 20 == 0:
            print(f"已处理 {done} 只", flush=True)

    print(f"\n全市场 {done} 只票周线验证汇总")
    base = {f: sum(G_BASE[f])/len(G_BASE[f]) for f in (1, 2, 4)}
    print(f"基准(所有样本未来N周平均收益): 1周{base[1]*100:+.2f}% 2周{base[2]*100:+.2f}% 4周{base[4]*100:+.2f}%")

    print("\n=== 周线状态信号 ===")
    for name in ('多头', '震荡', '空头'):
        if name not in G_STAT:
            continue
        print(f"  【{name}】")
        for fwd in (1, 2, 4):
            arr = G_STAT[name].get(fwd, [])
            if not arr:
                continue
            win = sum(1 for r in arr if r > 0) / len(arr) * 100
            avg = sum(arr) / len(arr) * 100
            print(f"    未来{fwd}周: n={len(arr):<6} 上涨率={win:.1f}%  平均收益={avg:+.2f}%  超额={avg-base[fwd]*100:+.2f}%")

    print("\n=== 周线事件信号 ===")
    for name in ('金叉', '死叉', '突破20周高', '跌破20周低'):
        if name not in G_EVENT:
            continue
        print(f"  【{name}】")
        for fwd in (1, 2, 4):
            arr = G_EVENT[name].get(fwd, [])
            if not arr:
                continue
            win = sum(1 for r in arr if r > 0) / len(arr) * 100
            avg = sum(arr) / len(arr) * 100
            print(f"    未来{fwd}周: n={len(arr):<6} 上涨率={win:.1f}%  平均收益={avg:+.2f}%  超额={avg-base[fwd]*100:+.2f}%")
