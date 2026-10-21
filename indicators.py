# -*- coding: utf-8 -*-
"""
扩展技术指标库
趋势 / 动能 / 量能 / 波动 / 多周期共振 / 相对强度
所有函数容错：数据不足返回 None，不抛异常
"""
import math


def _c(x):
    return [r["close"] for r in x]


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def ema_series(vals, n):
    if not vals:
        return []
    k = 2.0 / (n + 1)
    e = vals[0]
    out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out


def atr(kl, n=14):
    """平均真实波幅"""
    if len(kl) < n + 1:
        return None
    trs = []
    for i in range(1, len(kl)):
        h, l, pc = kl[i]["high"], kl[i]["low"], kl[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return None
    return sum(trs[-n:]) / n


def cci(kl, n=14):
    """顺势指标"""
    if len(kl) < n:
        return None
    seg = kl[-n:]
    tps = [(x["high"] + x["low"] + x["close"]) / 3 for x in seg]
    ma = sum(tps) / n
    md = sum(abs(t - ma) for t in tps) / n
    if md == 0:
        return 0.0
    return (tps[-1] - ma) / (0.015 * md)


def obv_slope(kl, n=20):
    """OBV 能量潮斜率（正=资金进场）"""
    if len(kl) < n + 1:
        return None
    obv = 0.0
    series = []
    for i in range(1, len(kl)):
        if kl[i]["close"] > kl[i - 1]["close"]:
            obv += kl[i]["volume"]
        elif kl[i]["close"] < kl[i - 1]["close"]:
            obv -= kl[i]["volume"]
        series.append(obv)
    seg = series[-n:]
    if not seg or abs(seg[0]) < 1e-9:
        return 0.0
    return (seg[-1] - seg[0]) / (abs(seg[0]) + 1e-9) * 100


def wr(kl, n=14):
    """威廉指标（0~100，越小越强）"""
    if len(kl) < n:
        return None
    seg = kl[-n:]
    hi = max(x["high"] for x in seg)
    lo = min(x["low"] for x in seg)
    if hi == lo:
        return 50.0
    return (hi - kl[-1]["close"]) / (hi - lo) * 100


def dmi(kl, n=14):
    """+DI / -DI / ADX 趋势强度"""
    if len(kl) < n * 2:
        return None
    trs, pdm, ndm = [], [], []
    for i in range(1, len(kl)):
        h, l, pc = kl[i]["high"], kl[i]["low"], kl[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
        up = h - kl[i - 1]["high"]
        dn = kl[i - 1]["low"] - l
        pdm.append(up if (up > dn and up > 0) else 0.0)
        ndm.append(dn if (dn > up and dn > 0) else 0.0)
    if len(trs) < n:
        return None
    atr_ = sum(trs[-n:]) / n
    if atr_ == 0:
        return None
    pdi = sum(pdm[-n:]) / n / atr_ * 100
    ndi = sum(ndm[-n:]) / n / atr_ * 100
    dx = abs(pdi - ndi) / (pdi + ndi + 1e-9) * 100
    return {"pdi": pdi, "mdi": ndi, "adx": dx}


def mfi(kl, n=14):
    """资金流量指标"""
    if len(kl) < n + 1:
        return None
    pos, neg = 0.0, 0.0
    for i in range(len(kl) - n, len(kl)):
        tp = (kl[i]["high"] + kl[i]["low"] + kl[i]["close"]) / 3
        tp0 = (kl[i - 1]["high"] + kl[i - 1]["low"] + kl[i - 1]["close"]) / 3
        mf = tp * kl[i]["volume"]
        if tp > tp0:
            pos += mf
        elif tp < tp0:
            neg += mf
    if neg == 0:
        return 100.0
    return 100 - 100 / (1 + pos / neg)


def bias(closes, n=20):
    ma = sma(closes, n)
    if not ma:
        return None
    return (closes[-1] / ma - 1) * 100


def psy(closes, n=12):
    if len(closes) < n + 1:
        return None
    up = sum(1 for i in range(len(closes) - n, len(closes)) if closes[i] > closes[i - 1])
    return up / n * 100


def roc(closes, n=12):
    if len(closes) < n + 1:
        return None
    base = closes[-n - 1]
    if base == 0:
        return None
    return (closes[-1] / base - 1) * 100


def donchian(kl, n=20):
    if len(kl) < n:
        return None
    seg = kl[-n:]
    hi = max(x["high"] for x in seg)
    lo = min(x["low"] for x in seg)
    return {"high": hi, "low": lo,
            "pos": (kl[-1]["close"] - lo) / (hi - lo) * 100 if hi > lo else 50.0}


def weekly_trend(kl):
    """日线聚合周线，判断周线趋势（多周期共振）"""
    if len(kl) < 25:
        return None
    weeks = []
    for i in range(0, len(kl), 5):
        chunk = kl[i:i + 5]
        if len(chunk) < 3:
            continue
        weeks.append({
            "close": chunk[-1]["close"],
            "high": max(x["high"] for x in chunk),
            "low": min(x["low"] for x in chunk),
        })
    if len(weeks) < 6:
        return None
    closes = [w["close"] for w in weeks]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    slope = closes[-1] - closes[-3] if len(closes) >= 3 else 0
    return {
        "above_ma5": ma5 is not None and closes[-1] > ma5,
        "ma5_above_ma10": bool(ma5 and ma10 and ma5 > ma10),
        "slope_up": slope > 0,
        "weeks": len(weeks),
    }


def rel_strength(closes, index_closes, n=20):
    """相对上证指数强度（20日超额收益，%）"""
    if len(closes) < n + 1 or len(index_closes) < n + 1:
        return None
    s = (closes[-1] / closes[-n - 1] - 1) * 100
    b = (index_closes[-1] / index_closes[-n - 1] - 1) * 100
    return s - b


def volatility(closes, n=20):
    """年化波动率 %"""
    if len(closes) < n + 1:
        return None
    rets = []
    for i in range(len(closes) - n, len(closes)):
        if closes[i - 1] > 0:
            rets.append(closes[i] / closes[i - 1] - 1)
    if len(rets) < 5:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(244) * 100


def streak(closes):
    """连续上涨/下跌天数（正=连涨）"""
    if len(closes) < 2:
        return 0
    cnt = 0
    last = closes[-1]
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1] and last >= closes[i - 1]:
            cnt += 1
        elif closes[i] < closes[i - 1] and last <= closes[i - 1]:
            cnt -= 1
        else:
            break
    return cnt


def vol_price_divergence(kl, n=10):
    """量价背离：价新高但量能萎缩 -> -1，价新低但放量止跌 -> +1"""
    if len(kl) < n + 5:
        return 0
    recent, prev = kl[-n:], kl[-2 * n:-n]
    if not prev:
        return 0
    p_hi_r = max(x["high"] for x in recent)
    p_hi_p = max(x["high"] for x in prev)
    v_r = sum(x["volume"] for x in recent) / len(recent)
    v_p = sum(x["volume"] for x in prev) / len(prev) or 1
    if p_hi_r > p_hi_p and v_r < v_p * 0.85:
        return -1
    p_lo_r = min(x["low"] for x in recent)
    p_lo_p = min(x["low"] for x in prev)
    if p_lo_r < p_lo_p and v_r > v_p * 1.15:
        return 1
    return 0


def compute_all(kl, quote=None, index_closes=None):
    """一次性计算全部扩展指标"""
    closes = _c(kl)
    out = {
        "atr": None, "atr_pct": None, "cci": None, "obv_slope": None,
        "wr": None, "adx": None, "pdi": None, "mdi": None, "mfi": None,
        "bias20": None, "psy": None, "roc12": None, "donchian": None,
        "weekly": None, "rs20": None, "volatility": None, "streak": 0,
        "vp_div": 0, "from_high20": None, "ma_slope20": None,
    }
    try:
        a = atr(kl)
        out["atr"] = round(a, 3) if a else None
        price = (quote or {}).get("price") or (closes[-1] if closes else 0)
        if a and price:
            out["atr_pct"] = round(a / price * 100, 2)
        out["cci"] = round(cci(kl), 1) if cci(kl) is not None else None
        o = obv_slope(kl)
        out["obv_slope"] = round(o, 1) if o is not None else None
        w = wr(kl)
        out["wr"] = round(w, 1) if w is not None else None
        d = dmi(kl)
        if d:
            out["adx"] = round(d["adx"], 1)
            out["pdi"] = round(d["pdi"], 1)
            out["mdi"] = round(d["mdi"], 1)
        m = mfi(kl)
        out["mfi"] = round(m, 1) if m is not None else None
        b = bias(closes)
        out["bias20"] = round(b, 2) if b is not None else None
        p = psy(closes)
        out["psy"] = round(p, 1) if p is not None else None
        r = roc(closes)
        out["roc12"] = round(r, 2) if r is not None else None
        dn = donchian(kl)
        if dn:
            out["donchian"] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in dn.items()}
            out["from_high20"] = round((closes[-1] / dn["high"] - 1) * 100, 2)
        out["weekly"] = weekly_trend(kl)
        if index_closes:
            rs = rel_strength(closes, index_closes)
            out["rs20"] = round(rs, 2) if rs is not None else None
        v = volatility(closes)
        out["volatility"] = round(v, 1) if v is not None else None
        out["streak"] = streak(closes)
        out["vp_div"] = vol_price_divergence(kl)
        out["ma_slope20"] = round(ma_slope(closes, 20, 5), 2) if ma_slope(closes, 20, 5) is not None else None
        out["boll_width"] = round(boll_width(closes), 2) if boll_width(closes) is not None else None
        out["vol_price_corr"] = round(vol_price_corr(kl), 3) if vol_price_corr(kl) is not None else None
        out["limit_up60"] = limit_up_count(kl, 60)
        out["up_ratio20"] = round(up_days_ratio(closes), 1) if up_days_ratio(closes) is not None else None
        out["max_dd60"] = max_drawdown(closes, 60)
        if len(closes) >= 21:
            out["ma_slope20"] = round((sma(closes, 20) or 0) / (sum(closes[-25:-5]) / 20 + 1e-9) * 100 - 100, 2)
        ind_tmp = {"ma5": sma(closes, 5), "ma20": sma(closes, 20)}
        out["patterns"] = [{"name": a, "dir": b, "desc": c}
                           for a, b, c in detect_patterns(kl, ind_tmp, quote)]
    except Exception:  # noqa: BLE001
        pass
    return out


# ================= 扩展指标 V2：形态、股性、量价结构 =================

def ma_slope(closes, n=20, lookback=5):
    """均线斜率（%）"""
    if len(closes) < n + lookback:
        return None
    now = sum(closes[-n:]) / n
    prev = sum(closes[-n - lookback:-lookback]) / n
    if prev == 0:
        return None
    return (now / prev - 1) * 100


def boll_width(closes, n=20):
    """布林带宽：越窄越可能变盘"""
    if len(closes) < n:
        return None
    seg = closes[-n:]
    mid = sum(seg) / n
    var = sum((x - mid) ** 2 for x in seg) / n
    std = math.sqrt(var)
    if mid == 0:
        return None
    return std * 4 / mid * 100


def vol_price_corr(kl, n=20):
    """量价相关系数（-1~1）"""
    if len(kl) < n:
        return None
    seg = kl[-n:]
    xs = [i["close"] for i in seg]
    ys = [i["volume"] for i in seg]
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / n
    vx = math.sqrt(sum((x - mx) ** 2 for x in xs) / n)
    vy = math.sqrt(sum((y - my) ** 2 for y in ys) / n)
    if vx == 0 or vy == 0:
        return None
    return cov / (vx * vy)


def limit_up_count(kl, n=60):
    """近 N 日涨停次数（按价格涨幅判断，兼容 10%/20%）"""
    if len(kl) < 2:
        return 0
    seg = kl[-n:] if len(kl) > n else kl
    cnt = 0
    for i in range(1, len(seg)):
        prev = seg[i - 1]["close"]
        if prev <= 0:
            continue
        chg = (seg[i]["close"] / prev - 1) * 100
        if chg >= 9.5:
            cnt += 1
    return cnt


def up_days_ratio(closes, n=20):
    """近 N 日上涨天数占比"""
    if len(closes) < n + 1:
        return None
    up = sum(1 for i in range(len(closes) - n, len(closes)) if closes[i] > closes[i - 1])
    return up / n * 100


def max_drawdown(closes, n=60):
    """近 N 日最大回撤（%）"""
    if len(closes) < 5:
        return None
    seg = closes[-n:] if len(closes) > n else closes
    peak = seg[0]
    mdd = 0.0
    for v in seg:
        if v > peak:
            peak = v
        if peak > 0:
            mdd = min(mdd, (v / peak - 1) * 100)
    return round(mdd, 2)


def detect_patterns(kl, ind, quote=None):
    """
    形态识别，返回 [(标签, 方向, 说明)]
    方向: 1 偏多 / -1 偏空 / 0 中性
    """
    out = []
    if len(kl) < 25:
        return out
    closes = [x["close"] for x in kl]
    vols = [x["volume"] for x in kl]
    price = closes[-1]
    ma5, ma20 = ind.get("ma5"), ind.get("ma20")
    v20 = sum(vols[-20:]) / 20 if vols else 0
    v5 = sum(vols[-5:]) / 5 if vols else 0
    hi20 = max(x["high"] for x in kl[-20:])
    lo20 = min(x["low"] for x in kl[-20:])

    # 1. 突破 20 日新高（放量）
    if price >= hi20 * 0.997 and v5 > v20 * 1.2:
        out.append(("突破新高", 1, "放量突破 20 日高点"))

    # 2. 回踩 MA20（趋势未破）
    if ma20 and abs(price / ma20 - 1) <= 0.02:
        sl = ma_slope(closes, 20, 5)
        if sl and sl > 0:
            out.append(("回踩均线", 1, "贴近 MA20 且均线向上"))

    # 3. 缩量整理
    if v5 and v20 and v5 < v20 * 0.72:
        rng = (max(closes[-5:]) - min(closes[-5:])) / price * 100 if price else 99
        if rng < 6:
            out.append(("缩量整理", 0, "量能萎缩、振幅收窄，等待方向"))

    # 4. 放量滞涨
    vr = (quote or {}).get("volume_ratio")
    chg = (quote or {}).get("change_pct") or 0
    if vr and vr > 2 and abs(chg) < 1.2:
        out.append(("放量滞涨", -1, "量能放大但价格没跟上，警惕出货"))

    # 5. 加速上涨
    if len(closes) >= 4:
        r1 = closes[-1] / closes[-2] - 1
        r2 = closes[-2] / closes[-3] - 1
        r3 = closes[-3] / closes[-4] - 1
        if r1 > r2 > r3 and r1 > 0.01 and (closes[-1] / closes[-4] - 1) > 0.06:
            out.append(("加速上涨", 1, "涨幅逐日扩大，短线强势"))

    # 6. 高位放量长上影（回落风险）
    last = kl[-1]
    body = abs(last["close"] - last["open"])
    upper = last["high"] - max(last["close"], last["open"])
    rng_all = last["high"] - last["low"]
    if rng_all > 0 and upper > body * 2 and upper / rng_all > 0.55 and v5 > v20 * 1.3:
        out.append(("长上影", -1, "放量冲高回落，上方抛压重"))

    # 7. 底部放量反弹
    if len(closes) >= 6:
        drop = closes[-6] / closes[-3] - 1 if closes[-3] else 0
        if drop < -0.05 and v5 > v20 * 1.25 and closes[-1] > closes[-2]:
            out.append(("超跌反弹", 1, "急跌后放量止跌"))

    return out
