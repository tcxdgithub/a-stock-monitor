# -*- coding: utf-8 -*-
"""数据质量审计: 检查103只票腾讯前复权日K线的脏数据。
检查项: 零价格/零成交/高低价矛盾/涨跌幅异常(前复权阈值25%)/缺失交易日。
前复权数据的复权跳变单独标记(除权除息日会整体缩放历史价, 属正常但影响涨跌幅)。"""
import sys, datetime
sys.path.insert(0, '/home/user/a-stock-monitor')
from backtest_rules import _get_kline, UNIVERSE

def audit(code, symbol, klines):
    issues = []
    n = len(klines)
    if n < 200:
        return [('数据不足', f'仅{n}根K线')], n
    prev_close = None
    prev_date = None
    missing = 0
    for i, k in enumerate(klines):
        try:
            d = k[0]
            o = float(k[1]); c = float(k[2]); h = float(k[3]); l = float(k[4]); v = float(k[5])
        except Exception:
            issues.append(('解析失败', f'第{i}根 {k}'))
            continue
        # 价格非法
        if c <= 0 or o <= 0 or h <= 0 or l <= 0:
            issues.append(('零/负价格', f'{d} o={o} c={c} h={h} l={l}'))
        # 高低价矛盾
        if h < l or h < max(o, c) or l > min(o, c):
            issues.append(('高低价矛盾', f'{d} h={h} l={l} o={o} c={c}'))
        # 零成交
        if v <= 0:
            issues.append(('零成交', f'{d} v={v}'))
        # 涨跌幅异常(前复权口径, 放宽到25%)
        if prev_close and prev_close > 0:
            pct = (c - prev_close) / prev_close * 100
            if abs(pct) > 25:
                issues.append(('涨跌幅异常', f'{d} {pct:+.1f}%'))
        # 缺失交易日(工作日间隔>7天, 可能停牌或数据缺)
        if prev_date:
            try:
                dd = (datetime.datetime.strptime(d, '%Y-%m-%d') - datetime.datetime.strptime(prev_date, '%Y-%m-%d')).days
                if dd > 7:
                    missing += 1
            except Exception:
                pass
        prev_close = c
        prev_date = d
    if missing:
        issues.append(('缺失交易日', f'{missing}处间隔>7天'))
    return issues, n

if __name__ == '__main__':
    all_issues = {}
    total_klines = 0
    for code in UNIVERSE:
        symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
        try:
            name, klines = _get_kline(symbol, 800)
        except Exception as e:
            all_issues[code] = [('获取失败', str(e))]
            continue
        issues, n = audit(code, symbol, klines)
        total_klines += n
        if issues:
            all_issues[code] = issues

    print(f"审计完成: {len(UNIVERSE)}只票, 共{total_klines}根K线")
    clean = len(UNIVERSE) - len(all_issues)
    print(f"完全干净: {clean}只  有问题: {len(all_issues)}只\n")

    # 按问题类型统计
    type_count = {}
    for code, issues in all_issues.items():
        for typ, _ in issues:
            type_count[typ] = type_count.get(typ, 0) + 1
    print("=== 问题类型统计 ===")
    for typ, cnt in sorted(type_count.items(), key=lambda x: -x[1]):
        print(f"  {typ}: {cnt}次")

    print("\n=== 问题票明细(前30) ===")
    for code, issues in list(all_issues.items())[:30]:
        print(f"  {code}: {'; '.join(f'{t}({d})' for t, d in issues[:3])}")
