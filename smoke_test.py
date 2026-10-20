# -*- coding: utf-8 -*-
"""冒烟测试：用flask桩导入真实app.py，验证资金流/大盘闸门/分析全链路"""
import sys, types, json

# ---- flask 最小桩（app.py 顶部只 import Flask/render_template/jsonify/request） ----
_flask = types.ModuleType("flask")
class _FakeFlask:
    def __init__(self, *a, **k):
        pass
    def route(self, *a, **k):
        def deco(f):
            return f
        return deco
    def run(self, *a, **k):
        pass
_flask.Flask = _FakeFlask
_flask.render_template = lambda *a, **k: ""
_flask.jsonify = lambda *a, **k: {}
_flask.request = types.SimpleNamespace(
    get_json=lambda *a, **k: {}, args={}, form={}, method="GET")
sys.modules["flask"] = _flask

import app

print("== import OK ==")

mf = app._get_money_flow("sh600519", 5)
print("== 资金流(茅台近5日) ==")
for x in mf:
    print(f"  {x['date']} 净流入{x['net']/1e8:+.2f}亿 占比{x['ratio']:+.2f}% 超大单{x['r0_net']/1e8:+.2f}亿")

mkt = app._get_market_context()
print(f"== 大盘闸门: 上证 {mkt['idx']} 5日{mkt['chg5']:+.2f}% 20日{mkt['chg20']:+.2f}% bear={mkt['bear']} ==")

cf = app._check_capital_filter("sh600519", "吸筹")
print(f"== 资金过滤(吸筹): through={cf['through']} score={cf['score']} reason={cf['reason']} ==")

res = app._analyze_stock("sh600519", 60)
a = res["analysis"]
pa = a["position_advice"]
print("== 个股分析全链路 ==")
print(f"  股票: {res['name']} 现价{res['latest']}")
print(f"  行为: {a['behavior']} ({a['behavior_desc']})")
print(f"  今日建议: {pa['action']} 仓位{pa['position_pct']}%")
print(f"  大盘字段: {a['market']}")
print(f"  风控: {pa['risk_notes']}")
print(f"  资金过滤: {pa['capital_filter']}")

ok = (mf and a["market"] and cf["score"] > 0 and res["latest"] > 0)
print("\n== SMOKE", "PASS" if ok else "FAIL", "==")
