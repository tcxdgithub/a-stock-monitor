# -*- coding: utf-8 -*-
"""
中盘选股引擎
六因子复合：低成交额 + 低波动 + 低振幅 + 低20日动量 + 低换手 + 低于MA20
十分档中 D7 为历史最优区，D6-D8 为可买区，D1-D3 为回避区
"""
import os, sys, json, time, pickle
import numpy as np
import pandas as pd
import requests

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data", "pool_mid.pkl")
OUT = os.path.join(BASE, "data", "pool_result.json")
H = {"Referer": "https://finance.sina.com.cn", "User-Agent": "Mozilla/5.0"}
FEATS = ["amt_log", "vol20", "amp", "chg20", "turnover", "ma20_dev"]


def update_recent(pool, bars=15):
    """增量拉取最近 bars 根，合并进缓存"""
    n = 0
    for code, info in pool.items():
        try:
            r = requests.get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                             f"CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen={bars}",
                             headers=H, timeout=15)
            new = json.loads(r.text)
            rows = {x["day"]: {"date": x["day"], "open": float(x["open"]), "close": float(x["close"]),
                               "high": float(x["high"]), "low": float(x["low"]),
                               "volume": float(x["volume"])} for x in new}
            old = {r0["date"]: r0 for r0 in info["rows"]}
            old.update(rows)
            info["rows"] = [old[d] for d in sorted(old)]
            n += 1
        except Exception:
            pass
        time.sleep(0.18)
    return n


def build_row(info):
    df = pd.DataFrame(info["rows"]).sort_values("date")
    c, h, l, v = df["close"], df["high"], df["low"], df["volume"]
    fshare = info["nmc"] * 10000 / info["price"]
    out = {"date": df["date"].iloc[-1]}
    out["close"] = float(c.iloc[-1])
    out["pct"] = float((c.iloc[-1] / c.iloc[-2] - 1) * 100) if len(c) > 1 else np.nan
    out["amt_log"] = float(np.log(v.iloc[-1] * c.iloc[-1] / 1e8)) if v.iloc[-1] * c.iloc[-1] > 0 else np.nan
    out["vol20"] = float(c.pct_change().rolling(20).std().iloc[-1] * 100)
    out["amp"] = float((h.iloc[-1] - l.iloc[-1]) / c.iloc[-2] * 100)
    out["chg20"] = float((c.iloc[-1] / c.iloc[-21] - 1) * 100) if len(c) > 21 else np.nan
    out["turnover"] = float(v.iloc[-1] / fshare * 100)
    out["ma20_dev"] = float((c.iloc[-1] / c.rolling(20).mean().iloc[-1] - 1) * 100)
    out["amt_yi"] = float(v.iloc[-1] * c.iloc[-1] / 1e8)
    return out


def main():
    pool = pickle.load(open(DATA, "rb"))
    if "--update" in sys.argv:
        print("增量更新...", flush=True)
        n = update_recent(pool)
        pickle.dump(pool, open(DATA, "wb"))
        print(f"更新 {n} 只")

    recs = []
    for code, info in pool.items():
        if len(info["rows"]) < 120:
            continue
        try:
            r = build_row(info)
            r["code"], r["name"] = code, info["name"]
            recs.append(r)
        except Exception:
            pass

    df = pd.DataFrame(recs).dropna(subset=FEATS)
    if len(df) < 50:
        print("有效标的不足", len(df))
        return

    rk = pd.DataFrame({f: df[f].rank(pct=True) for f in FEATS})
    df["comp"] = -(rk[FEATS].sum(axis=1)) / len(FEATS)
    df = df.sort_values("comp", ascending=False).reset_index(drop=True)
    df["decile"] = (pd.qcut(df["comp"].rank(method="first"), 10,
                            labels=False, duplicates="drop") + 1).astype(int)

    trade_date = df["date"].mode().iloc[0]
    items = []
    for _, r in df.iterrows():
        items.append({
            "code": r["code"], "name": r["name"], "close": round(r["close"], 2),
            "pct": round(r["pct"], 2), "comp": round(r["comp"], 4), "decile": int(r["decile"]),
            "amt_yi": round(r["amt_yi"], 2), "turnover": round(r["turnover"], 2),
            "vol20": round(r["vol20"], 2), "chg20": round(r["chg20"], 2),
            "ma20_dev": round(r["ma20_dev"], 2),
        })

    res = {"trade_date": str(trade_date), "total": len(items),
           "buy_zone": "D6-D8", "best": "D7", "avoid": "D1-D3", "items": items}
    json.dump(res, open(OUT, "w"), ensure_ascii=False)
    print(f"日期 {trade_date} | 有效 {len(items)} 只 | 已写 {OUT}")
    print("\n各档数量:", df["decile"].value_counts().sort_index().to_dict())
    print("\nD7 档（历史最优）:")
    d7 = df[df["decile"] == 7].head(12)
    for _, r in d7.iterrows():
        print(f"  {r['name']:<8} {r['close']:>8.2f} {r['pct']:>+6.2f}% "
              f"成交额{r['amt_yi']:>6.1f}亿 换手{r['turnover']:>5.2f}% 20日{r['chg20']:>+6.1f}%")


if __name__ == "__main__":
    main()
