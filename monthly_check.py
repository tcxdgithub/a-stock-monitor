# -*- coding: utf-8 -*-
"""月度系统健康检查 (爱德华·索普式纪律: 只玩正期望, 负了降级)

用法:
    & "D:\AppData\py\python.exe" monthly_check.py --run    # 重新抓数据跑全量回测(2-3分钟)
    & "D:\AppData\py\python.exe" monthly_check.py          # 只评估最近一次结果

判定逻辑:
    1. 概率校准单调性: 概率分桶越高 -> 实际3日胜率越高 (允许1处违反)
    2. 正期望: 最高概率桶的胜率/收益 必须显著高于 无信号参照
    3. 样本充足: 吸筹/洗盘 有效触发 >= 100 次, 否则结论不可信
    健康 -> 继续用; 退化 -> 警告降仓位; 失效 -> 建议停用/重调权重
"""
import json
import os
import sys

RESULT_FILE = "backtest_rules_result.json"


def run_backtest():
    import backtest_rules
    print(">>> 重新跑全量回测 (103只, 约2-3分钟)...")
    backtest_rules.run_backtest()
    print(">>> 回测完成, 结果已写入", RESULT_FILE)


def evaluate():
    if not os.path.exists(RESULT_FILE):
        print("FATAL: 没有回测结果文件, 先跑: monthly_check.py --run")
        return 2
    with open(RESULT_FILE, encoding="utf-8") as f:
        r = json.load(f)

    calib = [c for c in r.get("calibration", []) if c["bucket"] != "无信号(参照)"]
    ref = next((c for c in r.get("calibration", []) if c["bucket"] == "无信号(参照)"), None)
    behaviors = {b["behavior"]: b for b in r.get("behaviors", [])}

    print(f"样本: {r.get('stocks')}只 / {r.get('total_windows')}窗口 / 全样本3日基准 {r.get('baseline_ret3')}%")
    print(f"生成时间: {r.get('generated')}")
    print("--- 概率校准 ---")
    for c in calib:
        print(f"  {c['bucket']:>8}  n={c['n']:>5}  胜率{c['win3']:5.1f}%  收益{c['ret3']:+.3f}%")
    if ref:
        print(f"  {'参照':>8}  n={ref['n']:>5}  胜率{ref['win3']:5.1f}%  收益{ref['ret3']:+.3f}%")

    # 1) 单调性
    violations = 0
    for i in range(1, len(calib)):
        if calib[i]["win3"] < calib[i - 1]["win3"] - 0.5:
            violations += 1
    # 2) 正期望: 最高概率桶 vs 参照
    hi = calib[-1] if calib else None
    edge_ok = False
    if hi and ref:
        edge_ok = hi["win3"] > ref["win3"] + 2 and hi["ret3"] > 0
    # 3) 样本充足
    xc = behaviors.get("吸筹", {}).get("n", 0)
    xp = behaviors.get("洗盘", {}).get("n", 0)
    enough = xc >= 100 and xp >= 100

    print("--- 行为绩效 ---")
    for name in ("吸筹", "洗盘", "减仓", "止损", "止盈"):
        b = behaviors.get(name)
        if b:
            print(f"  {name:<3} n={b['n']:>5}  3日收益{b['ret3']:+.3f}%  胜率{b['win3']:.1f}%")

    print(f"--- 判定 ---")
    print(f"单调性: {'OK(<=1处违反)' if violations <= 1 else f'破坏({violations}处)'}")
    print(f"正期望: {'OK' if edge_ok else '丢失!'}")
    print(f"样本量: {'充足' if enough else '不足!'} (吸筹{xc}/洗盘{xp})")

    if edge_ok and violations <= 1 and enough:
        print("结论: 系统健康, 信号仍有正期望, 可以继续用")
        return 0
    if edge_ok and not enough:
        print("结论: 退化(样本不足), 结论不可靠, 建议降半仓观察一个月再评")
        return 1
    if not edge_ok:
        print("结论: 失效! 概率已无区分度/正期望丢失, 建议: 停用概率信号 OR 重跑调权重 OR 更换规则")
        return 2
    print("结论: 退化, 建议降半仓, 下月重测")
    return 1


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    if len(sys.argv) > 1 and sys.argv[1] == "--run":
        run_backtest()
    sys.exit(evaluate())
