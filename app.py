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
        # Windows 下 pythonw 父进程 spawn 控制台程序会自动弹黑窗, 用 CREATE_NO_WINDOW 压掉
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        r = subprocess.run(cmd, capture_output=True, timeout=15, creationflags=flags)
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
    """从新浪获取A股热门排行（按成交额降序，真正的市场热点）"""
    raw = _curl("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                "Market_Center.getHQNodeData?page=1&num=30&sort=amount&asc=0&node=hs_a",
                ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.sina.com.cn/"])
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return []
    out = []
    for x in data:
        try:
            code = x.get("code", "")
            symbol = x.get("symbol", "")
            name = x.get("name", "")
            if not code or symbol.startswith("bj") or "ST" in name.upper():
                continue
            out.append({
                "code": code,
                "name": name,
                "latest": float(x.get("trade", 0) or 0),
                "pct_change": float(x.get("changepercent", 0) or 0),
                "amount": float(x.get("amount", 0) or 0) / 1e4,
                "turnover": float(x.get("turnoverratio", 0) or 0),
            })
        except Exception:
            continue
    return out[:30]


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
    resp = app.make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

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


_KL_CACHE = {}  # symbol -> (ts, name, klines, days)
_KL_TTL = 300


def _get_kline(symbol, days=60):
    c = _KL_CACHE.get(symbol)
    if c and time.time() - c[0] < _KL_TTL and c[3] >= days:
        return c[1], c[2]
    # 腾讯双域名(主域常被WAF限流,备用优先;各重试)
    for attempt in range(2):
        for host in ("proxy.finance.qq.com", "web.ifzq.gtimg.cn"):
            url = f"https://{host}/appstock/app/fqkline/get?param={symbol},day,,,{days},qfq"
            data = _j(url, ["-H", f"User-Agent: {UA}", "-H", "Referer: https://web.ifzq.gtimg.cn/"])
            info = data.get("data", {}).get(symbol, {})
            qt = info.get("qt", {}).get(symbol, [])
            klines = info.get("qfqday", []) or info.get("day", [])
            if klines:
                name = qt[1] if len(qt) > 1 else ""
                _KL_CACHE[symbol] = (time.time(), name, klines, days)
                return name, klines
        time.sleep(0.5)
    # 搜狐备用(原始价, [date,open,close,chg,pct,low,high,vol,amount,...])
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
                # sohu: date,open,close,chg,pct,low,high,vol(手),amount(万),...
                klines.append([r[0], str(r[1]), str(r[2]), str(r[6]), str(r[5]), str(r[7])])
            except Exception:
                continue
        if len(klines) >= 15:
            klines.reverse()  # sohu最新在前→转升序(与腾讯一致,最后=最新)
            res = klines[-days:]
            _KL_CACHE[symbol] = (time.time(), "", res, days)
            return "", res
    except Exception:
        pass
    return "", []


_MF_CACHE = {}  # symbol -> (ts, money_flow_list)
_MF_TTL = 300


def _get_money_flow(symbol, days=5):
    """新浪个股主力资金流向(近N日): net=主力净流入额(元), ratio=净占比(%), r0=超大单净额"""
    c = _MF_CACHE.get(symbol)
    if c and time.time() - c[0] < _MF_TTL:
        return c[1]
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_qsfx_zjlrqs?page=1&num={days}&sort=opendate&asc=0&daima={symbol}")
    data = _j(url, ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.sina.com.cn/"])
    out = []
    if isinstance(data, list):
        for x in data:
            try:
                out.append({
                    "date": x.get("opendate", ""),
                    "net": float(x.get("netamount", 0) or 0),
                    "ratio": float(x.get("ratioamount", 0) or 0) * 100,
                    "r0_net": float(x.get("r0_net", 0) or 0),
                    "chg": float(x.get("changeratio", 0) or 0) * 100,
                })
            except Exception:
                continue
    _MF_CACHE[symbol] = (time.time(), out)
    return out


def _get_market_context():
    """大盘(上证指数)趋势闸门: bear=20日跌超5%视为弱势"""
    try:
        name, kl = _get_kline("sh000001", days=30)
        if not kl or len(kl) < 20:
            return {"idx": "", "chg5": 0.0, "chg20": 0.0, "bear": False}
        closes = [float(k[2]) for k in kl]
        chg5 = (closes[-1] - closes[-5]) / closes[-5] * 100
        chg20 = (closes[-1] - closes[-20]) / closes[-20] * 100
        return {"idx": name, "chg5": round(chg5, 2), "chg20": round(chg20, 2), "bear": chg20 < -5}
    except Exception:
        return {"idx": "", "chg5": 0.0, "chg20": 0.0, "bear": False}


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


def _calc_position_advice(score_xichou, score_xipan, score_chuhuo, cur, ma5, ma20, change_5d, change_20d, vol_ratio):
    """根据技术信号计算仓位管理建议"""
    advice = {
        "action": "持有",
        "position_pct": 0,
        "stop_loss": 0,
        "reason": "",
    }

    # 止损位：MA20下方3%
    if ma20[-1]:
        advice["stop_loss"] = round(ma20[-1] * 0.97, 2)

    # 强出货信号 → 大幅减仓
    if score_chuhuo >= 30 and score_chuhuo > score_xichou:
        if change_20d > 15:
            advice["action"] = "止盈减仓"
            advice["position_pct"] = 50
            advice["reason"] = "高位出货信号强,建议减仓50%锁定利润"
        elif cur < (ma20[-1] if ma20[-1] else cur):
            advice["action"] = "止损"
            advice["position_pct"] = 80
            advice["reason"] = "跌破支撑位,建议止损80%"
        else:
            advice["action"] = "减仓"
            advice["position_pct"] = 30
            advice["reason"] = "出货信号较强,建议减仓30%"
    # 中等出货信号 → 小幅减仓
    elif score_chuhuo >= 15 and score_chuhuo > score_xichou:
        advice["action"] = "减仓"
        advice["position_pct"] = 20
        advice["reason"] = "存在出货迹象,建议小幅减仓"
    # 吸筹信号 → 可加仓
    elif score_xichou >= 20 and score_xichou > score_chuhuo:
        if change_20d < -10:
            advice["action"] = "加仓"
            advice["position_pct"] = 20
            advice["reason"] = "超跌吸筹信号,可分批加仓"
        else:
            advice["action"] = "持有"
            advice["position_pct"] = 0
            advice["reason"] = "吸筹信号,继续持有"
    # 洗盘 → 持有不动
    elif score_xipan >= 20 and score_xipan > score_xichou:
        advice["action"] = "持有"
        advice["position_pct"] = 0
        advice["reason"] = "洗盘阶段,主力未走,持有不动"

    return advice


def _check_capital_filter(symbol, behavior):
    """资金面硬过滤：真实新浪个股主力资金流
    through=False 时仓位上限减半；吸筹/洗盘类必须资金配合"""
    try:
        mf = _get_money_flow(symbol, days=5)
        if not mf:
            return {"through": True, "score": 50, "reason": "资金数据获取失败,保守放行"}
        today = mf[0]
        net_5d = sum(x["net"] for x in mf)
        ratio_5d = sum(x["ratio"] for x in mf) / len(mf)
        net_today = today["net"]
        ratio_today = today["ratio"]
        # 净占比映射到0-100分: 占比+5%→75分, 0→50分, -5%→25分
        score = max(0, min(100, round(50 + ratio_5d * 5)))
        direction = "净流入" if net_5d >= 0 else "净流出"
        reason = f"主力近5日{direction} {net_5d/1e8:.2f}亿,今日净占比{ratio_today:+.2f}%"
        if behavior in ("吸筹", "洗盘"):
            # 买类信号必须资金配合：5日净流入，或今日无明显流出
            if net_5d > 0 or (net_5d > -2e7 and ratio_today > -0.5):
                return {"through": True, "score": score, "reason": reason}
            return {"through": False, "score": score, "reason": reason + ",资金未配合,信号降级"}
        elif behavior in ("止盈", "止损", "减仓"):
            # 卖出信号无需资金确认
            return {"through": True, "score": score, "reason": reason + "(卖出信号无需资金确认)"}
        elif behavior in ("观望", "震荡"):
            return {"through": score >= 50, "score": score, "reason": reason}
        else:
            return {"through": True, "score": score, "reason": reason}
    except Exception:
        return {"through": True, "score": 50, "reason": "资金面数据异常,默认放行"}


def _build_enhanced_advice(
    behavior, behavior_desc, behavior_color,
    cur, ma5, ma10, ma20, change_5d, change_20d,
    vol_ratio, vol_ratio_5, vol_level, vol_percentile,
    kdj_oversold, kdj_overbought,
    macd_gold_cross, macd_dead_cross,
    ma_bullish, above_ma5, above_ma20,
    price_change_5d, price_change_20d,
    drawdown,
    capital_filter,
    vol_trend_direction, vp_harmony,
    signals, vp_signals,
    day_change_pct, high_to_close,
    market=None,
):
    """构建增强版仓位建议：诊断报告 + 分级仓位 + 资金过滤 + 关键价位"""
    
    # === 关键支撑/压力位 ===
    support_ma5 = round(ma5[-1], 2) if ma5[-1] else None
    support_ma10 = round(ma10[-1], 2) if ma10[-1] else None
    support_ma20 = round(ma20[-1], 2) if ma20[-1] else None
    resistance_ma5 = support_ma5
    resistance_ma10 = support_ma10
    
    # === 基础止损位 ===
    stop_loss_ma20 = round(ma20[-1] * 0.97, 2) if ma20[-1] else None
    stop_loss_ma10 = round(ma10[-1] * 0.95, 2) if ma10[-1] else None
    stop_loss_ma5 = round(ma5[-1] * 0.98, 2) if ma5[-1] else None
    # 止损必须低于现价(买入价)3%以内，否则买入即触发止损，无意义
    # 场景：股价在MA20下方时 MA20*0.97 会高于现价
    stop_cap = round(cur * 0.97, 2)
    stop_loss_ma20 = min(stop_loss_ma20, stop_cap) if stop_loss_ma20 else stop_cap
    stop_loss_ma10 = min(stop_loss_ma10, stop_cap) if stop_loss_ma10 else stop_cap
    stop_loss_ma5 = min(stop_loss_ma5, stop_cap) if stop_loss_ma5 else stop_cap
    
    # === 行为对应的分级仓位方案 ===
    tiered_plan = {
        "吸筹": {
            "tier1": {"name": "试探性建仓", "pct": 10, "trigger": "缩量止跌+支撑位不破+KDJ<20金叉", "stop": stop_loss_ma20},
            "tier2": {"name": "确认性加仓", "pct": 25, "trigger": "放量突破MA5+MACD金叉", "stop": stop_loss_ma10},
            "tier3": {"name": "趋势跟随", "pct": 35, "trigger": "MA5/10/20多头排列+回调不破MA5", "stop": stop_loss_ma5},
            "tier4": {"name": "加速持有", "pct": 20, "trigger": "主升浪放量创新高", "stop": stop_loss_ma5},
            "max_total": 90,
            "cash_reserve": 10,
        },
        "洗盘": {
            "tier1": {"name": "末期试探", "pct": 10, "trigger": "地量+守住MA20+KDJ金叉", "stop": stop_loss_ma20},
            "tier2": {"name": "反弹确认", "pct": 20, "trigger": "放量突破MA5+收复MA10", "stop": stop_loss_ma10},
            "tier3": {"name": "趋势跟随", "pct": 30, "trigger": "站上MA10+缩量回调不破MA5", "stop": stop_loss_ma5},
            "tier4": {"name": "加速持有", "pct": 25, "trigger": "多头排列+板块联动", "stop": stop_loss_ma5},
            "max_total": 85,
            "cash_reserve": 15,
        },
        "震荡": {
            "tier1": {"name": "底部试探", "pct": 8, "trigger": "距近低<5%+缩量+KDJ超卖金叉", "stop": stop_loss_ma20},
            "tier2": {"name": "突破确认", "pct": 20, "trigger": "放量突破震荡上沿+MA5金叉", "stop": stop_loss_ma10},
            "tier3": {"name": "趋势跟随", "pct": 25, "trigger": "站上MA20+多头排列", "stop": stop_loss_ma5},
            "tier4": {"name": "加速持有", "pct": 15, "trigger": "突破前高+放量", "stop": stop_loss_ma5},
            "max_total": 70,
            "cash_reserve": 30,
        },
        "观望": {
            "tier1": {"name": "等待信号", "pct": 0, "trigger": "等待吸筹/洗盘确认", "stop": None},
            "max_total": 0,
            "cash_reserve": 100,
        },
        "减仓": {
            "tier1": {"name": "预警减仓", "pct": -20, "trigger": "冲高回落/量价背离/触及压力位", "stop": None},
            "tier2": {"name": "确认止盈", "pct": -30, "trigger": "放量跌破MA5/连续放量下跌", "stop": None},
            "tier3": {"name": "保底清仓", "pct": -50, "trigger": "破MA20不收回/破前低", "stop": None},
            "max_total": -100,
            "cash_reserve": 100,
        },
        "止盈": {
            "tier1": {"name": "止盈减仓", "pct": -40, "trigger": "高位放量大阴线/冲高回落", "stop": None},
            "tier2": {"name": "趋势止盈", "pct": -30, "trigger": "跌破MA5/量价背离", "stop": None},
            "tier3": {"name": "保底清仓", "pct": -30, "trigger": "破MA10/破前低", "stop": None},
            "max_total": -100,
            "cash_reserve": 100,
        },
        "止损": {
            "tier1": {"name": "紧急止损", "pct": -80, "trigger": "跌破关键支撑/破位下行", "stop": None},
            "tier2": {"name": "剩余清仓", "pct": -20, "trigger": "反抽不破MA5", "stop": None},
            "max_total": -100,
            "cash_reserve": 100,
        },
    }
    
    plan = tiered_plan.get(behavior, tiered_plan["震荡"])
    
    # === 资金面过滤调整 ===
    capital_ok = capital_filter.get("through", True)
    capital_score = capital_filter.get("score", 50)
    capital_reason = capital_filter.get("reason", "")
    
    if not capital_ok:
        # 资金面不通过：大幅降低仓位上限
        plan["max_total"] = max(0, plan["max_total"] // 2)
        plan["cash_reserve"] = 100 - plan["max_total"]
        for k in ["tier1", "tier2", "tier3", "tier4"]:
            if k in plan and isinstance(plan[k], dict) and "pct" in plan[k]:
                plan[k]["pct"] = max(0, plan[k]["pct"] // 2)
    
    # === 大盘闸门：弱势大盘降级买类信号 ===
    market_note = ""
    if market and market.get("bear") and behavior in ("吸筹", "洗盘"):
        market_note = f"大盘20日{market['chg20']:.1f}%弱势,买类信号降级"
        plan["max_total"] = 0
        plan["cash_reserve"] = 100
        for k in ["tier1", "tier2", "tier3", "tier4"]:
            if k in plan and isinstance(plan[k], dict) and "pct" in plan[k]:
                plan[k]["pct"] = 0
    
    # === 生成今日诊断报告 ===
    today_action = ""
    today_reason = ""
    today_urgency = "低"
    today_risk = "中"
    suggested_pct = 0
    key_prices = {}
    
    if behavior in ("吸筹", "洗盘"):
        today_action = "试探性建仓" if plan["tier1"]["pct"] > 0 else "观望"
        today_reason = plan["tier1"]["trigger"]
        today_urgency = "中" if plan["tier1"]["pct"] > 0 else "低"
        today_risk = "中"
        suggested_pct = plan["tier1"]["pct"]
        key_prices = {
            "买入区间": f"{round(cur*0.99,2)}-{round(cur*1.01,2)}",
            "止损": plan["tier1"]["stop"],
            "支撑位": {"MA5": support_ma5, "MA10": support_ma10, "MA20": support_ma20},
        }
    elif behavior in ("止盈", "减仓"):
        today_action = "预警减仓"
        today_reason = plan["tier1"]["trigger"]
        today_urgency = "高"
        today_risk = "高"
        suggested_pct = plan["tier1"]["pct"]
        key_prices = {
            "减仓区间": f"{round(cur*0.99,2)}-{round(cur*1.01,2)}",
            "止盈目标": resistance_ma5,
            "压力位": {"MA5": resistance_ma5, "MA10": resistance_ma10},
        }
    elif behavior == "止损":
        today_action = "紧急止损"
        today_reason = plan["tier1"]["trigger"]
        today_urgency = "高"
        today_risk = "极高"
        suggested_pct = plan["tier1"]["pct"]
        key_prices = {
            "止损价": plan["tier1"]["stop"],
            "支撑位": {"MA20": support_ma20},
        }
    elif behavior == "减仓":
        today_action = "分批止盈"
        today_reason = plan["tier1"]["trigger"]
        today_urgency = "中"
        today_risk = "中"
        suggested_pct = plan["tier1"]["pct"]
        key_prices = {
            "减仓区间": f"{round(cur*0.99,2)}-{round(cur*1.01,2)}",
            "压力位": {"MA5": resistance_ma5, "MA10": resistance_ma10},
        }
    else:
        today_action = "观望"
        today_reason = "震荡/观望，等待明确信号"
        today_urgency = "低"
        today_risk = "中"
        suggested_pct = 0
        key_prices = {}
    
    # === 组装返回 ===
    return {
        # 今日决策
        "action": today_action,
        "reason": today_reason,
        "urgency": today_urgency,
        "risk_level": today_risk,
        "position_pct": suggested_pct,
        
        # 分级仓位表
        "tiered_plan": plan,
        
        # 关键价位
        "key_prices": key_prices,
        
        # 资金面过滤
        "capital_filter": {
            "through": capital_ok,
            "score": capital_score,
            "reason": capital_reason,
        },
        
        # 确认信号
        "confirm_signals": [s["text"] for s in signals if s.get("weight", 0) >= 20],
        
        # 风控提醒
        "risk_notes": [
            f"单票上限: {plan['max_total']}%",
            f"现金底仓: {plan['cash_reserve']}%",
            f"资金面: {'通过' if capital_ok else '不通过'} ({capital_score}分)",
            f"大盘: {market_note or ('正常' if market and not market.get('bear') else '未知')}",
            f"止损纪律: 破位不补仓，止损不移动",
        ],
        
        # 兼容旧字段
        "stop_loss": stop_loss_ma20,
        "price_suggestion": round(cur, 2),
        "behavior": behavior,
        "behavior_desc": behavior_desc,
        "behavior_color": behavior_color,
    }


# === 回测证据：规则命中率(来自 backtest_rules_result.json) ===
_rule_stats = None
_RULE_TEXT_TO_ID = {
    "急跌后缩量企稳,主力可能逢低吸筹": "R1",
    "暴跌后放量反弹,资金试探性介入": "R2",
    "放量长上影线,拉高后遭遇抛压,注意出货": "R4",
    "底部长下影阳线,止跌信号明确": "R5",
    "高位横盘放量,主力可能派发筹码": "R6",
    "连续缩量,抛压逐渐衰竭": "R7",
    "放量突破MA5,短期趋势转强": "R8",
    "价涨量缩,上涨乏力,注意回调风险": "R10",
    "急跌后十字星,变盘信号,关注方向选择": "R11",
    "底部连续小阳线,主力缓慢建仓迹象": "R12",
    "涨幅后缩量回调,主力可能洗盘,清洗浮筹": "W1",
    "回调不破MA20,强势洗盘,主力护盘明显": "W2",
    "放量下跌后快速缩量,恐慌盘涌出,洗盘可能接近尾声": "W3",
    "横盘缩量震荡,主力可能在洗盘等待时机": "W4",
    "跌破MA5守住MA20,假跌破洗盘可能性大": "W5",
    "放量跌破MA5和MA20,断头铡刀形态,趋势转弱": "C3",
    "高位长上影缩量,拉高试盘失败,主力放弃护盘": "C5",
    "跌破近5日低点,破位下行,止损信号": "C6",
}


def _rule_text_to_id(text):
    if text.startswith("放量大阴线(跌"):
        return "C1"
    if text.startswith("冲高回落(高点回落"):
        return "C2"
    if text.startswith("连续两日放量下跌"):
        return "C4"
    return _RULE_TEXT_TO_ID.get(text, "")


def _load_rule_stats():
    global _rule_stats
    if _rule_stats is not None:
        return _rule_stats
    _rule_stats = {}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_rules_result.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for r in data.get("rules", []):
                _rule_stats[r.get("id", "")] = r.get("win3", 44.0)
    except Exception:
        pass
    return _rule_stats


_state_dist = None


def _load_state_dist():
    """分状态(牛/熊/震荡)未来3日涨跌幅分布, 来自 backtest_rules_result.json"""
    global _state_dist
    if _state_dist is not None:
        return _state_dist
    _state_dist = {"states": {}}
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_rules_result.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _state_dist = data.get("state_dist") or {"states": {}}
    except Exception:
        pass
    return _state_dist


def _calc_prob_up_3d(signals, market):
    """基于回测胜率证据的3日上涨概率估计(0-100)。
    原理：每条触发规则按其历史3日胜率偏离50%的幅度×权重贡献调整基准概率50%，
    再按大盘强弱修正。胜率数据来自 backtest_rules_result.json（backtest_rules.py 产出）。"""
    stats = _load_rule_stats()
    prob = 50.0
    for s in signals:
        rid = _rule_text_to_id(s.get("text", ""))
        win3 = stats.get(rid, 44.0)  # 缺省用全样本基线胜率
        # win3 即该规则触发后3日上涨胜率: 吸筹类>50、出货类<50,直接以偏离度×权重贡献
        prob += (win3 - 50.0) * (s.get("weight", 0) / 35.0)
    if market:
        if market.get("bear"):
            prob -= 8
        if market.get("chg20", 0) < -5:
            prob -= 5
        if market.get("chg5", 0) > 3:
            prob += 4
        elif market.get("chg5", 0) < -3:
            prob -= 4
    return round(max(0.0, min(100.0, prob)), 1)


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
    vol_10 = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else vol_5
    vol_20 = sum(volumes[-20:]) / min(20, len(volumes[-20:]))
    vol_ratio = vol_5 / vol_20 if vol_20 > 0 else 1
    # 当日量比：相对5日/10日均量（更敏感）
    vol_ratio_5 = volumes[-1] / vol_5 if vol_5 > 0 else 1
    vol_ratio_10 = volumes[-1] / vol_10 if vol_10 > 0 else 1
    
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
        score_xichou += 5
        signals.append({"text": "急跌后缩量企稳,主力可能逢低吸筹", "weight": 25, "type": "吸筹"})

    # 规则2: 暴跌后放量反弹 → 超跌反弹/试探性建仓
    if change_20d < -15 and change_5d > 5 and vol_ratio > 1.5:
        score_xichou += 25
        signals.append({"text": "暴跌后放量反弹,资金试探性介入", "weight": 20, "type": "吸筹"})

    # 规则4: 放量长上影线 → 拉高出货
    last = data[-1]
    upper_shadow = (last["high"] - max(last["open"], last["close"])) / last["open"] * 100
    if upper_shadow > 3 and vol_ratio > 1.5:
        score_chuhuo += 25
        signals.append({"text": "放量长上影线,拉高后遭遇抛压,注意出货", "weight": 30, "type": "出货"})

    # 规则5: 底部长下影阳线 → 止跌信号
    lower_shadow = (min(last["open"], last["close"]) - last["low"]) / last["open"] * 100
    if lower_shadow > 2 and last["close"] > last["open"] and near_low:
        score_xichou += 25
        signals.append({"text": "底部长下影阳线,止跌信号明确", "weight": 5, "type": "吸筹"})

    # 规则6: 高位横盘放量 → 派发
    if abs(change_5d) < 2 and change_20d > 10 and vol_ratio > 1.3:
        score_chuhuo += 10
        signals.append({"text": "高位横盘放量,主力可能派发筹码", "weight": 5, "type": "出货"})

    # 规则7: 连续缩量3日以上 → 抛压衰竭
    if all(volumes[-i] < volumes[-i - 1] for i in range(1, min(4, len(volumes)))):
        score_xichou += 5
        signals.append({"text": "连续缩量,抛压逐渐衰竭", "weight": 5, "type": "吸筹"})

    # 规则8: 放量突破MA5 → 短期转强
    if ma5[-1] and cur > ma5[-1] and data[-2]["close"] < ma5[-2] and vol_ratio > 1.2:
        score_xichou += 15
        signals.append({"text": "放量突破MA5,短期趋势转强", "weight": 5, "type": "吸筹"})

    # 规则10: 量价背离(价涨量缩) → 上涨乏力
    if change_5d > 2 and vol_ratio < 0.7:
        score_chuhuo += 5
        signals.append({"text": "价涨量缩,上涨乏力,注意回调风险", "weight": 5, "type": "出货"})

    # 规则11: 急跌后十字星 → 变盘信号
    body = abs(last["close"] - last["open"]) / last["open"] * 100
    if body < 0.5 and change_20d < -10:
        score_xichou += 20
        signals.append({"text": "急跌后十字星,变盘信号,关注方向选择", "weight": 20, "type": "吸筹"})

    # 规则12: 底部连续小阳线 → 主力缓慢建仓
    last3 = data[-3:]
    if all(d["close"] > d["open"] for d in last3) and all(abs(d["close"] - d["open"]) / d["open"] * 100 < 2 for d in last3):
        if near_low:
            score_xichou += 25
            signals.append({"text": "底部连续小阳线,主力缓慢建仓迹象", "weight": 15, "type": "吸筹"})

    # === 洗盘规则 ===
    # 规则W1: 涨幅后缩量回调 → 洗盘（主力不卖，散户恐慌）
    if change_20d > 10 and change_5d < -3 and vol_ratio < 0.7:
        score_xipan += 25
        signals.append({"text": "涨幅后缩量回调,主力可能洗盘,清洗浮筹", "weight": 5, "type": "洗盘"})

    # 规则W2: 回调不破MA20 → 强势洗盘
    if ma20[-1] and cur > ma20[-1] and change_5d < -2 and vol_ratio < 0.8:
        score_xipan += 5
        signals.append({"text": "回调不破MA20,强势洗盘,主力护盘明显", "weight": 5, "type": "洗盘"})

    # === 出货/减仓规则（精细化） ===
    # 规则C1: 高位放量大阴线 → 主力出货（大众交通9/23典型形态）
    day_change_pct = (last["close"] - last["open"]) / last["open"] * 100 if last["open"] > 0 else 0
    # 使用5日量比：相对近期均量放大，不受远期大量干扰
    if day_change_pct < -3 and vol_ratio_5 > 1.5:
        score_chuhuo += 35
        signals.append({"text": f"放量大阴线(跌{abs(day_change_pct):.1f}%,量比{vol_ratio_5:.1f}x),主力出货信号强烈", "weight": 35, "type": "出货"})

    # 规则C2: 冲高回落(从高点回落>3%) → 买盘衰竭
    high_to_close = (last["high"] - last["close"]) / last["high"] * 100 if last["high"] > 0 else 0
    if high_to_close > 3 and vol_ratio_5 > 1.2:
        score_chuhuo += 25
        signals.append({"text": f"冲高回落(高点回落{high_to_close:.1f}%),上方抛压沉重", "weight": 30, "type": "出货"})

    # 规则C3: 放量跌破MA5+MA20 → 断头铡刀
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur < ma20[-1]:
        if data[-2]["close"] > ma5[-2] and vol_ratio_5 > 1.3:
            score_chuhuo += 30
            signals.append({"text": "放量跌破MA5和MA20,断头铡刀形态,趋势转弱", "weight": 25, "type": "出货"})

    # 规则C4: 连续2日放量下跌 → 加速出货
    if len(data) >= 3:
        prev_day = data[-2]
        prev_change = (prev_day["close"] - prev_day["open"]) / prev_day["open"] * 100 if prev_day["open"] > 0 else 0
        prev_vol_ratio_5 = volumes[-2] / vol_5 if vol_5 > 0 else 1
        if day_change_pct < -1 and prev_change < -1 and vol_ratio_5 > 1.2 and prev_vol_ratio_5 > 1.2:
            score_chuhuo += 30
            signals.append({"text": "连续两日放量下跌,主力加速出货", "weight": 35, "type": "出货"})

    # 规则C5: 高位长上影+缩量 → 拉高试盘失败
    if upper_shadow > 2 and vol_ratio_5 < 0.8 and change_20d > 10:
        score_chuhuo += 5
        signals.append({"text": "高位长上影缩量,拉高试盘失败,主力放弃护盘", "weight": 5, "type": "出货"})

    # 规则C6: 跌破前低 → 破位下行
    if len(lows) >= 5:
        recent_low = min(lows[-5:])
        if cur < recent_low and last["close"] < last["open"]:
            score_chuhuo += 20
            signals.append({"text": "跌破近5日低点,破位下行,止损信号", "weight": 20, "type": "出货"})

    # 规则W3: 放量下跌后快速缩量 → 恐慌盘涌出后洗盘结束
    if len(volumes) >= 3 and volumes[-3] > vol_20 * 1.3 and volumes[-1] < vol_20 * 0.7:
        score_xipan += 5
        signals.append({"text": "放量下跌后快速缩量,恐慌盘涌出,洗盘可能接近尾声", "weight": 10, "type": "洗盘"})

    # 规则W4: 横盘震荡中量能萎缩 → 主力洗盘等时机
    if abs(change_5d) < 3 and abs(change_20d) < 8 and vol_ratio < 0.75:
        score_xipan += 8
        signals.append({"text": "横盘缩量震荡,主力可能在洗盘等待时机", "weight": 25, "type": "洗盘"})

    # 规则W5: 跌破MA5但守住MA20 → 假跌破洗盘
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur > ma20[-1] and change_5d < -2:
        score_xipan += 20
        signals.append({"text": "跌破MA5守住MA20,假跌破洗盘可能性大", "weight": 20, "type": "洗盘"})

    # --- 综合判断（精细化） ---
    print(f"[DEBUG] score_xichou={score_xichou} score_xipan={score_xipan} score_chuhuo={score_chuhuo}", flush=True)
    total = score_xichou + score_xipan + score_chuhuo
    if total == 0:
        total = 1

    # 精细化行为判断
    if score_chuhuo >= 30 and score_chuhuo > score_xichou and score_chuhuo > score_xipan:
        # 强出货信号
        if change_20d > 15 and day_change_pct < -3:
            behavior = "止盈"
            behavior_desc = "高位放量大阴线,建议止盈减仓锁定利润"
            behavior_color = "#f87171"
        elif cur < (ma20[-1] if ma20[-1] else cur * 0.9):
            behavior = "止损"
            behavior_desc = "跌破重要支撑位,建议止损离场"
            behavior_color = "#f87171"
        else:
            behavior = "减仓"
            behavior_desc = "出货信号较强,建议减仓控制风险"
            behavior_color = "#f59e0b"
    elif score_xichou >= 20 and score_xichou > score_xipan and score_xichou > score_chuhuo:
        behavior = "吸筹"
        behavior_desc = "主力可能正在低位吸筹,关注放量突破和均线金叉信号"
        behavior_color = "#16a34a"
    elif score_xipan >= 20 and score_xipan > score_xichou and score_xipan > score_chuhuo:
        behavior = "洗盘"
        behavior_desc = "主力可能正在洗盘,关注缩量企稳后的反弹机会"
        behavior_color = "#38bdf8"
    elif score_chuhuo >= 15 and score_chuhuo > score_xichou:
        behavior = "观望"
        behavior_desc = "存在出货迹象,建议观望等待明确方向"
        behavior_color = "#f59e0b"
    else:
        behavior = "震荡"
        behavior_desc = "当前处于震荡整理阶段,方向不明,建议观望"
        behavior_color = "#8ea3c4"
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

# === 资金面硬过滤 ===
    capital_filter = _check_capital_filter(symbol, behavior)

    # === 大盘上下文（指数趋势闸门） ===
    market_ctx = _get_market_context()

    # === 分状态涨跌分布: 当前大盘状态 + 该状态下未来3日涨跌概率分布(回测证据) ===
    _sd = _load_state_dist()
    _chg20 = market_ctx.get("chg20", 0)
    _cur_st = "bull" if _chg20 >= 5 else ("bear" if _chg20 < -5 else "normal")
    _cur_d = (_sd.get("states") or {}).get(_cur_st)
    market_state = {
        "state": _cur_st,
        "state_label": {"bull": "牛市", "normal": "震荡", "bear": "熊市"}.get(_cur_st, "震荡"),
        "chg20": _chg20,
        "win3": (_cur_d or {}).get("win3"),
        "ret3": (_cur_d or {}).get("ret3"),
        "buckets": (_cur_d or {}).get("buckets", []),
        "bucket_labels": _sd.get("bucket_labels", []),
    }

    # === 增强版仓位建议（含诊断报告/分级仓位/资金过滤） ===
    position_advice = _build_enhanced_advice(
        behavior, behavior_desc, behavior_color,
        cur, ma5, ma10, ma20, change_5d, change_20d,
        vol_ratio, vol_ratio_5, vol_level, vol_percentile,
        kdj_oversold, kdj_overbought,
        macd_gold_cross, macd_dead_cross,
        ma_bullish, above_ma5, above_ma20,
        price_change_5d=change_5d, price_change_20d=change_20d,
        drawdown=drawdown,
        capital_filter=capital_filter,
        vol_trend_direction=vol_trend_direction,
        vp_harmony=vp_harmony,
        signals=signals,
        vp_signals=vp_signals,
        day_change_pct=day_change_pct,
        high_to_close=(last["high"] - last["close"]) / last["high"] * 100 if last["high"] > 0 else 0,
        market=market_ctx,
    )

    # === 3日上涨概率(回测胜率证据) ===
    prob_up = _calc_prob_up_3d(signals, market_ctx)
    if prob_up >= 65:
        prob_label = "偏多"
    elif prob_up >= 55:
        prob_label = "温和偏多"
    elif prob_up >= 45:
        prob_label = "中性"
    elif prob_up >= 35:
        prob_label = "偏空"
    else:
        prob_label = "空头"
    position_advice.setdefault("risk_notes", []).append(f"3日上涨概率: {prob_up:.0f}% ({prob_label})")

    # === 半凯利配仓: 概率优势驱动仓位(索普式: 优势大重注, 优势小轻注, 无优势不入) ===
    if prob_up >= 65:
        kelly_scale, kelly_note = 1.0, f"概率配仓: {prob_up:.0f}%偏多, 满配"
    elif prob_up >= 55:
        kelly_scale, kelly_note = 0.7, f"概率配仓: {prob_up:.0f}%温和偏多, 7成"
    elif prob_up >= 45:
        kelly_scale, kelly_note = 0.5, f"概率配仓: {prob_up:.0f}%中性, 5成试探"
    elif prob_up >= 35:
        kelly_scale, kelly_note = 0.2, f"概率配仓: {prob_up:.0f}%偏空, 2成以下"
    else:
        kelly_scale, kelly_note = 0.0, f"概率配仓: {prob_up:.0f}%空头, 不入场"
    tp = position_advice.get("tiered_plan") or {}
    if tp.get("max_total", 0) > 0 and kelly_scale < 1.0:
        # 只对买类仓位生效; 满配时不动(上限已含资金面/大盘闸门的压制)
        tp["kelly_scale"] = kelly_scale
        for _k in ("tier1", "tier2", "tier3", "tier4"):
            _t = tp.get(_k)
            if isinstance(_t, dict) and "pct" in _t:
                _t["pct"] = max(0, int(_t["pct"] * kelly_scale))
        _new_max = max(0, int(tp["max_total"] * kelly_scale))
        tp["max_total"] = _new_max
        tp["cash_reserve"] = 100 - _new_max
        if position_advice.get("position_pct"):
            position_advice["position_pct"] = max(0, int(position_advice["position_pct"] * kelly_scale))
        position_advice.setdefault("risk_notes", []).append(kelly_note)
        # 同步'单票上限'/'现金底仓'风控文案为概率配仓后的真实值
        for _i, _n in enumerate(position_advice["risk_notes"]):
            if _n.startswith("单票上限:"):
                position_advice["risk_notes"][_i] = f"单票上限: {tp['max_total']}%"
            elif _n.startswith("现金底仓:"):
                position_advice["risk_notes"][_i] = f"现金底仓: {tp['cash_reserve']}%"
                break

    return {
        "symbol": symbol, "name": name,
        "market_state": market_state,
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
            "market": market_ctx,
            "prob_up_3d": prob_up,
            "prob_label": prob_label,
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


@app.route("/api/behavior_history")
def behavior_history():
    """获取股票历史行为标签（用于K线tooltip）"""
    code = request.args.get("code", "").strip()
    if not code:
        return jsonify({"code": -1, "msg": "code required"})
    symbol = _code_to_symbol(code)
    
    # 先查缓存
    cached = _load_behavior_cache(code)
    if cached:
        return jsonify({"code": 0, "data": cached, "cached": True})
    
    # 计算并缓存
    history = _compute_behavior_history(symbol, 60)
    if history:
        cache_data = {"symbol": symbol, "history": history, "updated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}
        _save_behavior_cache(code, cache_data)
        return jsonify({"code": 0, "data": cache_data, "cached": False})
    
    return jsonify({"code": 0, "data": {"history": {}}, "cached": False})


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
            s["advice_color"] = "#f87171"
        elif score >= 35:
            s["advice"] = "推荐"
            s["advice_color"] = "#f59e0b"
        elif score >= 25:
            s["advice"] = "关注"
            s["advice_color"] = "#38bdf8"
        else:
            s["advice"] = "观望"
            s["advice_color"] = "#8ea3c4"

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
            s["signal_color"] = "#f87171"
            s["confidence"] = "高"
            s["expected_open"] = f"+{round(pct*0.3+pct*0.5, 1)}%"
        elif score >= 40:
            s["signal"] = "买入"
            s["signal_color"] = "#f59e0b"
            s["confidence"] = "中"
            s["expected_open"] = f"+{round(pct*0.2+pct*0.3, 1)}%"
        elif score >= 28:
            s["signal"] = "关注"
            s["signal_color"] = "#38bdf8"
            s["confidence"] = "中低"
            s["expected_open"] = f"+{round(pct*0.1, 1)}%"
        else:
            s["signal"] = "观望"
            s["signal_color"] = "#8ea3c4"
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
        mood_color = "#f87171"
        mood_emoji = "🔥"
        advice = "市场过热，注意追高风险，可适当减仓"
    elif strength >= 55:
        mood = "贪婪"
        mood_color = "#f59e0b"
        mood_emoji = "📈"
        advice = "市场情绪良好，可正常操作，注意止盈"
    elif strength >= 45:
        mood = "中性"
        mood_color = "#8ea3c4"
        mood_emoji = "😐"
        advice = "市场方向不明，建议观望为主"
    elif strength >= 30:
        mood = "恐慌"
        mood_color = "#38bdf8"
        mood_emoji = "📉"
        advice = "市场恐慌情绪蔓延，可逢低布局优质股"
    else:
        mood = "极度恐慌"
        mood_color = "#34d399"
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
        flow_color = "#f87171"
    elif total_flow > 10e8:
        flow_mood = "净流入"
        flow_color = "#f59e0b"
    elif total_flow > -10e8:
        flow_mood = "平衡"
        flow_color = "#8ea3c4"
    elif total_flow > -50e8:
        flow_mood = "净流出"
        flow_color = "#38bdf8"
    else:
        flow_mood = "大幅流出"
        flow_color = "#34d399"

    # 外盘内盘比
    if outer_ratio > 55:
        buy_mood = "主动买入强势"
        buy_color = "#f87171"
    elif outer_ratio > 50:
        buy_mood = "买入略强"
        buy_color = "#f59e0b"
    elif outer_ratio > 45:
        buy_mood = "买卖平衡"
        buy_color = "#8ea3c4"
    else:
        buy_mood = "主动卖出强势"
        buy_color = "#34d399"

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
        main_color = "#f87171"
    else:
        main_behavior = "主力出货"
        main_desc = "内盘大于外盘，主力正在减仓"
        main_color = "#34d399"

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
        retail_color = "#8ea3c4"
    else:
        retail_behavior = "散户冷淡"
        retail_desc = "换手率低，散户观望情绪浓"
        retail_color = "#38bdf8"

    # 机构行为: 成交额大+涨跌幅小 = 机构调仓
    if total_amount > 5000e8:
        inst_behavior = "机构活跃"
        inst_desc = "成交额巨大，机构资金频繁调仓"
        inst_color = "#f87171"
    elif total_amount > 2000e8:
        inst_behavior = "机构正常"
        inst_desc = "成交额适中，机构按部就班操作"
        inst_color = "#8ea3c4"
    else:
        inst_behavior = "机构冷淡"
        inst_desc = "成交额低迷，机构资金休眠"
        inst_color = "#38bdf8"

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
        fg_color = "#f87171"
        fg_desc = "市场极度乐观，谨防回调"
    elif fg_score >= 60:
        fg_level = "贪婪"
        fg_color = "#f59e0b"
        fg_desc = "市场情绪偏乐观"
    elif fg_score >= 40:
        fg_level = "中性"
        fg_color = "#8ea3c4"
        fg_desc = "市场情绪平稳"
    elif fg_score >= 20:
        fg_level = "恐慌"
        fg_color = "#38bdf8"
        fg_desc = "市场恐慌情绪上升"
    else:
        fg_level = "极度恐慌"
        fg_color = "#34d399"
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
        advice_color = "#34d399"
        advice_emoji = "✅"
    elif score >= 55:
        suggestion = "谨慎买入"
        advice_color = "#f59e0b"
        advice_emoji = "⚠️"
    elif score >= 45:
        suggestion = "观望"
        advice_color = "#8ea3c4"
        advice_emoji = "⏸️"
    elif score >= 30:
        suggestion = "谨慎持有/减仓"
        advice_color = "#f59e0b"
        advice_emoji = "⚠️"
    else:
        suggestion = "建议卖出"
        advice_color = "#f87171"
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


# ===== 行为历史缓存 =====
_BEHAVIOR_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "behavior_cache")
os.makedirs(_BEHAVIOR_CACHE_DIR, exist_ok=True)


def _behavior_cache_file(code):
    """获取股票行为缓存文件路径"""
    return os.path.join(_BEHAVIOR_CACHE_DIR, f"{code}.json")


def _load_behavior_cache(code):
    """加载股票行为缓存"""
    fpath = _behavior_cache_file(code)
    if os.path.exists(fpath):
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_behavior_cache(code, data):
    """保存股票行为缓存"""
    fpath = _behavior_cache_file(code)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[BEHAVIOR CACHE ERR] {e}", flush=True)


def _compute_behavior_history(symbol, days=60):
    """计算股票历史行为（用于K线tooltip显示）"""
    name, klines = _get_kline(symbol, days)
    if not klines or len(klines) < 20:
        return {}
    
    # 复用分析逻辑
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
    
    if len(data) < 20:
        return {}
    
    # 逐日计算行为
    result = {}
    for i in range(19, len(data)):  # 从第20天开始（有足够数据计算MA20等）
        window = data[:i+1]
        try:
            analysis = _analyze_window(window)
            if analysis:
                result[window[-1]["date"]] = analysis["behavior"]
        except Exception:
            continue
    
    return result


def _analyze_window(data):
    """对单个窗口计算行为（复用现有逻辑的简化版）"""
    if len(data) < 20:
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
    
    if not all([ma5[-1], ma20[-1], vol_ma5[-1], vol_ma20[-1]]):
        return None
    
    latest = data[-1]
    cur = latest["close"]
    
    # 核心指标
    change_20d = (closes[-1] - closes[-20]) / closes[-20] * 100
    change_5d = (closes[-1] - closes[-5]) / closes[-5] * 100
    vol_5 = sum(volumes[-5:]) / 5
    vol_10 = sum(volumes[-10:]) / 10 if len(volumes) >= 10 else vol_5
    vol_20 = sum(volumes[-20:]) / 20
    vol_ratio_5 = volumes[-1] / vol_5 if vol_5 > 0 else 1
    vol_ratio_10 = volumes[-1] / vol_10 if vol_10 > 0 else 1
    
    # 当日涨跌
    day_change_pct = (latest["close"] - latest["open"]) / latest["open"] * 100 if latest["open"] > 0 else 0
    
    # 评分
    score_xichou = 0
    score_xipan = 0
    score_chuhuo = 0
    
    # 吸筹规则
    if change_20d < -15 and vol_ratio_5 < 0.8 and abs(change_5d) < 3:
        score_xichou += 30
    if change_20d < -15 and change_5d > 5 and vol_ratio_5 > 1.5:
        score_xichou += 25
    if ma5[-1] and cur > ma5[-1] and data[-2]["close"] < ma5[-2] and vol_ratio_5 > 1.2:
        score_xichou += 15
    
    # 出货规则
    if day_change_pct < -3 and vol_ratio_5 > 1.5:
        score_chuhuo += 35
    high_to_close = (latest["high"] - latest["close"]) / latest["high"] * 100 if latest["high"] > 0 else 0
    if high_to_close > 3 and vol_ratio_5 > 1.2:
        score_chuhuo += 25
    if ma5[-1] and ma20[-1] and cur < ma5[-1] and cur < ma20[-1]:
        if data[-2]["close"] > ma5[-2] and vol_ratio_5 > 1.3:
            score_chuhuo += 30
    
    # 洗盘规则
    if change_20d > 10 and change_5d < -3 and vol_ratio_5 < 0.7:
        score_xipan += 25
    if ma20[-1] and cur > ma20[-1] and change_5d < -2 and vol_ratio_5 < 0.8:
        score_xipan += 20
    
    # 判断行为
    if score_chuhuo >= 20 and score_chuhuo > score_xichou and score_chuhuo > score_xipan:
        if change_20d > 15 and day_change_pct < -3:
            behavior = "止盈"
        elif cur < (ma20[-1] if ma20[-1] else cur * 0.9):
            behavior = "止损"
        else:
            behavior = "减仓"
    elif score_xichou >= 20 and score_xichou > score_xipan and score_xichou > score_chuhuo:
        behavior = "吸筹"
    elif score_xipan >= 20 and score_xipan > score_xichou and score_xipan > score_chuhuo:
        behavior = "洗盘"
    elif score_chuhuo >= 15 and score_chuhuo > score_xichou:
        behavior = "观望"
    else:
        behavior = "震荡"
    
    return {"behavior": behavior, "scores": {"吸筹": score_xichou, "洗盘": score_xipan, "出货": score_chuhuo}}


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
        with open(fpath, "a", encoding="utf-8-sig", newline="") as f:
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
            with open(fpath, "r", encoding="utf-8-sig") as f:
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
                r["result"] = "待验证"
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


# ===== 关注列表 =====
_WATCHLIST_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")


def _load_watchlist():
    if os.path.exists(_WATCHLIST_FILE):
        try:
            with open(_WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_watchlist(data):
    with open(_WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@app.route("/api/watchlist")
def get_watchlist():
    return jsonify({"code": 0, "data": _load_watchlist()})


@app.route("/api/watchlist/add", methods=["POST"])
def add_watchlist():
    body = request.get_json(force=True, silent=True) or {}
    code = body.get("code", "").strip()
    name = body.get("name", "").strip()
    if not code:
        return jsonify({"code": 1, "msg": "code required"})
    wl = _load_watchlist()
    if any(w["code"] == code for w in wl):
        return jsonify({"code": 0, "msg": "already exists"})
    wl.append({"code": code, "name": name, "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})
    _save_watchlist(wl)
    return jsonify({"code": 0, "msg": "ok"})


@app.route("/api/watchlist/remove", methods=["POST"])
def remove_watchlist():
    body = request.get_json(force=True, silent=True) or {}
    code = body.get("code", "").strip()
    if not code:
        return jsonify({"code": 1, "msg": "code required"})
    wl = _load_watchlist()
    wl = [w for w in wl if w["code"] != code]
    _save_watchlist(wl)
    return jsonify({"code": 0, "msg": "ok"})


# ===== 跷跷板板块资金轮动策略 =====
_SEESAW_CACHE = {}
_SEESAW_CACHE_TTL = 1800
_SEESAW_POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seesaw_positions.json")
_SINA_H = ["-H", f"User-Agent: {UA}", "-H", "Referer: https://finance.sina.com.cn/"]


def _board_history(category, num=40):
    """板块每日资金流历史: r0_net=主力净额, turnover=成交额, avg_price=板块均价(合成指数)"""
    now = time.time()
    hit = _SEESAW_CACHE.get(category)
    if hit and hit[0] > now:
        return hit[1]
    url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           f"MoneyFlow.ssl_bkzj_zjlrqs?bankuai={category}&sort=opendate&asc=0&num={num}")
    data = _j(url, _SINA_H)
    rows = []
    if isinstance(data, list):
        for x in data:
            try:
                rows.append({
                    "date": x.get("opendate", ""),
                    "price": float(x.get("avg_price", 0) or 0),
                    "chg": float(x.get("avg_changeratio", 0) or 0) * 100,
                    "turnover": float(x.get("turnover", 0) or 0),
                    "net": float(x.get("netamount", 0) or 0),
                    "r0": float(x.get("r0_net", 0) or 0),
                })
            except Exception:
                continue
    rows.reverse()  # 旧->新
    _SEESAW_CACHE[category] = (now + _SEESAW_CACHE_TTL, rows)
    return rows


def _board_stocks(category, max_pages=5):
    """板块成分股(按涨幅降序), 翻页取全"""
    out = []
    for page in range(1, max_pages + 1):
        url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=100&sort=changepercent&asc=0&node={category}")
        data = _j(url, _SINA_H)
        if not isinstance(data, list) or not data:
            break
        for x in data:
            try:
                code = x.get("code", "")
                name = x.get("name", "")
                sym = x.get("symbol", "")
                if not code or sym.startswith("bj") or "ST" in name.upper():
                    continue
                out.append({
                    "code": code, "name": name, "symbol": sym,
                    "price": float(x.get("trade", 0) or 0),
                    "pct": float(x.get("changepercent", 0) or 0),
                })
            except Exception:
                continue
        if len(data) < 100:
            break
    return out


def _seesaw_market():
    """大盘环境: 上证指数近况 → 增量/存量震荡/系统性下跌"""
    rows = []
    start = (datetime.date.today() - datetime.timedelta(days=60)).strftime("%Y%m%d")
    end = datetime.date.today().strftime("%Y%m%d")
    raw = _curl(f"https://q.stock.sohu.com/hisHq?code=zs_000001&start={start}&end={end}", ["-H", f"User-Agent: {UA}"])
    try:
        arr = json.loads(raw)
        hq = arr[0].get("hq", []) if isinstance(arr, list) and arr else []
        for r in hq:
            # [date, open, close, chg, pct, low, high, vol(手), amount(万元), -]
            rows.append({"date": r[0], "close": float(r[2]), "pct": float(str(r[4]).replace("%", "")),
                         "vol": float(r[7]), "amount": float(r[8])})
    except Exception:
        rows = []
    if len(rows) < 12:
        # 腾讯备用
        raw = _curl("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get?param=sh000001,day,,,40,qfq",
                    ["-H", f"User-Agent: {UA}"])
        try:
            info = json.loads(raw).get("data", {}).get("sh000001", {})
            ks = info.get("qfqday") or info.get("day") or []
            for i, k in enumerate(ks):
                pct = (float(k[2]) / float(ks[i - 1][2]) - 1) * 100 if i else 0.0
                rows.append({"date": k[0], "close": float(k[2]), "pct": round(pct, 2), "vol": float(k[5]), "amount": 0})
        except Exception:
            rows = []
    if len(rows) < 12:
        return {"regime": "数据不足", "enabled": False, "reason": "上证指数行情获取失败,无法判定大盘,暂停策略"}

    vols = [r["vol"] for r in rows]
    # 系统性下跌: 连续2日放量大跌(跌≥1%且量>前5日均量)
    sys_flag = False
    for i in (len(rows) - 1, len(rows) - 2):
        prev5 = vols[max(0, i - 5):i]
        if rows[i]["pct"] <= -1.0 and prev5 and vols[i] > sum(prev5) / len(prev5):
            sys_flag = True
        else:
            sys_flag = False
            break
    if sys_flag:
        return {"regime": "系统性下跌", "enabled": False,
                "reason": "大盘连续2日放量大跌,暂停所有买入信号",
                "sh_close": rows[-1]["close"], "sh_pct": rows[-1]["pct"]}
    # 增量普涨: 近5日≥4日上涨 + 量能较前5日放大30%以上 + 近5日涨幅>3%
    last5 = rows[-5:]
    up_days = sum(1 for r in last5 if r["pct"] > 0)
    vol5_now = sum(vols[-5:]) / 5
    vol5_prev = sum(vols[-10:-5]) / 5 if len(vols) >= 10 else vol5_now
    chg5 = (rows[-1]["close"] / rows[-6]["close"] - 1) * 100 if len(rows) >= 6 else 0
    if up_days >= 4 and vol5_prev > 0 and vol5_now > vol5_prev * 1.3 and chg5 > 3:
        return {"regime": "增量(全面普涨)", "enabled": False,
                "reason": "大盘增量全面普涨,跷跷板效应失效,暂停本策略选股",
                "sh_close": rows[-1]["close"], "sh_pct": rows[-1]["pct"]}
    return {"regime": "存量震荡", "enabled": True,
            "reason": "存量资金博弈,板块跷跷板轮动有效,策略开启",
            "sh_close": rows[-1]["close"], "sh_pct": rows[-1]["pct"]}


def _limit_up_threshold(code):
    if code.startswith(("300", "301", "688", "689")):
        return 19.8
    return 9.8


def _seesaw_candidates(market):
    """买入筛选: 5条规则全命中"""
    with _lock:
        boards = list(_store.get("sector", []))
    if not boards:
        boards = _sina(0, 100)
    candidates, rejected = [], []

    for b in boards:
        cat, bname = b.get("code", ""), b.get("name", "")
        if not cat or b.get("net_inflow", 0) <= 0:
            continue  # 规则2前置: 今日净流入必须>0
        hist = _board_history(cat)
        fails = []
        checks = {"今日净流入(亿)": round(b["net_inflow"] / 1e8, 2)}

        # 规则1: 近20日最大涨幅<25%
        prices = [h["price"] for h in hist[-21:] if h["price"] > 0]
        max_gain = None
        if len(prices) >= 10:
            mn, mx = prices[0], prices[0]
            for p in prices:
                mn = min(mn, p)
                mx = max(mx, p)
            max_gain = round((mx / mn - 1) * 100, 1) if mn > 0 else None
            checks["20日最大涨幅%"] = max_gain
            if max_gain is None or max_gain >= 25:
                fails.append("R1近20日最大涨幅≥25%(非横盘)")
        else:
            fails.append("R1历史数据不足20日")

        # 规则2: 连续2日主力净流入(r0_net)
        r0s = [h["r0"] for h in hist[-3:]]
        if len(r0s) >= 2 and r0s[-1] > 0 and r0s[-2] > 0:
            # 连续流入天数
            streak = 0
            for v in reversed([h["r0"] for h in hist]):
                if v > 0:
                    streak += 1
                else:
                    break
            checks["连续主力流入天数"] = streak
            checks["今日主力净流入(亿)"] = round(r0s[-1] / 1e8, 2)
        else:
            fails.append("R2未连续2日主力净流入(或单日脉冲)")

        # 规则3: 连续2日量>20日均量, 且拒绝极端爆量(≤2.5倍)
        tos = [h["turnover"] for h in hist if h["turnover"] > 0]
        if len(tos) >= 22:
            base = sum(tos[-22:-2]) / 20
            d1, d2 = tos[-2], tos[-1]
            checks["量比20日(昨/今)"] = [round(d1 / base, 2), round(d2 / base, 2)] if base else None
            if not (d1 > base and d2 > base):
                fails.append("R3未连续2日高于20日均量")
            elif d2 > base * 2.5:
                fails.append("R3单日极端爆量(>2.5倍均量),拒绝")
        else:
            fails.append("R3量能历史不足22日")

        # 规则5前置: 大盘(调用方判断 enabled, 此处不重复)

        if fails:
            rejected.append({"name": bname, "code": cat, "fails": fails, "checks": checks})
            continue

        # 规则4: 涨停≥2家 且 >60%成分股收红 (需成分股)
        stocks = _board_stocks(cat)
        if not stocks:
            rejected.append({"name": bname, "code": cat, "fails": ["R4成分股获取失败"], "checks": checks})
            continue
        limit_up = [s for s in stocks if s["pct"] >= _limit_up_threshold(s["code"])]
        red = [s for s in stocks if s["pct"] > 0]
        red_pct = round(len(red) / len(stocks) * 100, 1)
        checks["涨停家数"] = len(limit_up)
        checks["红盘占比%"] = red_pct
        fails4 = []
        if len(limit_up) < 2:
            fails4.append("R4涨停<2家")
        if red_pct <= 60:
            fails4.append("R4红盘占比≤60%(板块未整体启动)")
        if fails4:
            rejected.append({"name": bname, "code": cat, "fails": fails4, "checks": checks})
            continue

        # 通过全部规则 → 候选
        hist_prices = [h["price"] for h in hist[-20:] if h["price"] > 0]
        resistance = round(max(hist_prices), 2) if hist_prices else None
        support = round(min(hist_prices[-10:]), 2) if hist_prices else None
        last_chg = round(hist[-1]["chg"], 2) if hist else b.get("avg_change", 0)
        candidates.append({
            "name": bname, "code": cat,
            "checks": checks,
            "max_gain20": max_gain,
            "inflow_streak": checks.get("连续主力流入天数", 0),
            "board_chg": last_chg,
            "core_stocks": [{"code": s["code"], "name": s["name"], "price": s["price"], "pct": s["pct"]}
                            for s in stocks[:5]],
            "support": support, "resistance": resistance,
            "limit_up": len(limit_up), "red_pct": red_pct,
        })

    # 优先级: 横盘更久(20日涨幅更小) + 资金流入持续性更强
    candidates.sort(key=lambda c: (c.get("max_gain20") or 999, -(c.get("inflow_streak") or 0)))
    return candidates, rejected


def _load_seesaw_positions():
    try:
        with open(_SEESAW_POS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_seesaw_positions(positions):
    with open(_SEESAW_POS_FILE, "w", encoding="utf-8") as f:
        json.dump(positions, f, ensure_ascii=False, indent=1)


def _seesaw_holdings():
    """持仓检测: 4条卖出规则 + 止盈止损硬规则"""
    positions = _load_seesaw_positions()
    results = []
    changed = False
    for p in positions:
        hist = _board_history(p["code"], num=30)
        entry = float(p.get("entry_price") or 0)
        peak = float(p.get("peak_price") or entry)
        triggers = []
        if len(hist) >= 3:
            # 更新持仓峰值(板块均价)
            cur = hist[-1]["price"]
            if cur > peak:
                peak = cur
                p["peak_price"] = peak
                changed = True
            pnl = round((cur / entry - 1) * 100, 2) if entry > 0 else 0
            # S2: 连续2日主力净流出
            r0s = [h["r0"] for h in hist[-2:]]
            if len(r0s) == 2 and r0s[0] < 0 and r0s[1] < 0:
                triggers.append("S2 连续2日主力资金净流出")
            # S3: 价格创新高但量能明显萎缩(<0.8倍)
            if len(hist) >= 21:
                p20 = [h["price"] for h in hist[-20:]]
                if hist[-1]["price"] >= max(p20) and hist[-1]["turnover"] < hist[-2]["turnover"] * 0.8:
                    triggers.append("S3 价格创20日新高但成交量明显萎缩(量价背离)")
            # S1: 高位放量滞涨(数据限制: 无OHLC,用涨幅+量能近似)
            if len(hist) >= 21:
                prices = [h["price"] for h in hist[-20:]]
                gain20 = (max(prices) / min(prices) - 1) * 100 if min(prices) > 0 else 0
                tos = [h["turnover"] for h in hist[-21:-1] if h["turnover"] > 0]
                if gain20 >= 15 and tos and hist[-1]["turnover"] > sum(tos) / len(tos) * 1.1 and hist[-1]["chg"] <= 0.3:
                    triggers.append("S1 高位(20日涨幅≥15%)放量滞涨")
            # 止盈止损硬规则
            if pnl >= 12:
                triggers.append(f"H1 盈利{pnl}%≥12%,触发分批止盈")
            dd = round((cur / peak - 1) * 100, 2) if peak > 0 else 0
            if dd <= -8:
                triggers.append(f"H2 自峰值回撤{dd}%≤-8%,无条件离场")
        else:
            pnl = 0
            dd = 0
            triggers.append("板块历史数据不足,无法完整检测")
        action = "持有"
        if any(t.startswith(("H1", "S2", "S3")) for t in triggers):
            action = "准备卖出/切换"
        if any(t.startswith("H2") for t in triggers):
            action = "立即离场"
        results.append({
            "name": p.get("name", ""), "code": p.get("code", ""),
            "entry_price": entry, "peak_price": peak,
            "pnl_pct": pnl if len(hist) >= 3 else None,
            "triggers": triggers, "action": action, "date": p.get("date", ""),
        })
    if changed:
        _save_seesaw_positions(positions)
    return results


@app.route("/api/seesaw")
def seesaw():
    """跷跷板板块资金轮动策略分析(严格按规则,数据驱动)"""
    market = _seesaw_market()
    holdings = _seesaw_holdings()
    candidates, rejected = [], []
    paused = None
    if not market.get("enabled"):
        paused = market.get("reason")
    else:
        candidates, rejected = _seesaw_candidates(market)

    risks = []
    if paused:
        risks.append(f"【暂停】{paused}")
    for c in candidates:
        rr = []
        if c.get("max_gain20") is not None and c["max_gain20"] >= 15:
            rr.append(f"20日已涨{c['max_gain20']}%,接近横盘上限,追高风险")
        if c.get("board_chg") is not None and c["board_chg"] >= 3:
            rr.append(f"板块单日涨{c['board_chg']}%,注意冲高回落")
        if (c.get("inflow_streak") or 0) <= 2:
            rr.append("资金刚翻红≤2日,持续性待观察,防一日游")
        if c.get("support") and c.get("resistance"):
            rr.append(f"支撑¥{c['support']} / 压力¥{c['resistance']}(20日高)")
        for s in c.get("core_stocks", []):
            if s["pct"] >= 7:
                rr.append(f"{s['name']}({s['code']})已涨{s['pct']}%,勿追高")
        risks.append(f"{c['name']}: " + ";".join(rr) if rr else f"{c['name']}: 无特别风险")
    risks.append("仓位规则: 轮动总资金=小仓位(底仓不参与),单板块≤40%,持仓≤3板块,2选1优先横盘更久+流入更持续的")
    risks.append("离场规则: 盈利12%分批止盈,自峰值回撤8%无条件离场")
    risks.append("数据限制: 板块无OHLC,'长上影线'形态暂用放量滞涨近似;突发小道消息类(无资金配合)已被资金规则自动过滤")

    return jsonify({"code": 0, "data": {
        "market": market,
        "paused": paused,
        "holdings": holdings,
        "candidates": candidates,
        "rejected_count": len(rejected),
        "rejected_sample": rejected[:8],
        "risks": risks,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }})


@app.route("/api/seesaw/position", methods=["POST"])
def seesaw_position():
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action", "add")
    cat = (body.get("code") or "").strip()
    if not cat:
        return jsonify({"code": 1, "msg": "code required"})
    positions = _load_seesaw_positions()
    if action == "remove":
        positions = [p for p in positions if p.get("code") != cat]
        _save_seesaw_positions(positions)
        return jsonify({"code": 0, "msg": "removed"})
    hist = _board_history(cat, num=5)
    price = float(body.get("price") or 0) or (hist[-1]["price"] if hist else 0)
    if price <= 0:
        return jsonify({"code": 1, "msg": "无法获取板块价格"})
    if not any(p.get("code") == cat for p in positions):
        positions.append({
            "code": cat, "name": body.get("name", ""),
            "entry_price": price, "peak_price": price,
            "date": datetime.date.today().strftime("%Y-%m-%d"),
        })
        _save_seesaw_positions(positions)
    return jsonify({"code": 0, "msg": "ok"})


# ===== 阴线尾盘隔夜套利策略 =====
_YX_POS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yinxian_positions.json")
_YX_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yinxian_log.json")


def _yinxian_pool():
    """股票池: 全A按成交额Top200(不按板块,不打板)"""
    out = []
    for page in (1, 2):
        url = (f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=100&sort=amount&asc=0&node=hs_a")
        data = _j(url, _SINA_H)
        if not isinstance(data, list):
            break
        for x in data:
            try:
                code, name, sym = x.get("code", ""), x.get("name", ""), x.get("symbol", "")
                if not code or sym.startswith("bj") or "ST" in name.upper():
                    continue
                out.append({"symbol": sym, "code": code, "name": name,
                            "price": float(x.get("trade", 0) or 0),
                            "pct": float(x.get("changepercent", 0) or 0),
                            "amount": float(x.get("amount", 0) or 0) / 1e4})  # 万元
            except Exception:
                continue
    # 去重保序
    seen, pool = set(), []
    for s in out:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            pool.append(s)
    return pool


def _yinxian_eval(klines, rt=None):
    """选股6规则(全命中): 阴线+缩量回调到MA10+前期爆量+MA10温和向上+MA10/MA20近
    klines: [date, open, close, high, low, volume]
    返回: {"pass":True,...} 或 {"pass":False,"reason":"淘汰规则"}"""
    try:
        if not klines or len(klines) < 25:
            return {"pass": False, "reason": "K线不足25日"}
        opens = [float(k[1]) for k in klines]
        closes = [float(k[2]) for k in klines]
        highs = [float(k[3]) for k in klines]
        lows = [float(k[4]) for k in klines]
        vols = [float(k[5]) for k in klines if len(k) > 5]
        if len(vols) < 21 or len(closes) < 21:
            return {"pass": False, "reason": "K线不足21日"}
        o, c, h, l, v = opens[-1], closes[-1], highs[-1], lows[-1], vols[-1]
        checks = {}

        # R1 阴线
        if rt is not None:
            # 用实时open/close覆盖(14:30盘中最后一根K线未收盘)
            o, c = rt.get("open", o), rt.get("price", c)
            h, l = max(h, rt.get("high", h)), min(l, rt.get("low", l))
            v = max(v, rt.get("volume", v))
        if c >= o:
            return {"pass": False, "reason": "R1非阴线"}
        checks["阴线"] = f"开{o} 收{c}"

        # R2 缩量: 今日量<昨日量 且 ≤5日均量
        vol5 = sum(vols[-6:-1]) / 5
        shrink = v / vols[-2] if vols[-2] > 0 else 1
        if not (v < vols[-2] and v <= vol5 * 1.05):
            return {"pass": False, "reason": "R2非缩量"}
        checks["缩量"] = f"{shrink:.2f}×昨量"

        # R3 缩量回调到10日均线(低点触及MA10附近,收盘守住)
        ma10 = sum(closes[-10:]) / 10
        touch = (l <= ma10 * 1.015 and c >= ma10 * 0.99) or abs(c - ma10) / ma10 <= 0.012
        if not touch:
            return {"pass": False, "reason": "R3未回踩MA10"}
        checks["回踩MA10"] = f"MA10={round(ma10,2)} 距离{round((c/ma10-1)*100,2)}%"

        # R4 前期有爆量(前10日内≥2×20日均量)
        base_vol = sum(vols[-21:-1]) / 20
        surge = max(vols[-11:-1]) / base_vol if base_vol > 0 else 0
        if surge < 2:
            return {"pass": False, "reason": "R4前期无爆量"}
        checks["前期爆量"] = f"{surge:.1f}×20日均量"

        # R5 MA10温和向上(昨日起抬升,5日斜率0~6%)
        ma10_prev = sum(closes[-11:-1]) / 10
        ma10_5 = sum(closes[-15:-5]) / 10
        slope5 = (ma10 / ma10_5 - 1) * 100 if ma10_5 > 0 else 0
        if not (ma10 > ma10_prev and 0 < slope5 <= 6):
            return {"pass": False, "reason": "R5非温和向上"}
        checks["MA10温和向上"] = f"5日斜率{round(slope5,2)}%"

        # R6 MA10与MA20距离近(≤2%)
        ma20 = sum(closes[-20:]) / 20
        gap = abs(ma10 - ma20) / ma20 * 100
        if gap > 2:
            return {"pass": False, "reason": "R6均线发散"}
        checks["MA10/20距离"] = f"{round(gap,2)}%"

        # 评分: 越贴MA10+缩量越狠+爆量越足 越优
        score = max(0, 100 - abs(c / ma10 - 1) * 3000) * 0.4 \
              + min(40, (1 - shrink) * 60) * 0.3 \
              + min(30, surge * 8) * 0.3
        return {
            "pass": True,
            "score": round(min(100, score)),
            "checks": checks,
            "ma10": round(ma10, 2), "ma20": round(ma20, 2),
            "price": c, "pct": round((c / closes[-2] - 1) * 100, 2) if len(closes) >= 2 else 0,
            "shrink": round(shrink, 2), "surge": round(surge, 1),
            "gap_pct": round(gap, 2),
        }
    except Exception:
        return {"pass": False, "reason": "解析异常"}


def _run_yinxian_scan():
    """14:30选股: 实时粗筛(阴线+流动性) → K线精筛6规则
    返回 (命中列表, 池子数, 淘汰统计)"""
    pool = _yinxian_pool()
    if not pool:
        return [], 0, {}
    quotes = _get_realtime_batch([s["symbol"] for s in pool])
    qmap = {q["symbol"]: q for q in quotes}
    # 粗筛: 阴线(价<开) + 亿级成交 + 跌幅不过深(排除闪崩)
    cands = []
    stats = {"池子": len(pool), "粗筛": 0}
    for s in pool:
        q = qmap.get(s["symbol"])
        if not q or q["price"] <= 0 or q["open"] <= 0:
            continue
        if q["price"] >= q["open"]:
            continue
        if q["amount"] < 1e4:  # 成交额<1亿不看
            continue
        if q["pct_change"] < -6:
            continue
        cands.append(s)
    stats["粗筛"] = len(cands)

    results = []

    def _work(s):
        try:
            q = qmap.get(s["symbol"])
            _, kl = _get_kline(s["symbol"], 40)
            if not kl:
                return "retry", None, "K线获取失败"
            ev = _yinxian_eval(kl, rt=q)
            if ev.get("pass"):
                r = {k: v for k, v in ev.items() if k != "pass"}
                r.update({"symbol": s["symbol"], "code": s["code"], "name": s["name"]})
                return "hit", r, None
            return "ok", None, ev.get("reason", "未知")
        except Exception:
            return "retry", None, "异常"

    from concurrent.futures import ThreadPoolExecutor
    # 4路并发: 8路易触发腾讯/搜狐限流导致大面积"K线不足"
    retry_idx = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i, (tag, r, why) in enumerate(ex.map(_work, cands)):
            if tag == "hit":
                results.append(r)
            elif tag == "retry":
                retry_idx.append(i)
            elif why:  # 明确规则淘汰才计数;获取失败留给串行重试统计
                stats[why] = stats.get(why, 0) + 1
    # 失败的串行重试一次(绕开并发限流)
    for i in retry_idx:
        tag, r, why = _work(cands[i])
        if tag == "hit":
            results.append(r)
        elif why:
            stats[why] = stats.get(why, 0) + 1
    stats["命中"] = len(results)
    results.sort(key=lambda x: -x["score"])
    return results, len(pool), stats


def _load_yx_positions():
    try:
        with open(_YX_POS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_yx_positions(ps):
    with open(_YX_POS_FILE, "w", encoding="utf-8") as f:
        json.dump(ps, f, ensure_ascii=False, indent=1)


def _yinxian_holdings():
    """卖出规则执行检测(次日): 开盘破MA10止损 / +2%止盈 / 冲高破分时均线卖 / 14:55尾盘清仓"""
    ps = _load_yx_positions()
    if not ps:
        return []
    quotes = _get_realtime_batch([p["symbol"] for p in ps])
    qmap = {q["symbol"]: q for q in quotes}
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    is_weekday = now.weekday() < 5
    out = []
    for p in ps:
        q = qmap.get(p["symbol"])
        entry = float(p.get("entry") or 0)
        item = dict(p)
        item["pnl_pct"] = round((q["price"] / entry - 1) * 100, 2) if q and entry > 0 else None
        item["price"] = q["price"] if q else None
        item["signals"] = []
        item["action"] = "持有"
        if not q:
            item["signals"].append("行情获取失败")
            out.append(item)
            continue

        # MA10参照: 截至昨日的10日均线(次日开盘对比)
        _, kl = _get_kline(p["symbol"], 30)
        ma10_ref = p.get("ma10")
        try:
            closes_prior = [float(k[2]) for k in kl if k[0] < today]
            if len(closes_prior) >= 10:
                ma10_ref = sum(closes_prior[-10:]) / 10
        except Exception:
            pass
        item["ma10_ref"] = round(ma10_ref, 2) if ma10_ref else None

        # 分时均线(VWAP) = 成交额(万)×100 / 成交量(手)
        vwap = (q["amount"] * 100 / q["volume"]) if q.get("volume") else None
        item["vwap"] = round(vwap, 2) if vwap else None

        if p.get("entry_date") == today:
            item["signals"].append("今日建仓,明日执行卖出规则")
            item["action"] = "持有"
            out.append(item)
            continue
        if not ma10_ref:
            item["signals"].append("MA10数据缺失,请手动判断")
            item["action"] = "持有"
            out.append(item)
            continue

        sell = None
        # 1) 次日开盘破10日线 → 直接止损
        if ma10_ref and q["open"] < ma10_ref:
            sell = f"开盘价{q['open']}<MA10 {round(ma10_ref,2)},破10日线"
            item["action"] = "止损"
        # 2) 盈利2% → 止盈
        elif entry > 0 and q["price"] >= entry * 1.02:
            sell = f"盈利{round((q['price']/entry-1)*100,2)}%≥2%"
            item["action"] = "止盈"
        # 3) 冲高后破分时均线 → 立刻卖
        elif vwap and q["high"] > vwap * 1.003 and q["price"] < vwap * 0.999:
            sell = f"冲高后跌破分时均线(VWAP {round(vwap,2)})"
            item["action"] = "破分时均线"
        # 4) 尾盘(≥14:55)没达标 → 全部清仓
        elif is_weekday and (now.hour * 100 + now.minute) >= 1455:
            sell = "尾盘未达止盈/止损标准,全部清仓"
            item["action"] = "尾盘清仓"
        if sell:
            item["signals"].append(sell)
        else:
            item["signals"].append("规则内,继续持有")
        out.append(item)
    return out


@app.route("/api/yinxian/scan")
def yinxian_scan():
    t0 = time.time()
    results, pool_n, stats = _run_yinxian_scan()
    return jsonify({"code": 0, "data": results, "pool": pool_n, "stats": stats,
                    "cost": round(time.time() - t0, 1),
                    "time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")})


@app.route("/api/yinxian/holdings")
def yinxian_holdings():
    return jsonify({"code": 0, "data": _yinxian_holdings(),
                    "time": datetime.datetime.now().strftime("%H:%M")})


@app.route("/api/yinxian/buy", methods=["POST"])
def yinxian_buy():
    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"code": 1, "msg": "symbol required"})
    ps = _load_yx_positions()
    if any(p["symbol"] == symbol for p in ps):
        return jsonify({"code": 1, "msg": "已持有该标的"})
    price = float(body.get("price") or 0)
    _, kl = _get_kline(symbol, 30)
    ma10 = None
    try:
        closes = [float(k[2]) for k in kl]
        if len(closes) >= 10:
            ma10 = round(sum(closes[-10:]) / 10, 3)
    except Exception:
        pass
    if price <= 0:
        return jsonify({"code": 1, "msg": "价格无效"})
    ps.append({
        "symbol": symbol, "code": body.get("code", ""), "name": body.get("name", ""),
        "entry": price, "entry_date": datetime.date.today().strftime("%Y-%m-%d"),
        "ma10": ma10, "time": datetime.datetime.now().strftime("%H:%M"),
    })
    _save_yx_positions(ps)
    return jsonify({"code": 0, "msg": "ok"})


@app.route("/api/yinxian/sell", methods=["POST"])
def yinxian_sell():
    body = request.get_json(force=True, silent=True) or {}
    symbol = (body.get("symbol") or "").strip()
    ps = _load_yx_positions()
    hit = [p for p in ps if p["symbol"] == symbol]
    if hit:
        p = hit[0]
        # 记录复盘日志
        try:
            log = []
            if os.path.exists(_YX_LOG_FILE):
                with open(_YX_LOG_FILE, "r", encoding="utf-8") as f:
                    log = json.load(f)
            log.append({**{k: p.get(k) for k in ("symbol", "code", "name", "entry", "entry_date")},
                        "exit_price": body.get("price"), "reason": body.get("reason", ""),
                        "exit_date": datetime.date.today().strftime("%Y-%m-%d"),
                        "exit_time": datetime.datetime.now().strftime("%H:%M")})
            with open(_YX_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(log[-200:], f, ensure_ascii=False, indent=1)
        except Exception:
            pass
        ps = [x for x in ps if x["symbol"] != symbol]
        _save_yx_positions(ps)
    return jsonify({"code": 0, "msg": "ok"})


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    threading.Thread(target=_loop, daemon=True).start()
    threading.Thread(target=_short_term_loop, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=port)
