# -*- coding: utf-8 -*-
"""重启后验证概率输出 + 资金过滤一致性"""
import json, subprocess, sys, time

def api(code):
    r = subprocess.run(["curl.exe", "-s", "-m", "25",
                        f"http://127.0.0.1:5000/api/stock_search?code={code}"],
                       capture_output=True, text=True)
    return json.loads(r.stdout)

for code in ["600519", "600611"]:
    d = api(code)["data"]
    a = d["analysis"]
    pa = a["position_advice"]
    print(f"=== {d['name']} ===")
    print("behavior:", a["behavior"], "| prob:", a.get("prob_up_3d"), a.get("prob_label"))
    print("action:", pa.get("action"), pa.get("position_pct"), "%")
    print("filter:", pa.get("capital_filter"))
    print("risk:", pa.get("risk_notes"))
