# -*- coding: utf-8 -*-
"""箱体逻辑回测：识别箱体/突破/跌破/箱底箱顶位置，验证未来1/3/5日的胜率与超额。
箱体定义: 最近N日 max(high)=箱顶, min(low)=箱底; 宽度=(箱顶-箱底)/箱底。
信号:
  - 突破箱顶: 昨收<=昨箱顶 且 今收>今箱顶
  - 跌破箱底: 昨收>=昨箱底 且 今收<今箱底
  - 箱底附近: 箱体宽度5-25% 且 pos<0.2
  - 箱顶附近: 箱体宽度5-25% 且 pos>0.8
  - 有效突破: 突破箱顶后,未来5日不跌回箱顶下方(真突破)
  - 假突破: 突破箱顶后,未来5日内跌回箱顶下方
"""
import sys, datetime
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import _get_kline, UNIVERSE

TARGETS = ["600611", "600702", "000568", "603605"]
N = 20  # 箱体窗口
MIN_W = 0.05  # 最小箱体宽度5%
MAX_W = 0.30  # 最大箱体宽度30%

def parse(klines):
    out = []
    for k in klines:
        try:
            dt = datetime.datetime.strptime(k[0], '%Y-%m-%d')
        except Exception:
            continue
        out.append({'date': k[0], 'open': float(k[1]), 'close': float(k[2]),
                    'high': float(k[3]), 'low': float(k[4]), 'vol': float(k[5])})
    return out

# 汇总桶
G_SIG = {}   # 信号名 -> {fwd: [收益]}
G_BASE = {1: [], 3: [], 5: []}

def add(name, fwd, r):
    G_SIG.setdefault(name, {}).setdefault(fwd, []).append(r)

def run(data, track_breakout=False):
    n = len(data)
    for T in range(N, n):
        win = data[T-N:T]  # 前N日(不含T)
        box_top = max(d['high'] for d in win)
        box_bot = min(d['low'] for d in win)
        w = (box_top - box_bot) / box_bot if box_bot > 0 else 0
        close = data[T]['close']
        prev_close = data[T-1]['close']
        # 前一日箱体(用T-1往前N日)
        if T-1 >= N:
            pwin = data[T-1-N:T-1]
            ptop = max(d['high'] for d in pwin)
            pbot = min(d['low'] for d in pwin)
        else:
            ptop = pbot = None
        pos = (close - box_bot) / (box_top - box_bot) if box_top > box_bot else 0.5

        # 基准
        for fwd in (1, 3, 5):
            if T + fwd < n:
                r = (data[T+fwd]['close'] - close) / close
                G_BASE[fwd].append(r)

        # 信号判断
        is_breakout = ptop is not None and prev_close <= ptop and close > box_top
        is_breakdown = pbot is not None and prev_close >= pbot and close < box_bot
        in_box = MIN_W <= w <= MAX_W

        if is_breakout:
            for fwd in (1, 3, 5):
                if T + fwd < n:
                    add('突破箱顶', fwd, (data[T+fwd]['close']-close)/close)
            # 真/假突破
            if track_breakout:
                back = False
                for j in range(1, 6):
                    if T + j < n and data[T+j]['close'] < box_top:
                        back = True
                        break
                add('真突破(5日不回箱)', 5, (data[T+5]['close']-close)/close if T+5 < n else 0) if not back else None
                if back:
                    add('假突破(5日回箱)', 5, (data[T+5]['close']-close)/close if T+5 < n else 0)
        if is_breakdown:
            for fwd in (1, 3, 5):
                if T + fwd < n:
                    add('跌破箱底', fwd, (data[T+fwd]['close']-close)/close)
        if in_box:
            if pos < 0.2:
                for fwd in (1, 3, 5):
                    if T + fwd < n:
                        add('箱底附近', fwd, (data[T+fwd]['close']-close)/close)
            elif pos > 0.8:
                for fwd in (1, 3, 5):
                    if T + fwd < n:
                        add('箱顶附近', fwd, (data[T+fwd]['close']-close)/close)
            else:
                for fwd in (1, 3, 5):
                    if T + fwd < n:
                        add('箱体中轨', fwd, (data[T+fwd]['close']-close)/close)

def report():
    base = {f: sum(G_BASE[f])/len(G_BASE[f]) for f in (1, 3, 5) if G_BASE[f]}
    print(f"基准: 1日{base[1]*100:+.2f}% 3日{base[3]*100:+.2f}% 5日{base[5]*100:+.2f}%")
    for name in ('箱底附近', '箱体中轨', '箱顶附近', '突破箱顶', '跌破箱底', '真突破(5日不回箱)', '假突破(5日回箱)'):
        if name not in G_SIG:
            continue
        print(f"\n【{name}】")
        for fwd in (1, 3, 5):
            arr = G_SIG[name].get(fwd, [])
            if not arr:
                continue
            win = sum(1 for r in arr if r > 0) / len(arr) * 100
            avg = sum(arr) / len(arr) * 100
            print(f"  未来{fwd}日: n={len(arr):<5} 上涨率={win:.1f}%  平均={avg:+.2f}%  超额={avg-base[fwd]*100:+.2f}%")

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'target'
    codes = TARGETS if mode == 'target' else UNIVERSE
    for code in codes:
        symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
        name, klines = _get_kline(symbol, 800)
        if len(klines) < 100:
            continue
        data = parse(klines)
        run(data, track_breakout=True)
    print(f"\n{'='*60}\n{'四只票' if mode=='target' else '全市场'} 箱体逻辑回测 (N={N}, 宽度{MIN_W*100:.0f}-{MAX_W*100:.0f}%)")
    report()
