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
        if len(closes) >= 21:
            out["ma_slope20"] = round((sma(closes, 20) or 0) / (sum(closes[-25:-5]) / 20 + 1e-9) * 100 - 100, 2)
    except Exception:  # noqa: BLE001
        pass
    return out
