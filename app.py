import json, subprocess, threading, time, re, os, csv, datetime, platform
from flask import Flask, render_template, jsonify, request

app = Flask(__name__)
IS_WINDOWS = platform.system() == "Windows"
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
_store = {"sector": [], "concept": [], "stock": []}
_lock = threading.Lock()
_ready = threading.Event()


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


def _sina(fenlei, num=30):
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_bkzj_bk?page=1&num={num}&sort=netamount&asc=0&fenlei={fenlei}")
    data = _j(url, ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.sina.com.cn/"])
    if not isinstance(data, list):
        return []
    out = []
    for x in data:
        try:
            out.append({
                "code": x.get("category", ""), "name": x.get("name", ""),
                "net_inflow": float(x.get("netamount", 0) or 0),
                "in_amount": float(x.get("inamount", 0) or 0),
                "out_amount": float(x.get("outamount", 0) or 0),
                "avg_change": float(x.get("avg_changeratio", 0) or 0) * 100,
                "turnover": float(x.get("turnover", 0) or 0),
                "top_stock": x.get("ts_name", ""),
                "top_change": float(x.get("ts_changeratio", 0) or 0) * 100,
            })
        except Exception:
            continue
    return out


def _stocks():
    codes = ["sh600519","sh601318","sz000858","sh600036","sz002594",
             "sz300750","sz002475","sh600900","sz000001","sh601899",
             "sz002371","sz000651","sz300059","sz002049","sh601166",
             "sz000568","sz300760","sh600887","sh603288","sz002714",
             "sh601398","sh600276","sz300274","sh688981","sz002230",
             "sh600031","sz300015","sz000725","sh600585","sz000002"]
    raw = _curl(f"https://qt.gtimg.cn/q={','.join(codes)}",
                ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")
    out = []
    for line in raw.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            p = line.split("=", 1)[1].strip('"').split("~")
            if len(p) < 46:
                continue
            out.append({
                "code": p[2], "name": p[1],
                "latest": float(p[3] or 0), "pct_change": float(p[32] or 0),
                "amount": float(p[37] or 0), "turnover": float(p[38] or 0),
            })
        except Exception:
            continue
    out.sort(key=lambda x: x["amount"], reverse=True)
    return out


def _refresh():
    t0 = time.time()
    sector = _sina(0, 30)
    concept = _sina(1, 30)
    stock = _stocks()
    with _lock:
        _store.update(sector=sector, concept=concept, stock=stock)
    print(f"[OK] {time.time()-t0:.1f}s 行业{len(sector)} 概念{len(concept)} 个股{len(stock)}", flush=True)
    _ready.set()


def _loop():
    while True:
        try:
            _refresh()
        except Exception as e:
            print(f"[ERR] {e}", flush=True)
        time.sleep(300)


@app.route("/api/debug")
def debug():
    """调试接口，检查API连通性"""
    import platform
    test_urls = {
        "sina": "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/MoneyFlow.ssl_bkzj_bk?page=1&num=5&sort=netamount&asc=0&fenlei=0",
        "tencent": "https://qt.gtimg.cn/q=sh600519",
    }
    results = {}
    for name, url in test_urls.items():
        try:
            raw = _curl(url, enc="gbk" if name == "tencent" else "utf-8")
            results[name] = {"ok": bool(raw), "length": len(raw), "sample": raw[:100] if raw else "empty"}
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}
    return jsonify({"platform": platform.system(), "tests": results})


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    return jsonify({"ready": _ready.is_set()})

@app.route("/api/<name>_flow")
def flow(name):
    with _lock:
        return jsonify({"code": 0, "data": list(_store.get(name, []))})


# ===== 板块预测 =====
def _predict_sectors(sectors, fenlei_name="行业"):
    """预测明日可能启动或资金流向的板块"""
    if not sectors or len(sectors) < 5:
        return []

    predictions = []

    # 计算板块的综合指标
    for s in sectors:
        net = s["net_inflow"]
        avg_chg = s["avg_change"]
        turnover = s["turnover"]
        in_amt = s["in_amount"]
        out_amt = s["out_amount"]

        # 资金流向比 = 流入/流出
        flow_ratio = in_amt / out_amt if out_amt > 0 else 1.0

        # 流入强度 = 净流入/总成交额
        total_amt = in_amt + out_amt
        net_intensity = net / total_amt if total_amt > 0 else 0

        s["_flow_ratio"] = flow_ratio
        s["_net_intensity"] = net_intensity

    # === 策略1: 超跌反弹 — 近期大跌但资金流出减弱 ===
    for s in sectors:
        net = s["net_inflow"]
        avg_chg = s["avg_change"]
        flow_ratio = s["_flow_ratio"]

        # 板块跌但资金开始回流(流出减少或已转为流入)
        if avg_chg < -1 and net > 0 and flow_ratio > 0.95:
            score = min(30, abs(avg_chg) * 5 + (flow_ratio - 0.95) * 100)
            predictions.append({
                "name": s["name"],
                "code": s["code"],
                "reason": f"板块跌{avg_chg:.1f}%但资金已回流(流入/流出={flow_ratio:.2f})",
                "type": "超跌反弹",
                "score": round(score),
                "confidence": "中",
                "top_stock": s["top_stock"],
                "net_inflow": net,
            })

    # === 策略2: 底部吸筹 — 小幅下跌但高换手,主力暗中吸货 ===
    for s in sectors:
        net = s["net_inflow"]
        avg_chg = s["avg_change"]
        turnover = s["turnover"]

        # 跌幅小但换手高,说明有资金在换手
        if -2 < avg_chg < 0 and turnover > 3 and net > 0:
            score = min(25, turnover * 2 + abs(avg_chg) * 3)
            predictions.append({
                "name": s["name"],
                "code": s["code"],
                "reason": f"小幅调整{avg_chg:.1f}%但换手{turnover:.1f}%高,资金暗中换手",
                "type": "底部吸筹",
                "score": round(score),
                "confidence": "中",
                "top_stock": s["top_stock"],
                "net_inflow": net,
            })

    # === 策略3: 主力试盘 — 小幅上涨+净流入+高换手 ===
    for s in sectors:
        net = s["net_inflow"]
        avg_chg = s["avg_change"]
        turnover = s["turnover"]
        flow_ratio = s["_flow_ratio"]

        if 0 < avg_chg < 2 and net > 0 and turnover > 2 and flow_ratio > 1.05:
            score = min(28, avg_chg * 5 + turnover * 1.5)
            predictions.append({
                "name": s["name"],
                "code": s["code"],
                "reason": f"微涨{avg_chg:.1f}%+资金净流入+高换手{turnover:.1f}%,主力试探性拉升",
                "type": "主力试盘",
                "score": round(score),
                "confidence": "高",
                "top_stock": s["top_stock"],
                "net_inflow": net,
            })

    # === 策略4: 强势蓄力 — 涨幅不大但资金大幅流入 ===
    for s in sectors:
        net = s["net_inflow"]
        avg_chg = s["avg_change"]
        flow_ratio = s["_flow_ratio"]
        net_intensity = s["_net_intensity"]

        if 0 < avg_chg < 3 and net_intensity > 0.05 and flow_ratio > 1.1:
            score = min(35, net_intensity * 200 + avg_chg * 3)
            predictions.append({
                "name": s["name"],
                "code": s["code"],
                "reason": f"涨幅{avg_chg:.1f}%温和但净流入强度{net_intensity*100:.1f}%高,蓄力待发",
                "type": "强势蓄力",
                "score": round(score),
                "confidence": "高",
                "top_stock": s["top_stock"],
                "net_inflow": net,
            })

    # === 策略5: 跌停洗盘 — 大跌但资金大幅流入(洗盘特征) ===
    for s in sectors:
        net = s["net_inflow"]
        avg_chg = s["avg_change"]
        in_amt = s["in_amount"]
        out_amt = s["out_amount"]

        if avg_chg < -3 and net > 0 and in_amt > out_amt:
            score = min(30, abs(avg_chg) * 4 + (in_amt - out_amt) / in_amt * 50)
            predictions.append({
                "name": s["name"],
                "code": s["code"],
                "reason": f"大跌{avg_chg:.1f}%但资金逆势流入,疑似洗盘",
                "type": "洗盘信号",
                "score": round(score),
                "confidence": "中",
                "top_stock": s["top_stock"],
                "net_inflow": net,
            })

    # 去重(同板块只保留最高分)
    seen = {}
    for p in predictions:
        key = p["code"]
        if key not in seen or p["score"] > seen[key]["score"]:
            seen[key] = p
    predictions = list(seen.values())

    # 按分数排序
    predictions.sort(key=lambda x: x["score"], reverse=True)

    # 限制返回数量
    return predictions[:8]


@app.route("/api/sector_predict")
def sector_predict():
    """预测板块资金流向"""
    fenlei = request.args.get("fenlei", "0")  # 0=行业, 1=概念
    with _lock:
        if fenlei == "1":
            sectors = list(_store.get("concept", []))
            fenlei_name = "概念"
        else:
            sectors = list(_store.get("sector", []))
            fenlei_name = "行业"
    result = _predict_sectors(sectors, fenlei_name)
    return jsonify({"code": 0, "fenlei": fenlei_name, "data": result})


def _predict_stocks(stocks):
    """预测个股可能启动的信号"""
    if not stocks:
        return []
    predictions = []
    for s in stocks:
        pct = s["pct_change"]
        amount = s["amount"]
        turnover = s["turnover"]
        # 策略1: 温和上涨+高成交额(资金关注)
        if 0 < pct < 3 and amount > 5e9:
            score = min(30, pct * 5 + turnover * 0.5)
            predictions.append({
                "name": s["name"], "code": s["code"],
                "reason": f"涨{pct:.1f}%+成交{F(amount)},资金持续关注",
                "type": "资金关注", "score": round(score),
                "confidence": "高", "pct_change": pct, "amount": amount,
            })
        # 策略2: 小幅调整但成交活跃(洗盘后吸筹)
        elif -2 < pct < 0 and turnover > 3 and amount > 3e9:
            score = min(25, abs(pct) * 4 + turnover * 0.8)
            predictions.append({
                "name": s["name"], "code": s["code"],
                "reason": f"跌{pct:.1f}%但换手{turnover:.1f}%高,主力可能吸筹",
                "type": "底部吸筹", "score": round(score),
                "confidence": "中", "pct_change": pct, "amount": amount,
            })
        # 策略3: 大涨放量(强势启动)
        elif pct > 5 and turnover > 5:
            score = min(35, pct * 2 + turnover * 0.3)
            predictions.append({
                "name": s["name"], "code": s["code"],
                "reason": f"涨{pct:.1f}%+换手{turnover:.1f}%,强势启动",
                "type": "强势启动", "score": round(score),
                "confidence": "高", "pct_change": pct, "amount": amount,
            })
    # 去重
    seen = {}
    for p in predictions:
        key = p["code"]
        if key not in seen or p["score"] > seen[key]["score"]:
            seen[key] = p
    predictions = list(seen.values())
    predictions.sort(key=lambda x: x["score"], reverse=True)
    return predictions[:6]


@app.route("/api/stock_predict")
def stock_predict():
    """预测个股资金流向"""
    with _lock:
        stocks = list(_store.get("stock", []))
    result = _predict_stocks(stocks)
    return jsonify({"code": 0, "data": result})


# ===== 个股分析 =====
def _code_to_symbol(code):
    code = code.strip()
    if code.startswith(("sh", "sz")):
        return code
    if code.startswith(("6", "5", "9")):
        return "sh" + code
    if code.startswith(("0", "3", "2")):
        return "sz" + code
    if code.startswith("688"):
        return "sh" + code
    return "sh" + code


def _get_kline(symbol, days=60):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
    data = _j(url, ["-H", f"User-Agent: {UA}", "-H", "Referer: https://web.ifzq.gtimg.cn/"])
    info = data.get("data", {}).get(symbol, {})
    qt = info.get("qt", {}).get(symbol, [])
    klines = info.get("qfqday", []) or info.get("day", [])
    name = qt[1] if len(qt) > 1 else ""
    return name, klines


def _calc_ma(closes, period):
    result = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        result[i] = sum(closes[i - period + 1:i + 1]) / period
    return result


def _analyze_stock(symbol, days=60):
    name, klines = _get_kline(symbol, days)
    if not klines or len(klines) < 15:
        return None

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

    if len(data) < 15:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]

    ma5 = _calc_ma(closes, 5)
    ma10 = _calc_ma(closes, 10)
    ma20 = _calc_ma(closes, 20)
    vol_ma5 = _calc_ma(volumes, 5)

    latest = data[-1]
    cur = latest["close"]

    # === 核心指标计算 ===
    # 最大回撤
    peak = max(highs)
    drawdown = (cur - peak) / peak * 100
    # 60日涨跌幅
    change_60d = (closes[-1] - closes[0]) / closes[0] * 100
    # 20日涨跌幅
    change_20d = (closes[-1] - closes[-20]) / closes[-20] * 100 if len(closes) >= 20 else 0
    # 5日涨跌幅
    change_5d = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0

    # 量能
    vol_5 = sum(volumes[-5:]) / 5
    vol_20 = sum(volumes[-20:]) / min(20, len(volumes[-20:]))
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1

    # 振幅
    amplitude_5d = [(d["high"] - d["low"]) / d["open"] * 100 for d in data[-5:]]
    avg_amplitude = sum(amplitude_5d) / len(amplitude_5d)

    # === 量价背离检测(近5日) ===
    vp_signals = []
    for i in range(-5, 0):
        if i - 1 < -len(data):
            continue
        prev, cur_d = data[i - 1], data[i]
        price_up = cur_d["close"] > prev["close"]
        vol_up = cur_d["volume"] > prev["volume"]
        if price_up and not vol_up:
            vp_signals.append({"date": cur_d["date"], "type": "缩量上涨", "note": "主力控盘,散户惜售"})
        elif not price_up and vol_up:
            vp_signals.append({"date": cur_d["date"], "type": "放量下跌", "note": "恐慌盘涌出或主力出货"})
        elif price_up and vol_up:
            vp_signals.append({"date": cur_d["date"], "type": "放量上涨", "note": "资金积极介入"})
        else:
            vp_signals.append({"date": cur_d["date"], "type": "缩量调整", "note": "正常回调,抛压减弱"})

    # === 支撑压力位计算 ===
    # 方法1: 前高前低
    recent_highs = sorted(highs[-20:], reverse=True)[:3]
    recent_lows = sorted(lows[-20:])[:3]

    # 方法2: 均线支撑
    ma_support = []
    for ma, label in [(ma5, "MA5"), (ma10, "MA10"), (ma20, "MA20")]:
        if ma[-1]:
            diff = abs(cur - ma[-1]) / ma[-1] * 100
            if diff < 3:
                ma_support.append({"price": round(ma[-1], 2), "label": label, "dist": round(diff, 1)})

    # 方法3: 成交量密集区(用价格区间近似)
    vol_price_zones = []
    for d in data[-20:]:
        mid = (d["high"] + d["low"]) / 2
        vol_price_zones.append({"price": mid, "volume": d["volume"]})
    vol_price_zones.sort(key=lambda x: x["volume"], reverse=True)
    top_zones = vol_price_zones[:3]

    # === 行为识别(12条规则) ===
    score_xipan = 0
    score_xichou = 0
    score_chuhuo = 0
    score_zhenDang = 0
    signals = []

    # --- 趋势判断 ---
    trend_down = change_20d < -5
    trend_up = change_20d > 5
    near_low = (cur - min(lows[-20:])) / min(lows[-20:]) * 100 < 5 if len(lows) >= 20 else False

    # 规则1: 急跌后缩量企稳 → 底部吸筹
    if change_20d < -15 and vol_ratio < 0.8 and abs(change_5d) < 3:
        score_xichou += 30
        signals.append({"text": "急跌后缩量企稳,主力可能逢低吸筹", "weight": 30, "type": "吸筹"})

    # 规则2: 暴跌后放量反弹 → 超跌反弹/试探性建仓
    if change_20d < -15 and change_5d > 5 and vol_ratio > 1.5:
        score_xichou += 25
        signals.append({"text": "暴跌后放量反弹,资金试探性介入", "weight": 25, "type": "吸筹"})

    # 规则3: 缩量阴跌 → 洗盘末期或无庄
    if change_20d < -10 and change_5d < -2 and vol_ratio < 0.7:
        score_xipan += 20
        signals.append({"text": "缩量阴跌,洗盘末期或无庄股", "weight": 20, "type": "洗盘"})

    # 规则4: 放量长上影线 → 拉高出货
    last = data[-1]
    upper_shadow = (last["high"] - max(last["open"], last["close"])) / last["open"] * 100
    if upper_shadow > 3 and vol_ratio > 1.5:
        score_chuhuo += 25
        signals.append({"text": "放量长上影线,拉高后遭遇抛压,注意出货", "weight": 25, "type": "出货"})

    # 规则5: 底部长下影阳线 → 止跌信号
    lower_shadow = (min(last["open"], last["close"]) - last["low"]) / last["open"] * 100
    if lower_shadow > 2 and last["close"] > last["open"] and near_low:
        score_xichou += 20
        signals.append({"text": "底部长下影阳线,止跌信号明确", "weight": 20, "type": "吸筹"})

    # 规则6: 高位横盘放量 → 派发
    if abs(change_5d) < 2 and change_20d > 10 and vol_ratio > 1.3:
        score_chuhuo += 25
        signals.append({"text": "高位横盘放量,主力可能派发筹码", "weight": 25, "type": "出货"})

    # 规则7: 连续缩量3日以上 → 抛压衰竭
    if all(volumes[-i] < volumes[-i - 1] for i in range(1, min(4, len(volumes)))):
        score_xichou += 15
        signals.append({"text": "连续缩量,抛压逐渐衰竭", "weight": 15, "type": "吸筹"})

    # 规则8: 放量突破MA5 → 短期转强
    if ma5[-1] and cur > ma5[-1] and data[-2]["close"] < ma5[-2] and vol_ratio > 1.2:
        score_xichou += 15
        signals.append({"text": "放量突破MA5,短期趋势转强", "weight": 15, "type": "吸筹"})

    # 规则9: 跌破MA20后快速收回 → 洗盘
    if ma20[-1] and data[-2]["close"] < ma20[-2] and cur > ma20[-1]:
        score_xipan += 20
        signals.append({"text": "跌破MA20后快速收回,典型洗盘手法", "weight": 20, "type": "洗盘"})

    # 规则10: 量价背离(价涨量缩) → 上涨乏力
    if change_5d > 2 and vol_ratio < 0.7:
        score_chuhuo += 15
        signals.append({"text": "价涨量缩,上涨乏力,注意回调风险", "weight": 15, "type": "出货"})

    # 规则11: 急跌后十字星 → 变盘信号
    body = abs(last["close"] - last["open"]) / last["open"] * 100
    if body < 0.5 and change_20d < -10:
        score_xichou += 15
        signals.append({"text": "急跌后十字星,变盘信号,关注方向选择", "weight": 15, "type": "吸筹"})

    # 规则12: 底部连续小阳线 → 主力缓慢建仓
    last3 = data[-3:]
    if all(d["close"] > d["open"] for d in last3) and all(abs(d["close"] - d["open"]) / d["open"] * 100 < 2 for d in last3):
        if near_low:
            score_xichou += 20
            signals.append({"text": "底部连续小阳线,主力缓慢建仓迹象", "weight": 20, "type": "吸筹"})

    # --- 综合判断 ---
    total = score_xichou + score_xipan + score_chuhuo
    if total == 0:
        total = 1

    if score_xichou > score_xipan and score_xichou > score_chuhuo and score_xichou >= 15:
        behavior = "吸筹"
        behavior_desc = "主力可能正在低位吸筹,关注放量突破和均线金叉信号"
        behavior_color = "#ef4444"
    elif score_xipan > score_xichou and score_xipan >= 15:
        behavior = "洗盘"
        behavior_desc = "主力可能正在洗盘,关注缩量企稳后的反弹机会"
        behavior_color = "#22c55e"
    elif score_chuhuo > score_xichou and score_chuhuo > score_xipan and score_chuhuo >= 15:
        behavior = "出货"
        behavior_desc = "主力可能正在出货,注意控制风险,不宜追高"
        behavior_color = "#f59e0b"
    else:
        behavior = "震荡"
        behavior_desc = "当前处于震荡整理阶段,方向不明,建议观望"
        behavior_color = "#94a3b8"

    # === 买卖点位(定制规则) ===
    low_20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    high_20 = max(highs[-20:]) if len(highs) >= 20 else max(highs)

    buy_points = []
    sell_points = []

    # 买入规则:
    # B1: 前低支撑(20日最低价附近)
    buy_points.append({
        "price": round(low_20, 2),
        "label": "B1 前低支撑",
        "reason": "20日最低价区域,跌破则止损",
        "risk": "中"
    })
    # B2: 均线支撑
    if ma20[-1]:
        buy_points.append({
            "price": round(ma20[-1], 2),
            "label": "B2 MA20支撑",
            "reason": "20日均线回踩确认",
            "risk": "中"
        })
    # B3: 放量阳线实体下沿(如果最近有放量阳线)
    for d in data[-5:]:
        if d["close"] > d["open"] and d["volume"] > vol_20 * 1.3:
            buy_points.append({
                "price": round(d["open"], 2),
                "label": "B3 放量阳线底",
                "reason": d["date"] + " 放量阳线开盘价",
                "risk": "低"
            })
            break
    # B4: 黄金分割位(从近期高低点算)
    if high_20 > low_20:
        fib_382 = low_20 + (high_20 - low_20) * 0.382
        fib_500 = low_20 + (high_20 - low_20) * 0.5
        buy_points.append({
            "price": round(fib_382, 2),
            "label": "B4 黄金分割0.382",
            "reason": "0.382回撤位",
            "risk": "中"
        })
        buy_points.append({
            "price": round(fib_500, 2),
            "label": "B5 黄金分割0.5",
            "reason": "0.5回撤位",
            "risk": "高"
        })

    # 卖出规则:
    # S1: 前高压力
    sell_points.append({
        "price": round(high_20, 2),
        "label": "S1 前高压力",
        "reason": "20日最高价区域,突破可加仓",
        "risk": "-"
    })
    # S2: MA10压力
    if ma10[-1] and ma10[-1] > cur:
        sell_points.append({
            "price": round(ma10[-1], 2),
            "label": "S2 MA10压力",
            "reason": "10日均线压力位",
            "risk": "-"
        })
    # S3: 放量阴线实体上沿
    for d in data[-5:]:
        if d["close"] < d["open"] and d["volume"] > vol_20 * 1.3:
            sell_points.append({
                "price": round(d["open"], 2),
                "label": "S3 放量阴线顶",
                "reason": d["date"] + " 放量阴线开盘价",
                "risk": "-"
            })
            break
    # S4: 黄金分割压力
    if high_20 > low_20:
        fib_618 = low_20 + (high_20 - low_20) * 0.618
        sell_points.append({
            "price": round(fib_618, 2),
            "label": "S4 黄金分割0.618",
            "reason": "0.618压力位",
            "risk": "-"
        })

    # 按价格排序
    buy_points.sort(key=lambda x: x["price"], reverse=True)
    sell_points.sort(key=lambda x: x["price"])

    return {
        "symbol": symbol, "name": name,
        "latest": cur,
        "pct_change": round((cur - data[-2]["close"]) / data[-2]["close"] * 100, 2) if len(data) > 1 else 0,
        "kline": data,
        "ma5": [round(v, 2) if v else None for v in ma5],
        "ma10": [round(v, 2) if v else None for v in ma10],
        "ma20": [round(v, 2) if v else None for v in ma20],
        "vol_ma5": [round(v, 0) if v else None for v in vol_ma5],
        "analysis": {
            "behavior": behavior,
            "behavior_desc": behavior_desc,
            "behavior_color": behavior_color,
            "score_xipan": round(score_xipan / total * 100),
            "score_xichou": round(score_xichou / total * 100),
            "score_chuhuo": round(score_chuhuo / total * 100),
            "signals": signals,
            "vol_trend": round(vol_ratio, 2),
            "price_change_5d": round(change_5d, 2),
            "price_change_20d": round(change_20d, 2),
            "drawdown": round(drawdown, 2),
            "avg_amplitude": round(avg_amplitude, 2),
            "vp_signals": vp_signals[-5:],
        },
        "buy_points": buy_points,
        "sell_points": sell_points,
    }


# ===== 日志模块 =====
def _log_file(date_str=None):
    """获取/创建指定日期的日志文件路径"""
    if not date_str:
        date_str = datetime.date.today().strftime("%Y-%m-%d")
    return os.path.join(LOG_DIR, f"{date_str}.csv")


def _log_exists(fpath, symbol):
    """检查今天是否已记录该股票"""
    if not os.path.exists(fpath):
        return False
    with open(fpath, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if row and row[0] == symbol:
                return True
    return False


def _save_log(result):
    """保存分析结果到日志"""
    today = datetime.date.today().strftime("%Y-%m-%d")
    fpath = _log_file(today)
    a = result["analysis"]
    write_header = not os.path.exists(fpath)

    with open(fpath, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if write_header:
            w.writerow([
                "symbol", "name", "date", "price", "pct_change",
                "behavior", "score_xichou", "score_xipan", "score_chuhuo",
                "signals_count", "vol_ratio", "change_5d", "change_20d",
                "drawdown", "buy_price", "sell_price"
            ])
        signals = a["signals"]
        # 提取最近的买卖点价格
        buy_prices = ";".join([str(p["price"]) for p in result["buy_points"][:3]])
        sell_prices = ";".join([str(p["price"]) for p in result["sell_points"][:3]])
        w.writerow([
            result["symbol"], result["name"], today, result["latest"],
            result["pct_change"], a["behavior"],
            a["score_xichou"], a["score_xipan"], a["score_chuhuo"],
            len(signals), a["vol_trend"], a["price_change_5d"],
            a["price_change_20d"], a["drawdown"],
            buy_prices, sell_prices
        ])
    print(f"[LOG] {result['name']}({result['symbol']}) -> {fpath}", flush=True)


def _load_logs(date_str=None, symbol=None):
    """加载日志记录, 可按日期和股票筛选"""
    if date_str:
        fpath = _log_file(date_str)
        if not os.path.exists(fpath):
            return []
        files = [(date_str, fpath)]
    else:
        files = []
        for fname in sorted(os.listdir(LOG_DIR), reverse=True):
            if fname.endswith(".csv"):
                files.append((fname[:-4], os.path.join(LOG_DIR, fname)))

    rows = []
    for date, fpath in files:
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if symbol and row.get("symbol") != symbol:
                        continue
                    row["date"] = date
                    for k in ["price", "pct_change", "score_xichou", "score_xipan",
                              "score_chuhuo", "vol_ratio", "change_5d",
                              "change_20d", "drawdown"]:
                        try:
                            row[k] = float(row[k])
                        except (ValueError, KeyError):
                            row[k] = 0
                    try:
                        row["signals_count"] = int(row.get("signals_count", 0))
                    except ValueError:
                        row["signals_count"] = 0
                    rows.append(row)
        except Exception:
            continue
    return rows


def _calc_success_rate():
    """计算预测成功率 AB组对比: A=次日验证, B=5日验证"""
    all_logs = _load_logs()
    if not all_logs:
        return {"total": 0, "records": [], "summary": {}, "group_a": {}, "group_b": {}}

    # 按股票分组
    by_stock = {}
    for r in all_logs:
        sym = r["symbol"]
        if sym not in by_stock:
            by_stock[sym] = []
        by_stock[sym].append(r)

    # AB组统计
    group_a = {"total": 0, "correct": 0, "by_behavior": {}}
    group_b = {"total": 0, "correct": 0, "by_behavior": {}}
    stock_results = []

    for sym, records in by_stock.items():
        records.sort(key=lambda x: x["date"])
        if len(records) < 2:
            continue

        a_correct = 0
        a_total = 0
        b_correct = 0
        b_total = 0
        details = []

        for i in range(len(records) - 1):
            today_r = records[i]
            today_price = today_r["price"]
            behavior = today_r["behavior"]

            # === A组: 次日验证 ===
            tomorrow_r = records[i + 1]
            tomorrow_price = tomorrow_r["price"]
            day1_change = (tomorrow_price - today_price) / today_price * 100

            a_hit = False
            if behavior == "吸筹" and day1_change > 0:
                a_hit = True
            elif behavior == "出货" and day1_change < 0:
                a_hit = True
            elif behavior == "震荡":
                a_hit = abs(day1_change) < 3
            elif behavior == "洗盘":
                if i + 2 < len(records):
                    day3_price = records[i + 2]["price"]
                    a_hit = day1_change < 0 and day3_price > tomorrow_price
                else:
                    a_hit = True

            if behavior != "洗盘" or i + 2 < len(records):
                a_total += 1
                if a_hit:
                    a_correct += 1

            # === B组: 5日验证 ===
            b_hit = False
            end_idx = min(i + 6, len(records))  # 最多看5天
            if end_idx > i + 1:
                future_prices = [records[j]["price"] for j in range(i + 1, end_idx)]
                if behavior == "吸筹":
                    # 5日内最高价比今天涨了就算对
                    max_future = max(future_prices)
                    b_hit = (max_future - today_price) / today_price > 0
                elif behavior == "出货":
                    # 5日内最低价比今天跌了就算对
                    min_future = min(future_prices)
                    b_hit = (min_future - today_price) / today_price < 0
                elif behavior == "震荡":
                    # 5日内最大波动<5%算对
                    max_change = max(abs((p - today_price) / today_price * 100) for p in future_prices)
                    b_hit = max_change < 5
                elif behavior == "洗盘":
                    # 5日内先跌后涨算对
                    if len(future_prices) >= 2:
                        min_idx = future_prices.index(min(future_prices))
                        b_hit = min_idx > 0 and future_prices[-1] > today_price
                    else:
                        b_hit = True

                b_total += 1
                if b_hit:
                    b_correct += 1

            details.append({
                "date": today_r["date"],
                "behavior": behavior,
                "price": today_price,
                "day1_change": round(day1_change, 2),
                "a_hit": a_hit,
                "b_hit": b_hit,
            })

        # 按行为类型统计A组
        for d in details:
            b = d["behavior"]
            if b not in group_a["by_behavior"]:
                group_a["by_behavior"][b] = {"total": 0, "correct": 0}
            group_a["by_behavior"][b]["total"] += 1
            if d["a_hit"]:
                group_a["by_behavior"][b]["correct"] += 1

            if b not in group_b["by_behavior"]:
                group_b["by_behavior"][b] = {"total": 0, "correct": 0}
            group_b["by_behavior"][b]["total"] += 1
            if d["b_hit"]:
                group_b["by_behavior"][b]["correct"] += 1

        group_a["total"] += a_total
        group_a["correct"] += a_correct
        group_b["total"] += b_total
        group_b["correct"] += b_correct

        if a_total > 0:
            stock_results.append({
                "symbol": sym,
                "name": records[0].get("name", ""),
                "a_total": a_total, "a_correct": a_correct,
                "a_rate": round(a_correct / a_total * 100, 1),
                "b_total": b_total, "b_correct": b_correct,
                "b_rate": round(b_correct / b_total * 100, 1) if b_total > 0 else 0,
                "details": details
            })

    # 计算百分比
    group_a["overall_rate"] = round(group_a["correct"] / group_a["total"] * 100, 1) if group_a["total"] > 0 else 0
    group_b["overall_rate"] = round(group_b["correct"] / group_b["total"] * 100, 1) if group_b["total"] > 0 else 0
    for b in group_a["by_behavior"]:
        s = group_a["by_behavior"][b]
        s["rate"] = round(s["correct"] / s["total"] * 100, 1) if s["total"] > 0 else 0
    for b in group_b["by_behavior"]:
        s = group_b["by_behavior"][b]
        s["rate"] = round(s["correct"] / s["total"] * 100, 1) if s["total"] > 0 else 0

    return {
        "total": group_a["total"],
        "correct": group_a["correct"],
        "overall_rate": group_a["overall_rate"],
        "by_behavior": group_a["by_behavior"],
        "stocks": sorted(stock_results, key=lambda x: x["a_rate"], reverse=True),
        "group_a": {
            "label": "A组: 次日验证",
            "desc": "预测后次日涨跌即验证",
            "total": group_a["total"],
            "correct": group_a["correct"],
            "overall_rate": group_a["overall_rate"],
            "by_behavior": group_a["by_behavior"],
        },
        "group_b": {
            "label": "B组: 5日验证",
            "desc": "预测后5日内走势验证",
            "total": group_b["total"],
            "correct": group_b["correct"],
            "overall_rate": group_b["overall_rate"],
            "by_behavior": group_b["by_behavior"],
        }
    }


# ===== 日志API =====
@app.route("/api/log/save", methods=["POST"])
def api_log_save():
    """手动保存某只股票的分析结果"""
    data = request.get_json(force=True, silent=True) or {}
    code = data.get("code", "").strip()
    if not code:
        return jsonify({"code": -1, "msg": "请输入股票代码"})
    symbol = _code_to_symbol(code)
    result = _analyze_stock(symbol)
    if not result:
        return jsonify({"code": -1, "msg": f"未找到股票 {code} 的数据"})
    _save_log(result)
    return jsonify({"code": 0, "msg": f"{result['name']} 分析结果已保存"})


@app.route("/api/log/list")
def api_log_list():
    """查询日志记录"""
    date = request.args.get("date", "")
    symbol = request.args.get("symbol", "")
    logs = _load_logs(date_str=date or None, symbol=symbol or None)
    return jsonify({"code": 0, "data": logs})


@app.route("/api/log/success_rate")
def api_log_success_rate():
    """计算预测成功率"""
    return jsonify({"code": 0, "data": _calc_success_rate()})


@app.route("/api/log/dates")
def api_log_dates():
    """列出有日志的日期"""
    dates = []
    for fname in sorted(os.listdir(LOG_DIR), reverse=True):
        if fname.endswith(".csv"):
            dates.append(fname[:-4])
    return jsonify({"code": 0, "data": dates})


# ===== 股票名称搜索 =====
def _search_stock_by_name(keyword):
    """通过关键词搜索股票(名称/代码)"""
    url = f"https://suggest3.sinajs.cn/suggest/type=&key={keyword}"
    raw = _curl(url, ["-H", f"Referer: https://finance.sina.com.cn/"], enc="gbk")
    if not raw:
        return []
    results = []
    for line in raw.split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            # var suggestvalue="sh600519,11,600519,sh600519,贵州茅台,,贵州茅台,99,1,ESG,,";
            val = line.split('"')[1]
            if not val:
                continue
            arr = val.split(",")
            if len(arr) < 4:
                continue
            # arr[2]=代码, arr[3]=sh/sz+代码, arr[4]=名称
            code = arr[2]
            full = arr[3]  # sh600519
            market = full[:2]  # sh/sz
            if market not in ("sh", "sz"):
                continue
            name = arr[4] if len(arr) > 4 and arr[4] else code
            results.append({
                "code": code,
                "name": name,
                "symbol": full,
                "market": "沪" if market == "sh" else "深",
            })
        except Exception:
            continue
    return results[:10]


@app.route("/api/stock_name_search")
def stock_name_search():
    """按名称/代码搜索股票"""
    kw = request.args.get("kw", "").strip()
    if not kw:
        return jsonify({"code": -1, "msg": "请输入搜索关键词"})
    results = _search_stock_by_name(kw)
    return jsonify({"code": 0, "data": results})


@app.route("/api/stock_search")
def stock_search():
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"code": -1, "msg": "请输入股票代码"})
    symbol = _code_to_symbol(code)
    result = _analyze_stock(symbol)
    if not result:
        return jsonify({"code": -1, "msg": f"未找到股票 {code} 的数据"})
    # 自动记录日志
    try:
        _save_log(result)
    except Exception as e:
        print(f"[LOG ERR] {e}", flush=True)
    return jsonify({"code": 0, "data": result})


# ===== 低位埋伏股(价值投资) =====
def _get_value_stocks():
    """获取低位埋伏股候选池：低估值+高股息+超跌"""
    candidates = []

    # 从预设的价值股池子中筛选
    value_pool = [
        "sh601398","sh601939","sh601288","sh601328","sh601988",
        "sh601088","sh601857","sh601628","sh601601","sh601318",
        "sh600519","sz000858","sh600036","sz000001","sh601166",
        "sh600276","sz000568","sz000651","sh600887","sh603288",
        "sz000002","sz002475","sz002594","sh600900","sh600585",
        "sh601899","sz002371","sz300059","sz002049","sz002714",
        "sh600031","sz300015","sz000725","sh600030","sh601688",
        "sh600048","sz000063","sh601669","sh601211","sh601012",
        "sz002304","sz000333","sh600309","sh601009","sh600000",
        "sh601998","sz000002","sh601390","sh601186","sh601766",
        "sh601800","sh601919","sz002142","sz000069","sz002027",
    ]
    codes = list(set(value_pool))

    raw = _curl(f"https://qt.gtimg.cn/q={','.join(codes)}",
                ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")
    for line in raw.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            p = line.split("=", 1)[1].strip('"').split("~")
            if len(p) < 46:
                continue
            latest = float(p[3] or 0)
            pct = float(p[32] or 0)
            amount = float(p[37] or 0)
            turnover = float(p[38] or 0)
            pe = float(p[39] or 0)
            pb = float(p[46] or 0) if len(p) > 46 else 0
            high_52w = float(p[33] or 0) if len(p) > 33 else 0
            low_52w = float(p[34] or 0) if len(p) > 34 else 0
            name = p[1]
            code = p[2]

            # 过滤无效数据
            if latest <= 0 or pe <= 0:
                continue

            # 计算距52周高点的回撤
            drawdown_from_high = (latest - high_52w) / high_52w * 100 if high_52w > 0 else 0
            # 计算距52周低点的距离
            above_low = (latest - low_52w) / low_52w * 100 if low_52w > 0 else 0

            candidates.append({
                "code": code, "name": name,
                "latest": latest, "pct_change": pct,
                "pe": round(pe, 2), "pb": round(pb, 2),
                "amount": amount, "turnover": turnover,
                "high_52w": high_52w, "low_52w": low_52w,
                "drawdown_from_high": round(drawdown_from_high, 2),
                "above_low": round(above_low, 2),
            })
        except Exception:
            continue

    # 评分策略：低PE + 低PB + 大幅回撤 + 接近低点
    for s in candidates:
        score = 0
        # PE评分 (PE越低越好, 0-15分)
        if s["pe"] > 0:
            if s["pe"] < 8: score += 15
            elif s["pe"] < 12: score += 12
            elif s["pe"] < 18: score += 8
            elif s["pe"] < 25: score += 4

        # PB评分 (PB越低越好, 0-15分)
        if s["pb"] > 0:
            if s["pb"] < 0.8: score += 15
            elif s["pb"] < 1.2: score += 12
            elif s["pb"] < 1.8: score += 8
            elif s["pb"] < 2.5: score += 4

        # 回撤评分 (回撤越大机会越大, 0-20分)
        dd = abs(s["drawdown_from_high"])
        if dd > 40: score += 20
        elif dd > 30: score += 16
        elif dd > 20: score += 12
        elif dd > 10: score += 6

        # 接近低点评分 (越接近低点越安全, 0-15分)
        al = s["above_low"]
        if al < 5: score += 15
        elif al < 10: score += 12
        elif al < 20: score += 8
        elif al < 30: score += 4

        # 当日不跌太多（-3%以内）加分
        if -3 < s["pct_change"] < 0: score += 5
        elif s["pct_change"] >= 0: score += 3

        s["score"] = score

        # 投资建议
        if score >= 45:
            s["advice"] = "强烈推荐"
            s["advice_color"] = "#ef4444"
        elif score >= 35:
            s["advice"] = "推荐"
            s["advice_color"] = "#f59e0b"
        elif score >= 25:
            s["advice"] = "关注"
            s["advice_color"] = "#3b82f6"
        else:
            s["advice"] = "观望"
            s["advice_color"] = "#94a3b8"

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:15]


@app.route("/api/value_stocks")
def api_value_stocks():
    """低位埋伏股"""
    result = _get_value_stocks()
    return jsonify({"code": 0, "data": result})


# ===== 短线交易(尾盘埋伏) =====
def _get_short_term_stocks():
    """短线交易：寻找尾盘放量+主力流入的股票"""
    candidates = []

    # 热门活跃股池
    hot_pool = [
        "sh600519","sh601318","sz000858","sh600036","sz002594",
        "sz300750","sz002475","sh600900","sz000001","sh601899",
        "sz002371","sz000651","sz300059","sz002049","sh601166",
        "sz000568","sz300760","sh600887","sh603288","sz002714",
        "sh601398","sh600276","sz300274","sh688981","sz002230",
        "sh600031","sz300015","sz000725","sh600585","sz000002",
        "sh601688","sh600030","sz002142","sz000333","sh600309",
        "sh601012","sz002304","sh601088","sh601857","sh601628",
        "sh601211","sh601919","sh600048","sz000063","sh601669",
        "sh601766","sh601800","sz000069","sz002027","sh601009",
        "sh600000","sh601998","sh601390","sh601186","sz000688",
        "sz002415","sz000725","sz002304","sz300015","sh600809",
        "sz000568","sh603369","sz002032","sz002241","sz300274",
        "sh688981","sh600745","sz002475","sz300750","sh603259",
        "sh601127","sz002594","sz300496","sh600760","sz002049",
    ]
    codes = list(set(hot_pool))

    raw = _curl(f"https://qt.gtimg.cn/q={','.join(codes)}",
                ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")

    for line in raw.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            p = line.split("=", 1)[1].strip('"').split("~")
            if len(p) < 46:
                continue
            name = p[1]
            code = p[2]
            latest = float(p[3] or 0)
            pct = float(p[32] or 0)
            amount = float(p[37] or 0)
            turnover = float(p[38] or 0)
            # p[49]=外盘, p[50]=内盘
            outer = float(p[49] or 0) if len(p) > 49 else 0
            inner = float(p[50] or 0) if len(p) > 50 else 0
            # 今日开盘价
            open_price = float(p[5] or 0) if len(p) > 5 else 0
            # 昨日收盘
            prev_close = float(p[4] or 0) if len(p) > 4 else 0

            if latest <= 0 or amount <= 0:
                continue

            # 外盘占比（主动买入力度）
            total_vol = outer + inner
            outer_ratio = outer / total_vol * 100 if total_vol > 0 else 50

            candidates.append({
                "code": code, "name": name,
                "latest": latest, "open": open_price,
                "prev_close": prev_close, "pct_change": pct,
                "amount": amount, "turnover": turnover,
                "outer": outer, "inner": inner,
                "outer_ratio": round(outer_ratio, 1),
            })
        except Exception:
            continue

    # 短线评分策略
    for s in candidates:
        score = 0
        pct = s["pct_change"]
        turnover = s["turnover"]
        amount = s["amount"]
        outer_ratio = s["outer_ratio"]
        latest = s["latest"]
        open_p = s["open"]
        prev = s["prev_close"]

        # 条件1: 涨幅适中 (1%~5% 区间最佳)
        if 1 <= pct <= 3: score += 20
        elif 3 < pct <= 5: score += 15
        elif 0.5 <= pct < 1: score += 10
        elif 5 < pct <= 7: score += 8

        # 条件2: 高换手 (活跃度)
        if turnover > 8: score += 15
        elif turnover > 5: score += 12
        elif turnover > 3: score += 8
        elif turnover > 1.5: score += 4

        # 条件3: 高成交额 (资金关注)
        if amount > 20e9: score += 12
        elif amount > 10e9: score += 10
        elif amount > 5e9: score += 7
        elif amount > 2e9: score += 4

        # 条件4: 外盘占比高 (主动买入强)
        if outer_ratio > 65: score += 15
        elif outer_ratio > 58: score += 10
        elif outer_ratio > 52: score += 5

        # 条件5: 尾盘拉升特征 (收盘价接近最高价 - 用开盘到收盘判断)
        if open_p > 0 and latest > open_p:
            intraday_strength = (latest - open_p) / open_p * 100
            if intraday_strength > 3: score += 12
            elif intraday_strength > 1.5: score += 8
            elif intraday_strength > 0.5: score += 4

        # 条件6: 不追高（避免高位接盘）
        if 0 < pct < 6: score += 5
        elif pct >= 6: score -= 5

        # 条件7: 连板风险扣分
        if pct > 9.5: score -= 20

        s["score"] = max(0, score)

        # 预测次日高开概率
        if score >= 55:
            s["signal"] = "强买入"
            s["signal_color"] = "#ef4444"
            s["confidence"] = "高"
            s["expected_open"] = f"+{round(pct*0.3+pct*0.5, 1)}%"
        elif score >= 40:
            s["signal"] = "买入"
            s["signal_color"] = "#f59e0b"
            s["confidence"] = "中"
            s["expected_open"] = f"+{round(pct*0.2+pct*0.3, 1)}%"
        elif score >= 28:
            s["signal"] = "关注"
            s["signal_color"] = "#3b82f6"
            s["confidence"] = "中低"
            s["expected_open"] = f"+{round(pct*0.1, 1)}%"
        else:
            s["signal"] = "观望"
            s["signal_color"] = "#94a3b8"
            s["confidence"] = "低"
            s["expected_open"] = "平开"

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:15]


@app.route("/api/short_term")
def api_short_term():
    """短线交易(尾盘埋伏)"""
    result = _get_short_term_stocks()
    return jsonify({"code": 0, "data": result})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=_loop, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=port)
