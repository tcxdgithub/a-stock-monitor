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


def _calc_ema(data, period):
    """计算EMA"""
    ema = [None] * len(data)
    if len(data) < period:
        return ema
    # 第一个EMA用SMA初始化
    ema[period - 1] = sum(data[:period]) / period
    multiplier = 2 / (period + 1)
    for i in range(period, len(data)):
        ema[i] = (data[i] - ema[i - 1]) * multiplier + ema[i - 1]
    return ema


def _calc_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD: DIF, DEA, MACD柱"""
    if len(closes) < slow:
        return [], [], []
    
    ema_fast = _calc_ema(closes, fast)
    ema_slow = _calc_ema(closes, slow)
    
    # DIF = EMA快线 - EMA慢线
    dif = [None] * len(closes)
    for i in range(len(closes)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            dif[i] = ema_fast[i] - ema_slow[i]
    
    # DEA = DIF的EMA
    dif_values = [v for v in dif if v is not None]
    if len(dif_values) < signal:
        return dif, [], []
    
    dea = [None] * len(closes)
    # 找到第一个非None的DIF位置
    start_idx = next((i for i, v in enumerate(dif) if v is not None), len(dif))
    if start_idx + signal <= len(dif):
        dea[start_idx + signal - 1] = sum(dif_values[:signal]) / signal
        multiplier = 2 / (signal + 1)
        for i in range(start_idx + signal, len(dif)):
            if dif[i] is not None and dea[i - 1] is not None:
                dea[i] = (dif[i] - dea[i - 1]) * multiplier + dea[i - 1]
    
    # MACD柱 = (DIF - DEA) * 2
    macd_bar = [None] * len(closes)
    for i in range(len(closes)):
        if dif[i] is not None and dea[i] is not None:
            macd_bar[i] = (dif[i] - dea[i]) * 2
    
    return dif, dea, macd_bar


def _calc_kdj(highs, lows, closes, period=9):
    """计算KDJ指标: K, D, J"""
    n = len(closes)
    if n < period:
        return [], [], []
    
    k_values = [None] * n
    d_values = [None] * n
    j_values = [None] * n
    
    # 计算RSV
    rsv = [None] * n
    for i in range(period - 1, n):
        highest = max(highs[i - period + 1:i + 1])
        lowest = min(lows[i - period + 1:i + 1])
        if highest != lowest:
            rsv[i] = (closes[i] - lowest) / (highest - lowest) * 100
        else:
            rsv[i] = 50
    
    # 计算K, D, J (使用SMA平滑)
    k_values[period - 1] = 50  # 初始值
    d_values[period - 1] = 50
    
    for i in range(period, n):
        if rsv[i] is not None:
            k_values[i] = 2/3 * k_values[i-1] + 1/3 * rsv[i]
            d_values[i] = 2/3 * d_values[i-1] + 1/3 * k_values[i]
            j_values[i] = 3 * k_values[i] - 2 * d_values[i]
    
    return k_values, d_values, j_values


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
    vol_ma10 = _calc_ma(volumes, 10)
    vol_ma20 = _calc_ma(volumes, 20)
    dif, dea, macd_bar = _calc_macd(closes)
    k_values, d_values, j_values = _calc_kdj(highs, lows, closes)

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
    
    # === 量能分级（百分位法） ===
    vol_sorted = sorted(volumes[-20:]) if len(volumes) >= 20 else sorted(volumes)
    vol_percentile = sum(1 for v in vol_sorted if v <= volumes[-1]) / len(vol_sorted) * 100 if vol_sorted else 50
    
    if vol_percentile <= 10:
        vol_level = "地量"
        vol_level_desc = "成交极度低迷,可能见底"
    elif vol_percentile <= 30:
        vol_level = "缩量"
        vol_level_desc = "成交清淡,抛压减弱"
    elif vol_percentile <= 70:
        vol_level = "温和"
        vol_level_desc = "成交正常"
    elif vol_percentile <= 90:
        vol_level = "放量"
        vol_level_desc = "成交活跃,资金介入"
    else:
        vol_level = "天量"
        vol_level_desc = "成交异常放大,注意变盘"
    
    # === 量价趋势性背离检测 ===
    price_divergence = None
    vol_divergence = None
    
    if len(closes) >= 10 and len(volumes) >= 10:
        # 价格趋势：近5日高点 vs 前5日高点
        recent_highs = [d["high"] for d in data[-5:]]
        prev_highs = [d["high"] for d in data[-10:-5]]
        price_trend_up = max(recent_highs) > max(prev_highs)
        price_trend_down = min(recent_highs) < min(prev_highs)
        
        # 成交量趋势：近5日均量 vs 前5日均量
        recent_vol_avg = sum(volumes[-5:]) / 5
        prev_vol_avg = sum(volumes[-10:-5]) / 5
        vol_trend_up = recent_vol_avg > prev_vol_avg * 1.1
        vol_trend_down = recent_vol_avg < prev_vol_avg * 0.9
        
        # 顶背离：价格创新高但量能萎缩
        if price_trend_up and vol_trend_down:
            price_divergence = "顶背离"
        # 底背离：价格创新低但量能萎缩（地量见地价）
        elif price_trend_down and vol_trend_down:
            price_divergence = "底背离"
    
    # === 成交量均线趋势 ===
    vol_trend_direction = "横盘"
    vol_trend_strength = 0
    if len(vol_ma5) >= 3 and len(vol_ma10) >= 3 and len(vol_ma20) >= 3:
        if vol_ma5[-1] and vol_ma10[-1] and vol_ma20[-1]:
            # 量能多头排列：vol_ma5 > vol_ma10 > vol_ma20
            if vol_ma5[-1] > vol_ma10[-1] > vol_ma20[-1]:
                vol_trend_direction = "量能递增"
                vol_trend_strength = 1
            # 量能空头排列：vol_ma5 < vol_ma10 < vol_ma20
            elif vol_ma5[-1] < vol_ma10[-1] < vol_ma20[-1]:
                vol_trend_direction = "量能递减"
                vol_trend_strength = -1
            # 量能金叉：vol_ma5上穿vol_ma10
            elif vol_ma5[-2] and vol_ma10[-2] and vol_ma5[-2] < vol_ma10[-2] and vol_ma5[-1] > vol_ma10[-1]:
                vol_trend_direction = "量能金叉"
                vol_trend_strength = 0.5
            # 量能死叉：vol_ma5下穿vol_ma10
            elif vol_ma5[-2] and vol_ma10[-2] and vol_ma5[-2] > vol_ma10[-2] and vol_ma5[-1] < vol_ma10[-1]:
                vol_trend_direction = "量能死叉"
                vol_trend_strength = -0.5
    
    # === 异常量检测 ===
    vol_anomaly = None
    vol_anomaly_type = None
    if len(volumes) >= 20:
        vol_std = (sum((v - vol_20)**2 for v in volumes[-20:]) / 20) ** 0.5
        if vol_std > 0:
            vol_zscore = (volumes[-1] - vol_20) / vol_std
            if vol_zscore > 2:
                vol_anomaly = "异常放量"
                vol_anomaly_type = "顶部异常" if change_20d > 10 else "底部异常" if change_20d < -10 else "中部异常"
            elif vol_zscore < -2:
                vol_anomaly = "异常缩量"
                vol_anomaly_type = "地量信号" if vol_percentile < 15 else "正常缩量"
        
        # 突然缩量检测
        if volumes[-2] > 0 and volumes[-1] < volumes[-2] * 0.5:
            vol_anomaly = "量能骤降"
            vol_anomaly_type = "昨日量的" + str(round(volumes[-1]/volumes[-2]*100)) + "%"
    
    # === 量价配合度评分 (0-100) ===
    vp_harmony = 50  # 基准分
    
    # 1. 量价同向性 (±20分)
    last_pct = (cur - data[-2]["close"]) / data[-2]["close"] * 100 if len(data) >= 2 else 0
    if last_pct > 0 and vol_ratio > 1:
        vp_harmony += 20  # 量价齐升，健康
    elif last_pct < 0 and vol_ratio < 1:
        vp_harmony += 10  # 量价齐跌，正常调整
    elif last_pct > 0 and vol_ratio < 0.7:
        vp_harmony -= 15  # 缩量上涨，不健康
    elif last_pct < 0 and vol_ratio > 1.3:
        vp_harmony -= 20  # 放量下跌，危险
    
    # 2. 量能位置 (±15分)
    if vol_percentile < 20 and change_20d < -10:
        vp_harmony += 15  # 底部地量，可能见底
    elif vol_percentile > 80 and change_20d > 10:
        vp_harmony -= 15  # 顶部天量，可能见顶
    
    # 3. 量能趋势 (±10分)
    if vol_trend_direction == "量能递增" and last_pct > 0:
        vp_harmony += 10  # 放量上涨，趋势延续
    elif vol_trend_direction == "量能递减" and last_pct < 0:
        vp_harmony += 5  # 缩量下跌，抛压减弱
    elif vol_trend_direction == "量能递增" and last_pct < 0:
        vp_harmony -= 10  # 放量下跌，恐慌
    
    # 4. 背离惩罚 (±15分)
    if price_divergence == "顶背离":
        vp_harmony -= 15
    elif price_divergence == "底背离":
        vp_harmony += 15
    
    vp_harmony = max(0, min(100, vp_harmony))
    
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

    # === 洗盘规则 ===
    # 规则W1: 涨幅后缩量回调 → 洗盘（主力不卖，散户恐慌）
    if change_20d > 10 and change_5d < -3 and vol_ratio < 0.7:
        score_xipan += 25
        signals.append({"text": "涨幅后缩量回调,主力可能洗盘,清洗浮筹", "weight": 25, "type": "洗盘"})

    # 规则W2: 回调不破MA20 → 强势洗盘
    if ma20[-1] and cur > ma20[-1] and change_5d < -2 and vol_ratio < 0.8:
        score_xipan += 20
        signals.append({"text": "回调不破MA20,强势洗盘,主力护盘明显", "weight": 20, "type": "洗盘"})

    # 规则W3: 放量下跌后快速缩量 → 恐慌盘涌出后洗盘结束
    if len(volumes) >= 3 and volumes[-3] > vol_20 * 1.3 and volumes[-1] < vol_20 * 0.7:
        score_xipan += 15
        signals.append({"text": "放量下跌后快速缩量,恐慌盘涌出,洗盘可能接近尾声", "weight": 15, "type": "洗盘"})

    # 规则W4: 横盘震荡中量能萎缩 → 主力洗盘等时机
    if abs(change_5d) < 3 and abs(change_20d) < 8 and vol_ratio < 0.75:
        score_xipan += 15
        signals.append({"text": "横盘缩量震荡,主力可能在洗盘等待时机", "weight": 15, "type": "洗盘"})

    # 规则W5: 跌破MA5但守住MA20 → 假跌破洗盘
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur > ma20[-1] and change_5d < -2:
        score_xipan += 15
        signals.append({"text": "跌破MA5守住MA20,假跌破洗盘可能性大", "weight": 15, "type": "洗盘"})

    # --- 综合判断 ---
    print(f"[DEBUG] score_xichou={score_xichou} score_xipan={score_xipan} score_chuhuo={score_chuhuo}", flush=True)
    total = score_xichou + score_xipan + score_chuhuo
    if total == 0:
        total = 1

    # 更主动的判断逻辑：只有当分数明确时才判定，否则为震荡
    if score_xichou >= 20 and score_xichou > score_xipan and score_xichou > score_chuhuo:
        behavior = "吸筹"
        behavior_desc = "主力可能正在低位吸筹,关注放量突破和均线金叉信号"
        behavior_color = "#ef4444"
    elif score_xipan >= 20 and score_xipan > score_xichou and score_xipan > score_chuhuo:
        behavior = "洗盘"
        behavior_desc = "主力可能正在洗盘,关注缩量企稳后的反弹机会"
        behavior_color = "#22c55e"
    elif score_chuhuo >= 20 and score_chuhuo > score_xichou and score_chuhuo > score_xipan:
        behavior = "出货"
        behavior_desc = "主力可能正在出货,注意控制风险,不宜追高"
        behavior_color = "#f59e0b"
    else:
        # 震荡：分数不够明确，或多个信号并存
        behavior = "震荡"
        behavior_desc = "当前处于震荡整理阶段,方向不明,建议观望"
        behavior_color = "#94a3b8"
    print(f"[DEBUG] behavior={behavior}", flush=True)

    # === MACD + KDJ 联合确认 ===
    macd_warning = None
    signals_confirm = []  # 联合确认信号
    
    # KDJ状态判断
    kdj_oversold = False
    kdj_overbought = False
    kdj_gold = False
    kdj_dead = False
    
    if len(k_values) > 1 and k_values[-1] is not None:
        # KDJ金叉: K上穿D
        if k_values[-2] is not None and d_values[-2] is not None:
            kdj_gold = k_values[-2] < d_values[-2] and k_values[-1] > d_values[-1]
            kdj_dead = k_values[-2] > d_values[-2] and k_values[-1] < d_values[-1]
        
        # 超买超卖
        kdj_oversold = k_values[-1] < 20 and d_values[-1] < 20 if d_values[-1] else False
        kdj_overbought = k_values[-1] > 80 and d_values[-1] > 80 if d_values[-1] else False
    
    # MACD状态判断
    macd_gold = False
    macd_dead = False
    if len(dif) > 1 and dif[-1] is not None and dea[-1] is not None:
        if dif[-2] is not None and dea[-2] is not None:
            macd_gold = dif[-2] < dea[-2] and dif[-1] > dea[-1]
            macd_dead = dif[-2] > dea[-2] and dif[-1] < dea[-1]
    
    # 联合确认信号
    if behavior == "吸筹":
        if macd_dead and kdj_dead:
            macd_warning = "MACD+KDJ双双死叉,吸筹判断存疑,建议观望"
        elif macd_dead:
            macd_warning = "MACD死叉与吸筹矛盾,建议等待MACD金叉确认"
        elif kdj_dead:
            macd_warning = "KDJ死叉,短期可能回调,吸筹需谨慎"
        elif kdj_oversold and macd_gold:
            signals_confirm.append("KDJ超卖+MACD金叉,底部信号强")
        elif kdj_oversold:
            signals_confirm.append("KDJ超卖区域,可能触底")
    
    elif behavior == "出货":
        if macd_gold and kdj_gold:
            macd_warning = "MACD+KDJ双双金叉,出货判断存疑,可能假出货"
        elif macd_gold:
            macd_warning = "MACD金叉与出货矛盾,可能是假出货"
        elif kdj_gold:
            macd_warning = "KDJ金叉,短期可能反弹,出货需谨慎"
        elif kdj_overbought and macd_dead:
            signals_confirm.append("KDJ超买+MACD死叉,顶部信号强")
        elif kdj_overbought:
            signals_confirm.append("KDJ超买区域,注意回调风险")
    
    elif behavior == "震荡":
        if macd_gold and kdj_gold:
            signals_confirm.append("MACD+KDJ金叉共振,可能向上突破")
        elif macd_dead and kdj_dead:
            signals_confirm.append("MACD+KDJ死叉共振,可能向下突破")
        elif kdj_oversold:
            signals_confirm.append("KDJ超卖,短期可能反弹")
        elif kdj_overbought:
            signals_confirm.append("KDJ超买,短期可能回调")

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

    # === 技术指标状态 ===
    macd_gold_cross = False
    macd_dead_cross = False
    if len(dif) > 1 and dif[-1] is not None and dea[-1] is not None:
        if dif[-2] is not None and dea[-2] is not None:
            macd_gold_cross = dif[-2] < dea[-2] and dif[-1] > dea[-1]
            macd_dead_cross = dif[-2] > dea[-2] and dif[-1] < dea[-1]

    kdj_oversold = False
    kdj_overbought = False
    if len(k_values) > 0 and k_values[-1] is not None:
        kdj_oversold = k_values[-1] < 20
        kdj_overbought = k_values[-1] > 80

    ma_bullish = False
    if ma5[-1] and ma10[-1] and ma20[-1]:
        ma_bullish = ma5[-1] > ma10[-1] > ma20[-1]

    above_ma5 = cur > ma5[-1] if ma5[-1] else False
    above_ma20 = cur > ma20[-1] if ma20[-1] else False

    # === 加仓/减仓建议（多指标确认） ===
    position_advice = {
        "action": "观望",
        "reason": "",
        "urgency": "低",
        "risk_level": "中",
        "position_pct": 0,
        "stop_loss": None,
        "price_suggestion": None,
        "confirm_signals": [],
    }

    if behavior == "吸筹":
        confirm_count = 0
        confirm_reasons = []

        if macd_gold_cross:
            confirm_count += 2
            confirm_reasons.append("MACD金叉")
        elif len(dif) > 0 and dif[-1] is not None and dea[-1] is not None and dif[-1] > dea[-1]:
            confirm_count += 1
            confirm_reasons.append("MACD向上")

        if kdj_oversold:
            confirm_count += 2
            confirm_reasons.append("KDJ超卖")
        elif len(k_values) > 0 and k_values[-1] is not None and k_values[-1] < 40:
            confirm_count += 1
            confirm_reasons.append("KDJ低位")

        if above_ma20:
            confirm_count += 1
            confirm_reasons.append("站上MA20")

        if vol_level in ("地量", "缩量"):
            confirm_count += 1
            confirm_reasons.append("缩量企稳")

        position_advice["confirm_signals"] = confirm_reasons
        if confirm_count >= 4:
            position_advice["action"] = "加仓"
            position_advice["reason"] = "主力吸筹+%s,多指标共振" % "+".join(confirm_reasons[:2])
            position_advice["urgency"] = "高"
            position_advice["risk_level"] = "低"
            position_advice["position_pct"] = 30
        elif confirm_count >= 2:
            position_advice["action"] = "加仓"
            position_advice["reason"] = "主力吸筹+%s,可小仓位" % confirm_reasons[0]
            position_advice["urgency"] = "中"
            position_advice["risk_level"] = "中"
            position_advice["position_pct"] = 15
        else:
            position_advice["action"] = "观望"
            position_advice["reason"] = "吸筹但技术指标未确认,等待共振"
            position_advice["urgency"] = "低"
            position_advice["risk_level"] = "中"

        if ma20[-1]:
            position_advice["stop_loss"] = round(ma20[-1] * 0.97, 2)
        position_advice["price_suggestion"] = round(cur, 2)

    elif behavior == "洗盘":
        if vol_level == "地量" and change_5d > -3 and above_ma20:
            position_advice["action"] = "加仓"
            position_advice["reason"] = "洗盘末期+地量+守住MA20,可能反弹"
            position_advice["urgency"] = "中"
            position_advice["risk_level"] = "中"
            position_advice["position_pct"] = 15
            position_advice["price_suggestion"] = round(cur, 2)
            if ma20[-1]:
                position_advice["stop_loss"] = round(ma20[-1] * 0.97, 2)
        else:
            position_advice["action"] = "观望"
            position_advice["reason"] = "洗盘进行中,等待缩量企稳+守住均线"
            position_advice["urgency"] = "低"
            position_advice["risk_level"] = "高"

    elif behavior == "出货":
        position_advice["action"] = "减仓"
        position_advice["reason"] = "主力出货,风险加大"
        position_advice["urgency"] = "高"
        position_advice["risk_level"] = "高"
        if ma10[-1] and ma10[-1] > cur:
            position_advice["price_suggestion"] = round(ma10[-1], 2)
        if ma20[-1]:
            position_advice["stop_loss"] = round(ma20[-1] * 0.95, 2)

    elif behavior == "震荡":
        _dist_from_low = (cur - low_20) / low_20 * 100 if low_20 > 0 else 0

        confirm_count = 0
        confirm_reasons = []
        if macd_gold_cross:
            confirm_count += 2
            confirm_reasons.append("MACD金叉")
        if kdj_oversold:
            confirm_count += 2
            confirm_reasons.append("KDJ超卖")
        if above_ma20:
            confirm_count += 1
            confirm_reasons.append("站上MA20")
        if vol_level == "地量":
            confirm_count += 1
            confirm_reasons.append("地量")

        position_advice["confirm_signals"] = confirm_reasons

        if _dist_from_low < 5 and confirm_count >= 2:
            position_advice["action"] = "加仓"
            position_advice["reason"] = "底部+%s,可试探" % "+".join(confirm_reasons[:2])
            position_advice["urgency"] = "中"
            position_advice["risk_level"] = "中"
            position_advice["position_pct"] = 10
            position_advice["price_suggestion"] = round(cur, 2)
            if ma20[-1]:
                position_advice["stop_loss"] = round(ma20[-1] * 0.97, 2)
        elif _dist_from_low < 5:
            position_advice["action"] = "观望"
            position_advice["reason"] = "接近底部但技术指标未确认,等待信号"
            position_advice["urgency"] = "低"
            position_advice["risk_level"] = "中"
        elif _dist_from_low > 15:
            position_advice["action"] = "减仓"
            position_advice["reason"] = "接近顶部,建议减仓锁利"
            position_advice["urgency"] = "中"
            position_advice["risk_level"] = "中"
        else:
            position_advice["action"] = "观望"
            position_advice["reason"] = "震荡整理中,方向不明"
            position_advice["urgency"] = "低"

    return {
        "symbol": symbol, "name": name,
        "latest": cur,
        "pct_change": round((cur - data[-2]["close"]) / data[-2]["close"] * 100, 2) if len(data) > 1 else 0,
        "kline": data,
        "ma5": [round(v, 2) if v else None for v in ma5],
        "ma10": [round(v, 2) if v else None for v in ma10],
        "ma20": [round(v, 2) if v else None for v in ma20],
        "vol_ma5": [round(v, 0) if v else None for v in vol_ma5],
        "dif": [round(v, 4) if v else None for v in dif],
        "dea": [round(v, 4) if v else None for v in dea],
        "macd_bar": [round(v, 4) if v else None for v in macd_bar],
        "k_values": [round(v, 2) if v else None for v in k_values],
        "d_values": [round(v, 2) if v else None for v in d_values],
        "j_values": [round(v, 2) if v else None for v in j_values],
        "analysis": {
            "behavior": behavior,
            "behavior_desc": behavior_desc,
            "behavior_color": behavior_color,
            "macd_warning": macd_warning,
            "signals_confirm": signals_confirm,
            "score_xipan": round(score_xipan / total * 100),
            "score_xichou": round(score_xichou / total * 100),
            "score_chuhuo": round(score_chuhuo / total * 100),
            "signals": signals,
            "vol_trend": round(vol_ratio, 2),
            "vol_level": vol_level,
            "vol_level_desc": vol_level_desc,
            "vol_percentile": round(vol_percentile, 1),
            "vol_trend_direction": vol_trend_direction,
            "vol_anomaly": vol_anomaly,
            "vol_anomaly_type": vol_anomaly_type,
            "vp_harmony": vp_harmony,
            "price_divergence": price_divergence,
            "price_change_5d": round(change_5d, 2),
            "price_change_20d": round(change_20d, 2),
            "drawdown": round(drawdown, 2),
            "avg_amplitude": round(avg_amplitude, 2),
            "vp_signals": vp_signals[-5:],
            "position_advice": position_advice,
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
    # 提取引号内的内容
    import re
    match = re.search(r'"([^"]+)"', raw)
    if not match:
        return []
    entries = match.group(1).split(";")
    for entry in entries:
        try:
            arr = entry.split(",")
            if len(arr) < 4:
                continue
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


# ===== 市场情绪分析 =====
def _get_market_sentiment():
    """综合市场情绪分析"""
    result = {
        "overall": {},
        "capital": {},
        "behavior": {},
        "sector_heat": [],
        "fear_greed": {},
    }

    # === 1. 市场整体情绪 ===
    # 获取大盘指数
    index_codes = ["sh000001","sz399001","sz399006","sh000300","sh000016"]
    raw_idx = _curl(f"https://qt.gtimg.cn/q={','.join(index_codes)}",
                    ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")
    indices = []
    for line in raw_idx.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            p = line.split("=", 1)[1].strip('"').split("~")
            if len(p) < 35:
                continue
            indices.append({
                "name": p[1], "code": p[2],
                "latest": float(p[3] or 0),
                "pct_change": float(p[32] or 0),
                "amount": float(p[37] or 0),
                "high": float(p[33] or 0),
                "low": float(p[34] or 0),
            })
        except Exception:
            continue

    # 上证指数作为大盘代表
    sh_index = next((x for x in indices if x["code"] == "000001"), None)

    # 获取涨跌统计 - 用更多股票采样
    sample_codes = [
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
        "sz002415","sh600809","sh603369","sz002032","sz002241",
        "sh600745","sh603259","sh601127","sz002032","sz300496",
        "sh600760","sz000876","sz002601","sh601888","sh600837",
        "sh601111","sh600111","sz000063","sz002352","sh600050",
        "sh601398","sh601939","sh601288","sh601328","sh601988",
        "sh601088","sh601857","sh601628","sh601601","sh601318",
        "sz000001","sh600036","sz002594","sz300750","sz002475",
        "sh600900","sh600585","sh601899","sz002371","sz000651",
    ]
    sample_codes = list(set(sample_codes))[:80]
    raw_stocks = _curl(f"https://qt.gtimg.cn/q={','.join(sample_codes)}",
                       ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")

    up_count = 0
    down_count = 0
    flat_count = 0
    limit_up = 0
    limit_down = 0
    total_amount = 0
    avg_change = 0
    changes = []
    outer_total = 0
    inner_total = 0

    for line in raw_stocks.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        try:
            p = line.split("=", 1)[1].strip('"').split("~")
            if len(p) < 50:
                continue
            pct = float(p[32] or 0)
            amount = float(p[37] or 0)
            outer = float(p[49] or 0) if len(p) > 49 else 0
            inner = float(p[50] or 0) if len(p) > 50 else 0

            changes.append(pct)
            total_amount += amount
            outer_total += outer
            inner_total += inner

            if pct > 0: up_count += 1
            elif pct < 0: down_count += 1
            else: flat_count += 1

            if pct >= 9.9: limit_up += 1
            elif pct <= -9.9: limit_down += 1
        except Exception:
            continue

    if changes:
        avg_change = sum(changes) / len(changes)
    total_stocks = up_count + down_count + flat_count

    # 涨跌比
    up_ratio = up_count / total_stocks * 100 if total_stocks > 0 else 50
    # 涨停跌停比
    limit_ratio = limit_up / (limit_up + limit_down) * 100 if (limit_up + limit_down) > 0 else 50

    # 外盘占比(主动买入)
    vol_total = outer_total + inner_total
    outer_ratio = outer_total / vol_total * 100 if vol_total > 0 else 50

    # 市场强度评分 (0-100)
    strength = 0
    strength += min(30, up_ratio * 0.6)  # 涨跌比贡献0-30
    strength += min(25, limit_ratio * 0.25)  # 涨停贡献0-25
    strength += min(20, max(0, avg_change * 5))  # 平均涨幅贡献0-20
    strength += min(15, outer_ratio * 0.15)  # 买盘贡献0-15
    if sh_index:
        strength += min(10, max(0, sh_index["pct_change"] * 3))  # 大盘贡献0-10

    # 情绪状态
    if strength >= 70:
        mood = "极度贪婪"
        mood_color = "#ef4444"
        mood_emoji = "🔥"
        advice = "市场过热，注意追高风险，可适当减仓"
    elif strength >= 55:
        mood = "贪婪"
        mood_color = "#f59e0b"
        mood_emoji = "📈"
        advice = "市场情绪良好，可正常操作，注意止盈"
    elif strength >= 45:
        mood = "中性"
        mood_color = "#94a3b8"
        mood_emoji = "😐"
        advice = "市场方向不明，建议观望为主"
    elif strength >= 30:
        mood = "恐慌"
        mood_color = "#3b82f6"
        mood_emoji = "📉"
        advice = "市场恐慌情绪蔓延，可逢低布局优质股"
    else:
        mood = "极度恐慌"
        mood_color = "#22c55e"
        mood_emoji = "😱"
        advice = "市场极度恐慌，反而是长线布局的好时机"

    result["overall"] = {
        "indices": indices,
        "up_count": up_count,
        "down_count": down_count,
        "flat_count": flat_count,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "avg_change": round(avg_change, 2),
        "total_amount": total_amount,
        "strength": round(strength),
        "mood": mood,
        "mood_color": mood_color,
        "mood_emoji": mood_emoji,
        "advice": advice,
    }

    # === 2. 资金情绪 ===
    # 板块资金流向
    sector_data = _sina(0, 20)
    concept_data = _sina(1, 20)

    # 主力资金统计
    sector_inflow = sum(s["net_inflow"] for s in sector_data)
    concept_inflow = sum(s["net_inflow"] for s in concept_data)

    # 资金情绪判断
    total_flow = sector_inflow + concept_inflow
    if total_flow > 50e8:
        flow_mood = "大幅流入"
        flow_color = "#ef4444"
    elif total_flow > 10e8:
        flow_mood = "净流入"
        flow_color = "#f59e0b"
    elif total_flow > -10e8:
        flow_mood = "平衡"
        flow_color = "#94a3b8"
    elif total_flow > -50e8:
        flow_mood = "净流出"
        flow_color = "#3b82f6"
    else:
        flow_mood = "大幅流出"
        flow_color = "#22c55e"

    # 外盘内盘比
    if outer_ratio > 55:
        buy_mood = "主动买入强势"
        buy_color = "#ef4444"
    elif outer_ratio > 50:
        buy_mood = "买入略强"
        buy_color = "#f59e0b"
    elif outer_ratio > 45:
        buy_mood = "买卖平衡"
        buy_color = "#94a3b8"
    else:
        buy_mood = "主动卖出强势"
        buy_color = "#22c55e"

    result["capital"] = {
        "sector_inflow": sector_inflow,
        "concept_inflow": concept_inflow,
        "total_flow": total_flow,
        "flow_mood": flow_mood,
        "flow_color": flow_color,
        "outer_ratio": round(outer_ratio, 1),
        "buy_mood": buy_mood,
        "buy_color": buy_color,
    }

    # === 3. 不同角色行为情绪 ===
    # 主力行为: 大单+超大单 = 外盘-内盘的差值反映主力方向
    main_force_diff = outer_total - inner_total
    if main_force_diff > 0:
        main_behavior = "主力建仓"
        main_desc = "外盘大于内盘，主力正在吸筹"
        main_color = "#ef4444"
    else:
        main_behavior = "主力出货"
        main_desc = "内盘大于外盘，主力正在减仓"
        main_color = "#22c55e"

    # 散户行为: 换手率高+涨幅小 = 散户换手
    avg_turnover = sum(float(line.split("=", 1)[1].strip('"').split("~")[38] or 0)
                       for line in raw_stocks.split(";") if "=" in line
                       and len(line.split("=", 1)[1].strip('"').split("~")) > 38) / max(1, total_stocks)

    if avg_turnover > 5:
        retail_behavior = "散户活跃"
        retail_desc = "换手率高，散户交易频繁，多空分歧大"
        retail_color = "#f59e0b"
    elif avg_turnover > 2:
        retail_behavior = "散户正常"
        retail_desc = "换手率适中，散户参与度一般"
        retail_color = "#94a3b8"
    else:
        retail_behavior = "散户冷淡"
        retail_desc = "换手率低，散户观望情绪浓"
        retail_color = "#3b82f6"

    # 机构行为: 成交额大+涨跌幅小 = 机构调仓
    if total_amount > 5000e8:
        inst_behavior = "机构活跃"
        inst_desc = "成交额巨大，机构资金频繁调仓"
        inst_color = "#ef4444"
    elif total_amount > 2000e8:
        inst_behavior = "机构正常"
        inst_desc = "成交额适中，机构按部就班操作"
        inst_color = "#94a3b8"
    else:
        inst_behavior = "机构冷淡"
        inst_desc = "成交额低迷，机构资金休眠"
        inst_color = "#3b82f6"

    result["behavior"] = {
        "main_force": {
            "behavior": main_behavior,
            "desc": main_desc,
            "color": main_color,
            "diff": main_force_diff,
        },
        "retail": {
            "behavior": retail_behavior,
            "desc": retail_desc,
            "color": retail_color,
            "avg_turnover": round(avg_turnover, 2),
        },
        "institution": {
            "behavior": inst_behavior,
            "desc": inst_desc,
            "color": inst_color,
            "total_amount": total_amount,
        },
    }

    # === 4. 板块热度 ===
    all_sectors = sector_data + concept_data
    all_sectors.sort(key=lambda x: x["net_inflow"], reverse=True)
    top_hot = all_sectors[:5]  # 最热门板块(资金流入最多)
    top_cold = all_sectors[-5:]  # 最冷门板块(资金流出最多)
    result["sector_heat"] = {
        "hot": [{"name": s["name"], "net_inflow": s["net_inflow"], "avg_change": s["avg_change"]} for s in top_hot],
        "cold": [{"name": s["name"], "net_inflow": s["net_inflow"], "avg_change": s["avg_change"]} for s in top_cold],
    }

    # === 5. 恐慌贪婪指数 ===
    # 综合多个指标计算
    fg_score = 50  # 基准50
    # 涨跌比
    fg_score += (up_ratio - 50) * 0.3
    # 涨停跌停
    fg_score += (limit_ratio - 50) * 0.2
    # 平均涨幅
    fg_score += avg_change * 5
    # 资金流向
    if total_flow > 0:
        fg_score += min(10, total_flow / 10e8)
    else:
        fg_score += max(-10, total_flow / 10e8)
    # 外盘比
    fg_score += (outer_ratio - 50) * 0.2

    fg_score = max(0, min(100, fg_score))

    if fg_score >= 80:
        fg_level = "极度贪婪"
        fg_color = "#ef4444"
        fg_desc = "市场极度乐观，谨防回调"
    elif fg_score >= 60:
        fg_level = "贪婪"
        fg_color = "#f59e0b"
        fg_desc = "市场情绪偏乐观"
    elif fg_score >= 40:
        fg_level = "中性"
        fg_color = "#94a3b8"
        fg_desc = "市场情绪平稳"
    elif fg_score >= 20:
        fg_level = "恐慌"
        fg_color = "#3b82f6"
        fg_desc = "市场恐慌情绪上升"
    else:
        fg_level = "极度恐慌"
        fg_color = "#22c55e"
        fg_desc = "市场极度悲观，可能是底部"

    result["fear_greed"] = {
        "score": round(fg_score),
        "level": fg_level,
        "color": fg_color,
        "desc": fg_desc,
    }

    # === 6. 综合分析总结 ===
    summary_points = []
    risk_warnings = []
    opportunities = []

    # 大盘判断
    if sh_index:
        if sh_index["pct_change"] > 1:
            summary_points.append("大盘放量上涨,市场做多氛围浓厚")
        elif sh_index["pct_change"] > 0:
            summary_points.append("大盘小幅上涨,市场情绪偏暖")
        elif sh_index["pct_change"] > -1:
            summary_points.append("大盘小幅下跌,市场震荡调整")
        else:
            summary_points.append("大盘大幅下跌,市场恐慌情绪蔓延")
            risk_warnings.append("大盘走弱,注意系统性风险")

    # 涨跌比判断
    if up_ratio > 65:
        summary_points.append("个股普涨,赚钱效应明显")
        opportunities.append("市场强势,可积极参与")
    elif up_ratio > 50:
        summary_points.append("涨多跌多,市场分化")
    elif up_ratio > 35:
        summary_points.append("跌多涨少,市场偏弱")
        risk_warnings.append("多数个股下跌,控制仓位")
    else:
        summary_points.append("个股普亏,市场极度弱势")
        risk_warnings.append("市场恐慌,建议空仓观望")

    # 涨停跌停判断
    if limit_up > 20:
        summary_points.append("涨停板数量多,短线资金活跃")
        opportunities.append("涨停效应强,可关注打板机会")
    elif limit_up > 5:
        summary_points.append("有一定涨停数量,市场有热点")

    if limit_down > 10:
        risk_warnings.append("跌停板较多,注意个股风险")
    elif limit_down > 5:
        risk_warnings.append("有跌停出现,注意回避高位股")

    # 资金流向判断
    if total_flow > 30e8:
        summary_points.append("主力资金大幅流入,市场做多意愿强")
        opportunities.append("资金持续流入,可跟随主力方向")
    elif total_flow > 0:
        summary_points.append("主力资金小幅流入,市场情绪偏暖")
    elif total_flow > -30e8:
        summary_points.append("主力资金小幅流出,市场观望情绪浓")
    else:
        summary_points.append("主力资金大幅流出,市场避险情绪升温")
        risk_warnings.append("资金持续流出,建议降低仓位")

    # 外盘比判断
    if outer_ratio > 55:
        summary_points.append("主动买盘强势,市场承接力强")
    elif outer_ratio < 45:
        summary_points.append("主动卖盘强势,市场抛压较重")
        risk_warnings.append("卖盘占优,注意短期回调风险")

    # 板块热度判断
    hot_names = [s["name"] for s in top_hot[:3]] if top_hot else []
    cold_names = [s["name"] for s in top_cold[:3]] if top_cold else []
    if hot_names:
        opportunities.append("热门板块: " + "、".join(hot_names))
    if cold_names:
        risk_warnings.append("冷门板块: " + "、".join(cold_names))

    # 操作建议
    if strength >= 70:
        operation_advice = "市场过热,建议减仓锁利,不要追高"
    elif strength >= 55:
        operation_advice = "市场偏强,可正常操作,注意止盈止损"
    elif strength >= 45:
        operation_advice = "市场震荡,建议轻仓观望,等待方向明确"
    elif strength >= 30:
        operation_advice = "市场偏弱,建议控制仓位,逢高减仓"
    else:
        operation_advice = "市场恐慌,建议空仓或极轻仓,等待企稳信号"

    # 明日预判
    if strength >= 60:
        tomorrow_outlook = "市场情绪偏乐观,明日有望继续反弹"
    elif strength >= 45:
        tomorrow_outlook = "市场情绪中性,明日大概率震荡整理"
    else:
        tomorrow_outlook = "市场情绪偏悲观,明日可能继续调整"

    result["summary"] = {
        "points": summary_points,
        "risk_warnings": risk_warnings,
        "opportunities": opportunities,
        "operation_advice": operation_advice,
        "tomorrow_outlook": tomorrow_outlook,
        "hot_sectors": hot_names,
        "cold_sectors": cold_names,
    }

    return result


@app.route("/api/market_sentiment")
def api_market_sentiment():
    """市场情绪分析"""
    result = _get_market_sentiment()
    return jsonify({"code": 0, "data": result})


# ===== ETF模块 =====
def _get_etf_data():
    """获取ETF数据并分析"""
    etf_pool = [
        # 宽基指数ETF
        "sh510300", "sh510500", "sh510050", "sh510880", "sh513100",
        "sh513300", "sh513500", "sh159901", "sh159915", "sh159919",
        "sh159920", "sh159922", "sh512100", "sh512880", "sh588000",
        "sh588080", "sh588180",
        # 行业ETF
        "sh512010", "sh512800", "sh515030", "sh515790", "sh516160",
        "sh516510", "sh516970", "sh512480", "sh512660", "sh512690",
        "sh512760", "sh512880", "sh512980", "sh515050", "sh515180",
        "sh515210", "sh515230", "sh515790", "sh516110", "sh516150",
        "sh516160", "sh516510", "sh516970", "sh518880", "sh519674",
        # 跨境ETF
        "sh513100", "sh513300", "sh513500", "sh159920", "sh159922",
        # 商品ETF
        "sh518880", "sh159985",
        # 债券ETF
        "sh511010", "sh511260", "sh511380",
    ]
    codes = list(set(etf_pool))[:60]

    raw = _curl(f"https://qt.gtimg.cn/q={','.join(codes)}",
                ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")

    etfs = []
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
            high = float(p[33] or 0)
            low = float(p[34] or 0)
            open_price = float(p[5] or 0) if len(p) > 5 else 0
            prev_close = float(p[4] or 0) if len(p) > 4 else 0

            if latest <= 0:
                continue

            # 计算振幅
            amplitude = (high - low) / open_price * 100 if open_price > 0 else 0

            # 判断ETF类型
            etf_type = "其他"
            if "510" in code or "15990" in code or "15991" in code or "15992" in code:
                etf_type = "宽基"
            elif "512" in code or "515" in code or "516" in code:
                etf_type = "行业"
            elif "513" in code:
                etf_type = "跨境"
            elif "518" in code or "15998" in code:
                etf_type = "商品"
            elif "511" in code:
                etf_type = "债券"

            etfs.append({
                "code": code, "name": name, "type": etf_type,
                "latest": latest, "pct_change": pct,
                "amount": amount, "turnover": turnover,
                "high": high, "low": low,
                "amplitude": round(amplitude, 2),
            })
        except Exception:
            continue

    # 按成交额排序
    etfs.sort(key=lambda x: x["amount"], reverse=True)

    # 分类统计
    by_type = {}
    for e in etfs:
        t = e["type"]
        if t not in by_type:
            by_type[t] = []
        by_type[t].append(e)

    # 各类型热门ETF
    hot_etfs = {}
    for t, items in by_type.items():
        hot_etfs[t] = items[:5]  # 每类取成交额前5

    # 涨幅榜和跌幅榜
    sorted_by_change = sorted(etfs, key=lambda x: x["pct_change"], reverse=True)
    top_gainers = sorted_by_change[:10]
    top_losers = sorted_by_change[-10:][::-1]  # 倒序取最后10个，再反转

    # 资金流向统计（用成交额近似）
    total_amount = sum(e["amount"] for e in etfs)
    up_amount = sum(e["amount"] for e in etfs if e["pct_change"] > 0)
    down_amount = sum(e["amount"] for e in etfs if e["pct_change"] < 0)

    return {
        "etfs": etfs[:30],  # 返回成交额前30
        "hot_by_type": hot_etfs,
        "top_gainers": top_gainers,
        "top_losers": top_losers,
        "stats": {
            "total": len(etfs),
            "total_amount": total_amount,
            "up_count": len([e for e in etfs if e["pct_change"] > 0]),
            "down_count": len([e for e in etfs if e["pct_change"] < 0]),
            "flat_count": len([e for e in etfs if e["pct_change"] == 0]),
            "up_amount": up_amount,
            "down_amount": down_amount,
        },
    }


@app.route("/api/etf")
def api_etf():
    """ETF数据"""
    result = _get_etf_data()
    return jsonify({"code": 0, "data": result})


# ===== 交易决策 =====
# 缓存机制
_cache = {}
_cache_time = {}
CACHE_TTL = 300  # 5分钟缓存

def _get_cached_data(key, fetch_fn, ttl=CACHE_TTL):
    """通用缓存函数"""
    now = time.time()
    if key in _cache and now - _cache_time.get(key, 0) < ttl:
        return _cache[key]
    data = fetch_fn()
    _cache[key] = data
    _cache_time[key] = now
    return data

# 权重预设配置
WEIGHT_PRESETS = {
    "balanced": {
        "name": "均衡模式",
        "desc": "各维度均衡考虑",
        "market": 0.25, "sentiment": 0.15, "sector": 0.20, "technical": 0.25, "valuation": 0.15
    },
    "market_first": {
        "name": "大盘优先",
        "desc": "重视大盘趋势，顺势操作",
        "market": 0.35, "sentiment": 0.15, "sector": 0.15, "technical": 0.20, "valuation": 0.15
    },
    "technical_first": {
        "name": "技术优先",
        "desc": "重视个股技术形态",
        "market": 0.15, "sentiment": 0.10, "sector": 0.15, "technical": 0.40, "valuation": 0.20
    },
    "value_first": {
        "name": "价值优先",
        "desc": "重视估值和基本面",
        "market": 0.15, "sentiment": 0.10, "sector": 0.15, "technical": 0.20, "valuation": 0.40
    },
}

def _get_trading_decision(symbol, risk_pref="moderate", weight_preset="balanced"):
    """综合分析股票，给出买入/卖出/持有建议"""
    # 1. 获取个股分析数据
    analysis = _analyze_stock(symbol, 60)
    if not analysis:
        return None

    name = analysis["name"]
    code = analysis["symbol"]
    latest = analysis["latest"]
    pct_change = analysis["pct_change"]
    a = analysis.get("analysis", {})
    behavior = a.get("behavior", "")
    signals = a.get("signals", [])
    kline = analysis.get("kline", [])
    ma5 = analysis.get("ma5", [])
    ma10 = analysis.get("ma10", [])
    ma20 = analysis.get("ma20", [])

    # 2. 获取市场情绪（带缓存）
    sentiment = _get_cached_data("sentiment", _get_market_sentiment)
    overall = sentiment.get("overall", {})
    capital = sentiment.get("capital", {})
    fear_greed = sentiment.get("fear_greed", {})
    strength = overall.get("strength", 50)

    # 3. 获取板块资金流向（带缓存）
    sector_data = _get_cached_data("sector_flow", lambda: _sina(0, 30))
    concept_data = _get_cached_data("concept_flow", lambda: _sina(1, 30))

    # 获取权重配置
    weights = WEIGHT_PRESETS.get(weight_preset, WEIGHT_PRESETS["balanced"])

    # 4. 分析维度打分
    pos_signals = []   # 正面信号
    neg_signals = []   # 负面信号
    neu_signals = []   # 中性信号
    score = 50         # 基础分

    # === 维度1: 大盘趋势 ===
    sh_index = next((x for x in overall.get("indices", []) if x["code"] == "000001"), None)
    if sh_index:
        sh_pct = sh_index.get("pct_change", 0)
        if sh_pct > 0.5:
            pos_signals.append({"text": "大盘上涨", "detail": f"上证涨{sh_pct:.1f}%，顺势操作"})
            score += round(30 * weights["market"])
        elif sh_pct > 0:
            pos_signals.append({"text": "大盘微涨", "detail": "市场偏多"})
            score += round(15 * weights["market"])
        elif sh_pct > -0.5:
            neu_signals.append({"text": "大盘横盘", "detail": "方向不明，谨慎为主"})
            score += 0
        else:
            neg_signals.append({"text": "大盘下跌", "detail": f"上证跌{sh_pct:.1f}%，逆势风险大"})
            score -= round(30 * weights["market"])

    # === 维度2: 市场情绪 ===
    if strength >= 60:
        pos_signals.append({"text": "市场情绪积极", "detail": f"情绪强度{strength}，资金活跃"})
        score += round(20 * weights["sentiment"])
    elif strength >= 45:
        neu_signals.append({"text": "市场情绪中性", "detail": f"情绪强度{strength}"})
        score += 0
    else:
        neg_signals.append({"text": "市场情绪低迷", "detail": f"情绪强度{strength}，恐慌情绪"})
        score -= round(20 * weights["sentiment"])

    # === 维度3: 板块资金 ===
    total_sector_flow = sum(s["net_inflow"] for s in sector_data)
    if total_sector_flow > 10e8:
        pos_signals.append({"text": "板块资金净流入", "detail": "板块整体资金流入"})
        score += round(25 * weights["sector"])
    elif total_sector_flow > -10e8:
        neu_signals.append({"text": "板块资金平衡", "detail": "资金流向中性"})
        score += 0
    else:
        neg_signals.append({"text": "板块资金净流出", "detail": "板块整体资金流出"})
        score -= round(25 * weights["sector"])

    # === 维度4: 技术形态 ===
    cur_ma5 = ma5[-1] if ma5 else 0
    cur_ma10 = ma10[-1] if ma10 else 0
    cur_ma20 = ma20[-1] if ma20 else 0

    # 均线多头排列
    if cur_ma5 > cur_ma10 > cur_ma20 > 0:
        pos_signals.append({"text": "均线多头排列", "detail": "5/10/20日线向上发散"})
        score += round(30 * weights["technical"])
    elif cur_ma5 < cur_ma10 < cur_ma20 and cur_ma20 > 0:
        neg_signals.append({"text": "均线空头排列", "detail": "5/10/20日线向下发散"})
        score -= round(30 * weights["technical"])
    else:
        neu_signals.append({"text": "均线交织", "detail": "方向不明"})
        score += 0

    # 量价配合
    vol_trend = a.get("vol_trend", 1)
    if vol_trend > 1.5 and pct_change > 0:
        pos_signals.append({"text": "放量上涨", "detail": f"量比{vol_trend:.1f}倍，资金积极介入"})
        score += round(20 * weights["technical"])
    elif vol_trend > 1.5 and pct_change < 0:
        neg_signals.append({"text": "放量下跌", "detail": f"量比{vol_trend:.1f}倍，资金出逃"})
        score -= round(20 * weights["technical"])
    elif vol_trend < 0.7 and pct_change > 0:
        neu_signals.append({"text": "缩量上涨", "detail": "主力控盘，散户惜售"})
        score += round(5 * weights["technical"])
    elif vol_trend < 0.7 and pct_change < 0:
        pos_signals.append({"text": "缩量下跌", "detail": "抛压减弱，洗盘概率大"})
        score += round(5 * weights["technical"])

    # 行为识别
    if behavior == "吸筹":
        pos_signals.append({"text": "主力吸筹", "detail": "有资金在收集筹码"})
        score += round(15 * weights["technical"])
    elif behavior == "出货":
        neg_signals.append({"text": "主力出货", "detail": "有资金在撤离"})
        score -= round(15 * weights["technical"])
    elif behavior == "洗盘":
        neu_signals.append({"text": "洗盘阶段", "detail": "正常调整，清洗浮筹"})
        score += round(3 * weights["technical"])

    # === 维度5: 估值 ===
    try:
        raw = _curl(f"https://qt.gtimg.cn/q={symbol}",
                    ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")
        if "=" in raw:
            p = raw.split("=", 1)[1].strip('"').split("~")
            pe = float(p[39] or 0) if len(p) > 39 else 0
            pb = float(p[46] or 0) if len(p) > 46 else 0

            if 0 < pe < 15:
                pos_signals.append({"text": "估值偏低", "detail": f"PE={pe:.1f}，处于历史低位"})
                score += round(20 * weights["valuation"])
            elif 15 <= pe < 30:
                neu_signals.append({"text": "估值适中", "detail": f"PE={pe:.1f}"})
                score += 0
            elif pe >= 30:
                neg_signals.append({"text": "估值偏高", "detail": f"PE={pe:.1f}，注意泡沫风险"})
                score -= round(20 * weights["valuation"])

            if 0 < pb < 1:
                pos_signals.append({"text": "破净", "detail": f"PB={pb:.2f}，股价低于净资产"})
                score += round(10 * weights["valuation"])
            elif pb >= 5:
                neg_signals.append({"text": "PB偏高", "detail": f"PB={pb:.2f}"})
                score -= round(5 * weights["valuation"])
    except Exception:
        pe, pb = 0, 0

    # === 限制分数范围 ===
    score = max(0, min(100, score))

    # === 生成建议 ===
    stop_loss_map = {"conservative": 0.02, "moderate": 0.03, "aggressive": 0.05}
    position_map = {"conservative": 0.15, "moderate": 0.20, "aggressive": 0.30}
    stop_loss_pct = stop_loss_map.get(risk_pref, 0.03)
    target_position = position_map.get(risk_pref, 0.20)

    stop_loss_price = round(latest * (1 - stop_loss_pct), 2)

    if score >= 70:
        suggestion = "可以买入"
        advice_color = "#22c55e"
        advice_emoji = "✅"
    elif score >= 55:
        suggestion = "谨慎买入"
        advice_color = "#f59e0b"
        advice_emoji = "⚠️"
    elif score >= 45:
        suggestion = "观望"
        advice_color = "#94a3b8"
        advice_emoji = "⏸️"
    elif score >= 30:
        suggestion = "谨慎持有/减仓"
        advice_color = "#f59e0b"
        advice_emoji = "⚠️"
    else:
        suggestion = "建议卖出"
        advice_color = "#ef4444"
        advice_emoji = "🔴"

    return {
        "symbol": code,
        "name": name,
        "latest": latest,
        "pct_change": pct_change,
        "behavior": behavior,
        "score": round(score),
        "suggestion": suggestion,
        "advice_color": advice_color,
        "advice_emoji": advice_emoji,
        "stop_loss_price": stop_loss_price,
        "stop_loss_pct": round(stop_loss_pct * 100, 1),
        "target_position": round(target_position * 100),
        "pos_signals": pos_signals,
        "neg_signals": neg_signals,
        "neu_signals": neu_signals,
        "market_strength": strength,
        "market_mood": overall.get("mood", "中性"),
    }


@app.route("/api/trading_decision")
def api_trading_decision():
    """交易决策API"""
    code = request.args.get("code", "").strip()
    risk_pref = request.args.get("risk", "moderate")
    weight_preset = request.args.get("preset", "balanced")
    if not code:
        return jsonify({"code": -1, "msg": "请输入股票代码"})
    symbol = _code_to_symbol(code)
    result = _get_trading_decision(symbol, risk_pref, weight_preset)
    if not result:
        return jsonify({"code": -1, "msg": f"未找到股票 {code} 的数据"})
    # 添加权重配置信息
    result["weight_preset"] = weight_preset
    result["weight_info"] = WEIGHT_PRESETS.get(weight_preset, {})
    return jsonify({"code": 0, "data": result})


# ===== 回测验证 =====
@app.route("/api/backtest")
def api_backtest():
    """回测交易决策评分的准确性"""
    days = int(request.args.get("days", 30))
    symbol = request.args.get("symbol", "").strip()
    
    # 获取历史日志
    logs = _load_logs()
    if not logs:
        return jsonify({"code": 0, "data": {"total": 0, "msg": "暂无历史数据"}})
    
    # 按股票分组
    stock_logs = {}
    for log in logs:
        sym = log.get("symbol", "")
        if symbol and sym != symbol:
            continue
        if sym not in stock_logs:
            stock_logs[sym] = []
        stock_logs[sym].append(log)
    
    results = []
    for sym, sym_logs in stock_logs.items():
        # 按日期排序
        sym_logs.sort(key=lambda x: x.get("date", ""))
        
        for i, log in enumerate(sym_logs):
            # 计算当时的评分（简化版：基于行为识别）
            behavior = log.get("behavior", "")
            base_score = 50
            if behavior == "吸筹":
                base_score += 20
            elif behavior == "出货":
                base_score -= 20
            elif behavior == "洗盘":
                base_score += 5
            
            # 检查后续表现
            future_logs = sym_logs[i+1:i+6]  # 未来5天
            if not future_logs:
                continue
            
            price_changes = []
            for fl in future_logs:
                if "pct_change" in fl:
                    price_changes.append(fl["pct_change"])
            
            if price_changes:
                avg_change = sum(price_changes) / len(price_changes)
                # 评分>60建议买入，检查后续是否涨
                suggestion_correct = (base_score >= 60 and avg_change > 0) or \
                                    (base_score < 40 and avg_change < 0) or \
                                    (40 <= base_score < 60)
                
                results.append({
                    "symbol": sym,
                    "name": log.get("name", ""),
                    "date": log.get("date", ""),
                    "score": base_score,
                    "behavior": behavior,
                    "future_avg_change": round(avg_change, 2),
                    "correct": suggestion_correct,
                })
    
    # 统计准确率
    if results:
        correct_count = sum(1 for r in results if r["correct"])
        total = len(results)
        accuracy = round(correct_count / total * 100, 1) if total > 0 else 0
        
        # 按评分区间统计
        score_ranges = {"60-100": [], "40-60": [], "0-40": []}
        for r in results:
            s = r["score"]
            if s >= 60:
                score_ranges["60-100"].append(r)
            elif s >= 40:
                score_ranges["40-60"].append(r)
            else:
                score_ranges["0-40"].append(r)
        
        range_stats = {}
        for range_name, range_logs in score_ranges.items():
            if range_logs:
                correct = sum(1 for r in range_logs if r["correct"])
                range_stats[range_name] = {
                    "total": len(range_logs),
                    "correct": correct,
                    "accuracy": round(correct / len(range_logs) * 100, 1),
                    "avg_future_change": round(sum(r["future_avg_change"] for r in range_logs) / len(range_logs), 2)
                }
        
        return jsonify({"code": 0, "data": {
            "total": total,
            "correct": correct_count,
            "accuracy": accuracy,
            "range_stats": range_stats,
            "details": results[-20:]  # 最近20条
        }})
    
    return jsonify({"code": 0, "data": {"total": 0, "msg": "数据不足"}})


# ===== 价格预警 =====
alerts = {}  # {symbol: [{price, direction, message}]}

@app.route("/api/alert/add", methods=["POST"])
def api_alert_add():
    """添加价格预警"""
    data = request.get_json()
    if not data:
        return jsonify({"code": -1, "msg": "无效数据"})
    
    symbol = data.get("symbol", "").strip()
    price = data.get("price")
    direction = data.get("direction", "above")  # above/below
    message = data.get("message", "")
    
    if not symbol or price is None:
        return jsonify({"code": -1, "msg": "缺少参数"})
    
    if symbol not in alerts:
        alerts[symbol] = []
    
    alerts[symbol].append({
        "price": float(price),
        "direction": direction,
        "message": message,
        "created": time.time()
    })
    
    return jsonify({"code": 0, "msg": "预警已添加"})


@app.route("/api/alert/list")
def api_alert_list():
    """获取所有预警"""
    return jsonify({"code": 0, "data": alerts})


@app.route("/api/alert/check")
def api_alert_check():
    """检查预警触发"""
    triggered = []
    
    for symbol, alert_list in list(alerts.items()):
        # 获取当前价格
        try:
            raw = _curl(f"https://qt.gtimg.cn/q={symbol}",
                        ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")
            if "=" in raw:
                p = raw.split("=", 1)[1].strip('"').split("~")
                current_price = float(p[3] or 0)
                
                for alert in alert_list[:]:
                    if alert["direction"] == "above" and current_price >= alert["price"]:
                        triggered.append({
                            "symbol": symbol,
                            "current": current_price,
                            "target": alert["price"],
                            "message": alert["message"],
                            "direction": "above"
                        })
                        alerts[symbol].remove(alert)
                    elif alert["direction"] == "below" and current_price <= alert["price"]:
                        triggered.append({
                            "symbol": symbol,
                            "current": current_price,
                            "target": alert["price"],
                            "message": alert["message"],
                            "direction": "below"
                        })
                        alerts[symbol].remove(alert)
        except Exception:
            continue
    
    # 清理空列表
    for sym in list(alerts.keys()):
        if not alerts[sym]:
            del alerts[sym]
    
    return jsonify({"code": 0, "data": triggered})


# ===== 短线爆发股预警系统 =====
_short_term_pool = []  # 扫描股票池
_short_term_alerts = []  # 预警列表
_short_term_lock = threading.Lock()
_short_term_scanning = False
_short_term_timer = None


def _build_short_term_pool():
    """构建短线扫描股票池：活跃个股 + 板块龙头"""
    pool = set()
    # 从已缓存的板块数据中提取龙头股
    with _lock:
        for s in _store.get("sector", []):
            if s.get("top_stock"):
                pool.add(s["top_stock"])
        for s in _store.get("concept", []):
            if s.get("top_stock"):
                pool.add(s["top_stock"])
        # 加入热门个股
        for s in _store.get("stock", []):
            code = s.get("code", "")
            if code:
                prefix = "sh" if code.startswith("6") else "sz"
                pool.add(prefix + code)
    # 加入预设短线池（中小盘活跃股）
    preset = [
        "sz002594","sz300750","sz002475","sz300059","sz002049",
        "sz002371","sz002714","sz300274","sh688981","sz002230",
        "sz300015","sz000725","sz000063","sz002304","sz000333",
        "sz300760","sz002142","sz000069","sz002027","sh600309",
        "sz000858","sz000568","sz000651","sh600887","sh603288",
        "sh601012","sh601669","sh601211","sh601800","sh601919",
        "sh600276","sh600031","sh600585","sh600048","sh601688",
        "sh601998","sh601390","sh601186","sh601766","sh601899",
    ]
    for c in preset:
        pool.add(c)
    return list(pool)


def _get_realtime_batch(codes):
    """批量获取实时行情（腾讯API）"""
    if not codes:
        return []
    results = []
    batch_size = 50
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i+batch_size]
        raw = _curl(f"https://qt.gtimg.cn/q={','.join(batch)}",
                    ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.qq.com/"], enc="gbk")
        for line in raw.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            try:
                p = line.split("=", 1)[1].strip('"').split("~")
                if len(p) < 46:
                    continue
                code = p[2]
                name = p[1]
                latest = float(p[3] or 0)
                pct = float(p[32] or 0)
                amount = float(p[37] or 0)
                turnover = float(p[38] or 0)
                vol = float(p[6] or 0)  # 成交量（手）
                open_p = float(p[5] or 0)
                high = float(p[33] or 0) if len(p) > 33 else float(p[41] or 0)
                low = float(p[34] or 0) if len(p) > 34 else float(p[42] or 0)
                prev_close = float(p[4] or 0)
                if latest <= 0 or prev_close <= 0:
                    continue
                # 提取前缀
                prefix = "sh" if line.startswith("v_sh") else "sz" if line.startswith("v_sz") else ""
                results.append({
                    "symbol": prefix + code,
                    "code": code,
                    "name": name,
                    "price": latest,
                    "pct_change": pct,
                    "amount": amount,
                    "turnover": turnover,
                    "volume": vol,
                    "open": open_p,
                    "high": high,
                    "low": low,
                    "prev_close": prev_close,
                })
            except Exception:
                continue
    return results


def _detect_volume_surge(stock, klines):
    """量能突变检测 + 短线爆发力评分"""
    if not klines or len(klines) < 10:
        return None

    # 提取历史数据
    closes = []
    volumes = []
    highs = []
    lows = []
    for k in klines:
        try:
            closes.append(float(k[2]))
            volumes.append(float(k[5]) if len(k) > 5 else 0)
            highs.append(float(k[3]))
            lows.append(float(k[4]))
        except Exception:
            continue

    if len(closes) < 10:
        return None

    cur = stock["price"]
    prev_close = stock["prev_close"]

    # 前5日平均成交量（不含今日K线）
    hist_vols = volumes[:-1] if len(volumes) > 10 else volumes
    vol_5avg = sum(hist_vols[-5:]) / min(5, len(hist_vols[-5:])) if hist_vols else 0

    # 当前成交量（从实时数据）
    cur_vol = stock["volume"]

    # 盘中时间权重：计算当前交易时间占全天的比例
    now = datetime.datetime.now()
    t = now.hour * 100 + now.minute
    # A股交易时间: 9:30-11:30, 13:00-15:00 (共4小时=240分钟)
    if t < 930:
        time_ratio = 0.01
    elif t <= 1130:
        minutes = (t - 930)  # 9:30开始
        time_ratio = minutes / 240.0
    elif t < 1300:
        time_ratio = 120 / 240.0  # 上午2小时
    elif t <= 1500:
        minutes = 120 + (t - 1300)  # 上午2小时 + 下午
        time_ratio = minutes / 240.0
    else:
        time_ratio = 1.0  # 收盘后

    time_ratio = max(0.05, min(1.0, time_ratio))

    # 量能突变度：用时间加权的均量作为基准
    if vol_5avg <= 0:
        return None
    vol_surge = cur_vol / (vol_5avg * time_ratio) if vol_5avg > 0 else 0

    # 过滤条件
    pct = stock["pct_change"]
    turnover = stock["turnover"]

    # 收盘后降低阈值，让用户看到潜在异动股
    is_trading = 930 <= (now.hour * 100 + now.minute) <= 1500 and now.weekday() < 5
    if is_trading:
        min_vol_surge = 2.0
        min_turnover = 3.0
        min_amount = 1e4    # 1亿=10000万元
    else:
        min_vol_surge = 0.8
        min_turnover = 0.3
        min_amount = 5e3    # 5000万=5000万元

    if vol_surge < min_vol_surge:
        return None
    if pct < -3:  # 跌幅超过3%不选
        return None
    if turnover < min_turnover:
        return None
    if stock["amount"] < min_amount:
        return None

    # === 评分 ===
    score = 0

    # 1. 量能突变度（0-40分）
    if vol_surge >= 5:
        score += 40
    elif vol_surge >= 4:
        score += 35
    elif vol_surge >= 3:
        score += 30
    elif vol_surge >= 2.5:
        score += 25
    else:
        score += 20

    # 2. 价格强度（0-25分）
    if pct >= 5:
        score += 25
    elif pct >= 3:
        score += 20
    elif pct >= 1:
        score += 15
    else:
        score += 10

    # 3. 位置优势（0-20分）：低位启动得分高
    ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else cur
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else cur
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else cur

    # 距离20日最低价的距离
    low_20 = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    dist_from_low = (cur - low_20) / low_20 * 100 if low_20 > 0 else 0

    if dist_from_low < 5:
        score += 20  # 接近底部
    elif dist_from_low < 10:
        score += 15
    elif dist_from_low < 20:
        score += 10
    else:
        score += 5

    # 4. 流动性（0-15分）
    if turnover >= 10:
        score += 10  # 换手率过高可能有风险
    elif turnover >= 5:
        score += 15
    elif turnover >= 3:
        score += 12
    else:
        score += 8

    # 生成原因
    reasons = []
    reasons.append("量能放大%.1f倍(时间加权)" % vol_surge)
    if pct > 3:
        reasons.append("涨幅%.1f%%" % pct)
    elif pct > 0:
        reasons.append("微涨%.1f%%" % pct)
    if dist_from_low < 10:
        reasons.append("低位启动")
    if turnover > 5:
        reasons.append("换手%.1f%%" % turnover)
    elif turnover > 2:
        reasons.append("换手%.1f%%" % turnover)
    if not is_trading:
        reasons.append("收盘后预判")

    return {
        "symbol": stock["symbol"],
        "code": stock["code"],
        "name": stock["name"],
        "alert_type": "量能突变",
        "vol_surge": round(vol_surge, 2),
        "vol_ratio": round(vol_surge, 2),
        "price": stock["price"],
        "pct_change": pct,
        "amount": stock["amount"],
        "turnover": turnover,
        "score": min(100, score),
        "reason": ",".join(reasons),
        "time": datetime.datetime.now().strftime("%H:%M"),
        "date": datetime.datetime.now().strftime("%Y-%m-%d"),
    }


def _run_short_term_scan():
    """执行一次短线扫描"""
    global _short_term_alerts, _short_term_scanning
    _short_term_scanning = True
    try:
        pool = _build_short_term_pool()
        if not pool:
            print("[SHORT] 股票池为空", flush=True)
            return []

        # 批量获取实时行情
        stocks = _get_realtime_batch(pool)

        # 对每只股票获取K线并检测量能突变
        # 先用实时数据粗筛，减少K线请求
        candidates = []
        for stock in stocks:
            if stock["turnover"] < 0.2:
                continue
            if stock["amount"] < 3e3:
                continue
            candidates.append(stock)

        new_alerts = []
        for stock in candidates:
            try:
                name, klines = _get_kline(stock["symbol"], 30)
                if not klines or len(klines) < 10:
                    continue
                alert = _detect_volume_surge(stock, klines)
                if alert:
                    new_alerts.append(alert)
            except Exception as e:
                continue

        # 按评分排序
        new_alerts.sort(key=lambda x: x["score"], reverse=True)

        with _short_term_lock:
            # 合并预警（去重：同一只股票只保留最新的）
            existing = {a["symbol"]: a for a in _short_term_alerts}
            for a in new_alerts:
                existing[a["symbol"]] = a
            _short_term_alerts = sorted(existing.values(), key=lambda x: x["score"], reverse=True)
            # 只保留最近50条
            _short_term_alerts = _short_term_alerts[:50]

        print(f"[SHORT] 扫描{len(stocks)}只, 发现{len(new_alerts)}只异动", flush=True)
        # 自动保存预警到历史
        if new_alerts:
            _save_short_alerts(new_alerts)
        return new_alerts
    except Exception as e:
        print(f"[SHORT ERR] {e}", flush=True)
        return []
    finally:
        _short_term_scanning = False


def _short_term_loop():
    """定时扫描循环"""
    while True:
        try:
            # 只在交易时间扫描（9:30-15:00）
            now = datetime.datetime.now()
            hour = now.hour
            minute = now.minute
            t = hour * 100 + minute
            if 930 <= t <= 1500 and now.weekday() < 5:
                _run_short_term_scan()
            else:
                print("[SHORT] 非交易时间,跳过扫描", flush=True)
        except Exception as e:
            print(f"[SHORT ERR] {e}", flush=True)
        time.sleep(300)  # 5分钟扫描一次


@app.route("/api/short_term/scan")
def short_term_scan():
    """手动触发一次扫描"""
    alerts = _run_short_term_scan()
    return jsonify({"code": 0, "count": len(alerts), "data": alerts})


@app.route("/api/short_term/alerts")
def short_term_alerts():
    """获取最新预警列表"""
    with _short_term_lock:
        return jsonify({"code": 0, "data": _short_term_alerts})


@app.route("/api/short_term/status")
def short_term_status():
    """获取扫描状态"""
    return jsonify({
        "code": 0,
        "scanning": _short_term_scanning,
        "pool_size": len(_build_short_term_pool()),
        "alert_count": len(_short_term_alerts),
    })


# ===== 短线预警历史追踪 =====
_SHORT_HIST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "short_history")
os.makedirs(_SHORT_HIST_DIR, exist_ok=True)


def _short_hist_file(date_str=None):
    if not date_str:
        date_str = datetime.date.today().strftime("%Y-%m-%d")
    return os.path.join(_SHORT_HIST_DIR, f"{date_str}.csv")


def _save_short_alerts(alerts):
    """保存预警到CSV"""
    if not alerts:
        return
    fpath = _short_hist_file()
    exists = os.path.exists(fpath)
    try:
        with open(fpath, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(["date", "time", "code", "name", "price", "pct_change",
                                 "vol_ratio", "turnover", "score", "alert_type", "reason"])
            for a in alerts:
                writer.writerow([
                    a.get("date", datetime.date.today().strftime("%Y-%m-%d")),
                    a.get("time", ""),
                    a.get("code", ""),
                    a.get("name", ""),
                    a.get("price", 0),
                    a.get("pct_change", 0),
                    a.get("vol_ratio", 0),
                    a.get("turnover", 0),
                    a.get("score", 0),
                    a.get("alert_type", ""),
                    a.get("reason", ""),
                ])
    except Exception as e:
        print(f"[SHORT HIST ERR] {e}", flush=True)


def _load_short_history(days=10):
    """加载最近N天的预警历史"""
    records = []
    today = datetime.date.today()
    for i in range(days):
        d = today - datetime.timedelta(days=i)
        fpath = _short_hist_file(d.strftime("%Y-%m-%d"))
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    row["date"] = d.strftime("%Y-%m-%d")
                    try:
                        row["price"] = float(row.get("price", 0))
                        row["pct_change"] = float(row.get("pct_change", 0))
                        row["vol_ratio"] = float(row.get("vol_ratio", 0))
                        row["turnover"] = float(row.get("turnover", 0))
                        row["score"] = int(float(row.get("score", 0)))
                    except Exception:
                        pass
                    records.append(row)
        except Exception:
            continue
    return records


def _check_alert_performance(records):
    """检查预警后次日表现（胜率统计）"""
    results = []
    for r in records:
        alert_date = r.get("date", "")
        code = r.get("code", "")
        if not code:
            continue

        # 获取预警日的次日K线
        try:
            prefix = "sh" if code.startswith("6") else "sz"
            symbol = prefix + code
            name, klines = _get_kline(symbol, 10)
            if not klines or len(klines) < 2:
                r["next_day_change"] = None
                r["result"] = "未知"
                results.append(r)
                continue

            # 找到预警日次日的收盘价
            alert_dt = None
            next_close = None
            for j, k in enumerate(klines):
                if k[0] == alert_date:
                    alert_dt = j
                    break

            if alert_dt is not None and alert_dt + 1 < len(klines):
                alert_close = float(klines[alert_dt][2])
                next_close = float(klines[alert_dt + 1][2])
                next_day_change = (next_close - alert_close) / alert_close * 100
                r["next_day_change"] = round(next_day_change, 2)
                if next_day_change > 1:
                    r["result"] = "成功"
                elif next_day_change < -1:
                    r["result"] = "失败"
                else:
                    r["result"] = "持平"
                # 最高点收益
                next_high = float(klines[alert_dt + 1][3])
                r["max_gain"] = round((next_high - alert_close) / alert_close * 100, 2)
            else:
                r["next_day_change"] = None
                r["result"] = "待验证"
        except Exception:
            r["next_day_change"] = None
            r["result"] = "待验证"

        results.append(r)
    return results


@app.route("/api/short_term/history")
def short_term_history():
    """获取预警历史+胜率统计"""
    days = int(request.args.get("days", 10))
    records = _load_short_history(days)
    records = _check_alert_performance(records)

    # 按日期分组统计
    by_date = {}
    for r in records:
        d = r.get("date", "")
        if d not in by_date:
            by_date[d] = {"total": 0, "success": 0, "fail": 0, "flat": 0, "pending": 0}
        by_date[d]["total"] += 1
        if r["result"] == "成功":
            by_date[d]["success"] += 1
        elif r["result"] == "失败":
            by_date[d]["fail"] += 1
        elif r["result"] == "持平":
            by_date[d]["flat"] += 1
        else:
            by_date[d]["pending"] += 1

    # 总体胜率
    total = len(records)
    success = sum(1 for r in records if r["result"] == "成功")
    fail = sum(1 for r in records if r["result"] == "失败")
    flat = sum(1 for r in records if r["result"] == "持平")
    pending = sum(1 for r in records if r["result"] in ("未知", "待验证"))
    win_rate = success / (success + fail) * 100 if (success + fail) > 0 else 0

    return jsonify({
        "code": 0,
        "data": {
            "records": records[:200],
            "by_date": by_date,
            "summary": {
                "total": total,
                "success": success,
                "fail": fail,
                "flat": flat,
                "pending": pending,
                "win_rate": round(win_rate, 1),
            }
        }
    })


@app.route("/api/short_term/save", methods=["POST"])
def short_term_save():
    """手动保存当前预警到历史"""
    with _short_term_lock:
        alerts = list(_short_term_alerts)
    _save_short_alerts(alerts)
    return jsonify({"code": 0, "saved": len(alerts)})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_short_term_loop, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=port)
