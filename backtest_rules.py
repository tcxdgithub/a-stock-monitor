# -*- coding: utf-8 -*-
"""
规则回测脚本：验证 app.py 个股分析模块中每条行为识别规则的预测准确性。
方法：对每个股票，用"截至 T 日"的数据运行规则引擎，记录触发的规则和判定行为，
      对照 T+1/T+3/T+5 日的真实收益，计算每条规则的命中率与超额收益。
输出：规则级命中率表 + 行为级收益表，保存 backtest_rules_result.json。
"""
import json, sys, time, datetime, subprocess, platform, os

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
IS_WINDOWS = platform.system() == "Windows"


def _curl(url, headers=None, enc="utf-8"):
    curl_bin = "curl.exe" if IS_WINDOWS else "curl"
    cmd = [curl_bin, "-s", "-m", "10", "--ipv4", url]
    if headers:
        for h in headers:
            cmd += ["-H", h]
    else:
        cmd += ["-H", f"User-Agent: {UA}"]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=15)
        return r.stdout.decode(enc, errors="replace").strip()
    except Exception:
        return ""


def _j(url, headers=None, enc="utf-8"):
    raw = _curl(url, headers, enc)
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


_KL_CACHE = {}


def _get_kline(symbol, days=60):
    """复制自 app.py：腾讯前复权K线，失败降级搜狐"""
    c = _KL_CACHE.get(symbol)
    if c and c[0] >= days:
        return c[1], c[2]
    for attempt in range(2):
        for host in ("proxy.finance.qq.com", "web.ifzq.gtimg.cn"):
            url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
            data = _j(url, ["-H", f"User-Agent: {UA}", "-H", "Referer: https://web.ifzq.gtimg.cn/"])
            info = data.get("data", {}).get(symbol, {})
            qt = info.get("qt", {}).get(symbol, [])
            klines = info.get("qfqday", []) or info.get("day", [])
            if klines:
                name = qt[1] if len(qt) > 1 else ""
                _KL_CACHE[symbol] = (days, name, klines)
                return name, klines
        time.sleep(0.5)
    try:
        sohu_code = "cn_" + symbol[2:]
        start = (datetime.date.today() - datetime.timedelta(days=int(days * 1.6) + 25)).strftime("%Y%m%d")
        end = datetime.date.today().strftime("%Y%m%d")
        raw = _curl(f"https://q.stock.sohu.com/hisHq?code={sohu_code}&start={start}&end={end}",
                    ["-H", f"User-Agent: {UA}"])
        arr = json.loads(raw)
        hq = arr[0].get("hq", []) if isinstance(arr, list) and arr else []
        klines = []
        for r in hq:
            try:
                klines.append([r[0], str(r[1]), str(r[2]), str(r[6]), str(r[5]), str(r[7])])
            except Exception:
                continue
        if len(klines) >= 15:
            klines.reverse()
            res = klines[-days:]
            _KL_CACHE[symbol] = (days, "", res)
            return "", res
    except Exception:
        pass
    return "", []

UNIVERSE = [
    # 自选股
    "600519", "000858", "002594", "600611", "000568", "603605", "600702",
    # 金融
    "601318", "600036", "600030", "601166", "000001",
    # 消费/白马
    "000333", "601888", "600887", "000651",
    # 新能源/科技
    "300750", "002475", "601899", "600276", "000725", "688981",
    # 其他
    "601012", "600048", "002230",
]

WARMUP = 30       # 指标预热所需K线数（MACD需26根以上）
FWD = [1, 3, 5]   # 前瞻天数


def parse_klines(klines):
    data = []
    for k in klines:
        try:
            data.append({
                "date": k[0],
                "open": float(k[1]),
                "close": float(k[2]),
                "high": float(k[3]),
                "low": float(k[4]),
                "volume": float(k[5]) if len(k) > 5 else 0,
            })
        except Exception:
            continue
    return data


def calc_ma(vals, period):
    out = [None] * len(vals)
    for i in range(period - 1, len(vals)):
        out[i] = sum(vals[i - period + 1:i + 1]) / period
    return out


def calc_ema(vals, period):
    ema = [None] * len(vals)
    if len(vals) < period:
        return ema
    ema[period - 1] = sum(vals[:period]) / period
    mult = 2 / (period + 1)
    for i in range(period, len(vals)):
        ema[i] = (vals[i] - ema[i - 1]) * mult + ema[i - 1]
    return ema


def calc_macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow:
        return [], [], []
    ema_f = calc_ema(closes, fast)
    ema_s = calc_ema(closes, slow)
    dif = [None] * len(closes)
    for i in range(len(closes)):
        if ema_f[i] is not None and ema_s[i] is not None:
            dif[i] = ema_f[i] - ema_s[i]
    dif_vals = [v for v in dif if v is not None]
    if len(dif_vals) < signal:
        return dif, [], []
    dea = [None] * len(closes)
    start = next((i for i, v in enumerate(dif) if v is not None), len(dif))
    if start + signal <= len(dif):
        dea[start + signal - 1] = sum(dif_vals[:signal]) / signal
        mult = 2 / (signal + 1)
        for i in range(start + signal, len(dif)):
            if dif[i] is not None and dea[i - 1] is not None:
                dea[i] = (dif[i] - dea[i - 1]) * mult + dea[i - 1]
    macd_bar = [None] * len(closes)
    for i in range(len(closes)):
        if dif[i] is not None and dea[i] is not None:
            macd_bar[i] = (dif[i] - dea[i]) * 2
    return dif, dea, macd_bar


def calc_kdj(highs, lows, closes, period=9):
    n = len(closes)
    if n < period:
        return [], [], []
    k = [50.0] * n
    d = [50.0] * n
    j = [50.0] * n
    for i in range(period - 1, n):
        ll = min(lows[i - period + 1:i + 1])
        hh = max(highs[i - period + 1:i + 1])
        rsv = (closes[i] - ll) / (hh - ll) * 100 if hh > ll else 50
        k[i] = k[i - 1] * 2 / 3 + rsv / 3
        d[i] = d[i - 1] * 2 / 3 + k[i] / 3
        j[i] = 3 * k[i] - 2 * d[i]
    return k, d, j


def evaluate_window(data):
    """对截至当前日的窗口运行全部规则。返回 (signals, scores, behavior)。
    signals: [{id, type, weight, text}]  规则ID与app.py规则一一对应
    scores: {xichou, xipan, chuhuo, zhendang}
    """
    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]

    ma5 = calc_ma(closes, 5)
    ma10 = calc_ma(closes, 10)
    ma20 = calc_ma(closes, 20)
    vol_ma5 = calc_ma(volumes, 5)
    vol_ma10 = calc_ma(volumes, 10)
    vol_ma20 = calc_ma(volumes, 20)
    dif, dea, _ = calc_macd(closes)
    k_vals, d_vals, _ = calc_kdj(highs, lows, closes)

    cur = closes[-1]
    change_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
    change_5d = (closes[-1] - closes[-5]) / closes[-5] * 100
    vol_5 = sum(volumes[-5:]) / 5
    vol_20 = sum(volumes[-20:]) / 20
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1
    vol_ratio_5 = volumes[-1] / vol_5 if vol_5 > 0 else 1

    vol_sorted = sorted(volumes[-20:])
    vol_percentile = sum(1 for v in vol_sorted if v <= volumes[-1]) / len(vol_sorted) * 100

    last = data[-1]
    day_change_pct = (last["close"] - last["open"]) / last["open"] * 100
    upper_shadow = (last["high"] - max(last["open"], last["close"])) / last["open"] * 100
    lower_shadow = (min(last["open"], last["close"]) - last["low"]) / last["open"] * 100
    high_to_close = (last["high"] - last["close"]) / last["high"] * 100
    body = abs(last["close"] - last["open"]) / last["open"] * 100

    trend_down = change_20d < -5
    trend_up = change_20d > 5
    near_low = (cur - min(lows[-20:])) / min(lows[-20:]) * 100 < 5

    xichou = xipan = chuhuo = zhendang = 0
    signals = []

    def fire(rid, rtype, weight, text):
        nonlocal xichou, xipan, chuhuo
        if rtype == "吸筹":
            xichou += weight
        elif rtype == "洗盘":
            xipan += weight
        elif rtype == "出货":
            chuhuo += weight
        elif rtype == "震荡":
            zhendang += weight
        signals.append({"id": rid, "type": rtype, "weight": weight, "text": text})

    # R1 急跌后缩量企稳（回测方向为负，权重下调）
    if change_20d < -15 and vol_ratio < 0.8 and abs(change_5d) < 3:
        fire("R1", "吸筹", 5, "急跌后缩量企稳")
    # R2 暴跌后放量反弹
    if change_20d < -15 and change_5d > 5 and vol_ratio > 1.5:
        fire("R2", "吸筹", 25, "暴跌后放量反弹")
    # R4 放量长上影线
    if upper_shadow > 3 and vol_ratio > 1.5:
        fire("R4", "出货", 25, "放量长上影线")
    # R5 底部长下影阳线（回测+1.60，权重上调）
    if lower_shadow > 2 and last["close"] > last["open"] and near_low:
        fire("R5", "吸筹", 25, "底部长下影阳线")
    # R6 高位横盘放量（回测接近中性，权重下调）
    if abs(change_5d) < 2 and change_20d > 10 and vol_ratio > 1.3:
        fire("R6", "出货", 10, "高位横盘放量")
    # R7 连续缩量3日（回测低于基准，权重下调）
    if all(volumes[-i] < volumes[-i - 1] for i in range(1, min(4, len(volumes)))):
        fire("R7", "吸筹", 5, "连续缩量")
    # R8 放量突破MA5
    if ma5[-1] and cur > ma5[-1] and data[-2]["close"] < ma5[-2] and vol_ratio > 1.2:
        fire("R8", "吸筹", 15, "放量突破MA5")
    # R10 价涨量缩（回测方向反，权重下调）
    if change_5d > 2 and vol_ratio < 0.7:
        fire("R10", "出货", 5, "价涨量缩")
    # R11 急跌后十字星（回测+0.67，权重上调）
    if body < 0.5 and change_20d < -10:
        fire("R11", "吸筹", 20, "急跌后十字星")
    # R12 底部连续小阳线（回测+0.78/胜率58%，权重上调）
    last3 = data[-3:]
    if all(d["close"] > d["open"] for d in last3) and all(abs(d["close"] - d["open"]) / d["open"] * 100 < 2 for d in last3) and near_low:
        fire("R12", "吸筹", 25, "底部连续小阳线")
    # W1 涨幅后缩量回调
    if change_20d > 10 and change_5d < -3 and vol_ratio < 0.7:
        fire("W1", "洗盘", 25, "涨幅后缩量回调")
    # W2 回调不破MA20（回测-1.38方向反，权重下调）
    if ma20[-1] and cur > ma20[-1] and change_5d < -2 and vol_ratio < 0.8:
        fire("W2", "洗盘", 5, "回调不破MA20")
    # W3 放量下跌后快速缩量（回测低于基准，权重下调）
    if len(volumes) >= 3 and volumes[-3] > vol_20 * 1.3 and volumes[-1] < vol_20 * 0.7:
        fire("W3", "洗盘", 5, "恐慌盘后缩量")
    # W4 横盘缩量震荡（回测接近中性，权重微调）
    if abs(change_5d) < 3 and abs(change_20d) < 8 and vol_ratio < 0.75:
        fire("W4", "洗盘", 8, "横盘缩量震荡")
    # W5 跌破MA5守住MA20（回测+0.65，权重上调）
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur > ma20[-1] and change_5d < -2:
        fire("W5", "洗盘", 20, "跌破MA5守MA20")
    # C1 高位放量大阴线
    if day_change_pct < -3 and vol_ratio_5 > 1.5:
        fire("C1", "出货", 35, "放量大阴线")
    # C2 冲高回落
    if high_to_close > 3 and vol_ratio_5 > 1.2:
        fire("C2", "出货", 25, "冲高回落")
    # C3 放量跌破MA5+MA20
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur < ma20[-1] and data[-2]["close"] > ma5[-2] and vol_ratio_5 > 1.3:
        fire("C3", "出货", 30, "断头铡刀")
    # C4 连续2日放量下跌
    if len(data) >= 3:
        prev = data[-2]
        prev_chg = (prev["close"] - prev["open"]) / prev["open"] * 100
        prev_vr = volumes[-2] / vol_5 if vol_5 > 0 else 1
        if day_change_pct < -1 and prev_chg < -1 and vol_ratio_5 > 1.2 and prev_vr > 1.2:
            fire("C4", "出货", 30, "连续放量下跌")
    # C5 高位长上影缩量（回测方向反，权重下调）
    if upper_shadow > 2 and vol_ratio_5 < 0.8 and change_20d > 10:
        fire("C5", "出货", 5, "高位长上影缩量")
    # C6 跌破前低
    if len(lows) >= 5:
        recent_low = min(lows[-5:])
        if cur < recent_low and last["close"] < last["open"]:
            fire("C6", "出货", 20, "跌破前低")

    # ---- 行为判定（与 app.py 一致） ----
    total = xichou + xipan + chuhuo
    if total == 0:
        behavior = "震荡"
    elif chuhuo >= 30 and chuhuo > xichou and chuhuo > xipan:
        if change_20d > 15 and day_change_pct < -3:
            behavior = "止盈"
        elif cur < (ma20[-1] if ma20[-1] else cur * 0.9):
            behavior = "止损"
        else:
            behavior = "减仓"
    elif xichou >= 20 and xichou > xipan and xichou > chuhuo:
        behavior = "吸筹"
    elif xipan >= 20 and xipan > xichou and xipan > chuhuo:
        behavior = "洗盘"
    elif chuhuo >= 15 and chuhuo > xichou:
        behavior = "观望"
    else:
        behavior = "震荡"

    return signals, {"xichou": xichou, "xipan": xipan, "chuhuo": chuhuo}, behavior


def run_backtest():
    rule_stats = {}     # id -> {n, sum_ret1, sum_ret3, sum_ret5, win3}
    behav_stats = {}    # behavior -> {n, sum_ret1, sum_ret3, sum_ret5, win3}
    all_ret3 = []
    skipped = []
    total_windows = 0
    stock_results = []

    for code in UNIVERSE:
        symbol = ("sh" if code.startswith(("6", "9")) else "sz") + code
        name, klines = _get_kline(symbol, days=180)
        data = parse_klines(klines)
        if len(data) < WARMUP + 6:
            skipped.append((code, name, len(data)))
            continue
        for T in range(WARMUP, len(data) - 6):
            window = data[:T + 1]
            signals, scores, behavior = evaluate_window(window)
            base = data[T]["close"]
            if base <= 0:
                continue
            fwd = {}
            for d in FWD:
                fwd[d] = (data[T + d]["close"] - base) / base * 100
            total_windows += 1
            all_ret3.append(fwd[3])

            for s in signals:
                rs = rule_stats.setdefault(s["id"], {"n": 0, "r1": 0.0, "r3": 0.0, "r5": 0.0, "w3": 0})
                rs["n"] += 1
                rs["r1"] += fwd[1]
                rs["r3"] += fwd[3]
                rs["r5"] += fwd[5]
                if fwd[3] > 0:
                    rs["w3"] += 1

            bs = behav_stats.setdefault(behavior, {"n": 0, "r1": 0.0, "r3": 0.0, "r5": 0.0, "w3": 0})
            bs["n"] += 1
            bs["r1"] += fwd[1]
            bs["r3"] += fwd[3]
            bs["r5"] += fwd[5]
            if fwd[3] > 0:
                bs["w3"] += 1

        stock_results.append({"code": code, "name": name, "bars": len(data)})
        print(f"[OK] {code} {name} bars={len(data)}", flush=True)

    baseline3 = sum(all_ret3) / len(all_ret3) if all_ret3 else 0

    # ---- 规则统计 ----
    rule_rows = []
    for rid, rs in rule_stats.items():
        n = rs["n"]
        rule_rows.append({
            "id": rid, "n": n,
            "ret1": rs["r1"] / n, "ret3": rs["r3"] / n, "ret5": rs["r5"] / n,
            "win3": rs["w3"] / n * 100,
            "edge3": rs["r3"] / n - baseline3,
        })
    rule_rows.sort(key=lambda x: x["edge3"], reverse=True)

    behav_rows = []
    for b, bs in behav_stats.items():
        n = bs["n"]
        behav_rows.append({
            "behavior": b, "n": n,
            "ret1": bs["r1"] / n, "ret3": bs["r3"] / n, "ret5": bs["r5"] / n,
            "win3": bs["w3"] / n * 100,
        })

    result = {
        "generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "stocks": len(UNIVERSE), "skipped": skipped,
        "total_windows": total_windows,
        "baseline_ret3": round(baseline3, 3),
        "rules": rule_rows,
        "behaviors": behav_rows,
        "per_stock": stock_results,
    }
    with open("backtest_rules_result.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    return result


def print_report(r):
    print("=" * 78)
    print(f"回测样本: {r['stocks']} 只股票 | 有效窗口: {r['total_windows']} | 全样本3日基准收益: {r['baseline_ret3']:.3f}%")
    if r["skipped"]:
        print("跳过(历史不足):", r["skipped"])
    print("=" * 78)
    print("\n【行为判定准确性】  看「吸筹/洗盘」类是否真涨、「出货/减仓/止损」类是否真跌")
    print(f"{'行为':<6}{'次数':>6}{'后1日%':>9}{'后3日%':>9}{'后5日%':>9}{'3日胜率%':>10}")
    for b in r["behaviors"]:
        mark = ""
        if b["behavior"] in ("吸筹", "洗盘") and b["ret3"] <= 0:
            mark = "  ← 喊买入但跌,不准"
        elif b["behavior"] in ("出货", "减仓", "止损", "止盈") and b["ret3"] >= 0:
            mark = "  ← 喊卖出但涨,不准"
        print(f"{b['behavior']:<6}{b['n']:>6}{b['ret1']:>9.3f}{b['ret3']:>9.3f}{b['ret5']:>9.3f}{b['win3']:>10.1f}{mark}")

    print("\n【规则命中率排序】  edge3 = 触发后3日均收益 - 全样本基准；吸筹/洗盘类期望>0，出货类期望<0")
    print(f"{'规则':<5}{'方向':<5}{'次数':>6}{'后1日%':>9}{'后3日%':>9}{'后5日%':>9}{'3日胜率%':>10}{'edge3%':>9}  评价")
    for rw in r["rules"]:
        judge = "可用" if (rw["edge3"] > 0.3 if "吸筹" in str(rw.get("type", "")) else rw["edge3"] < -0.3) else "待观察"
        print(f"{rw['id']:<5}{'':<5}{rw['n']:>6}{rw['ret1']:>9.3f}{rw['ret3']:>9.3f}{rw['ret5']:>9.3f}{rw['win3']:>10.1f}{rw['edge3']:>9.3f}")


if __name__ == "__main__":
    res = run_backtest()
    print_report(res)
