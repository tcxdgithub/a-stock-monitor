# -*- coding: utf-8 -*-
"""前向验证：真实样本外预测准确率统计。

对每只票，站在每个历史时点 T（只用 T 及之前的K线）跑规则引擎，
预测 T+1 涨跌，对照实际，统计看多/看空/方向命中率。
无未来数据污染——这是程序"当时真实能给出的预测"的准确率上限。
"""
import json
from backtest_rules import _get_kline, parse_klines, evaluate_window

CODES = ['600611', '600702', '000568', '603605']
FWD_DAYS = 15  # 最近15个交易日滚动前向验证


def _load_win3():
    try:
        d = json.load(open('backtest_rules_result.json', encoding='utf-8'))
        return {r['id']: r['win3'] for r in d.get('rules', [])}
    except Exception:
        return {}


def main():
    win3 = _load_win3()
    stats = {"bull_hit": 0, "bull_n": 0, "bear_hit": 0, "bear_n": 0,
             "neutral_n": 0, "total": 0}
    print("前向验证：站在T日(只用T及之前数据)预测T+1，对照实际")
    print("=" * 80)
    for code in CODES:
        symbol = ('sh' if code.startswith(('6', '9')) else 'sz') + code
        name, klines = _get_kline(symbol, days=120)
        data = parse_klines(klines)
        if len(data) < 35:
            print(code, "数据不足", len(data))
            continue
        print(f"\n{name} {code}")
        rows = []
        for T in range(len(data) - FWD_DAYS, len(data) - 1):
            window = data[:T + 1]
            signals, scores, behavior = evaluate_window(window)
            # 概率 = 规则胜率加权(与 app.py _calc_prob_up_3d 同逻辑,不含大盘修正)
            prob = 50.0
            for s in signals:
                rid = s.get("id", "")
                w3 = win3.get(rid, 44.0)
                prob += (w3 - 50.0) * (s.get("weight", 0) / 35.0)
            prob = max(0, min(100, prob))
            if prob >= 53:
                direction = "看多"
            elif prob <= 47:
                direction = "看空"
            else:
                direction = "中性"
            actual = (data[T + 1]['close'] - data[T]['close']) / data[T]['close'] * 100
            actual_dir = "涨" if actual > 0 else "跌"
            hit = None
            if direction == "看多":
                hit = actual > 0
            elif direction == "看空":
                hit = actual < 0
            rows.append((data[T]['date'], prob, direction, actual, actual_dir, hit))
            stats["total"] += 1
            if direction == "看多":
                stats["bull_n"] += 1
                if actual > 0:
                    stats["bull_hit"] += 1
            elif direction == "看空":
                stats["bear_n"] += 1
                if actual < 0:
                    stats["bear_hit"] += 1
            else:
                stats["neutral_n"] += 1
        for (d, p, dirn, a, ad, h) in rows:
            mark = "✓" if h is True else ("✗" if h is False else "-")
            print(f"  {d}  概率{p:5.1f}%  {dirn}  → 次日{ad}{a:+.2f}%  {mark}")
    print("\n" + "=" * 80)
    print("汇总统计")
    print("=" * 80)
    tot_dir = stats["bull_n"] + stats["bear_n"]
    print(f"总预测 {stats['total']} 次：看多 {stats['bull_n']} / 看空 {stats['bear_n']} / 中性 {stats['neutral_n']}")
    if stats["bull_n"]:
        print(f"看多命中率: {stats['bull_hit']}/{stats['bull_n']} = {stats['bull_hit'] / stats['bull_n'] * 100:.1f}%")
    if stats["bear_n"]:
        print(f"看空命中率: {stats['bear_hit']}/{stats['bear_n']} = {stats['bear_hit'] / stats['bear_n'] * 100:.1f}%")
    if tot_dir:
        print(f"方向总准确率: {stats['bull_hit'] + stats['bear_hit']}/{tot_dir} = {(stats['bull_hit'] + stats['bear_hit']) / tot_dir * 100:.1f}%")


if __name__ == "__main__":
    main()
