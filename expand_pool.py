# -*- coding: utf-8 -*-
"""中盘池 300 -> 600: 拉取市值排名 601-900 名的完整日线并合并"""
import requests, json, pickle, time, os
BASE = "/home/user/a-stock-monitor"
DATA = os.path.join(BASE, "data", "pool_mid.pkl")
H = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}

pool = pickle.load(open(DATA, "rb"))
have = set(pool.keys())
new_items = []
for page in (7, 8, 9):
    u = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=100&sort=nmc&asc=0&node=hs_a"
    d = json.loads(requests.get(u, headers=H, timeout=20).text)
    for x in d:
        code = x["symbol"]
        if code not in have and code not in [i[0] for i in new_items]:
            new_items.append((code, x["name"], float(x["nmc"]), float(x["trade"] or 0)))
print(f"新增待拉: {len(new_items)} 只", flush=True)

ok = 0
for code, name, nmc, price in new_items:
    try:
        u = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=300"
        r = requests.get(u, headers=H, timeout=20)
        rows = json.loads(r.text)
        if not rows or len(rows) < 120:
            print(f"跳过 {code} {name} (只有{len(rows)}根)", flush=True); time.sleep(0.15); continue
        pool[code] = {
            "name": name, "nmc": nmc, "price": price,
            "rows": [{"date": x["day"], "open": float(x["open"]), "close": float(x["close"]),
                      "high": float(x["high"]), "low": float(x["low"]), "volume": float(x["volume"])} for x in rows],
        }
        ok += 1
        if ok % 30 == 0: print(f"已拉 {ok}", flush=True)
    except Exception as e:
        print(f"失败 {code} {name}: {e}", flush=True)
    time.sleep(0.15)

pickle.dump(pool, open(DATA, "wb"))
print(f"完成: 新增 {ok} 只, 池总数 {len(pool)}", flush=True)
