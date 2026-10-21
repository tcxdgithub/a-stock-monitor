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
    "601318", "600036", "600030", "601166", "000001", "601398", "601288",
    "600016", "601988", "601628",
    # 消费白马
    "000333", "000651", "600887", "601888", "600690", "002415",
    # 白酒食品
    "600809", "000596", "603369", "002304", "600600", "600132",
    "000895", "603288", "002507", "603345",
    # 医药
    "600276", "300760", "603259", "600436", "000538", "600196",
    "000661", "300015", "300122", "002821",
    # 新能源
    "300750", "002466", "002460", "601012", "600438", "300274",
    "300014", "002459", "002812", "300073",
    # 科技
    "688981", "603501", "603986", "002371", "688012", "002049",
    "603019", "000977", "002230", "600588", "688111", "300059",
    "300033", "600570",
    # 周期资源
    "601899", "600547", "601088", "601225", "600188", "600028",
    "601857", "600309", "600019", "600585",
    # 制造工程
    "600031", "000157", "000425", "000338", "601766", "600893",
    # 地产建筑
    "000002", "600048", "001979", "601668", "601390",
    # 交运公用
    "600900", "601985", "601919", "600009", "002352", "601111",
    # 面板消费电子
    "000725", "000100", "002475", "002241", "601138",
    # 汽车链
    "601689", "002050", "601633", "000625",
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


def _get_market_states(days=180):
    """上证指数分状态: 每交易日 chg20>=+5% 牛, <=-5% 熊, 其余震荡。
    返回 {date: (state, chg20)}，与 app.py 大盘闸门口径一致。"""
    _, klines = _get_kline("sh000001", days=days)
    data = parse_klines(klines)
    states = {}
    for i, x in enumerate(data):
        if i < 20:
            continue
        c_now, c_20 = x["close"], data[i - 20]["close"]
        if c_now <= 0 or c_20 <= 0:
            continue
        chg20 = (c_now / c_20 - 1) * 100
        st = "bull" if chg20 >= 5 else ("bear" if chg20 <= -5 else "normal")
        states[x["date"]] = (st, round(chg20, 2))
    return states


# 未来3日涨跌幅分布分桶（与"推算1%/5%/10%概率分布"对应的粒度）
DIST_BUCKETS = [
    ("跌>10%", -100, -10), ("跌7-10%", -10, -7), ("跌5-7%", -7, -5),
    ("跌3-5%", -5, -3), ("跌1-3%", -3, -1), ("跌0-1%", -1, 0),
    ("涨0-1%", 0, 1), ("涨1-3%", 1, 3), ("涨3-5%", 3, 5),
    ("涨5-7%", 5, 7), ("涨7-10%", 7, 10), ("涨>10%", 10, 100),
]


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

    # R1 急跌后缩量企稳（103只样本+0.748,唯一稳定吸筹信号,权重上调）
    if change_20d < -15 and vol_ratio < 0.8 and abs(change_5d) < 3:
        fire("R1", "吸筹", 25, "急跌后缩量企稳")
    # R2 暴跌后放量反弹
    if change_20d < -15 and change_5d > 5 and vol_ratio > 1.5:
        fire("R2", "吸筹", 20, "暴跌后放量反弹")
    # R4 放量长上影线（103只-0.682,出货方向准,权重上调）
    if upper_shadow > 3 and vol_ratio > 1.5:
        fire("R4", "出货", 30, "放量长上影线")
    # R5 底部长下影阳线（103只-0.303方向反,25只样本的+1.60是过拟合,权重下调）
    if lower_shadow > 2 and last["close"] > last["open"] and near_low:
        fire("R5", "吸筹", 5, "底部长下影阳线")
    # R6 高位横盘放量（103只+0.363方向反,权重下调）
    if abs(change_5d) < 2 and change_20d > 10 and vol_ratio > 1.3:
        fire("R6", "出货", 5, "高位横盘放量")
    # R7 连续缩量3日（中性,保持低权重）
    if all(volumes[-i] < volumes[-i - 1] for i in range(1, min(4, len(volumes)))):
        fire("R7", "吸筹", 5, "连续缩量")
    # R8 放量突破MA5（103只-0.211方向反,权重下调）
    if ma5[-1] and cur > ma5[-1] and data[-2]["close"] < ma5[-2] and vol_ratio > 1.2:
        fire("R8", "吸筹", 5, "放量突破MA5")
    # R10 价涨量缩（103只+0.199方向反,保持低权重）
    if change_5d > 2 and vol_ratio < 0.7:
        fire("R10", "出货", 5, "价涨量缩")
    # R11 急跌后十字星（103只+0.326,保留）
    if body < 0.5 and change_20d < -10:
        fire("R11", "吸筹", 20, "急跌后十字星")
    # R12 底部连续小阳线（103只+0.114弱正,权重下调）
    last3 = data[-3:]
    if all(d["close"] > d["open"] for d in last3) and all(abs(d["close"] - d["open"]) / d["open"] * 100 < 2 for d in last3) and near_low:
        fire("R12", "吸筹", 15, "底部连续小阳线")
    # W1 涨幅后缩量回调（103只-1.643最大坑,权重大砍）
    if change_20d > 10 and change_5d < -3 and vol_ratio < 0.7:
        fire("W1", "洗盘", 5, "涨幅后缩量回调")
    # W2 回调不破MA20（103只-0.302方向反,低权重）
    if ma20[-1] and cur > ma20[-1] and change_5d < -2 and vol_ratio < 0.8:
        fire("W2", "洗盘", 5, "回调不破MA20")
    # W3 放量下跌后快速缩量（103只+0.164,权重小幅上调）
    if len(volumes) >= 3 and volumes[-3] > vol_20 * 1.3 and volumes[-1] < vol_20 * 0.7:
        fire("W3", "洗盘", 10, "恐慌盘后缩量")
    # W4 横盘缩量震荡（103只+0.405最佳洗盘信号,权重上调）
    if abs(change_5d) < 3 and abs(change_20d) < 8 and vol_ratio < 0.75:
        fire("W4", "洗盘", 25, "横盘缩量震荡")
    # W5 跌破MA5守住MA20（103只+0.283,保留）
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur > ma20[-1] and change_5d < -2:
        fire("W5", "洗盘", 20, "跌破MA5守MA20")
    # C1 高位放量大阴线（103只-1.533,保留最高权重）
    if day_change_pct < -3 and vol_ratio_5 > 1.5:
        fire("C1", "出货", 35, "放量大阴线")
    # C2 冲高回落（103只-0.309,权重上调）
    if high_to_close > 3 and vol_ratio_5 > 1.2:
        fire("C2", "出货", 30, "冲高回落")
    # C3 放量跌破MA5+MA20（103只-0.158,权重略降）
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur < ma20[-1] and data[-2]["close"] > ma5[-2] and vol_ratio_5 > 1.3:
        fire("C3", "出货", 25, "断头铡刀")
    # C4 连续2日放量下跌（103只-1.083,权重上调）
    if len(data) >= 3:
        prev = data[-2]
        prev_chg = (prev["close"] - prev["open"]) / prev["open"] * 100
        prev_vr = volumes[-2] / vol_5 if vol_5 > 0 else 1
        if day_change_pct < -1 and prev_chg < -1 and vol_ratio_5 > 1.2 and prev_vr > 1.2:
            fire("C4", "出货", 35, "连续放量下跌")
    # C5 高位长上影缩量（中性,低权重）
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
    window_recs = []   # 校准用: 每窗口 {sigs, fwd3}

    # 分状态(牛/熊/震荡)统计: 每个窗口按当日大盘chg20归类
    market_states = _get_market_states(180)
    if not market_states:
        print("[WARN] 上证指数数据获取失败, 分状态分布统计为空", flush=True)
    state_stats = {s: {"n": 0, "r1": 0.0, "r3": 0.0, "w3": 0} for s in ("bull", "normal", "bear")}
    state_dist = {s: {lbl: 0 for lbl, _, _ in DIST_BUCKETS} for s in ("bull", "normal", "bear")}

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
            window_recs.append({
                "sigs": [(s["id"], s["weight"]) for s in signals],
                "fwd3": fwd[3],
            })

            # 分状态分布
            st, _chg = market_states.get(data[T]["date"], ("normal", 0.0))
            ss = state_stats[st]
            ss["n"] += 1
            ss["r1"] += fwd[1]
            ss["r3"] += fwd[3]
            if fwd[3] > 0:
                ss["w3"] += 1
            for lbl, lo, hi in DIST_BUCKETS:
                if lo <= fwd[3] < hi:
                    state_dist[st][lbl] += 1
                    break

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

    # ---- 概率校准: 与 app.py _calc_prob_up_3d 同逻辑(规则胜率证据, 不含大盘修正) ----
    win3_map = {rid: rs["w3"] / rs["n"] * 100 for rid, rs in rule_stats.items()}
    BUCKETS = [(0, 35), (35, 40), (40, 45), (45, 50), (50, 55), (55, 60), (60, 65), (65, 101)]
    calib = {b: {"n": 0, "wins": 0, "ret3": 0.0} for b in BUCKETS}
    no_sig = {"n": 0, "wins": 0, "ret3": 0.0}
    for rec in window_recs:
        if not rec["sigs"]:
            no_sig["n"] += 1
            no_sig["wins"] += 1 if rec["fwd3"] > 0 else 0
            no_sig["ret3"] += rec["fwd3"]
            continue
        prob = 50.0
        for rid, weight in rec["sigs"]:
            w3 = win3_map.get(rid, 44.0)
            prob += (w3 - 50.0) * (weight / 35.0)
        prob = max(0.0, min(100.0, prob))
        for b in BUCKETS:
            if b[0] <= prob < b[1]:
                calib[b]["n"] += 1
                calib[b]["wins"] += 1 if rec["fwd3"] > 0 else 0
                calib[b]["ret3"] += rec["fwd3"]
                break
    calib_rows = []
    for b in BUCKETS:
        c = calib[b]
        if c["n"] > 0:
            calib_rows.append({
                "bucket": f"{b[0]}-{b[1] if b[1] <= 100 else 100}%",
                "n": c["n"], "win3": c["wins"] / c["n"] * 100,
                "ret3": c["ret3"] / c["n"],
            })
    calib_rows.append({
        "bucket": "无信号(参照)", "n": no_sig["n"],
        "win3": no_sig["wins"] / no_sig["n"] * 100 if no_sig["n"] else 0,
        "ret3": no_sig["ret3"] / no_sig["n"] if no_sig["n"] else 0,
    })

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
        "calibration": calib_rows,
        "state_dist": {
            "states": {
                st: {
                    "n": ss["n"],
                    "win3": round(ss["w3"] / ss["n"] * 100, 1) if ss["n"] else 0,
                    "ret3": round(ss["r3"] / ss["n"], 3) if ss["n"] else 0,
                    "buckets": [
                        {"label": lbl, "n": state_dist[st][lbl],
                         "pct": round(state_dist[st][lbl] / ss["n"] * 100, 1) if ss["n"] else 0}
                        for lbl, _, _ in DIST_BUCKETS
                    ],
                }
                for st, ss in state_stats.items() if ss["n"] > 0
            },
            "bucket_labels": [lbl for lbl, _, _ in DIST_BUCKETS],
        },
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

    print("\n【概率校准】  概率高→实际3日上涨率应越高(单调)。in-sample胜率,轻微乐观偏差")
    print(f"{'概率区间':<12}{'次数':>8}{'实际3日胜率%':>12}{'实际3日收益%':>12}")
    for c in r.get("calibration", []):
        print(f"{c['bucket']:<12}{c['n']:>8}{c['win3']:>12.1f}{c['ret3']:>12.3f}")


if __name__ == "__main__":
    res = run_backtest()
    print_report(res)
